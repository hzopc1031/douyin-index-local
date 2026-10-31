"""Serial pagination independent of Playwright; transport owns issued-request caps."""

from . import site
from .errors import Stop
from .models import Thread, normalize


async def collect(video, transport, page_budget=1500):
    used = 0

    async def page(root, cursor):
        nonlocal used
        for attempt in range(2):
            if used >= page_budget:
                raise Stop(2, "video_page_budget")
            used += 1
            video.pages += 1
            result = await transport.fetch(video.video_id, root, cursor)
            try:
                return site.decode_page(result)
            except Stop as error:
                if error.reason in ("timeout", "network"):
                    if attempt == 0:
                        continue
                    raise Stop(3, "repeated_" + error.reason) from None
                raise

    async def walk(root=None):
        cursor, cursors, seen = "0", {"0"}, set()
        thread = video.threads.get(root)
        while True:
            comments, next_cursor, more, reported = await page(root, cursor)
            if root is None and reported is not None:
                video.reported = reported
            fresh = 0
            for raw in comments:
                comment = normalize(raw, root, "reply" if root else "top")
                cid = comment.comment_id
                if cid not in seen:
                    fresh += 1
                    seen.add(cid)
                existing = video.comments.get(cid)
                if existing and (existing.level != comment.level or
                                 existing.root_comment_id != comment.root_comment_id):
                    raise Stop(3, "conflicting_comment_relation")
                if existing is None or existing.source == "inline":
                    video.comments[cid] = comment
                if root:
                    thread.ids.add(cid)
                else:
                    if cid not in video.threads:
                        video.threads[cid] = Thread(cid, comment.reply_count_reported)
                    inline = raw.get(site.FIELDS["inline"])
                    inline = [] if inline is None else inline
                    if not isinstance(inline, list):
                        raise Stop(3, "suspicious_schema")
                    for reply in inline:
                        if not isinstance(reply, dict):
                            raise Stop(3, "suspicious_schema")
                        item = normalize(reply, cid, "inline")
                        previous = video.comments.get(item.comment_id)
                        if previous and (previous.level != 2 or previous.root_comment_id != cid):
                            raise Stop(3, "conflicting_comment_relation")
                        video.comments.setdefault(item.comment_id, item)
                        video.threads[cid].ids.add(item.comment_id)
            # Even a final all-duplicate page is not proof of completion.
            if comments and not fresh:
                return "duplicate_page"
            if not more:
                return "exhausted"
            if not comments:
                return "empty_with_more"
            if next_cursor in cursors:
                return "cursor_cycle" if next_cursor != cursor else "cursor_unchanged"
            cursors.add(next_cursor)
            cursor = next_cursor

    try:
        top_reason = await walk()
        video.reason = top_reason
        for root, thread in video.threads.items():
            # Unknown reported counts still require independent reply pagination.
            if thread.reported == 0 and not thread.ids:
                reason = "exhausted"
            else:
                reason = await walk(root)
            if thread.reported is not None and len(thread.ids) < thread.reported:
                reason = "reply_shortfall" if reason == "exhausted" else reason
            thread.reason = reason
            thread.status = "complete" if reason == "exhausted" else "partial"
        video.status = "complete" if (top_reason == "exhausted" and
                       all(t.status == "complete" for t in video.threads.values())) else "partial"
        if video.status == "partial" and video.reason == "exhausted":
            video.reason = "partial_threads"
    except Stop as error:
        video.status, video.reason = "partial", error.reason
        for thread in video.threads.values():
            if thread.reason == "pending":
                thread.reason = error.reason
        raise
    finally:
        for comment in video.comments.values():
            thread = video.threads[comment.root_comment_id]
            comment.thread_status = thread.status
