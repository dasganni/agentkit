from agentkit.history import estimate_tokens, should_compact, trim_to_budget


def msg(n: int) -> dict[str, str]:
    return {"role": "user", "text": "x" * (n * 4)}


def test_estimate_tokens() -> None:
    assert estimate_tokens("x" * 40) == 10


def test_trim_keeps_newest_within_budget() -> None:
    a, b, c = msg(10), msg(10), msg(10)
    assert trim_to_budget([a, b, c], 25) == [b, c]
    assert trim_to_budget([a], 5) == []


def test_should_compact_over_limit() -> None:
    assert should_compact([msg(60), msg(60)], limit=100)
    assert not should_compact([msg(50), msg(50)], limit=100)
