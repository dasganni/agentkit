from pathlib import Path

from agentkit.skillcheck import check_skills, scan_tokens

SCHEMAS = [
    {
        "name": "accounts",
        "ops": ["list", {"name": "add", "params": {"properties": {"account_id": {}}}}],
        "params": {"properties": {"limit": {}}},
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


def test_token_scan(tmp_path: Path) -> None:
    clean = tmp_path / "clean.md"
    clean.write_text("Use tt_<token> or `Bearer <token>`; rw_short")
    assert scan_tokens(clean) == []
    dirty = tmp_path / "dirty.md"
    dirty.write_text("key tt_" + "a" * 24 + "\nAuthorization: Bearer " + "b" * 30)
    assert len(scan_tokens(dirty)) == 2
    assert len(scan_tokens(tmp_path)) == 2
