"""Effort levels shared by every provider."""

from collections.abc import Sequence
from typing import Literal

Effort = Literal["auto", "minimal", "low", "medium", "high", "xhigh", "max"]

LEVELS: tuple[Effort, ...] = ("minimal", "low", "medium", "high", "xhigh", "max")


def nearest(wanted: Effort, supported: Sequence[str]) -> Effort | None:
    """The supported level closest to `wanted`, ties downward; "auto" passes through."""
    if wanted == "auto":
        return "auto"
    target = LEVELS.index(wanted)
    known = [level for level in LEVELS if level in supported]
    if not known:
        return None
    return min(known, key=lambda level: (abs(LEVELS.index(level) - target), LEVELS.index(level)))
