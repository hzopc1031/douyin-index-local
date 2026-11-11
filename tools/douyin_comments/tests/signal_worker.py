"""Subprocess-only fixture; every browser request is routed by FakeSite."""
from argparse import Namespace
import asyncio
import json
from pathlib import Path
import sys

from dyc import site
from dyc.fake import FakeSite, FastPacer
from dyc.runner import fetch


async def main():
    root = Path(sys.argv[1])
    fake = FakeSite("hang")
    args = Namespace(urls=[site.FAKE_ORIGIN + "/video/100", site.FAKE_ORIGIN + "/video/200"],
                     urls_file=None, out=root / "out", profile_dir=root / "profile",
                     state_dir=root / "state", display=None, top=30, max_pages=25)
    async def ready():
        while len(fake.calls) < 3:
            await asyncio.sleep(0.02)
        (root / "ready").write_text("synthetic request is in flight")
    task = asyncio.create_task(ready())
    code, result = await fetch(args, backend=fake, pacer=FastPacer())
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not fake.foreign
    print(json.dumps(result), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
