import json
from typing import Any

import httpx
import httpx2
import openai
from anthropic import Anthropic
from langchain_core.messages import AIMessage
from pydantic import SecretStr

from agentkit.catalog import ModelInfo
from agentkit.providers import (
    ProviderConfig,
    RunOptions,
    build_chat_model,
    usage_from_message,
    usage_from_sidecar,
)

ANTHROPIC_REPLY = {
    "id": "m1",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5-5",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 3, "output_tokens": 2},
}
OPENAI_CHAT_REPLY = {
    "id": "c1",
    "object": "chat.completion",
    "created": 1,
    "model": "m",
    "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}

INFO = ModelInfo("claude-sonnet-5-5", "S", "anthropic", ("low", "medium", "high"), thinking=True)
PLAIN = ModelInfo("claude-haiku", "H", "anthropic", ("low", "high"), thinking=False)


def _capture(
    reply: dict[str, Any], seen: list[dict[str, Any]], urls: list[str] | None = None
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if urls is not None:
            urls.append(request.url.path)
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _capture2(reply: dict[str, Any], seen: list[dict[str, Any]]) -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json=reply)

    return httpx2.Client(transport=httpx2.MockTransport(handler))


def _anthropic_body(info: ModelInfo, effort: str) -> dict[str, Any]:
    seen: list[dict[str, Any]] = []
    model = build_chat_model(
        ProviderConfig("anthropic", SecretStr("k")),
        RunOptions(info.id, effort=effort, max_tokens=1024),  # type: ignore[arg-type]
        model_info=info,
    )
    model._client = Anthropic(  # type: ignore[attr-defined]
        api_key="k", http_client=_capture2(ANTHROPIC_REPLY, seen)
    )
    model.invoke("hi")
    return seen[0]


def _openai_body(base_url: str, effort: str) -> tuple[dict[str, Any], str]:
    seen: list[dict[str, Any]] = []
    urls: list[str] = []
    model = build_chat_model(
        ProviderConfig("openai", SecretStr("k"), base_url),
        RunOptions("m", effort=effort),  # type: ignore[arg-type]
        model_info=ModelInfo("m", "M", "openai", ("low", "medium", "high")),
    )
    http = _capture(OPENAI_CHAT_REPLY, seen, urls)
    model.root_client = openai.OpenAI(  # type: ignore[attr-defined]
        api_key="k", base_url=base_url, http_client=http
    )
    model.client = model.root_client.chat.completions  # type: ignore[attr-defined]
    model.invoke("hi")
    return seen[0], urls[0]


def test_anthropic_effort_in_request() -> None:
    body = _anthropic_body(INFO, "high")
    assert body["output_config"] == {"effort": "high"}
    assert "temperature" not in body and "top_p" not in body


def test_auto_sends_no_effort() -> None:
    assert "output_config" not in _anthropic_body(INFO, "auto")


def test_unsupported_effort_uses_nearest() -> None:
    assert _anthropic_body(INFO, "max")["output_config"] == {"effort": "high"}


def test_thinking_only_when_model_has_it() -> None:
    assert _anthropic_body(INFO, "auto")["thinking"] == {
        "type": "adaptive",
        "display": "summarized",
    }
    assert "thinking" not in _anthropic_body(PLAIN, "auto")


def test_cache_control_set() -> None:
    assert _anthropic_body(INFO, "auto")["cache_control"] == {"type": "ephemeral"}


def test_openrouter_reasoning_effort() -> None:
    body, path = _openai_body("https://openrouter.ai/api/v1", "medium")
    assert path.endswith("/chat/completions")
    assert body["reasoning"] == {"effort": "medium"}
    assert "reasoning_effort" not in body


def test_openai_reasoning_effort() -> None:
    body, path = _openai_body("https://api.openai.com/v1", "medium")
    assert path.endswith("/chat/completions")
    assert body["reasoning_effort"] == "medium"
    assert "reasoning" not in body


def test_usage_from_message_cache() -> None:
    msg = AIMessage(
        content="x",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 7,
            "total_tokens": 107,
            "input_token_details": {"cache_read": 60, "cache_creation": 10},
        },
    )
    usage = usage_from_message(msg)
    assert (usage.input, usage.output, usage.cache_read, usage.cache_write) == (30, 7, 60, 10)


def test_usage_from_sidecar_claude_and_codex() -> None:
    claude = usage_from_sidecar({"type": "usage", "input_tokens": 5, "output_tokens": 6})
    assert (claude.input, claude.output, claude.cache_read, claude.cache_write) == (5, 6, 0, 0)
    codex = usage_from_sidecar(
        {"type": "usage", "input_tokens": 9, "output_tokens": 1, "cache_read_tokens": 4}
    )
    assert (codex.input, codex.cache_read) == (9, 4)
