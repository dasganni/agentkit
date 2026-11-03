from types import SimpleNamespace
from typing import Any

from agentkit.titles import fallback_title, make_title, summarise


class FakeModel:
    def __init__(self, text: str = "", fail: bool = False) -> None:
        self.text, self.fail, self.prompts = text, fail, []

    async def ainvoke(self, input: str) -> Any:
        self.prompts.append(input)
        if self.fail:
            raise RuntimeError("boom")
        return SimpleNamespace(text=self.text)


async def test_make_title_uses_given_model() -> None:
    model = FakeModel('  "Budget plan"\n')
    assert await make_title(model, "Help me plan my budget") == "Budget plan"
    assert "Help me plan my budget" in model.prompts[0]


async def test_make_title_falls_back() -> None:
    assert await make_title(FakeModel(fail=True), "hello   world") == "hello world"
    assert fallback_title("word " * 30).endswith("word")
    assert len(fallback_title("word " * 30)) <= 60


async def test_summarise() -> None:
    model = FakeModel("short")
    assert await summarise(model, [{"role": "user", "text": "hi"}]) == "short"
    assert "user: hi" in model.prompts[0]
    assert await summarise(FakeModel(fail=True), []) is None
