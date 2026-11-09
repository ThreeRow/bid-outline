"""Serialized file I/O outside the event loop; cancellation drains in-progress writes."""
import asyncio
from functools import partial


class AuditIO:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.pending = set()

    async def run(self, function, *args, **kwargs):
        async with self.lock:
            task = asyncio.create_task(asyncio.to_thread(partial(function, *args, **kwargs)))
            self.pending.add(task)
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A worker thread cannot be safely killed during fsync. Do not
                # return cancellation while it can still mutate core evidence.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                task.result()  # Preserve an actual disk error if the drain failed.
                raise
            finally:
                self.pending.discard(task)

    async def drain(self):
        async with self.lock:
            pass
