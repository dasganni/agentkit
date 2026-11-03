"""Runs that outlive the connection that started them.

A run is an asyncio task that appends every event to a buffer; any number of subscribers read that
buffer from an offset. A finished run stays registered for `keep_seconds` so a late client still
receives the end. One process, so the registry is plain memory.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Hashable
from typing import Any

log = logging.getLogger(__name__)

KEEP_SECONDS = 60.0

Event = Any
EventSource = Callable[[], AsyncIterator[Event]]


class Run:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.finished = False
        self.settled = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self._waiters: list[asyncio.Future[None]] = []

    def _wake(self) -> None:
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)
        self._waiters.clear()

    async def drive(self, source: EventSource, error_event: Event = None) -> None:
        try:
            async for event in source():
                self.events.append(event)
                self._wake()
        except Exception:
            if error_event is None:
                raise
            self.events.append(error_event)
            self._wake()
            raise

    def settle(self) -> None:
        self.finished = True
        self.settled.set()
        self._wake()

    async def subscribe(self, start: int = 0) -> AsyncGenerator[Event]:
        """The buffered events from `start`, then live ones until the run has ended."""
        index = start
        while True:
            while index < len(self.events):
                yield self.events[index]
                index += 1
            if self.finished:
                return
            waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            try:
                await waiter
            finally:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)


class RunRegistry:
    def __init__(self, keep_seconds: float = KEEP_SECONDS) -> None:
        self.keep_seconds = keep_seconds
        self._runs: dict[Hashable, Run] = {}

    def get(self, key: Hashable) -> Run | None:
        return self._runs.get(key)

    def active(self, key: Hashable) -> bool:
        run = self._runs.get(key)
        return run is not None and not run.finished

    def start(self, key: Hashable, source: EventSource, error_event: Event = None) -> Run:
        """Register and start a run; the caller has checked that none is active for `key`.

        When the source raises, `error_event` (if given) is appended so attached clients see it.
        """
        run = Run()
        self._runs[key] = run
        run.task = asyncio.get_running_loop().create_task(run.drive(source, error_event))
        run.task.add_done_callback(lambda task: self._ended(key, run, task))
        return run

    def _ended(self, key: Hashable, run: Run, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            log.error("run %s failed: %s", key, type(task.exception()).__name__)
        run.settle()
        asyncio.get_running_loop().call_later(self.keep_seconds, self._drop, key, run)

    def _drop(self, key: Hashable, run: Run) -> None:
        if self._runs.get(key) is run:
            del self._runs[key]

    def attach(self, key: Hashable, start: int = 0) -> AsyncGenerator[Event]:
        """The events of the run for `key` from `start`; empty when there is none."""
        run = self._runs.get(key)
        return run.subscribe(start) if run is not None else _empty()

    async def cancel(self, key: Hashable) -> bool:
        """Cancel an active run and wait until it has ended; False when none was active."""
        run = self._runs.get(key)
        if run is None or run.finished or run.task is None:
            return False
        run.task.cancel()
        await run.settled.wait()
        return True

    async def cancel_all(self) -> None:
        await asyncio.gather(*(self.cancel(key) for key in list(self._runs)))


async def _empty() -> AsyncGenerator[Event]:
    return
    yield
