"""Run synchronous database work off the event loop and propagate cancellation."""

import asyncio
from collections.abc import Callable
from functools import partial
from threading import Event

from chatbi.sql_agent.database.contracts import DatabaseAdapter
from chatbi.sql_agent.models import QueryResult, SQLQuery


class SQLExecutor:
    def __init__(self, adapter: DatabaseAdapter) -> None:
        self._adapter = adapter

    async def execute(self, query: SQLQuery) -> QueryResult:
        cancelled = Event()
        return await run_in_worker(
            partial(self._adapter.execute_query, query, cancelled), cancelled
        )


async def run_in_worker[T](operation: Callable[[], T], cancelled: Event | None = None) -> T:
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if cancelled is not None:
            cancelled.set()
        # Await metadata/validation workers too, so deadlines do not abandon threads.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if cancelled is not None:
                    cancelled.set()
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise
