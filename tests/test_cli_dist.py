import gzip
import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit.cli_dist import cli_router


def client_for(dist: Path) -> TestClient:
    app = FastAPI()
    app.include_router(cli_router(dist))
    return TestClient(app)


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    data = gzip.compress(b"binary")
    (tmp_path / "version").write_text("1.2.3\n")
    (tmp_path / "linux-amd64.gz").write_bytes(data)
    (tmp_path / "linux-amd64.sha256").write_text(hashlib.sha256(data).hexdigest() + "\n")
    (tmp_path / "secret.gz").write_bytes(b"x")
    return tmp_path


def test_version_and_download(dist: Path) -> None:
    client = client_for(dist)
    assert client.get("/cli/version").text == "1.2.3"
    response = client.get("/cli/linux-amd64")
    assert response.status_code == 200
    assert gzip.decompress(response.content) == b"binary"


def test_sha256_matches(dist: Path) -> None:
    client = client_for(dist)
    digest = client.get("/cli/linux-amd64.sha256").text
    assert digest == hashlib.sha256((dist / "linux-amd64.gz").read_bytes()).hexdigest()


def test_unknown_target_404(dist: Path) -> None:
    client = client_for(dist)
    assert client.get("/cli/plan9-amd64").status_code == 404
    assert client.get("/cli/darwin-arm64").status_code == 404
    assert client_for(dist / "missing").get("/cli/version").status_code == 404


@pytest.mark.parametrize(
    "name", ["..%2Fversion", "%2e%2e/version", "secret", "linux-amd64/../secret"]
)
def test_traversal_rejected(dist: Path, name: str) -> None:
    assert client_for(dist).get(f"/cli/{name}").status_code == 404
