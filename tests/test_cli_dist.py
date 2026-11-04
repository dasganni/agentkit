import gzip
import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit.cli_dist import cli_router


def client_for(dist: Path) -> TestClient:
    app = FastAPI()
    app.include_router(cli_router(dist))
    return TestClient(app)


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cli-dist.sh"


def build_dist(tmp_path: Path, schema_text: str = "[]") -> Path:
    """Output of the real scripts/cli-dist.sh with a stub `go` that writes a fixed binary."""
    stub = tmp_path / "bin"
    stub.mkdir(parents=True)
    go = stub / "go"
    go.write_text(
        '#!/bin/sh\necho "$*" >>"$(dirname "$0")/calls"\n'
        'while [ $# -gt 0 ]; do [ "$1" = -o ] && printf binary >"$2"; shift; done\n'
    )
    go.chmod(0o755)
    out = tmp_path / "dist"
    schema = tmp_path / "tools.json"
    schema.write_text(schema_text)
    subprocess.run(
        ["sh", str(SCRIPT), "demo", "DEMO", str(schema), str(out), "1.2.3"],
        check=True,
        env={**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"},
    )
    return out


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    out = build_dist(tmp_path)
    (out / "secret.gz").write_bytes(b"x")
    return out


def published(out: Path) -> str:
    return (out / "version").read_text().strip()


def test_version_and_download(dist: Path) -> None:
    client = client_for(dist)
    assert client.get("/cli/version").text == published(dist)
    response = client.get("/cli/linux-amd64")
    assert response.status_code == 200
    assert gzip.decompress(response.content) == b"binary"
    assert response.content == (dist / "linux-amd64").read_bytes()


def test_version_is_injected_into_every_build(dist: Path) -> None:
    calls = (dist.parent / "bin" / "calls").read_text().splitlines()
    flag = f"-X github.com/dasganni/agentkit/cli/internal/version.Version={published(dist)}"
    assert len(calls) == 6 and all(flag in call for call in calls)


def test_version_format_and_manifest(dist: Path) -> None:
    version = published(dist)
    assert re.fullmatch(r"1\.2\.3-[0-9a-f]{8}", version)
    assert re.fullmatch(r"[0-9A-Za-z._-]+", version)
    assert (dist / "version").read_text() == version + "\n"


def test_version_stable_for_same_inputs_and_changes_with_schema(tmp_path: Path) -> None:
    a = published(build_dist(tmp_path / "a"))
    assert a == published(build_dist(tmp_path / "b"))
    assert a != published(build_dist(tmp_path / "c", '[{"name": "x"}]'))


def test_sha256_matches(dist: Path) -> None:
    client = client_for(dist)
    line = client.get("/cli/linux-amd64.sha256").text
    assert line.split()[0] == hashlib.sha256((dist / "linux-amd64").read_bytes()).hexdigest()


def test_unknown_target_404(dist: Path) -> None:
    client = client_for(dist)
    assert client.get("/cli/plan9-amd64").status_code == 404
    assert client.get("/cli/darwin-arm64.gz").status_code == 404
    assert client_for(dist / "missing").get("/cli/version").status_code == 404


@pytest.mark.parametrize(
    "name", ["..%2Fversion", "%2e%2e/version", "secret", "linux-amd64/../secret"]
)
def test_traversal_rejected(dist: Path, name: str) -> None:
    assert client_for(dist).get(f"/cli/{name}").status_code == 404
