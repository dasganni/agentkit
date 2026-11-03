"""Conversation titles and history summaries from a small chat model.

Prompts and replies are never logged; a failed call falls back to plain text.
"""

import logging
from collections.abc import Sequence
from typing import Any, Protocol

from agentkit.history import COMPACT_PROMPT, Message

log = logging.getLogger(__name__)

TITLE_CHARS = 60
TITLE_PROMPT = (
    "Write a title of at most six words for the conversation that starts with the message below. "
    "Use the message's language. Answer with the title only, no quotes.\n\n"
)


class ChatModel(Protocol):
    async def ainvoke(self, input: str) -> Any: ...


def fallback_title(message: str) -> str:
    """The message's first words, cut at a word boundary within 60 characters."""
    text = " ".join(message.split())
    if len(text) <= TITLE_CHARS:
        return text
    head = text[: TITLE_CHARS + 1]
    return head.rsplit(" ", 1)[0] if " " in head else head[:TITLE_CHARS]


async def make_title(model: ChatModel, message: str) -> str:
    """A short title for the first message; its first words when the call fails."""
    try:
        reply = await model.ainvoke(TITLE_PROMPT + message[: TITLE_CHARS * 20])
        title = str(reply.text).strip().strip("\"' \n")
    except Exception as error:
        log.warning("title call failed: %s", type(error).__name__)
        return fallback_title(message)
    return title[:TITLE_CHARS] or fallback_title(message)


async def summarise(model: ChatModel, messages: Sequence[Message]) -> str | None:
    """A summary of older turns; None when the call fails or is empty."""
    transcript = "\n\n".join(f"{m['role']}: {m['text']}" for m in messages)
    try:
        reply = await model.ainvoke(COMPACT_PROMPT + transcript)
        return str(reply.text).strip() or None
    except Exception as error:
        log.warning("summary call failed: %s", type(error).__name__)
        return None
