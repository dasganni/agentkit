import gzip
import hashlib
import os
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


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    """Output of the real scripts/cli-dist.sh with a stub `go` that writes a fixed binary."""
    stub = tmp_path / "bin"
    stub.mkdir()
    go = stub / "go"
    go.write_text(
        '#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && printf binary >"$2"; shift; done\n'
    )
    go.chmod(0o755)
    out = tmp_path / "dist"
    schema = tmp_path / "tools.json"
    schema.write_text("[]")
    subprocess.run(
        ["sh", str(SCRIPT), "demo", "DEMO", str(schema), str(out), "1.2.3"],
        check=True,
        env={**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"},
    )
    (out / "secret.gz").write_bytes(b"x")
    return out


def test_version_and_download(dist: Path) -> None:
    client = client_for(dist)
    assert client.get("/cli/version").text == "1.2.3"
    response = client.get("/cli/linux-amd64")
    assert response.status_code == 200
    assert gzip.decompress(response.content) == b"binary"
    assert response.content == (dist / "linux-amd64").read_bytes()


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
