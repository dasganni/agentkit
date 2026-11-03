import gzip
import hashlib
import http.server
import os
import platform
import shutil
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHIM = ROOT / "integrations" / "shim.sh"
HOOKS = ROOT / "integrations" / "hooks.json"
APP = "demoapp"
SHELL = shutil.which("dash") or "sh"

OS = {"Linux": "linux", "Darwin": "darwin"}[platform.system()]
ARCH = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}[
    platform.machine()
]


class Server:
    def __init__(self) -> None:
        self.version = "1.0.0"
        self.sha_override: str | None = None
        self.requests: list[str] = []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                server.requests.append(self.path)
                body = server.body(self.path)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def binary(self) -> bytes:
        script = f'#!/bin/sh\necho {APP}-{self.version} "$@"\n'
        return gzip.compress(script.encode(), mtime=0)

    def body(self, path: str) -> bytes | None:
        if path == "/cli/version":
            return self.version.encode()
        if path == f"/cli/{OS}-{ARCH}":
            return self.binary()
        if path == f"/cli/{OS}-{ARCH}.sha256":
            digest = self.sha_override or hashlib.sha256(self.binary()).hexdigest()
            return f"{digest}  {OS}-{ARCH}\n".encode()
        return None

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server() -> Iterator[Server]:
    s = Server()
    yield s
    s.stop()


@pytest.fixture
def shim(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / APP
    path.parent.mkdir()
    path.write_text(SHIM.read_text().replace("@APP@", APP))
    path.chmod(0o755)
    return path


def run(
    shim: Path, tmp_path: Path, url: str | None, *args: str
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }
    if url:
        env[f"{APP.upper()}_URL"] = url
    return subprocess.run(
        [SHELL, str(shim), *args], env=env, capture_output=True, text=True, timeout=60
    )


def force_recheck(tmp_path: Path) -> None:
    (tmp_path / "cache" / APP / "cli" / "checked").unlink()


def test_shim_is_posix() -> None:
    assert subprocess.run(["sh", "-n", str(SHIM)]).returncode == 0
    assert subprocess.run([SHELL, "-n", str(SHIM)]).returncode == 0


def test_hook_warms_in_background() -> None:
    command = HOOKS.read_text()
    assert "SessionStart" in command
    assert "--warm" in command
    assert command.count(" &") == 1


def test_shim_downloads_and_verifies(shim: Path, server: Server, tmp_path: Path) -> None:
    result = run(shim, tmp_path, server.url, "hello")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{APP}-1.0.0 hello"
    cached = tmp_path / "cache" / APP / "cli" / "1.0.0" / APP
    assert cached.is_file()
    run(shim, tmp_path, server.url, "again")
    assert server.requests.count(f"/cli/{OS}-{ARCH}") == 1
    assert server.requests.count("/cli/version") == 1


def test_shim_warm_only_fetches(shim: Path, server: Server, tmp_path: Path) -> None:
    result = run(shim, tmp_path, server.url, "--warm")
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert (tmp_path / "cache" / APP / "cli" / "1.0.0" / APP).is_file()


def test_shim_rejects_bad_sha(shim: Path, server: Server, tmp_path: Path) -> None:
    server.sha_override = "0" * 64
    result = run(shim, tmp_path, server.url, "hello")
    assert result.returncode != 0
    assert "sha256" in result.stderr
    assert result.stdout == ""
    assert not (tmp_path / "cache" / APP / "cli" / "1.0.0" / APP).exists()


def test_shim_offline_uses_cache(shim: Path, server: Server, tmp_path: Path) -> None:
    assert run(shim, tmp_path, server.url).returncode == 0
    url = server.url
    server.stop()
    force_recheck(tmp_path)
    result = run(shim, tmp_path, url, "offline")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{APP}-1.0.0 offline"


def test_shim_keeps_two_versions(shim: Path, server: Server, tmp_path: Path) -> None:
    for version in ("1.0.0", "1.1.0", "1.2.0"):
        server.version = version
        assert run(shim, tmp_path, server.url).returncode == 0
        force_recheck(tmp_path)
    cache = tmp_path / "cache" / APP / "cli"
    versions = sorted(p.name for p in cache.iterdir() if p.is_dir())
    assert versions == ["1.1.0", "1.2.0"]


def test_shim_no_server_no_cache_exit3(shim: Path, tmp_path: Path) -> None:
    result = run(shim, tmp_path, "http://127.0.0.1:9")
    assert result.returncode == 3
    assert run(shim, tmp_path, None).returncode == 3
