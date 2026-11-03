"""Check skill documents against a tool schema list and scan files for token-looking strings."""

import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

SKILL_FILES = ("SKILL.md", "reference.md")
BUILTIN_COMMANDS = frozenset({"login", "logout", "status", "token", "version", "tools", "help"})
# Flags every tool command takes: Run strips --json, the flag set answers --help.
TOOL_FLAGS = frozenset({"json", "help"})
_SHELL_BREAKS = frozenset({"|", "||", "&&", ";", ">", ">>", "<", "&"})
_FLAG = re.compile(r"--([A-Za-z0-9_-]+)")
_SPAN = re.compile(r"`([^`\n]+)`")
_TOKEN = re.compile(r"\b(?:tt|rw|fit|tto)_[A-Za-z0-9_-]{20,}|\bBearer\s+[A-Za-z0-9._~+/=-]{20,}")


def _props(schema: Any) -> set[str]:
    if isinstance(schema, dict):
        return set(schema.get("properties") or {})
    return set()


def _until_shell_break(words: list[str]) -> list[str]:
    """The words of the first command; a pipe, redirect or `&&` ends it."""
    for index, word in enumerate(words):
        if word in _SHELL_BREAKS or word.startswith("|"):
            return words[:index]
    return words


def _commands(text: str, app: str) -> Iterator[tuple[int, list[str]]]:
    """Yield (line number, words) for each `<app> ...` command in spans and fenced blocks."""
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        candidates = [line.strip()] if fenced else _SPAN.findall(line)
        for candidate in candidates:
            words = _until_shell_break(candidate.removeprefix("$ ").split())
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
    ops: list[str] = tool.get("ops") or []
    allowed = _props(tool.get("params")) | TOOL_FLAGS
    after = rest[1:]
    problem = _op_problem(app, rest[0], ops, after) if ops and after else None
    if problem:
        yield problem
        if problem.startswith("unknown operation"):
            return
    for word in after:
        match = _FLAG.match(word)
        if match and match.group(1) not in allowed:
            yield f"unknown flag '--{match.group(1)}' for {app} {rest[0]}"


def _op_problem(app: str, tool: str, ops: list[str], after: list[str]) -> str | None:
    """What the CLI's op parsing rejects: no op before the flags, or an op the tool lacks."""
    first = after[0]
    if first.startswith("-"):
        if {"--help", "-h"} & set(after):
            return None
        return f"missing op for {app} {tool}, expected one of: {', '.join(ops)}"
    if first.startswith(("<", "[")) or first in ops:
        return None
    return f"unknown operation '{first}' for {app} {tool}"


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
