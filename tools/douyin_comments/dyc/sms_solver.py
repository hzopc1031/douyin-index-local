"""Bounded subprocess for the optional, vendored ordinary-gap model.

Only redacted challenge pixels and geometry cross the anonymous pipe. No
browser/profile access, credential argv, logs or downloads. Missing model,
timeout, crash or uncertain recognition means manual verification.
"""
import asyncio
import multiprocessing
import os
import resource
import threading


def worker(connection):
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    with open(os.devnull, 'w') as null:
        os.dup2(null.fileno(), 1)
        os.dup2(null.fileno(), 2)
    try:
        from .slider_solver import LocalGapSolver
        solver = LocalGapSolver()
        while True:
            capture, challenge = connection.recv()
            connection.send(solver.solve(capture, challenge, threading.Event()))
    except (EOFError, BrokenPipeError):
        pass
    except Exception:
        try:
            connection.send(None)
        except Exception:
            pass
    finally:
        connection.close()


class GapWorker:
    def __init__(self):
        self.process = self.connection = None

    async def solve(self, capture, challenge):
        try:
            if self.process is None:
                context = multiprocessing.get_context('spawn')
                self.connection, child = context.Pipe()
                self.process = context.Process(target=worker, args=(child,), daemon=True)
                self.process.start()
                child.close()
            self.connection.send((capture, challenge))
            async with asyncio.timeout(10):
                while self.process.is_alive():
                    if self.connection.poll():
                        return self.connection.recv()
                    await asyncio.sleep(.05)
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception:
            pass
        await self.close()
        return None

    async def close(self):
        if self.connection:
            self.connection.close()
            self.connection = None
        if self.process:
            if self.process.is_alive():
                self.process.terminate()
            for _ in range(20):
                if not self.process.is_alive():
                    break
                await asyncio.sleep(.025)
            if self.process.is_alive():
                self.process.kill()
            self.process.join(timeout=.5)
            self.process.close()
            self.process = None
