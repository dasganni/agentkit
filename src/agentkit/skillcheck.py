"""Check skill documents against a tool schema list and scan files for token-looking strings."""

import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

SKILL_FILES = ("SKILL.md", "reference.md")
BUILTIN_COMMANDS = frozenset({"login", "logout", "status", "token", "version", "tools", "help"})
GLOBAL_FLAGS = frozenset({"json", "help", "url", "token-stdin"})
_FLAG = re.compile(r"--([A-Za-z0-9_-]+)")
_SPAN = re.compile(r"`([^`\n]+)`")
_TOKEN = re.compile(r"\b(?:tt|rw|fit|tto)_[A-Za-z0-9_-]{20,}|\bBearer\s+[A-Za-z0-9._~+/=-]{20,}")


def _props(schema: Any) -> set[str]:
    if isinstance(schema, dict):
        return set(schema.get("properties") or {})
    return set()


def _ops(tool: dict[str, Any]) -> dict[str, set[str]]:
    """Operation name -> flags that operation accepts (its own params)."""
    ops: dict[str, set[str]] = {}
    for op in tool.get("ops") or []:
        if isinstance(op, str):
            ops[op] = set()
        else:
            ops[op["name"]] = _props(op.get("params"))
    return ops


def _commands(text: str, app: str) -> Iterator[tuple[int, list[str]]]:
    """Yield (line number, words) for each `<app> ...` command in spans and fenced blocks."""
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        candidates = [line.strip()] if fenced else _SPAN.findall(line)
        for candidate in candidates:
            words = candidate.split()
            if words and words[0] == app:
                yield number, words


def _check_command(words: list[str], tools: dict[str, dict[str, Any]]) -> Iterator[str]:
    app, rest = words[0], words[1:]
    if not rest or rest[0].startswith(("-", "<", "[")) or rest[0] in BUILTIN_COMMANDS:
        return
    tool = tools.get(rest[0])
    if tool is None:
        yield f"unknown tool '{rest[0]}'"
        return
    ops = _ops(tool)
    allowed = _props(tool.get("params")) | GLOBAL_FLAGS
    after = rest[1:]
    if after and not after[0].startswith(("-", "<", "[")) and ops:
        if after[0] not in ops:
            yield f"unknown operation '{after[0]}' for {app} {rest[0]}"
            return
        allowed |= ops[after[0]]
    elif ops:
        allowed |= set().union(*ops.values())
    for word in after:
        match = _FLAG.match(word)
        if match and match.group(1) not in allowed:
            yield f"unknown flag '--{match.group(1)}' for {app} {rest[0]}"


def check_skills(skill_dirs: Iterable[Path], schemas: list[dict[str, Any]], app: str) -> list[str]:
    """List every `<app> <tool> [<op>] --<flag>` in SKILL.md/reference.md that the schema lacks."""
    tools = {t["name"]: t for t in schemas}
    problems: list[str] = []
    for skill_dir in skill_dirs:
        for name in SKILL_FILES:
            path = Path(skill_dir) / name
            if not path.is_file():
                continue
            for number, words in _commands(path.read_text(), app):
                problems.extend(f"{path}:{number}: {p}" for p in _check_command(words, tools))
    return problems


def scan_tokens(path: Path) -> list[str]:
    """Token-looking strings (tt_, rw_, fit_, tto_ prefixes, Bearer values) in a file or tree."""
    files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    found: list[str] = []
    for file in files:
        try:
            text = file.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        found.extend(_TOKEN.findall(text))
    return found
