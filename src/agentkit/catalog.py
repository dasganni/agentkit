"""Model catalog: effort levels, prices, live lists with a built-in fallback."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Protocol

import httpx

TIMEOUT = 5.0
ANTHROPIC_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
_CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_MTOK = Decimal(1_000_000)


class ProviderLike(Protocol):
    @property
    def kind(self) -> str: ...
    @property
    def api_key(self) -> Any: ...
    @property
    def base_url(self) -> str | None: ...


class UsageLike(Protocol):
    @property
    def input(self) -> int: ...
    @property
    def output(self) -> int: ...
    @property
    def cache_read(self) -> int: ...
    @property
    def cache_write(self) -> int: ...


class SidecarLike(Protocol):
    async def models(self, kind: str) -> list[str] | list[dict[str, Any]] | None: ...


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    provider: str
    efforts: tuple[str, ...] = ()
    thinking: bool = False
    input_per_mtok: Decimal | None = None
    output_per_mtok: Decimal | None = None
    cache_read_per_mtok: Decimal | None = None
    cache_write_per_mtok: Decimal | None = None
    context: int | None = None


def _api(id: str, label: str, price: tuple[str, str, str, str] | None) -> ModelInfo:
    inp, out, read, write = (Decimal(p) for p in price) if price else (None,) * 4
    return ModelInfo(
        id, label, "anthropic", _CLAUDE_EFFORTS, True, inp, out, read, write, context=1_000_000
    )


def _cli(id: str, label: str, provider: str, efforts: tuple[str, ...]) -> ModelInfo:
    return ModelInfo(id, label, provider, efforts, thinking=True)


FALLBACK: dict[str, list[ModelInfo]] = {
    "anthropic": [
        _api("claude-fable-5-1", "Claude Fable 5.1", ("10.00", "50.00", "0.25", "12.50")),
        _api("claude-fable-5", "Claude Fable 5", ("10.00", "50.00", "1.00", "12.50")),
        _api("claude-opus-5-5", "Claude Opus 5.5", ("4.00", "20.00", "0.20", "5.00")),
        _api("claude-sonnet-5-5", "Claude Sonnet 5.5", ("2.00", "10.00", "0.20", "2.50")),
        _api("claude-haiku-5-5", "Claude Haiku 5.5", ("0.10", "0.50", "0.01", "0.125")),
    ],
    "claude": [
        _cli(alias, f"Claude {alias.capitalize()}", "claude", _CLAUDE_EFFORTS)
        for alias in ("fable", "opus", "sonnet", "haiku")
    ],
    "codex": [_cli("gpt-5-codex", "GPT-5 Codex", "codex", ("low", "medium", "high", "xhigh"))],
    "openai": [],
}


def _prices_by_id(kind: str) -> dict[str, ModelInfo]:
    return {m.id: m for m in FALLBACK.get(kind, [])}


def _first(live: Decimal | None, base: Decimal | None) -> Decimal | None:
    return base if live is None else live


def _with_prices(info: ModelInfo, known: dict[str, ModelInfo]) -> ModelInfo:
    base = known.get(info.id)
    if base is None:
        return info
    return replace(
        info,
        input_per_mtok=_first(info.input_per_mtok, base.input_per_mtok),
        output_per_mtok=_first(info.output_per_mtok, base.output_per_mtok),
        cache_read_per_mtok=_first(info.cache_read_per_mtok, base.cache_read_per_mtok),
        cache_write_per_mtok=_first(info.cache_write_per_mtok, base.cache_write_per_mtok),
        context=info.context or base.context,
    )


def _supported_levels(effort: Any) -> tuple[str, ...]:
    """Levels from an effort capability: a list, or {level: {"supported": bool}}."""
    if isinstance(effort, list):
        return tuple(str(level) for level in effort)
    if isinstance(effort, dict):
        return tuple(
            level
            for level, v in effort.items()
            if isinstance(v, dict) and v.get("supported") is True
        )
    return ()


def _anthropic_model(item: dict[str, Any]) -> ModelInfo:
    caps = item.get("capabilities") or {}
    thinking = caps.get("thinking") or {}
    types = thinking.get("types") if isinstance(thinking, dict) else None
    adaptive = (types or {}).get("adaptive") or {}
    return ModelInfo(
        id=item["id"],
        label=item.get("display_name") or item["id"],
        provider="anthropic",
        efforts=_supported_levels(caps.get("effort")),
        thinking=adaptive.get("supported") is True,
        context=item.get("max_input_tokens"),
    )


async def _anthropic_list(client: httpx.AsyncClient, provider: ProviderLike) -> list[ModelInfo]:
    base = (provider.base_url or ANTHROPIC_URL).rstrip("/")
    key = provider.api_key.get_secret_value() if provider.api_key else ""
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    models: list[ModelInfo] = []
    params: dict[str, str | int] = {"limit": 1000}
    while True:
        resp = await client.get(f"{base}/v1/models", headers=headers, params=params)
        resp.raise_for_status()
        body = resp.json()
        models.extend(_anthropic_model(item) for item in body["data"])
        if not body.get("has_more") or not body.get("last_id"):
            return models
        params["after_id"] = body["last_id"]


def _per_mtok(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) * _MTOK if value not in (None, "") else None
    except ArithmeticError:
        return None


def _openrouter_model(item: dict[str, Any]) -> ModelInfo:
    reasoning = item.get("reasoning") or {}
    pricing = item.get("pricing") or {}
    return ModelInfo(
        id=item["id"],
        label=item.get("name") or item["id"],
        provider="openai",
        efforts=tuple(reasoning.get("supported_efforts") or ()),
        thinking=bool(reasoning),
        input_per_mtok=_per_mtok(pricing.get("prompt")),
        output_per_mtok=_per_mtok(pricing.get("completion")),
        cache_read_per_mtok=_per_mtok(pricing.get("input_cache_read")),
        cache_write_per_mtok=_per_mtok(pricing.get("input_cache_write")),
        context=item.get("context_length"),
    )


async def _openai_list(client: httpx.AsyncClient, provider: ProviderLike) -> list[ModelInfo]:
    if not provider.base_url:
        raise ValueError("base_url required")
    headers = {}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key.get_secret_value()}"
    resp = await client.get(f"{provider.base_url.rstrip('/')}/models", headers=headers)
    resp.raise_for_status()
    return [_openrouter_model(item) for item in resp.json()["data"]]


def _sidecar_model(item: str | dict[str, Any], kind: str, fallback: list[ModelInfo]) -> ModelInfo:
    """A sidecar entry: a bare name (known fallback entry or default efforts) or a full dict."""
    if isinstance(item, dict):
        return ModelInfo(
            id=item["id"],
            label=item.get("label") or item["id"],
            provider=kind,
            efforts=tuple(item.get("efforts") or ()),
            thinking=bool(item.get("thinking")),
        )
    known = next((m for m in fallback if m.id == item), None)
    efforts = fallback[0].efforts if fallback else ()
    return known or ModelInfo(item, item, kind, efforts, thinking=True)


async def _sidecar_list(kind: str, sidecar: SidecarLike) -> list[ModelInfo]:
    fallback = FALLBACK.get(kind, [])
    items = await sidecar.models(kind)
    return [_sidecar_model(item, kind, fallback) for item in items or []]


async def list_models(
    provider: ProviderLike,
    *,
    sidecar: SidecarLike | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[ModelInfo]:
    """Live model list merged with FALLBACK prices; FALLBACK on any error or empty list."""
    kind = provider.kind
    fallback = list(FALLBACK.get(kind, []))
    try:
        if kind in ("claude", "codex"):
            if sidecar is None:
                return fallback
            live = await _sidecar_list(kind, sidecar)
        else:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
                if kind == "anthropic":
                    live = await _anthropic_list(client, provider)
                else:
                    live = await _openai_list(client, provider)
    except Exception:
        return fallback
    known = _prices_by_id(kind)
    return [_with_prices(m, known) for m in live] or fallback


def cost(model: ModelInfo | None, usage: UsageLike) -> Decimal | None:
    """Dollar cost of the usage; None when the model or any needed price is unknown."""
    if model is None:
        return None
    parts = (
        (usage.input, model.input_per_mtok),
        (usage.output, model.output_per_mtok),
        (usage.cache_read, model.cache_read_per_mtok),
        (usage.cache_write, model.cache_write_per_mtok),
    )
    total = Decimal(0)
    for tokens, price in parts:
        if tokens == 0:
            continue
        if price is None:
            return None
        total += price * tokens
    return total / _MTOK
