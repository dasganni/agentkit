"""The sidecar over its unix socket with fake Claude Code and Codex executables."""

import json
import os
import socket
import stat
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentkit.sidecar import runner, server

SECRET = "sidecar-secret"
TOKEN = "tok_" + "x" * 40

FAKE_CLAUDE = """\
import json, os, sys
prompt = sys.stdin.read()
config = os.environ["CLAUDE_CONFIG_DIR"]
with open(os.path.join(config, "fake-calls.jsonl"), "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "env": sorted(os.environ)}) + "\\n")
def emit(obj):
    print(json.dumps(obj), flush=True)
if "AUTH" in prompt:
    emit({"type": "assistant", "error": "authentication_failed", "is_api_error_message": True,
          "message": {"content": [{"type": "text", "text": "Not logged in"}]}})
    emit({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in",
          "usage": {}})
    sys.exit(1)
if "DELTA" in prompt:
    for part in ("Hel", "lo"):
        emit({"type": "stream_event", "event": {"type": "content_block_delta",
              "delta": {"type": "text_delta", "text": part}}})
    emit({"type": "assistant", "message": {"model": "claude-sonnet-5-5",
          "usage": {"input_tokens": 5}, "content": [{"type": "text", "text": "Hello"}]}})
    emit({"type": "result", "subtype": "success", "result": "Hello",
          "usage": {"input_tokens": 5, "output_tokens": 3}})
    sys.exit(0)
if "HANG" in prompt:
    import time
    time.sleep(60)
emit({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "tu1", "name": "mcp__shop__search", "input": {}}]}})
emit({"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "tu1", "content": [{"type": "text", "text": "[]"}]}]}})
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}})
emit({"type": "result", "subtype": "success", "result": "Hello",
      "usage": {"input_tokens": 12, "output_tokens": 3}})
"""

