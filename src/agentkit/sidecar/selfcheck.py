"""Fail-closed tool self-check: each CLI is started with the exact run flags against a fake model
endpoint on loopback, and a backend whose CLI offers any tool beyond the expected built-ins is
refused until the image is fixed."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from agentkit.sidecar import runner
from agentkit.sidecar.runner import BACKENDS, Config, Runtime

log = logging.getLogger("agent-runner")

# Built-in tools the pinned CLIs still offer with every run flag and the cleaned Codex catalog:
# request_user_input has nobody to ask in exec mode.
EXPECTED_BUILTINS = {"claude": frozenset[str](), "codex": frozenset({"request_user_input"})}
UNKNOWN_PROBE_MODEL = "agentkit-probe-unknown-model"


class Health:
    """What the self-check found per backend, and the sandbox state the health report shows."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # None: not checked (yet); True: only the expected tools; False: refuse this backend.
        self.safe: dict[str, bool | None] = {b: None for b in BACKENDS}
        self.found: dict[str, list[str]] = {}
        self.checking = cfg.self_check

    def refusal(self, backend: str) -> str | None:
        """The error code a run of `backend` is refused with, or None."""
        if not self.cfg.self_check:
            return None
        if self.safe[backend] is None:
            return "starting" if self.checking else "unsafe"
        return None if self.safe[backend] else "unsafe"

    def report(self) -> dict[str, Any]:
        return {
            "safe": dict(self.safe),
            "unexpected_tools": {b: t for b, t in self.found.items() if t},
        }


class _Capture(BaseHTTPRequestHandler):
    """A fake model endpoint: records the tool names of the first request and refuses it."""

    server: Any

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            names = tool_names(json.loads(self.rfile.read(length) or b"{}"))
            if self.server.names is None:
                self.server.names = names
        except (ValueError, AttributeError):
            pass
        data = b'{"error":{"type":"invalid_request_error","message":"probe"}}'
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


def _add_tools(names: list[str], tools: list[Any], prefix: str = "") -> None:
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or tool.get("type"))
        members = tool.get("tools")
        if tool.get("type") != "namespace" and not isinstance(members, list):
            names.append(prefix + name)
            continue
        _add_tools(names, members if isinstance(members, list) else [], f"{prefix}{name}.")
        if not members:
            names.append(prefix + name)


def _walk_tools(names: list[str], value: object) -> None:
    items = (
        value.items()
        if isinstance(value, dict)
        else enumerate(value)
        if isinstance(value, list)
        else ()
    )
    for key, item in items:
        if key == "tools" and isinstance(item, list):
            _add_tools(names, item)
        else:
            _walk_tools(names, item)


def tool_names(body: dict[str, Any]) -> list[str]:
    """Every tool a model request offers: each `tools` list anywhere in the request (Codex puts
    most models' tools into an `additional_tools` input item), a namespace counted with its
    members as `<namespace>.<tool>`."""
    names: list[str] = []
    _walk_tools(names, body)
    return names


def probe_tools(runtime: Runtime, backend: str, model: str | None = None) -> list[str] | None:
    """The tools the CLI offers a model with the exact run flags, read off a fake model
    endpoint on loopback; None when it never asked."""
    cfg = runtime.cfg
    capture = HTTPServer(("127.0.0.1", 0), _Capture)
    capture.names = None  # type: ignore[attr-defined]
    threading.Thread(target=capture.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{capture.server_address[1]}"
    work = Path(tempfile.mkdtemp(prefix="probe-", dir=cfg.tmp))
    try:
        directory = work / backend
        runner.private_dir(directory)
        runner.private_dir(directory / "home")
        runner.private_dir(work / "tmp")
        req = runner.RunRequest(
            "probe", backend, "Answer in one word.", "hi", None, None, [], model, 1, 60, None
        )
        runner.write_work_files(cfg, req, work, runtime.catalog.path)
        if backend == "claude":
            args = runner.claude_args(cfg, req, work)
            extra = {"ANTHROPIC_BASE_URL": base, "ANTHROPIC_API_KEY": "probe"}
        else:
            provider = (
                f'model_providers.probe={{name="probe",base_url="{base}/v1",'
                'env_key="AGENT_PROBE_KEY",wire_api="responses"}'
            )
            args = runner.codex_args(cfg, req, work, runtime.catalog.path)
            args = [*args[:-1], "-c", provider, "-c", 'model_provider="probe"', "-"]
            extra = {"AGENT_PROBE_KEY": "probe"}
        env = runner.child_env(cfg, directory, backend, extra, tmp=work / "tmp")
        args, env = runner.sandboxed(cfg, args, env, directory, work, cwd=work)
        try:
            runner.run_tracked(
                args,
                timeout=90,
                input=b"hi",
                cwd=work,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        names: list[str] | None = capture.names  # type: ignore[attr-defined]
        return names
    finally:
        capture.shutdown()
        capture.server_close()
        shutil.rmtree(work, ignore_errors=True)


def _check_backend(runtime: Runtime, backend: str, health: Health) -> None:
    cfg = runtime.cfg
    if shutil.which(cfg.bins[backend], path=cfg.path) is None:
        health.safe[backend] = False
        return
    # Codex: its default model and every catalog model, with the cleaned catalog.
    models: list[str | None] = [None]
    if backend == "codex":
        if not runtime.catalog.ensure():
            health.found[backend] = ["<no model catalog>"]
            health.safe[backend] = False
            log.error("self-check: codex gave no model catalog")
            return
        models += [*sorted(runtime.catalog.models), UNKNOWN_PROBE_MODEL]
    unexpected: list[str] = []
    for model in models:
        names = probe_tools(runtime, backend, model)
        found = {"<no answer>"} if names is None else set(names)
        extra = sorted(found - EXPECTED_BUILTINS[backend])
        unexpected += [f"{model or 'default'}: {name}" for name in extra]
    health.found[backend] = unexpected
    health.safe[backend] = not unexpected
    if unexpected:
        log.error("self-check: %s offers unexpected tools %s", backend, unexpected)
    else:
        log.info("self-check: %s offers only the expected tools", backend)


def self_check(runtime: Runtime, health: Health) -> None:
    """Probe every backend; one that cannot be probed counts as unsafe."""
    try:
        for backend in BACKENDS:
            try:
                _check_backend(runtime, backend, health)
            except Exception:
                health.safe[backend] = False
                log.exception("self-check of %s failed", backend)
    finally:
        health.checking = False
