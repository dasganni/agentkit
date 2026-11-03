from agentkit.effort import nearest


def test_nearest_picks_closest_supported() -> None:
    assert nearest("high", ["low", "medium", "high"]) == "high"
    assert nearest("max", ["low", "medium", "high"]) == "high"
    assert nearest("minimal", ["medium", "high"]) == "medium"
    # equidistant: the lower level wins
    assert nearest("medium", ["low", "high"]) == "low"
    assert nearest("high", []) is None


def test_auto_passes_through() -> None:
    assert nearest("auto", []) == "auto"
    assert nearest("auto", ["low"]) == "auto"
