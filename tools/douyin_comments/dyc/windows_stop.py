"""Private stop-pipe groundwork for an owned Windows child process."""
import asyncio
import os
import threading


def start_stop_pipe(control, stream):
    """Stop once the parent writes or closes its pipe; never interpret data."""
    loop = asyncio.get_running_loop()

    def watch():
        try:
            # Do not hold the buffered stdin lock while the parent is idle:
            # Python must be able to finalize even if this daemon is blocked.
            os.read(stream.fileno(), 1)
        except (OSError, ValueError):
            pass
        try:
            loop.call_soon_threadsafe(control.stop, 2, 'interrupted')
        except RuntimeError:
            pass  # The process finished before its parent closed the pipe.

    thread = threading.Thread(target=watch, name='dyc-stop-pipe', daemon=True)
    thread.start()
    return thread
