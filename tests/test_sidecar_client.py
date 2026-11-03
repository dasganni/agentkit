import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from agentkit.sidecar_client import SidecarBusy, SidecarClient, SidecarRefused, SidecarUnavailable


class FakeSidecar:
    """A raw HTTP server on a unix socket; answers by (method, path)."""

    def __init__(self, path: Path) -> None:
        self.path = str(path)
        self.requests: list[dict[str, Any]] = []
        self.answers: dict[tuple[str, str], tuple[int, Any]] = {}
        self.run_lines: list[str] = []
        self.hang_after_lines = False
        self.closed = asyncio.Event()
        self.server: asyncio.Server | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._handle, self.path)

    async def stop(self) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        request_line, *header_lines = head.split("\r\n")[:-2]
        method, target, _ = request_line.split(" ")
        headers = {k.lower(): v for k, v in (h.split(": ", 1) for h in header_lines)}
        raw = await reader.readexactly(int(headers.get("content-length", "0")))
        path, _, query = target.partition("?")
        self.requests.append(
            {
                "method": method,
                "path": path,
                "query": query,
                "headers": headers,
                "body": json.loads(raw) if raw else None,
            }
        )
        if path == "/run":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\n")
            writer.write(b"Connection: close\r\n\r\n")
            for line in self.run_lines:
                writer.write(line.encode() + b"\n")
                await writer.drain()
            if self.hang_after_lines:
                await reader.read()  # returns when the client closes the connection
                self.closed.set()
        else:
            status, data = self.answers.get((method, path), (404, {"error": "not found"}))
            payload = json.dumps(data).encode()
            writer.write(f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n".encode())
            writer.write(f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode())
            writer.write(payload)
        await writer.drain()
        writer.close()


@pytest.fixture
async def sidecar(tmp_path: Path) -> AsyncIterator[FakeSidecar]:
    fake = FakeSidecar(tmp_path / "s.sock")
    await fake.start()
    yield fake
    await fake.stop()


def client(fake: FakeSidecar, secret: str = "s3") -> SidecarClient:
    return SidecarClient(fake.path, secret)


async def test_run_streams_ndjson_events(sidecar: FakeSidecar) -> None:
    sidecar.run_lines = [
        '{"type": "text", "text": "hi"}',
        "",
        "not json",
        '{"type": "final", "text": "hi"}',
    ]
    events = [e async for e in client(sidecar).run("claude", "hello", user="u1")]
    assert [e["type"] for e in events] == ["text", "final"]
    body = sidecar.requests[0]["body"]
    assert body["backend"] == "claude"
    assert body["user"] == "u1"
    assert body["messages"][-1] == {"role": "user", "text": "hello"}


async def test_secret_header_sent(sidecar: FakeSidecar) -> None:
    sidecar.answers[("GET", "/login/status")] = (200, {"connected": False})
    await client(sidecar, "topsecret").status("claude")
    assert sidecar.requests[0]["headers"]["x-agent-secret"] == "topsecret"


async def test_login_status_logout(sidecar: FakeSidecar) -> None:
    sidecar.answers[("POST", "/login/start")] = (200, {"url": "https://x", "code": "AB-1", "n": 1})
    sidecar.answers[("POST", "/login/code")] = (200, {"connected": True})
    sidecar.answers[("GET", "/login/status")] = (
        200,
        {"connected": True, "account_hint": "a@b", "token": "no"},
    )
    sidecar.answers[("POST", "/logout")] = (200, {"ok": True})
    c = client(sidecar)
    assert await c.login("codex", user="u") == {"url": "https://x", "code": "AB-1"}
    assert await c.login_code("claude", "123", user="u") == {"connected": True}
    assert await c.status("claude", user="u") == {"connected": True, "account_hint": "a@b"}
    await c.logout("claude", user="u")
    by_path = {r["path"]: r for r in sidecar.requests}
    assert by_path["/login/code"]["body"] == {"user": "u", "backend": "claude", "code": "123"}
    assert "backend=claude" in by_path["/login/status"]["query"]
    assert by_path["/logout"]["body"] == {"user": "u", "backend": "claude"}


async def test_error_mapping(sidecar: FakeSidecar) -> None:
    sidecar.answers[("POST", "/logout")] = (503, {"error": "busy"})
    sidecar.answers[("POST", "/login/start")] = (400, {"error": "bad user"})
    c = client(sidecar)
    with pytest.raises(SidecarBusy):
        await c.logout("claude")
    with pytest.raises(SidecarRefused, match="bad user"):
        await c.login("claude")
    with pytest.raises(SidecarUnavailable):
        await c.status("claude")
    with pytest.raises(SidecarUnavailable):
        await SidecarClient("/nonexistent.sock").status("claude")


async def test_models(sidecar: FakeSidecar) -> None:
    sidecar.answers[("GET", "/models")] = (200, {"models": ["a", "b"]})
    assert await client(sidecar).models("codex") == ["a", "b"]
    assert "backend=codex" in sidecar.requests[0]["query"]
    sidecar.answers[("GET", "/models")] = (200, {"models": None})
    assert await client(sidecar).models("codex") is None


async def test_effort_in_run_body(sidecar: FakeSidecar) -> None:
    sidecar.run_lines = ['{"type": "final"}']
    c = client(sidecar)
    _ = [e async for e in c.run("claude", "x", effort="high")]
    _ = [e async for e in c.run("claude", "x", effort="auto")]
    assert sidecar.requests[0]["body"]["effort"] == "high"
    assert "effort" not in sidecar.requests[1]["body"]


async def test_close_cancels(sidecar: FakeSidecar) -> None:
    sidecar.run_lines = ['{"type": "text", "text": "a"}']
    sidecar.hang_after_lines = True
    stream = client(sidecar).run("claude", "x")
    first = await anext(stream)
    assert first["type"] == "text"
    await stream.aclose()  # type: ignore[attr-defined]
    await asyncio.wait_for(sidecar.closed.wait(), 5)


async def test_status_surfaces_busy_and_unavailable(sidecar: FakeSidecar) -> None:
    sidecar.answers[("GET", "/login/status")] = (200, {"connected": False, "error": "busy"})
    with pytest.raises(SidecarBusy):
        await client(sidecar).status("claude", user="u")
    sidecar.answers[("GET", "/login/status")] = (200, {"connected": False, "error": "unavailable"})
    with pytest.raises(SidecarUnavailable):
        await client(sidecar).status("claude", user="u")


async def test_health_and_prune(sidecar: FakeSidecar) -> None:
    sidecar.answers[("GET", "/healthz")] = (200, {"ok": True, "safe": {"claude": True}})
    sidecar.answers[("POST", "/prune")] = (200, {"removed": 2})
    c = client(sidecar)
    assert (await c.health())["safe"] == {"claude": True}
    assert await c.prune(["k1", "k2"]) == 2
    assert sidecar.requests[-1]["body"] == {"keep": ["k1", "k2"]}
    sidecar.answers[("POST", "/prune")] = (400, {"error": "refusing to prune every user"})
    with pytest.raises(SidecarRefused):
        await c.prune([])


async def test_close_hangs_up_inside_a_cancelled_scope(sidecar: FakeSidecar) -> None:
    sidecar.run_lines = ['{"type": "text", "text": "a"}']
    sidecar.hang_after_lines = True
    seen: list[str] = []

    async def consume() -> None:
        with anyio.CancelScope() as scope:
            asyncio.get_running_loop().call_later(0.3, scope.cancel)
            async for event in client(sidecar).run("claude", "x"):
                seen.append(event["type"])  # the next read hangs until the scope is cancelled

    await consume()
    assert seen == ["text"]
    await asyncio.wait_for(sidecar.closed.wait(), 5)
