"""Chat model construction per provider and usage normalisation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from agentkit.catalog import ModelInfo
from agentkit.effort import Effort, nearest

ProviderKind = Literal["anthropic", "openai", "claude", "codex"]

_TIMEOUT = 120
_CACHE_CONTROL = {"type": "ephemeral"}
_THINKING = {"type": "adaptive", "display": "summarized"}


@dataclass
class ProviderConfig:
    kind: ProviderKind
    api_key: SecretStr | None = None
    base_url: str | None = None


@dataclass
class RunOptions:
    model: str
    effort: Effort = "auto"
    max_tokens: int | None = None


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


def _effort(opts: RunOptions, model_info: ModelInfo | None) -> str | None:
    """The effort to send: None for auto, no model info, or no supported level."""
    if opts.effort == "auto" or model_info is None or not model_info.efforts:
        return None
    chosen = nearest(opts.effort, model_info.efforts)
    return None if chosen in (None, "auto") else chosen


def _is_openrouter(base_url: str | None) -> bool:
    return base_url is not None and "openrouter.ai" in base_url


def _anthropic(cfg: ProviderConfig, opts: RunOptions, info: ModelInfo | None) -> ChatAnthropic:
    kwargs: dict[str, Any] = {}
    effort = _effort(opts, info)
    if effort:
        kwargs["output_config"] = {"effort": effort}
    if info is not None and info.thinking:
        kwargs["thinking"] = dict(_THINKING)
    if opts.max_tokens:
        kwargs["max_tokens"] = opts.max_tokens
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    return ChatAnthropic(
        model_name=opts.model,
        api_key=cfg.api_key or SecretStr(""),
        timeout=_TIMEOUT,
        max_retries=1,
        model_kwargs={"cache_control": dict(_CACHE_CONTROL)},
        **kwargs,
    )


def _openai(cfg: ProviderConfig, opts: RunOptions, info: ModelInfo | None) -> ChatOpenAI:
    extra: dict[str, Any] = {}
    effort = _effort(opts, info)
    if effort and _is_openrouter(cfg.base_url):
        extra["extra_body"] = {"reasoning": {"effort": effort}}
    elif effort:
        extra["reasoning_effort"] = effort
    if opts.max_tokens:
        extra["max_tokens"] = opts.max_tokens
    return ChatOpenAI(
        model=opts.model,
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        timeout=_TIMEOUT,
        max_retries=1,
        **extra,
    )


def build_chat_model(
    cfg: ProviderConfig, opts: RunOptions, *, model_info: ModelInfo | None
) -> BaseChatModel:
    """Chat model for the API kinds; claude and codex run through the sidecar instead."""
    if cfg.kind == "anthropic":
        return _anthropic(cfg, opts, model_info)
    if cfg.kind == "openai":
        return _openai(cfg, opts, model_info)
    raise ValueError(f"{cfg.kind} runs through the sidecar, not a chat model")


def usage_from_message(msg: Any) -> Usage:
    """Usage of a LangChain message; input excludes cached tokens so cost() prices them once."""
    meta = getattr(msg, "usage_metadata", None) or {}
    details = meta.get("input_token_details") or {}
    read = int(details.get("cache_read") or 0)
    write = int(details.get("cache_creation") or 0)
    write += sum(int(v or 0) for k, v in details.items() if k.startswith("ephemeral_"))
    total_in = int(meta.get("input_tokens") or 0)
    return Usage(
        input=max(total_in - read - write, 0),
        output=int(meta.get("output_tokens") or 0),
        cache_read=read,
        cache_write=write,
    )


def usage_from_sidecar(event: dict[str, Any]) -> Usage:
    """Usage of a sidecar `usage` event; cache counts are optional."""
    return Usage(
        input=int(event.get("input_tokens") or 0),
        output=int(event.get("output_tokens") or 0),
        cache_read=int(event.get("cache_read_tokens") or 0),
        cache_write=int(event.get("cache_write_tokens") or 0),
    )