FAKE_CODEX = """\
import json, os, sys
if sys.argv[1:3] == ["debug", "models"]:
    models = [{"slug": "gpt-x", "tool_mode": "code_mode_only"}, {"slug": "gpt-y"}]
    print(json.dumps({"models": models}))
    sys.exit(0)
prompt = sys.stdin.read()
with open(os.path.join(os.environ["CODEX_HOME"], "fake-calls.jsonl"), "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "env": sorted(os.environ)}) + "\\n")
def emit(obj):
    print(json.dumps(obj), flush=True)
emit({"type": "item.started", "item": {"type": "mcp_tool_call", "id": "i1", "tool": "search"}})
emit({"type": "item.completed", "item": {"type": "mcp_tool_call", "id": "i1", "tool": "search",
      "result": {"content": []}}})
emit({"type": "item.completed", "item": {"type": "agent_message", "text": "Hallo"}})
emit({"type": "turn.completed", "usage": {"input_tokens": 9,
      "cached_input_tokens": 2, "output_tokens": 1}})
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
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    return runner.Config(
        root=tmp_path / "home",
        bins={"claude": str(bins / "claude"), "codex": str(bins / "codex")},
        tmp=tmp,
        socket=str(tmp_path / "agent.sock"),
        secret=SECRET,
        mcp_name="shop",
        path=f"{bins}:/usr/bin:/bin",
        ro_paths=(*runner.SYSTEM_PATHS, str(bins), sys.prefix, sys.base_prefix),
        require_sandbox=False,
        self_check=False,
        userns="off",
        keep_files=("fake-calls.jsonl",),
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


def body(**extra: Any) -> dict[str, Any]:
    return {
        "system": "be brief",
        "prompt": "hi",
        "mcp": {"url": "http://shop:8000/mcp", "token": TOKEN},
        "allowed_tools": ["search", "items"],
        "max_turns": 5,
        "timeout_s": 20,
    } | extra


def events_of(res: httpx.Response) -> list[dict[str, Any]]:
    assert res.status_code == 200, res.text
    return [json.loads(line) for line in res.text.splitlines()]


def calls(cfg: runner.Config, backend: str, user: str = "") -> list[dict[str, Any]]:
    directory = runner.backend_dir(cfg, user, backend)
    return [json.loads(line) for line in (directory / "fake-calls.jsonl").read_text().splitlines()]


def test_wrong_secret_is_refused(client: httpx.Client) -> None:
    for headers in ({"X-Agent-Secret": "nope"}, {"X-Agent-Secret": ""}):
        assert client.post("/run", json=body(), headers=headers).status_code == 403
        assert client.get("/models", headers=headers).status_code == 403
        assert client.get("/healthz", headers=headers).status_code == 403


def test_a_server_without_a_secret_refuses_everything(cfg: runner.Config, tmp_path: Path) -> None:
    open_cfg = runner.Config(
        **{**cfg.__dict__, "secret": "", "socket": str(tmp_path / "open.sock")}
    )
    srv = server.Server(open_cfg.socket, open_cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=open_cfg.socket), base_url="http://agent"
    ) as http:
        assert http.get("/healthz").status_code == 403
    srv.shutdown()
    srv.server_close()


def test_health_reports_backends(client: httpx.Client) -> None:
    data = client.get("/healthz").json()
    assert data["ok"] is True and data["backends"] == {"claude": True, "codex": True}
    assert client.get("/health").status_code == 200


def test_claude_run_streams_events(client: httpx.Client, cfg: runner.Config) -> None:
    events = events_of(client.post("/run", json=body(model="haiku", effort="high")))
    assert [e["type"] for e in events] == ["tool_call", "tool_result", "text", "usage", "final"]
    assert events[0]["name"] == "search" and events[-1]["text"] == "Hello"
    argv = calls(cfg, "claude")[-1]["argv"]
    assert argv[argv.index("--model") + 1] == "haiku" and argv[argv.index("--effort") + 1] == "high"
    assert TOKEN not in json.dumps(calls(cfg, "claude"))
    directory = runner.backend_dir(cfg, "", "claude")
    assert sorted(p.name for p in directory.iterdir()) == ["fake-calls.jsonl", "home"]


def test_claude_run_emits_text_deltas(client: httpx.Client) -> None:
    events = events_of(client.post("/run", json=body(prompt="DELTA")))
    assert [e["type"] for e in events] == ["text_delta", "text_delta", "text", "usage", "final"]
    assert events[2]["streamed"] is True and events[3]["model"] == "claude-sonnet-5-5"


def test_a_missing_login_is_an_error_event(client: httpx.Client) -> None:
    events = events_of(client.post("/run", json=body(prompt="AUTH")))
    assert events[-1] == {"type": "error", "code": "not_connected", "message": ""}


def test_codex_run_uses_its_own_directory_and_token_env(
    client: httpx.Client, cfg: runner.Config
) -> None:
    events = events_of(
        client.post("/run", json=body(backend="codex", user="u1", model="gpt-x", effort="max"))
    )
    assert [e["type"] for e in events] == ["tool_call", "tool_result", "text", "usage", "final"]
    assert events[3]["cache_read_tokens"] == 2 and events[-1]["text"] == "Hallo"
    call = calls(cfg, "codex", "u1")[-1]
    assert 'model_reasoning_effort="xhigh"' in call["argv"]
    assert runner.MCP_TOKEN_ENV in call["env"] and TOKEN not in " ".join(call["argv"])
    assert (cfg.root / "users" / "u1" / "codex").is_dir()


def test_codex_catalog_file_has_no_extra_tools(client: httpx.Client, cfg: runner.Config) -> None:
    events_of(client.post("/run", json=body(backend="codex")))
    catalog = json.loads((cfg.tmp / "codex-model-catalog.json").read_text())
    assert [m["slug"] for m in catalog["models"]] == ["gpt-x", "gpt-y"]
    assert catalog["models"][0]["tool_mode"] is None
    assert stat.S_IMODE((cfg.tmp / "codex-model-catalog.json").stat().st_mode) == 0o644


def test_unknown_codex_model_is_refused(client: httpx.Client) -> None:
    res = client.post("/run", json=body(backend="codex", model="gpt-evil"))
    assert res.status_code == 400 and res.json() == {"error": "unknown Codex model"}


def test_models_lists_the_catalogs(client: httpx.Client, cfg: runner.Config) -> None:
    assert client.get("/models", params={"backend": "codex"}).json() == {
        "models": ["gpt-x", "gpt-y"]
    }
    assert client.get("/models", params={"backend": "claude"}).json() == {"models": None}
    assert client.get("/models").json() == {"models": None}
    assert client.get("/models", params={"backend": "gemini"}).status_code == 400
    listed = runner.Config(**{**cfg.__dict__, "claude_models": ("sonnet", "opus")})
    assert runner.Runtime(listed).models("claude") == ["sonnet", "opus"]


def test_missing_executable_is_an_error_event(client: httpx.Client, cfg: runner.Config) -> None:
    (Path(cfg.bins["codex"])).unlink()
    events = events_of(client.post("/run", json=body(backend="codex")))
    assert events == [{"type": "error", "code": "missing", "message": ""}]


def test_a_timeout_kills_the_cli(client: httpx.Client) -> None:
    events = events_of(client.post("/run", json=body(prompt="HANG", timeout_s=1)))
    assert events[-1] == {"type": "error", "code": "timeout", "message": ""}


def test_busy_when_the_slot_is_taken(cfg: runner.Config) -> None:
    runtime = runner.Runtime(cfg)
    assert runtime.slots.acquire("")
    events = list(runtime.run(runner.RunRequest.parse(body()), lambda: False))
    assert events == [{"type": "error", "code": "busy", "message": ""}]


def test_invalid_requests_are_400(client: httpx.Client) -> None:
    assert client.post("/run", json=body(allowed_tools=["Bash; rm"])).status_code == 400
    assert client.post("/run", json=body(effort="extreme")).status_code == 400
    assert client.post("/run", json={"system": "x"}).status_code == 400
    assert client.post("/run", content=b"[1]").status_code == 400
    assert client.post("/nowhere", json={}).status_code == 404


def test_bad_content_length_400(client: httpx.Client, cfg: runner.Config) -> None:
    for length in ("abc", "-5", "1e3", ""):
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(10)
            sock.connect(cfg.socket)
            head = f"POST /run HTTP/1.0\r\nX-Agent-Secret: {SECRET}\r\nContent-Length: {length}"
            sock.sendall(f"{head}\r\n\r\n{{}}".encode())
            answer = sock.makefile("rb").read()
        assert b" 400 " in answer.split(b"\r\n", 1)[0], (length, answer[:40])


def test_secret_file_0600_atomic(tmp_path: Path) -> None:
    target = tmp_path / "agent.secret"
    target.symlink_to(tmp_path / "elsewhere")
    server.write_secret(target, "s1")
    server.write_secret(target, "s2")
    assert not target.is_symlink() and target.read_text() == "s2"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not (tmp_path / "elsewhere").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["agent.secret"]


def test_help_lists_the_environment() -> None:
    import contextlib
    import io

    from agentkit.sidecar import __main__ as entry

    out = io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit):
        entry.main(["--help"])
    for name in ("AGENT_SOCKET", "AGENT_SECRET", "AGENT_HOME", "AGENT_MCP_NAME", "AGENT_USERNS"):
        assert name in out.getvalue()


def test_non_ascii_secret_is_refused(client: httpx.Client) -> None:
    res = client.get("/healthz", headers={"X-Agent-Secret": "é".encode("latin-1")})
    assert res.status_code == 403


def test_health_matches_what_the_apps_read(client: httpx.Client) -> None:
    data = client.get("/healthz").json()
    assert data["installed"] is True
    assert set(data["sandbox"]) == {"landlock", "min_abi", "bwrap", "required", "active", "reason"}
    assert "sandbox_problem" in data and data["sandbox_required"] is False
    assert data["safe"] == {"claude": None, "codex": None} and data["unexpected_tools"] == {}


def test_prune_removes_unknown_users(client: httpx.Client, cfg: runner.Config) -> None:
    users = cfg.root / "users"
    for name in ("keep", "gone", "fresh"):
        (users / name / "claude").mkdir(parents=True)
    old = time.time() - 3600
    for name in ("keep", "gone"):
        os.utime(users / name, (old, old))
    assert client.post("/prune", json={"keep": ["keep"]}).json() == {"removed": 1}
    assert sorted(p.name for p in users.iterdir()) == ["fresh", "keep"]
    assert client.post("/prune", json={"keep": []}).status_code == 400
    assert client.post("/prune", json={"keep": "all"}).status_code == 400
    assert client.post("/prune", json={"keep": ["../x"]}).status_code == 400
    assert sorted(p.name for p in users.iterdir()) == ["fresh", "keep"]


PROBE_CLAUDE = """\
import json, os, sys, urllib.request
sys.stdin.read()
tools = [{"name": t} for t in "TOOLS".split(",") if t]
req = urllib.request.Request(
    os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages",
    data=json.dumps({"tools": tools}).encode(),
    headers={"Content-Type": "application/json"},
)
try:
    urllib.request.urlopen(req)
