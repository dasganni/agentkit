"""Generic agent tool routes: list and call tools, protocol header, token revocation."""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

PROTOCOL = 1
PROTOCOL_MIN = 1
PROTOCOL_HEADER = "Agent-Protocol"

log = logging.getLogger(__name__)


@dataclass
class Caller:
    user_id: str
    role: Literal["read", "write", "admin"]
    agent: str | None = None


@dataclass
class ToolResult:
    text: str
    images: list[dict[str, Any]] = field(default_factory=list)


class ToolError(Exception):
    """A tool failure the caller may see: HTTP status, message and the offending field."""

    def __init__(self, status: int, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.field = field


class ToolRegistry(Protocol):
    def schemas(self, caller: Caller) -> list[dict[str, Any]]: ...

    async def call(self, name: str, params: dict[str, Any], caller: Caller) -> ToolResult: ...


def fence(text: str, source: str) -> str:
    """Mark text from banks, documents or imports as data, never instructions.

    The wrapper matches the apps' MCP convention; `source` names the origin for callers.
    """
    del source
    return json.dumps({"untrusted_text": text}, ensure_ascii=False)


def _reply(status: int, body: Any, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        body, status_code=status, headers={PROTOCOL_HEADER: str(PROTOCOL)} | (headers or {})
    )


def _protocol_error(request: Request) -> JSONResponse | None:
    raw = request.headers.get(PROTOCOL_HEADER)
    if raw is None:
        return None
    try:
        wanted = int(raw)
    except ValueError:
        wanted = -1
    if PROTOCOL_MIN <= wanted <= PROTOCOL:
        return None
    return _reply(
        426,
        {
            "error": f"Agent protocol {raw} is not supported (server speaks "
            f"{PROTOCOL_MIN}-{PROTOCOL}). Update the CLI."
        },
    )


def agent_router(
    registry: ToolRegistry,
    authenticate: Callable[[Request], Awaitable[Caller]],
    revoke: Callable[[Request, Caller], Awaitable[None]],
    *,
    prefix: str = "/api/v1/agent",
    resource_metadata: Callable[[Request], str] | None = None,
) -> APIRouter:
    """Routes under `prefix`; `resource_metadata` supplies the URL for the 401 challenge."""
    router = APIRouter(prefix=prefix)

    async def guarded(
        request: Request, action: Callable[[Caller], Awaitable[Response]]
    ) -> Response:
        if (refused := _protocol_error(request)) is not None:
            return refused
        try:
            caller = await authenticate(request)
            return await action(caller)
        except HTTPException as exc:
            headers = {k: str(v) for k, v in (exc.headers or {}).items()}
            if exc.status_code == 401:
                challenge = "Bearer"
                if resource_metadata is not None:
                    challenge += f' resource_metadata="{resource_metadata(request)}"'
                headers["WWW-Authenticate"] = challenge
            return _reply(exc.status_code, {"error": str(exc.detail)}, headers)
        except ToolError as exc:
            body: dict[str, Any] = {"error": exc.message}
            if exc.field:
                body["field"] = exc.field
            return _reply(exc.status, body)
        except Exception:
            log.exception("agent API call failed")
            return _reply(500, {"error": "Internal error"})

    @router.get("/tools")
    async def list_tools(request: Request) -> Response:
        async def action(caller: Caller) -> Response:
            return _reply(200, registry.schemas(caller))

        return await guarded(request, action)

    @router.post("/tools/{name}")
    async def call_tool(name: str, request: Request) -> Response:
        async def action(caller: Caller) -> Response:
            try:
                params = await request.json()
            except ValueError:
                raise ToolError(400, "Body must be a JSON object") from None
            if not isinstance(params, dict):
                raise ToolError(400, "Body must be a JSON object")
            result = await registry.call(name, params, caller)
            return _reply(200, {"text": result.text, "images": result.images})

        return await guarded(request, action)

    @router.delete("/token")
    async def delete_token(request: Request) -> Response:
        async def action(caller: Caller) -> Response:
            await revoke(request, caller)
            return Response(status_code=204, headers={PROTOCOL_HEADER: str(PROTOCOL)})

        return await guarded(request, action)

    return router
