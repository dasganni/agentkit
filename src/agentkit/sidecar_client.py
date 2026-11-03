"""Async client of the sidecar protocol over its unix socket.

Prompts, replies and tool payloads are never logged.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx

CALL_TIMEOUT = 40.0
# The sidecar's /login/code waits up to 20 s for the CLI, 30 s for a status slot and 30 s for the
# status call itself; the client waits longer than that sum.
LOGIN_CODE_TIMEOUT = 90.0


class SidecarUnavailable(Exception):
    """The sidecar is not running, not reachable or answered with an error."""


class SidecarBusy(SidecarUnavailable):
    """The sidecar runs but is at its limit right now."""


class SidecarRefused(Exception):
    """The sidecar rejected the request itself (HTTP 400)."""


class SidecarClient:
    """One sidecar: `kind` is the CLI ("claude" or "codex"), `user` the sidecar's per-user key."""

    def __init__(self, socket_path: str, secret: str = "") -> None:
        self.socket_path = socket_path
        self.headers = {"X-Agent-Secret": secret} if secret else {}

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=self.socket_path),
            base_url="http://agent",
            headers=self.headers,
            timeout=timeout,
        )

    async def run(
        self,
        kind: str,
        prompt: str,
        *,
        user: str = "",
        model: str | None = None,
        effort: str | None = None,
        system: str = "",
        messages: list[dict[str, Any]] | None = None,
        mcp_config: dict[str, Any] | None = None,
        allowed_tools: list[str] | None = None,
        json_schema: dict[str, Any] | None = None,
        max_turns: int = 30,
        timeout: int = 180,
    ) -> AsyncIterator[dict[str, Any]]:
        """The NDJSON events of one run. Closing the iterator closes the connection, which makes
        the sidecar kill the CLI. `prompt` is sent as the last user message."""
        body: dict[str, Any] = {
            "user": user,
            "backend": kind,
            "system": system,
            "messages": [*(messages or []), {"role": "user", "text": prompt}],
            "mcp": mcp_config,
            "allowed_tools": allowed_tools or [],
            "model": model or None,
            "max_turns": max_turns,
            "timeout_s": timeout,
        }
        if effort and effort != "auto":
            body["effort"] = effort
        if json_schema is not None:
            body["json_schema"] = json_schema
        client = self._client(timeout + 30)
        try:
            async with client.stream("POST", "/run", json=body) as res:
                if res.status_code != 200:
                    raise await _error(res)
                async for line in res.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        yield event
        except (httpx.TransportError, OSError) as error:
            raise SidecarUnavailable("the agent sidecar is not reachable") from error
        finally:
            # Shielded: a cancelled caller must still hang up, or the sidecar's CLI keeps running.
            with anyio.CancelScope(shield=True):
                await client.aclose()

    async def _call(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        timeout: float = CALL_TIMEOUT,
    ) -> dict[str, Any]:
        try:
            async with self._client(timeout) as client:
                if method == "GET":
                    res = await client.get(path, params=params)
                else:
                    res = await client.post(path, json=params)
                if res.status_code != 200:
                    raise await _error(res)
        except (httpx.TransportError, OSError) as error:
            raise SidecarUnavailable("the agent sidecar is not reachable") from error
        data = _json(res)
        if not isinstance(data, dict):
            raise SidecarUnavailable("the sidecar answered without an object")
        return data

    async def login(self, kind: str, *, user: str = "") -> dict[str, Any]:
        """{url, code?}: the vendor's sign-in page (for Codex the one-time code to enter there)."""
        data = await self._call("POST", "/login/start", {"user": user, "backend": kind})
        return {k: data[k] for k in ("url", "code") if isinstance(data.get(k), str)}

    async def login_code(self, kind: str, code: str, *, user: str = "") -> dict[str, Any]:
        """Passes the code from Anthropic's page to the waiting sign-in; {connected}."""
        data = await self._call(
            "POST",
            "/login/code",
            {"user": user, "backend": kind, "code": code},
            LOGIN_CODE_TIMEOUT,
        )
        return {"connected": bool(data.get("connected"))}

    async def status(self, kind: str, *, user: str = "") -> dict[str, Any]:
        """{connected, account_hint?}; never a token. Busy or unavailable raise."""
        data = await self._call("GET", "/login/status", {"user": user, "backend": kind})
        if data.get("error") == "busy":
            raise SidecarBusy("the agent sidecar is busy")
        if data.get("error"):
            raise SidecarUnavailable("the CLI status could not run")
        out: dict[str, Any] = {"connected": bool(data.get("connected"))}
        if isinstance(data.get("account_hint"), str):
            out["account_hint"] = data["account_hint"]
        return out

    async def logout(self, kind: str, *, user: str = "") -> None:
        """Signs the user out of the CLI and removes their files for it."""
        await self._call("POST", "/logout", {"user": user, "backend": kind})

    async def health(self) -> dict[str, Any]:
        """The sidecar's own report (backends, sandbox, self-check); never a secret."""
        return await self._call("GET", "/healthz", {}, 10.0)

    async def prune(self, keep: list[str]) -> int:
        """Removes the directories of every user key not in `keep`; how many went."""
        data = await self._call("POST", "/prune", {"keep": keep}, 120.0)
        removed = data.get("removed")
        return removed if isinstance(removed, int) else 0

    async def models(self, kind: str) -> list[str] | None:
        """The model names a run accepts; None when the sidecar does not restrict them."""
        data = await self._call("GET", "/models", {"backend": kind})
        names = data.get("models")
        if isinstance(names, list):
            return [str(n) for n in names]
        return None


def _json(res: httpx.Response) -> Any:
    try:
        return res.json()
    except ValueError:
        return {}


async def _error(res: httpx.Response) -> Exception:
    """The exception for a non-200 answer; reads the body of a streamed response first."""
    await res.aread()
    data = _json(res)
    code = str(data.get("error") or "") if isinstance(data, dict) else ""
    if res.status_code == 503 and code == "busy":
        return SidecarBusy("the agent sidecar is busy")
    if res.status_code == 400:
        return SidecarRefused(code or "refused")
    return SidecarUnavailable(f"the sidecar answered {res.status_code}")
