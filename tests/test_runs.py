import asyncio
from collections.abc import AsyncIterator

from agentkit.runs import RunRegistry


async def collect(gen: AsyncIterator[object]) -> list[object]:
    return [e async for e in gen]


async def test_run_survives_detach_and_replays() -> None:
    reg = RunRegistry()
    gate = asyncio.Event()

    async def source() -> AsyncIterator[str]:
        yield "a"
        await gate.wait()
        yield "b"

    reg.start("k", source)
    await asyncio.sleep(0)
    first = reg.attach("k")
    assert await anext(first) == "a"
    await first.aclose()  # the client goes away
    gate.set()
    run = reg.get("k")
    assert run is not None
    await run.settled.wait()
    assert await collect(reg.attach("k")) == ["a", "b"]
    assert await collect(reg.attach("k", 1)) == ["b"]
    assert await collect(reg.attach("missing")) == []


async def test_run_dropped_after_keep() -> None:
    reg = RunRegistry(keep_seconds=0.01)

    async def source() -> AsyncIterator[str]:
        yield "a"

    run = reg.start("k", source)
    await run.settled.wait()
    assert reg.get("k") is run
    await asyncio.sleep(0.05)
    assert reg.get("k") is None


async def test_cancel_stops_run() -> None:
    reg = RunRegistry()

    async def source() -> AsyncIterator[str]:
        yield "a"
        await asyncio.Event().wait()
        yield "never"

    reg.start("k", source)
    await asyncio.sleep(0.01)
    assert reg.active("k")
    assert await reg.cancel("k")
    assert not reg.active("k")
    assert await collect(reg.attach("k")) == ["a"]
    assert not await reg.cancel("k")


async def test_failing_source_appends_error_event() -> None:
    reg = RunRegistry()

    async def source() -> AsyncIterator[str]:
        yield "a"
        raise RuntimeError("boom")

    run = reg.start("k", source, error_event="failed")
    await run.settled.wait()
    assert await collect(reg.attach("k")) == ["a", "failed"]
