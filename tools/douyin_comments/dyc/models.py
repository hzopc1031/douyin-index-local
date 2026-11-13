from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from . import site
from .errors import Stop

SHANGHAI = ZoneInfo("Asia/Shanghai")


def now_iso():
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


@dataclass
class Comment:
    comment_id: str
    level: int
    root_comment_id: str
    parent_comment_id: str
    raw_reply_id: str = ""
    raw_reply_to_reply_id: str = ""
    text: str = ""
    created_at: str = ""
    has_image: bool | None = None
    has_sticker: bool | None = None
    like_count: int | None = None
    reply_count_reported: int | None = None
    user_id: str = ""
    nickname: str = ""
    reply_to_user_id: str = ""
    reply_to_nickname: str = ""
    ip_label: str = ""
    thread_status: str = "partial"
    source: str = "top"
    fetched_at: str = field(default_factory=now_iso)


def normalize(raw, root=None, source="top"):
    f = site.FIELDS
    cid = site.identifier(raw.get(f["id"]))
    reply_id = site.identifier(raw.get(f["reply_id"]), optional=True)
    reply_to = site.identifier(raw.get(f["reply_to_reply"]), optional=True)
    user = raw.get(f["user"])
    other = raw.get(f["reply_user"])
    user = {} if user is None else user
    other = {} if other is None else other
    if not isinstance(user, dict) or not isinstance(other, dict):
        raise Stop(3, "suspicious_schema")
    if raw.get(f["images"]) is not None and not isinstance(raw[f["images"]], list):
        raise Stop(3, "suspicious_schema")
    if raw.get(f["sticker"]) is not None and not isinstance(raw[f["sticker"]], dict):
        raise Stop(3, "suspicious_schema")
    created = raw.get(f["created"])
    stamp = ""
    if created is not None:
        site.integer(created)
        try:
            stamp = datetime.fromtimestamp(created, SHANGHAI).isoformat()
        except (OverflowError, OSError, ValueError):
            raise Stop(3, "suspicious_timestamp") from None
    parent = next((v for v in (reply_to, reply_id, root) if v and v != "0"), "") if root else ""
    return Comment(
        comment_id=cid, level=2 if root else 1, root_comment_id=root or cid,
        parent_comment_id=parent, raw_reply_id=reply_id, raw_reply_to_reply_id=reply_to,
        text=site.plain(raw.get(f["text"])), created_at=stamp,
        has_image=bool(raw[f["images"]]) if raw.get(f["images"]) is not None else None,
        has_sticker=bool(raw[f["sticker"]]) if raw.get(f["sticker"]) is not None else None,
        like_count=site.integer(raw.get(f["likes"]), optional=True),
        reply_count_reported=site.integer(raw.get(f["reply_count"]), optional=True),
        user_id=site.identifier(user.get(f["uid"]), optional=True),
        nickname=site.plain(user.get(f["nickname"])),
        reply_to_user_id=site.identifier(other.get(f["uid"]), optional=True),
        reply_to_nickname=site.plain(other.get(f["nickname"])),
        ip_label=site.plain(raw.get(f["ip"])), source=source,
    )


@dataclass
class Thread:
    root_id: str
    reported: int | None
    ids: set = field(default_factory=set)
    status: str = "partial"
    reason: str = "pending"

    def summary(self):
        return dict(root_comment_id=self.root_id, reported=self.reported,
                    collected=len(self.ids), shortfall=max(0, self.reported - len(self.ids))
                    if self.reported is not None else None,
                    status=self.status, reason=self.reason)


@dataclass
class Video:
    video_id: str
    video_url: str
    title: str = ""
    displayed: str = ""
    reported: int | None = None
    status: str = "partial"
    reason: str = "pending"
    comments: dict = field(default_factory=dict)
    threads: dict = field(default_factory=dict)
    pages: int = 0

    top_requests: int = 0
    top_items: int = 0
    reply_requests: int = 0
    reply_items: int = 0
    retries: int = 0
    empty_pages: int = 0

    def record_request(self, root, retry=False):
        self.pages += 1
        if root is None:
            self.top_requests += 1
        else:
            self.reply_requests += 1
        self.retries += int(retry)

    def record_page(self, root, decoded):
        count = len(decoded[0])
        if root is None:
            self.top_items += count
        else:
            self.reply_items += count
        self.empty_pages += int(count == 0)

    def summary(self):
        return dict(video_id=self.video_id, video_url=self.video_url,
                    status=self.status, reason=self.reason, pages=self.pages,
                    title=self.title, reported=self.reported, displayed=self.displayed,
                    top_requests=self.top_requests, top_items=self.top_items,
                    reply_requests=self.reply_requests, reply_items=self.reply_items,
                    retries=self.retries, empty_pages=self.empty_pages,
                    top_count=sum(c.level == 1 for c in self.comments.values()),
                    reply_count=sum(c.level == 2 for c in self.comments.values()),
                    partial_threads=[t.summary() for t in self.threads.values()
                                     if t.status != "complete"])
