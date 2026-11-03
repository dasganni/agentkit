"""Sidecar runner: command lines, stream parsing, request checks, directories and the sandbox."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentkit.sidecar import runner, sandbox

TOKEN = "tok_" + "x" * 40
needs_landlock = pytest.mark.skipif(sandbox.abi() < 3, reason="Landlock ABI 3 not available")


def make_cfg(tmp_path: Path, **extra: Any) -> runner.Config:
    return runner.Config(
        root=tmp_path / "home",
        bins={"claude": "claude", "codex": "codex"},
        tmp=tmp_path,
        mcp_name="shop",
        require_sandbox=False,
        userns="off",
        **extra,
    )


def body(**extra: Any) -> dict[str, Any]:
    return {
        "system": "be brief",
        "prompt": "hi",
        "mcp": {"url": "http://shop:8000/mcp", "token": TOKEN},
        "allowed_tools": ["search", "items"],
        "max_turns": 5,
        "timeout_s": 20,
    } | extra


def flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def test_claude_arguments_are_locked_down(tmp_path: Path) -> None:
    req = runner.RunRequest.parse(body(model="sonnet"))
    args = runner.claude_args(make_cfg(tmp_path), req, tmp_path)
    assert flag(args, "--tools") == ""
    assert flag(args, "--permission-mode") == "dontAsk"
    assert flag(args, "--model") == "sonnet"
    assert flag(args, "--allowedTools").split(",") == ["mcp__shop__search", "mcp__shop__items"]
    assert {"--strict-mcp-config", "--no-session-persistence", "--include-partial-messages"} <= set(
        args
    )
    denied = args[args.index("--disallowedTools") + 1 : args.index("--strict-mcp-config")]
    assert {"Bash", "Read", "Write", "Edit", "WebFetch"} <= set(denied)
    joined = " ".join(args)
    assert TOKEN not in joined and "be brief" not in joined and "dangerously" not in joined


def test_claude_without_mcp_denies_every_mcp_tool(tmp_path: Path) -> None:
    req = runner.RunRequest.parse(body(mcp=None, allowed_tools=[]))
    args = runner.claude_args(make_cfg(tmp_path), req, tmp_path)
    assert "--mcp-config" not in args and args[-2:] == ["--disallowedTools", "mcp__*"]


def test_claude_argv_effort(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    for wanted, given in (("low", "low"), ("minimal", "low"), ("xhigh", "xhigh"), ("max", "max")):
        args = runner.claude_args(cfg, runner.RunRequest.parse(body(effort=wanted)), tmp_path)
        assert flag(args, "--effort") == given


def test_codex_argv_effort(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    for wanted, given in (("minimal", "minimal"), ("high", "high"), ("max", "xhigh")):
        args = runner.codex_args(
            cfg, runner.RunRequest.parse(body(effort=wanted, backend="codex")), tmp_path, None
        )
        assert f'model_reasoning_effort="{given}"' in args


def test_auto_effort_omits_flag(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    for effort in ("auto", None):
        claude = runner.claude_args(cfg, runner.RunRequest.parse(body(effort=effort)), tmp_path)
        codex = runner.codex_args(
            cfg, runner.RunRequest.parse(body(effort=effort, backend="codex")), tmp_path, None
        )
        assert "--effort" not in claude
        assert not any("model_reasoning_effort" in a for a in codex)


def test_unknown_effort_is_refused() -> None:
    with pytest.raises(runner.BadRequest):
        runner.RunRequest.parse(body(effort="extreme"))


def test_codex_arguments_are_locked_down(tmp_path: Path) -> None:
    req = runner.RunRequest.parse(
        body(backend="codex", model="gpt-x", json_schema={"type": "object"})
    )
    args = runner.codex_args(make_cfg(tmp_path), req, tmp_path, tmp_path / "catalog.json")
    assert flag(args, "--sandbox") == "read-only" and args[-1] == "-"
    assert flag(args, "--model") == "gpt-x" and flag(args, "--output-schema") == str(
        tmp_path / "schema.json"
    )
    configs = [args[i + 1] for i, a in enumerate(args) if a == "-c"]
    assert 'approval_policy="never"' in configs and "features.shell_tool=false" in configs
    assert 'mcp_servers.shop.enabled_tools=["search", "items"]' in configs
    assert f'mcp_servers.shop.bearer_token_env_var="{runner.MCP_TOKEN_ENV}"' in configs
    assert TOKEN not in " ".join(args)
    assert flag(args, "--disable") == "shell_tool"


def test_request_checks() -> None:
    for bad in (
        body(allowed_tools=["Bash; rm"]),
        body(mcp={"url": "file:///etc/passwd", "token": TOKEN}),
        body(mcp={"url": "http://shop/mcp", "token": "short"}),
        body(mcp=None),
        body(user="../x"),
        body(backend="gemini"),
        body(model="a b"),
        body(max_turns=0),
        body(timeout_s=10_000),
        body(json_schema="x"),
        {"system": "x"},
    ):
        with pytest.raises(runner.BadRequest):
            runner.RunRequest.parse(bad)
    assert runner.RunRequest.parse(body()).backend == "claude"


def test_messages_become_a_prompt() -> None:
    messages = [
        {"role": "user", "text": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "text": "now"},
    ]
    prompt = runner.render_prompt({"messages": messages})
    assert prompt.endswith("The user's new message:\nnow") and '"two"' in prompt
    assert runner.render_prompt({"messages": [{"role": "user", "text": "solo"}]}) == "solo"
    with pytest.raises(runner.BadRequest):
        runner.render_prompt({"messages": [{"role": "assistant", "text": "x"}]})


def test_directories_per_user(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    assert runner.backend_dir(cfg, "", "claude") == cfg.root / "claude"
    assert runner.backend_dir(cfg, "abc", "codex") == cfg.root / "users" / "abc" / "codex"
    directory = runner.prepare(cfg, "abc", "codex")
    assert (directory / "home").is_dir() and oct(directory.stat().st_mode & 0o777) == "0o700"
    env = runner.child_env(cfg, directory, "codex")
    assert env["CODEX_HOME"] == str(directory) and "CLAUDE_CONFIG_DIR" not in env


def test_tidy_keeps_only_the_login(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    directory = runner.prepare(cfg, "", "claude")
    (directory / ".credentials.json").write_text("{}")
    (directory / "junk.log").write_text("x")
    (directory / "cache").mkdir()
    (directory / "cache" / "a").write_text("x")
    (directory / "home" / "left").write_text("x")
    runner.tidy(directory, "claude")
    assert sorted(p.name for p in directory.iterdir()) == [".credentials.json", "home"]
    assert list((directory / "home").iterdir()) == []


def test_tidy_replaces_planted_symlinks(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    directory = runner.prepare(cfg, "", "codex")
    outside = tmp_path / "outside"
    outside.mkdir()
    (directory / "auth.json").symlink_to(outside)
    runner.tidy(directory, "codex")
    assert outside.is_dir() and not (directory / "auth.json").exists()


def test_clean_catalog_keeps_known_fields_and_fixes_tools() -> None:
    raw = {
        "models": [
            {
                "slug": "gpt-x",
                "apply_patch_tool_type": "freeform",
                "tool_mode": "code_mode_only",
                "priority": 1,
            }
        ]
    }
    model = runner.clean_catalog(raw)["models"][0]
    assert model["slug"] == "gpt-x" and model["priority"] == 1
    assert model["apply_patch_tool_type"] is None and model["tool_mode"] is None
    with pytest.raises(ValueError):
        runner.clean_catalog({"models": []})


def _delta(index: int, text: str) -> dict[str, Any]:
    event = {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "text_delta", "text": text},
    }
    return {"type": "stream_event", "api_message_id": "m", "event": event}


def _assistant(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "assistant", "message": {"id": "m", "content": list(content), "usage": {}}}


def test_claude_stream_events_and_usage() -> None:
    stream = runner.ClaudeStream("shop")
    lines: list[dict[str, Any]] = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "t1", "name": "mcp__shop__search"}]},
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "[]"}],
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "model": "claude-sonnet-5-5",
                "content": [{"type": "text", "text": "Hi"}],
                "usage": {
                    "input_tokens": 5,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 20,
                },
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "result": "Hi",
            "structured_output": {"a": 1},
            "usage": {
                "input_tokens": 5,
                "output_tokens": 3,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 20,
            },
        },
    ]
    events = [e for line in lines for e in stream.feed(line)]
    assert [e["type"] for e in events] == ["tool_call", "tool_result", "text", "usage", "final"]
    assert (
        events[0]["name"] == "search"
        and events[1]["name"] == "search"
        and events[1]["output"] == "[]"
    )
    assert events[3] == {
        "type": "usage", "model": "claude-sonnet-5-5", "input_tokens": 5, "cache_read_tokens": 100,
        "cache_write_tokens": 20, "output_tokens": 3, "context_tokens": 125,
    }  # fmt: skip
    assert events[4] == {"type": "final", "text": "Hi", "structured": {"a": 1}} and stream.ended


def test_claude_stream_error_codes() -> None:
    stream = runner.ClaudeStream()
    stream.feed({"type": "assistant", "error": "rate_limit", "is_api_error_message": True})
    out = stream.feed(
        {"type": "result", "subtype": "success", "is_error": True, "result": "x", "usage": {}}
    )
    assert out[-1] == {"type": "error", "code": "limit", "message": ""}
    out = runner.ClaudeStream().feed({"type": "result", "subtype": "error_max_turns", "usage": {}})
    assert out[-1]["code"] == "max_turns"
    assert (
        runner.classify("Not logged in") == "not_connected" and runner.classify("boom") == "failed"
    )


def test_codex_stream_events() -> None:
    stream = runner.CodexStream(structured=True, model="gpt-x")
    lines: list[dict[str, Any]] = [
        {"type": "item.started", "item": {"type": "mcp_tool_call", "id": "i1", "tool": "search"}},
        {
            "type": "item.completed",
            "item": {"type": "mcp_tool_call", "id": "i1", "tool": "search", "status": "failed"},
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"a": 1}'}},
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2},
        },
    ]
    events = [e for line in lines for e in stream.feed(line)]
    assert [e["type"] for e in events] == ["tool_call", "tool_result", "text", "usage", "final"]
    assert events[1]["is_error"] is True
    assert events[3] == {
        "type": "usage", "model": "gpt-x", "input_tokens": 10,
        "cache_read_tokens": 4, "output_tokens": 2,
    }  # fmt: skip
    assert events[4]["structured"] == {"a": 1}


def test_codex_stream_failure_and_bad_structure() -> None:
    stream = runner.CodexStream(structured=False)
    stream.feed({"type": "error", "message": "usage limit reached"})
    out = stream.feed({"type": "turn.failed", "error": {}})
    assert out == [{"type": "error", "code": "limit", "message": ""}] and stream.ended
    bad = runner.CodexStream(structured=True)
    bad.feed({"type": "item.completed", "item": {"type": "agent_message", "text": "no json"}})
    assert bad.feed({"type": "turn.completed", "usage": {}})[-1]["code"] == "failed"


def _abandoned_and_retried(first_complete: bool) -> list[dict[str, Any]]:
    """stdout of Claude Code (`--include-partial-messages`) when the API stream of the first attempt
    breaks off mid-text and the answer streams again."""
    meta = {"session_id": "s", "parent_tool_use_id": None}

    def event(mid: str, inner: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {"type": "stream_event", "event": inner, **meta, "api_message_id": mid, **extra}

    def text_block(mid: str, index: int, pieces: list[str]) -> list[dict[str, Any]]:
        start = {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "text", "text": ""},
        }
        deltas = [
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "text_delta", "text": p},
            }
            for p in pieces
        ]
        return [event(mid, start), *(event(mid, d) for d in deltas)]

    def assistant(mid: str, text: str) -> dict[str, Any]:
        message = {
            "id": mid,
            "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": text}],
            "usage": {},
        }
        return {"type": "assistant", "message": message, **meta}

    def message_start(mid: str) -> dict[str, Any]:
        return event(
            mid,
            {"type": "message_start", "message": {"id": mid, "role": "assistant", "content": []}},
        )

    cut = 1 if first_complete else 0
    lines = [{"type": "system", "subtype": "init", "session_id": "s"}, message_start("msg_a")]
    if first_complete:
        lines += [*text_block("msg_a", 0, ["Vorweg."]), assistant("msg_a", "Vorweg.")]
        lines.append(event("msg_a", {"type": "content_block_stop", "index": 0}))
    lines += text_block("msg_a", cut, ["Ich habe ", "deinen Eintrag "])
    lines.append(event("msg_a", {"type": "content_block_stop", "index": cut}))
    abandoned = {"api_message_id": "msg_a", "from_block_index": cut}
    lines.append(event("msg_a", {"type": "message_stop"}, abandoned_blocks=abandoned))
    lines += [
        message_start("msg_b"),
        *text_block("msg_b", 0, ["Ich habe ", "deinen Eintrag ", "gespeichert."]),
    ]
    lines += [assistant("msg_b", "Ich habe deinen Eintrag gespeichert.")]
    lines.append(event("msg_b", {"type": "message_stop"}))
    lines.append(
        {"type": "result", "subtype": "success", "is_error": False, "result": "x", "usage": {}}
    )
    return lines


@pytest.mark.parametrize("first_complete", [False, True])
def test_an_abandoned_response_is_retracted(first_complete: bool) -> None:
    stream = runner.ClaudeStream()
    events = [e for line in _abandoned_and_retried(first_complete) for e in stream.feed(line)]
    shown = ""
    for e in events:
        if e["type"] == "text_delta":
            shown += e["text"]
        elif e["type"] == "retract":
            assert shown.endswith(e["text"])
            shown = shown.removesuffix(e["text"])
    gap = "\n\n" if first_complete else ""
    assert {"type": "retract", "text": f"{gap}Ich habe deinen Eintrag "} in events
    assert (
        shown == f"{'Vorweg.' if first_complete else ''}{gap}Ich habe deinen Eintrag gespeichert."
    )


def test_a_new_text_block_starts_a_paragraph() -> None:
    stream = runner.ClaudeStream("shop")
    tool = {"type": "tool_use", "id": "t1", "name": "mcp__shop__search", "input": {}}
    lines = [
        _delta(0, "Erster "),
        _delta(0, "Block."),
        _assistant({"type": "text", "text": "Erster Block."}),
        _assistant(tool),
        _delta(2, "Zweiter."),
        _assistant({"type": "text", "text": "Zweiter."}),
        _delta(3, "Dritter."),
        _assistant({"type": "text", "text": "Dritter."}),
    ]
    events = [e for line in lines for e in stream.feed(line)]
    shown = "".join(e["text"] for e in events if e["type"] == "text_delta")
    assert shown == "Erster Block.Zweiter.\n\nDritter."


def test_unstreamed_text_blocks_are_separated_too() -> None:
    out = runner.ClaudeStream().feed(
        _assistant({"type": "text", "text": "Eins."}, {"type": "text", "text": "Zwei."})
    )
    assert [e["text"] for e in out] == ["Eins.", "\n\nZwei."]


def test_abandoned_blocks_of_another_response_retract_nothing() -> None:
    stream = runner.ClaudeStream()
    delta = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "Hi"},
    }
    stream.feed({"type": "stream_event", "api_message_id": "msg_a", "event": delta})
    stop = {"type": "stream_event", "event": {"type": "message_stop"}, "api_message_id": "msg_z"}
    assert (
        stream.feed(stop | {"abandoned_blocks": {"api_message_id": "msg_z", "from_block_index": 0}})
        == []
    )
    assert (
        stream.feed(stop | {"abandoned_blocks": {"api_message_id": "msg_a", "from_block_index": 1}})
        == []
    )
    assert stream.feed(
        stop | {"abandoned_blocks": {"api_message_id": "msg_a", "from_block_index": 0}}
    ) == [{"type": "retract", "text": "Hi"}]


def test_slots_limit_per_user_and_total() -> None:
    slots = runner.Slots(per_user=1, total=2)
    assert slots.acquire("a") and not slots.acquire("a")
    assert slots.acquire("b") and not slots.acquire("c")
    slots.release("a")
    assert slots.acquire("c")


def test_sandboxed_without_landlock_passes_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "sandbox_abi", lambda: 0)
    args, env = runner.sandboxed(make_cfg(tmp_path), ["claude"], {"A": "1"}, tmp_path)
    assert args == ["claude"] and env == {"A": "1"}


def test_sandboxed_keeps_paths_off_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "sandbox_abi", lambda: 3)
    work = tmp_path / "work"
    args, env = runner.sandboxed(make_cfg(tmp_path), ["claude", "-p"], {}, work)
    assert args[:3] == [sys.executable, "-I", "-S"] and args[-3:] == ["--", "claude", "-p"]
    assert str(work) not in " ".join(args) and str(work) in env[sandbox.RULES_ENV]


def test_bwrap_view_hides_the_volume_and_mounts_back(tmp_path: Path) -> None:
    command = sandbox.bwrap_command(
        "bwrap", ("--proc", "/proc"), ["claude"], [str(tmp_path)], [str(tmp_path)], str(tmp_path)
    )
    assert command[0] == "bwrap" and command[-2:] == ["--", "claude"]
    assert command.index("--tmpfs", command.index("/dev")) < command.index("--bind")
    assert command[command.index("--chdir") + 1] == str(tmp_path)


def test_sandbox_problem_reports_the_abi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_cfg(tmp_path)
    assert runner.sandbox_problem(cfg) is None
    strict = runner.Config(root=cfg.root, bins=cfg.bins, userns="off")
    monkeypatch.setattr(runner, "sandbox_abi", lambda: 0)
    assert "not available" in str(runner.sandbox_problem(strict))
    monkeypatch.setattr(runner, "sandbox_abi", lambda: 2)
    assert "below" in str(runner.sandbox_problem(strict))


def test_sandbox_bare_name_resolves(tmp_path: Path) -> None:
    if sandbox.abi() < 1:
        pytest.skip("Landlock not available")
    bins = tmp_path / "bin"
    bins.mkdir()
    cli = bins / "fakecli"
    cli.write_text(f"#!{sys.executable}\nprint('ran')\n")
    cli.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    cfg = runner.Config(
        root=tmp_path / "home",
        bins={},
        path=f"{bins}:/usr/bin:/bin",
        ro_paths=(*runner.SYSTEM_PATHS, str(bins)),
        require_sandbox=False,
        userns="off",
    )
    args, env = runner.sandboxed(cfg, ["fakecli"], {"PATH": cfg.path}, work)
    assert args[0] == sys.executable and sandbox.RULES_ENV in env
    done = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ran"


@needs_landlock
def test_sandbox_reports_a_missing_command() -> None:
    env = {
        "PATH": "/usr/bin:/bin",
        sandbox.RULES_ENV: '{"ro": ["/usr", "/lib", "/bin", "/etc"], "rw": []}',
    }
    done = subprocess.run(
        [sys.executable, "-I", "-S", str(runner.SANDBOX), "--", "no-such-command-here"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 127


@needs_landlock
def test_sandbox_blocks_files_outside_the_rules(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    ro = ["/usr", "/lib", "/lib64", "/bin", "/etc", sys.prefix, sys.base_prefix]
    rules = json.dumps({"ro": ro, "rw": [str(allowed)]})
    env = {"PATH": "/usr/bin:/bin", sandbox.RULES_ENV: rules}
    code = f"import sys\ntry:\n    open({str(secret)!r}).read()\nexcept OSError:\n    sys.exit(7)\n"
    done = subprocess.run(
        [sys.executable, "-I", "-S", str(runner.SANDBOX), "--", sys.executable, "-I", "-c", code],
        env=env,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 7, done.stderr
