"""Background worker scheduler for periodic reliability jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from loguru import logger

TaskFn = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    name: str
    interval_seconds: float
    fn: TaskFn


class BackgroundScheduler:
    """Runs named periodic async tasks until shutdown."""

    def __init__(self):
        self._tasks: list[ScheduledTask] = []
        self._workers: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()

    def register(self, task: ScheduledTask) -> None:
        self._tasks.append(task)

    def start(self) -> None:
        if self._workers:
            return
        self._stop.clear()
        for task in self._tasks:
            self._workers.append(asyncio.create_task(self._run_task(task)))

    async def stop(self) -> None:
        self._stop.set()
        workers = tuple(self._workers)
        self._workers.clear()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    async def _run_task(self, task: ScheduledTask) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=task.interval_seconds)
                return
            except TimeoutError:
                pass
            try:
                await task.fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Scheduler task failed: name={} exc_type={}",
                    task.name,
                    type(exc).__name__,
                )
            continue
