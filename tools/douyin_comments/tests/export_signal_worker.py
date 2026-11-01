"""Subprocess fixture: routed synthetic collection, gated synchronous export."""

from argparse import Namespace
import asyncio
import csv
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

from dyc import output, site
from dyc.fake import FakeSite, FastPacer, fixture_comment, fixture_page
from dyc.runner import fetch


class LargeBatch(FakeSite):
    def __init__(self):
        super().__init__()
        # Prepare the same 30,000 comments before browser deadlines start.
        # Compact UTF-8 bytes avoid repeated JSON escaping across Playwright/CDP
        # while preserving every field in the normal collection/export pipeline.
        self.body = json.dumps(fixture_page(
            [fixture_comment(i) for i in range(1, 30001)], 0, False, 30000),
            ensure_ascii=False, separators=(",", ":")).encode()

    async def deliver(self, route):
        if site.request_kind(route.request.url):
            self.calls.append("top")
            await route.fulfill(status=200, content_type="application/json",
                                body=self.body)
        else:
            await super().deliver(route)


async def main():
    root, stage = Path(sys.argv[1]), sys.argv[2]
    fake = LargeBatch()
    args = Namespace(urls=[site.FAKE_ORIGIN + "/video/100"], urls_file=None,
                     out=root / "out", profile_dir=root / "profile", state_dir=root / "state",
                     display=None, top=30, max_pages=1500)

    def gate():
        (root / "ready").write_text(stage)
        deadline = time.monotonic() + 10
        while not (root / "continue").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("synthetic export gate timed out")
            time.sleep(0.01)

    original_row, original_fsync = csv.DictWriter.writerow, os.fsync

    def row(writer, data):
        if stage == "serialize" and data.get("comment_id") == "15000":
            gate()
        return original_row(writer, data)

    def fsync(fd):
        # Pause inside the CSV atomic write, after serialization and before
        # its temporary file is published. Quota writes never reach this gate.
        if stage == "write" and (root / "serialized").exists() and not (root / "ready").exists():
            gate()
        return original_fsync(fd)

    original_atomic = output.atomic_text

    def atomic(path, text):
        if path.name == "comments.csv":
            (root / "serialized").write_text("ready for file write")
        return original_atomic(path, text)

    with patch.object(csv.DictWriter, "writerow", row), patch.object(output, "atomic_text", atomic), \
            patch("dyc.runtime.os.fsync", fsync):
        code, result = await fetch(args, backend=fake, pacer=FastPacer())
    assert fake.calls == ["top"] and not fake.foreign
    print(json.dumps(result), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