except Exception:
    pass
"""


def self_checked(cfg: runner.Config, tools: str) -> Any:
    fake(Path(cfg.bins["claude"]), PROBE_CLAUDE.replace("TOOLS", tools))
    cfg = runner.Config(
        **{**cfg.__dict__, "self_check": True, "bins": {**cfg.bins, "codex": "no-such-codex"}}
    )
    srv = server.Server(cfg.socket, cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    http = httpx.Client(
        transport=httpx.HTTPTransport(uds=cfg.socket),
        base_url="http://agent",
        headers={"X-Agent-Secret": SECRET},
        timeout=60,
    )
    for _ in range(300):
        if http.get("/healthz").json()["safe"]["claude"] is not None:
            break
        time.sleep(0.1)
    return srv, http


def test_self_check_refuses_a_backend_with_unexpected_tools(cfg: runner.Config) -> None:
    srv, http = self_checked(cfg, "Bash,Read")
    try:
        health = http.get("/healthz").json()
        assert health["safe"]["claude"] is False
        assert health["unexpected_tools"]["claude"] == ["default: Bash", "default: Read"]
        events = events_of(http.post("/run", json=body(backend="claude")))
        assert events == [{"type": "error", "code": "unsafe", "message": ""}]
    finally:
        http.close()
        srv.shutdown()
        srv.server_close()


def test_self_check_passes_a_clean_backend(cfg: runner.Config) -> None:
    srv, http = self_checked(cfg, "")
    try:
        health = http.get("/healthz").json()
        assert health["safe"]["claude"] is True and "claude" not in health["unexpected_tools"]
        assert health["safe"]["codex"] is False  # not installed
        events = events_of(http.post("/run", json=body(backend="claude")))
        assert all(
            e.get("code") not in ("unsafe", "starting") for e in events
        )  # the probe CLI says nothing
    finally:
        http.close()
        srv.shutdown()
        srv.server_close()
