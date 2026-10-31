"""Only fixed reason codes cross the logging/output boundary."""

import asyncio


class Stop(Exception):
    def __init__(self, code, reason):
        self.code, self.reason = code, reason
        super().__init__(reason)


class Control:
    def __init__(self):
        self.event = asyncio.Event()
        self.error = None

    def stop(self, code, reason):
        if self.error is None:
            self.error = Stop(code, reason)
            self.event.set()

    def check(self):
        if self.error:
            raise self.error

    async def run(self, awaitable):
        task = asyncio.ensure_future(awaitable)
        stopped = asyncio.create_task(self.event.wait())
        try:
            await asyncio.wait((task, stopped), return_when=asyncio.FIRST_COMPLETED)
            self.check()
            return await task
        finally:
            for pending in (task, stopped):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, stopped, return_exceptions=True)
