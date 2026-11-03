"""HTTP server of the sidecar on a unix socket, guarded by the shared secret.

Endpoints: POST /run (NDJSON events), POST /login/start, POST /login/code, GET /login/status,
POST /logout, POST /prune {keep: [user keys]}, GET /models?backend=, GET /healthz (also /health).
Standard library only.
The socket lives on a volume shared with the app and nothing else; every request must carry the
secret in `X-Agent-Secret`.

Environment:
  AGENT_SOCKET            socket path (default /run/agent/agent.sock); the secret file
                          `agent.secret` is written next to it when AGENT_SECRET is not set
  AGENT_SECRET            shared secret (default: a fresh one at each start)
  AGENT_HOME              directory of the CLI logins (default /home/agent); a request with a
                          user key uses <home>/users/<key>/<backend>, one without <home>/<backend>
  AGENT_MCP_NAME          name the CLIs see the app MCP server under; Claude Code tools become
                          mcp__<name>__<tool> (default app)
  AGENT_CLAUDE_BIN        Claude Code executable (default claude)
  AGENT_CODEX_BIN         Codex executable (default codex)
  AGENT_CLAUDE_MODELS     comma list of model names /models reports for Claude Code (default none:
                          the CLI checks the name itself)
  AGENT_MAX_RUNS          runs at once over all users (default 3)
  AGENT_MAX_PER_USER      runs at once per user (default 1)
  AGENT_MAX_THREADS       open connections at once (default 32)
  AGENT_REQUIRE_SANDBOX   0 accepts a kernel without Landlock ABI 3 (default 1: refuse to run)
  AGENT_USERNS            bubblewrap layer on top of Landlock: auto, on or off (default auto)
  AGENT_SELF_CHECK        0 skips the start-up tool self-check (default 1: a backend whose CLI
                          offers unexpected tools is refused as unsafe)
  AGENT_BWRAP             bubblewrap executable (default bwrap)
  AGENT_RO_PATHS          more read-only paths for the CLIs, colon separated
  PATH, HTTPS_PROXY, HTTP_PROXY, NO_PROXY, SSL_CERT_FILE, NODE_EXTRA_CA_CERTS, TZ
                          the only variables of the sidecar environment a CLI receives
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import select
import signal
import socket
import socketserver
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from agentkit.sidecar import login, runner
from agentkit.sidecar.runner import BadRequest, Busy, Config, Runtime
from agentkit.sidecar.selfcheck import Health, self_check

log = logging.getLogger("agent-runner")
MAX_BODY = 1_000_000


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, cfg: Config) -> None:
        self.cfg = cfg
        self.runtime = Runtime(cfg)
        self.logins = login.Logins(cfg)
        self.health = Health(cfg)
        self.runtime.refusal = self.health.refusal
        if cfg.self_check:
            threading.Thread(
                target=self_check, args=(self.runtime, self.health), daemon=True
            ).start()
        self.threads = threading.BoundedSemaphore(cfg.max_threads)
        if os.path.exists(path):
            os.unlink(path)
        old = os.umask(0o117)
        try:
            super().__init__(path, Handler)
        finally:
            os.umask(old)

    def process_request(self, request: Any, client_address: Any) -> None:
        # At most max_threads requests at once; beyond that the connection is answered busy at once.
        if not self.threads.acquire(blocking=False):
            try:
                body = b'{"error": "busy"}'
                request.sendall(
                    b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode()
                    + body
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.threads.release()


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.0"
    # An idle or slow connection is dropped, so it cannot hold a request thread.
    timeout = 10

    def address_string(self) -> str:
        return "unix"

    def log_message(self, format: str, *args: Any) -> None:
        pass  # request lines name users; the sidecar logs run ids only

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self) -> bool:
        secret = self.server.cfg.secret
        given = self.headers.get("X-Agent-Secret", "").encode("latin-1", "replace")
        if not secret or not hmac.compare_digest(given, secret.encode()):
            self._json(403, {"error": "forbidden"})
            return False
        return True

    def _body(self) -> dict[str, Any]:
        raw = (self.headers.get("Content-Length") or "0").strip()
        if not (raw.isascii() and raw.isdigit()):
            raise BadRequest("invalid Content-Length")
        length = int(raw)
        if length > MAX_BODY:
            raise BadRequest("body too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError as exc:
            raise BadRequest("invalid JSON") from exc
        if not isinstance(body, dict):
            raise BadRequest("invalid JSON")
        return body

    def _health(self) -> dict[str, Any]:
        cfg, runtime = self.server.cfg, self.server.runtime
        backends = {b: runtime.installed(b) for b in runner.BACKENDS}
        return {
            "ok": True,
            "backends": backends,
            "installed": backends["claude"],
            "sandbox": runner.sandbox_state(cfg),
            "sandbox_problem": runner.sandbox_problem(cfg),
            "sandbox_required": cfg.require_sandbox,
            **self.server.health.report(),
        }

    def do_GET(self) -> None:
        if not self._authorised():
            return
        url = urlsplit(self.path)
        try:
            if url.path in ("/healthz", "/health"):
                self._json(200, self._health())
            elif url.path == "/login/status":
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                self._json(
                    200,
                    self.server.logins.aux.status(
                        runner.check_user(query.get("user")),
                        runner.check_backend(query.get("backend") or "claude"),
                    ),
                )
            elif url.path == "/models":
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                backend = runner.check_backend(query.get("backend") or "claude")
                self._json(200, {"models": self.server.runtime.models(backend)})
            else:
                self._json(404, {"error": "not found"})
        except BadRequest as exc:
            self._json(400, {"error": str(exc)})
        except Busy:
            self._json(503, {"error": "busy"})

    def do_POST(self) -> None:
        if not self._authorised():
            return
        try:
            body = self._body()
            path = urlsplit(self.path).path
            if path == "/run":
                req = runner.RunRequest.parse(body)
                self.server.runtime.validate(req)
                self._run(req)
            elif path == "/prune":
                self._json(
                    200,
                    {"removed": login.prune(self.server.cfg, self.server.logins, body.get("keep"))},
                )
            elif path in ("/login/start", "/login/code", "/logout"):
                self._login(path, body)
            else:
                self._json(404, {"error": "not found"})
        except BadRequest as exc:
            self._json(400, {"error": str(exc)})
        except Busy:
            self._json(503, {"error": "busy"})

    def _login(self, path: str, body: dict[str, Any]) -> None:
        logins = self.server.logins
        user, backend = body.get("user"), body.get("backend")
        if path == "/login/start":
            self._json(200, logins.start(user, backend))
        elif path == "/login/code":
            code = body.get("code")
            if not isinstance(code, str):
                raise BadRequest("invalid code")
            self._json(200, {"connected": logins.code(user, backend, code) == 0})
        else:
            login.logout(self.server.cfg, logins, user, backend)
            self._json(200, {"ok": True})

    def _disconnected(self) -> bool:
        conn: socket.socket = self.connection
        try:
            ready, _, _ = select.select([conn], [], [], 0)
            return bool(ready) and conn.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def _run(self, req: runner.RunRequest) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        gone = threading.Event()

        def disconnected() -> bool:
            return gone.is_set() or self._disconnected()

        for event in self.server.runtime.run(req, disconnected):
            if gone.is_set():
                continue
            try:
                self.wfile.write(json.dumps(event, ensure_ascii=False).encode() + b"\n")
                self.wfile.flush()
            except OSError:
                gone.set()


def write_secret(secret_file: Path, secret: str) -> None:
    """Replace the secret file atomically: a new file (mode 0600, no symlink followed) is written
    next to it and renamed over it, so a reader never sees a partial or world-readable secret."""
    tmp = secret_file.with_name(f"{secret_file.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(secret)
        os.replace(tmp, secret_file)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def serve() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    runner.private_dir(cfg.root)
    if not cfg.secret:
        # No AGENT_SECRET given: make one for this start and leave it next to the socket, where
        # the app (and only the app, which shares the volume) reads it.
        cfg = replace(cfg, secret=secrets.token_urlsafe(32))
        write_secret(Path(cfg.socket).with_name("agent.secret"), cfg.secret)
    runner.become_subreaper()
    with Server(cfg.socket, cfg) as server:
        if (
            not cfg.self_check
            and runner.sandbox_problem(cfg) is None
            and server.runtime.installed("codex")
        ):
            threading.Thread(target=server.runtime.catalog.ensure, daemon=True).start()
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
        log.info("agent sidecar listening")
        server.serve_forever()
    return 0
