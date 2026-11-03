from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from pydantic import SecretStr

from agentkit.catalog import FALLBACK, ModelInfo, cost, list_models


@dataclass
class Provider:
    kind: str
    api_key: SecretStr | None = None
    base_url: str | None = None


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


def _transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_anthropic_list_parses_capabilities() -> None:
    pages = {
        None: {
            "data": [
                {
                    "id": "claude-opus-5-5",
                    "display_name": "Opus",
                    "max_input_tokens": 500000,
                    "capabilities": {
                        "effort": {
                            "supported": True,
                            "low": {"supported": True},
                            "high": {"supported": True},
                            "max": {"supported": False},
                        },
                        "thinking": {"supported": True},
                    },
                }
            ],
            "has_more": True,
            "last_id": "claude-opus-5-5",
        },
        "claude-opus-5-5": {
            "data": [{"id": "claude-x", "capabilities": {"thinking": {"supported": False}}}],
            "has_more": False,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "k"
        return httpx.Response(200, json=pages[request.url.params.get("after_id")])

    models = await list_models(Provider("anthropic", SecretStr("k")), transport=_transport(handler))
    opus, other = models
    assert opus.efforts == ("low", "high")
    assert opus.thinking is True
    assert opus.context == 500000
    assert opus.input_per_mtok == Decimal("4.00")  # price merged from FALLBACK
    assert other.efforts == () and other.thinking is False


async def test_openrouter_list_parses_efforts() -> None:
    body = {
        "data": [
            {
                "id": "vendor/model",
                "name": "Model",
                "context_length": 128000,
                "pricing": {"prompt": "0.000001", "completion": "0.000004"},
                "reasoning": {"supported_efforts": ["low", "high"]},
            },
            {"id": "vendor/plain"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://openrouter.ai/api/v1/models"
        return httpx.Response(200, json=body)

    models = await list_models(
        Provider("openai", SecretStr("k"), "https://openrouter.ai/api/v1"),
        transport=_transport(handler),
    )
    assert models[0].efforts == ("low", "high")
    assert models[0].input_per_mtok == Decimal("1")
    assert models[0].output_per_mtok == Decimal("4")
    assert models[1].efforts == ()


async def test_list_falls_back_on_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    models = await list_models(Provider("anthropic", SecretStr("k")), transport=_transport(handler))
    assert models == FALLBACK["anthropic"]


async def test_sidecar_list_and_fallback() -> None:
    class Sidecar:
        async def models(self, kind: str) -> list[dict[str, Any]]:
            return [{"id": "opus", "efforts": ["low", "max"], "thinking": True}]

    class Broken:
        async def models(self, kind: str) -> list[dict[str, Any]]:
            raise OSError("socket")

    live = await list_models(Provider("claude"), sidecar=Sidecar())
    assert live[0].efforts == ("low", "max")
    assert await list_models(Provider("claude"), sidecar=Broken()) == FALLBACK["claude"]


def test_fallback_has_prices_for_claude_api_models() -> None:
    priced = [m for m in FALLBACK["anthropic"] if m.id != "claude-fable-5-1"]
    assert len(priced) == 3
    assert all(m.input_per_mtok and m.cache_write_per_mtok for m in priced)


def test_cost_counts_cache_tokens() -> None:
    model = ModelInfo(
        "m",
        "M",
        "anthropic",
        input_per_mtok=Decimal(2),
        output_per_mtok=Decimal(10),
        cache_read_per_mtok=Decimal("0.2"),
        cache_write_per_mtok=Decimal("2.5"),
    )
    usage = Usage(1_000_000, 100_000, 500_000, 200_000)
    assert cost(model, usage) == Decimal("2") + Decimal("1") + Decimal("0.1") + Decimal("0.5")
    assert cost(None, usage) is None
    assert cost(ModelInfo("m", "M", "openai"), usage) is None
