from pathlib import Path

import pytest

from agentkit.skillcheck import check_skills, scan_tokens

SCHEMAS = [
    {
        "name": "accounts",
        "ops": ["list", "add"],
        "params": {"properties": {"limit": {}, "account_id": {}, "dry_run": {"type": "boolean"}}},
    },
    {"name": "budget", "params": {"properties": {"month_start": {}}}},
]


def skill(tmp_path: Path, body: str) -> list[Path]:
    (tmp_path / "SKILL.md").write_text(body)
    return [tmp_path]


def test_skill_unknown_command_reported(tmp_path: Path) -> None:
    dirs = skill(tmp_path, "Run `demo accounts list` and `demo nothing` and `demo accounts bogus`.")
    problems = check_skills(dirs, SCHEMAS, "demo")
    assert len(problems) == 2
    assert "unknown tool 'nothing'" in problems[0]
    assert "unknown operation 'bogus'" in problems[1]


def test_skill_unknown_flag_reported(tmp_path: Path) -> None:
    dirs = skill(tmp_path, "`demo accounts list --limit 5 --json`\n`demo accounts list --nope`")
    problems = check_skills(dirs, SCHEMAS, "demo")
    assert len(problems) == 1
    assert "--nope" in problems[0] and ":2:" in problems[0]


def test_underscore_names_checked(tmp_path: Path) -> None:
    body = "```\ndemo budget --month_start 2025-01-01\ndemo budget --month-start x\n```\n"
    problems = check_skills(skill(tmp_path, body), SCHEMAS, "demo")
    assert len(problems) == 1
    assert "--month-start" in problems[0]
    ok = skill(tmp_path, "`demo accounts add --account_id 3`")
    assert check_skills(ok, SCHEMAS, "demo") == []


@pytest.mark.parametrize(
    "command, fragment",
    [
        ("demo accounts --limit 3", "missing op"),
        ("demo accounts list --url http://x", "unknown flag '--url'"),
        ("demo accounts list --token-stdin", "unknown flag '--token-stdin'"),
        ("demo accounts list | jq --raw-output .", None),
        ("$ demo accounts list --nope", "unknown flag '--nope'"),
        ("$ demo accounts list --limit 2 --json", None),
        ("demo accounts --help", None),
    ],
)
def test_matches_go_cli_parser(tmp_path: Path, command: str, fragment: str | None) -> None:
    problems = check_skills(skill(tmp_path, f"```\n{command}\n```\n"), SCHEMAS, "demo")
    if fragment is None:
        assert problems == []
    else:
        assert len(problems) == 1 and fragment in problems[0]


def test_inline_span_and_fenced_commands_checked(tmp_path: Path) -> None:
    body = "Try `demo accounts bogus` now.\n```sh\ndemo accounts nope\n```\n"
    problems = check_skills(skill(tmp_path, body), SCHEMAS, "demo")
    assert [("bogus" in p, "nope" in p) for p in problems] == [(True, False), (False, True)]


def test_positional_word_must_be_an_op(tmp_path: Path) -> None:
    problems = check_skills(skill(tmp_path, "`demo accounts frobnicate`"), SCHEMAS, "demo")
    assert len(problems) == 1 and "unknown operation 'frobnicate'" in problems[0]
    assert check_skills(skill(tmp_path, "`demo accounts add`"), SCHEMAS, "demo") == []


def test_boolean_flag_takes_no_value(tmp_path: Path) -> None:
    for value in ("true", "false"):
        body = f"`demo accounts add --dry_run {value}`"
        problems = check_skills(skill(tmp_path, body), SCHEMAS, "demo")
        assert len(problems) == 1 and "boolean flag '--dry_run' takes no value" in problems[0]
    ok = skill(
        tmp_path, "`demo accounts add --dry_run --limit 5`\n`demo accounts add --dry_run=true`"
    )
    assert check_skills(ok, SCHEMAS, "demo") == []


def test_token_scan(tmp_path: Path) -> None:
    clean = tmp_path / "clean.md"
    clean.write_text("Use tt_<token> or `Bearer <token>`; rw_short")
    assert scan_tokens(clean) == []
    dirty = tmp_path / "dirty.md"
    dirty.write_text("key tt_" + "a" * 24 + "\nAuthorization: Bearer " + "b" * 30)
    assert len(scan_tokens(dirty)) == 2
    assert len(scan_tokens(tmp_path)) == 2
