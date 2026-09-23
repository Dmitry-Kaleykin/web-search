"""Share simultaneous identical requests, including refresh, without caching failures."""

from __future__ import annotations

import asyncio
import copy
from contextlib import suppress


class SharedSearches:
    def __init__(self):
        self.pending = {}
        self.waiters = {}

    async def run(self, key, retrieve):
        if key not in self.pending:
            self.pending[key] = asyncio.create_task(retrieve())
            self.waiters[key] = 0
        task = self.pending[key]
        self.waiters[key] += 1
        try:
            return copy.deepcopy(await asyncio.shield(task))
        finally:
            self.waiters[key] -= 1
            if not self.waiters[key]:
                self.waiters.pop(key)
                self.pending.pop(key)
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    async def close(self):
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
