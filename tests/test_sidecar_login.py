"""Sign-in, code entry, status and logout against fake Claude Code and Codex executables."""

import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from agentkit.sidecar import runner, server

SECRET = "sidecar-secret"

FAKE_CLAUDE = """\
import os, sys
config = os.environ["CLAUDE_CONFIG_DIR"]
if sys.argv[1:3] == ["auth", "login"]:
    print("Open https://claude.ai/oauth/authorize?x=1 and paste the code", flush=True)
    code = sys.stdin.readline().strip()
    if code != "good-code":
        sys.exit(1)
    open(os.path.join(config, ".credentials.json"), "w").write("{}")
    sys.exit(0)
if sys.argv[1:3] == ["auth", "status"]:
    ok = os.path.exists(os.path.join(config, ".credentials.json"))
    print('{"loggedIn": true, "email": "a@b.c"}' if ok else '{"loggedIn": false}')
    sys.exit(0 if ok else 1)
if sys.argv[1:3] == ["auth", "logout"]:
    os.remove(os.path.join(config, ".credentials.json"))
    sys.exit(0)
"""

FAKE_CODEX = """\
import os, sys, time
home = os.environ["CODEX_HOME"]
if sys.argv[1:3] == ["login", "--device-auth"]:
    print("Go to https://auth.example.com/codex/device and enter ABCD-12345", flush=True)
    time.sleep(0.3)
    open(os.path.join(home, "auth.json"), "w").write("{}")
    sys.exit(0)
if sys.argv[1:3] == ["login", "status"]:
    ok = os.path.exists(os.path.join(home, "auth.json"))
    print("Logged in using ChatGPT" if ok else "Not logged in")
    sys.exit(0 if ok else 1)
sys.exit(0)
"""


def fake(path: Path, source: str) -> None:
    path.write_text(f"#!{sys.executable}\n{source}")
    path.chmod(0o755)


@pytest.fixture
def cfg(tmp_path: Path) -> runner.Config:
    bins = tmp_path / "bin"
    bins.mkdir()
    fake(bins / "claude", FAKE_CLAUDE)
    fake(bins / "codex", FAKE_CODEX)
    return runner.Config(
        root=tmp_path / "home",
        bins={"claude": str(bins / "claude"), "codex": str(bins / "codex")},
        tmp=tmp_path,
        socket=str(tmp_path / "agent.sock"),
        secret=SECRET,
        path=f"{bins}:/usr/bin:/bin",
        ro_paths=(*runner.SYSTEM_PATHS, str(bins), sys.prefix, sys.base_prefix),
        require_sandbox=False,
        userns="off",
        status_ttl=0.0,
    )


@pytest.fixture
def client(cfg: runner.Config) -> Iterator[httpx.Client]:
    srv = server.Server(cfg.socket, cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=cfg.socket),
        base_url="http://agent",
        headers={"X-Agent-Secret": SECRET},
        timeout=60,
    ) as http:
        yield http
    srv.shutdown()
    srv.server_close()


def status(client: httpx.Client, backend: str) -> dict:
    res = client.get("/login/status", params={"user": "u1", "backend": backend})
    assert res.status_code == 200, res.text
    return res.json()


def test_login_extracts_url(client: httpx.Client) -> None:
    claude = client.post("/login/start", json={"user": "u1", "backend": "claude"})
    assert claude.json() == {"url": "https://claude.ai/oauth/authorize?x=1"}
    codex = client.post("/login/start", json={"user": "u1", "backend": "codex"})
    assert codex.json() == {"url": "https://auth.example.com/codex/device", "code": "ABCD-12345"}


def test_code_is_written_to_pty(client: httpx.Client) -> None:
    body = {"user": "u1", "backend": "claude"}
    client.post("/login/start", json=body).raise_for_status()
    assert status(client, "claude") == {"connected": False}
    res = client.post("/login/code", json={**body, "code": "good-code"})
    assert res.json() == {"connected": True}
    assert status(client, "claude") == {"connected": True, "account_hint": "a@b.c"}

    client.post("/login/start", json=body).raise_for_status()
    assert client.post("/login/code", json={**body, "code": "bad"}).json() == {"connected": False}
    assert client.post("/login/code", json={**body, "code": "x y"}).status_code == 400


def test_codex_signs_in_without_a_code(client: httpx.Client) -> None:
    client.post("/login/start", json={"user": "u1", "backend": "codex"}).raise_for_status()
    for _ in range(50):
        if status(client, "codex")["connected"]:
            break
        threading.Event().wait(0.1)
    assert status(client, "codex") == {"connected": True, "account_hint": "Logged in using ChatGPT"}
    res = client.post("/login/code", json={"user": "u1", "backend": "codex", "code": "abc"})
    assert res.status_code == 400


def test_logout_removes_credentials(client: httpx.Client, cfg: runner.Config) -> None:
    body = {"user": "u1", "backend": "claude"}
    client.post("/login/start", json=body).raise_for_status()
    client.post("/login/code", json={**body, "code": "good-code"}).raise_for_status()
    directory = runner.backend_dir(cfg, "u1", "claude")
    assert (directory / ".credentials.json").exists()
    assert client.post("/logout", json=body).status_code == 200
    assert not directory.exists()
    assert status(client, "claude") == {"connected": False}
