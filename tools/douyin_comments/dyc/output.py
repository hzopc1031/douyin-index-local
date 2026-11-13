"""Private, atomic exports. Raw responses/templates never reach this module."""

from dataclasses import asdict
import csv
import html
import io
import json
import re
import sys

from . import site
from .runtime import atomic_text, private_dir, private_open

COLUMNS = ("run_id fetched_at video_id video_url video_title source comment_id level "
           "root_comment_id parent_comment_id raw_reply_id raw_reply_to_reply_id text "
           "created_at has_image has_sticker like_count reply_count_reported user_id "
           "nickname reply_to_user_id reply_to_nickname ip_label thread_status").split()
NUMERIC = {"level", "has_image", "has_sticker", "like_count", "reply_count_reported"}
SECRET_NAMES = ("cookie", "set-cookie", "authorization", site.PARAM["token"], *site.SIGNATURES)
SECRET = re.compile(r"(?i)(?:" + "|".join(re.escape(name) for name in SECRET_NAMES) +
                    r")\s*[=:]\s*[^\s,;<>]+")
URL_QUERY = re.compile(r"(https?://[^\s?<>]+)\?[^\s<>]*", re.I)


def clean(value):
    value = str(value)
    return SECRET.sub("[redacted]", URL_QUERY.sub(r"\1?[redacted]", value))


def cell(value, numeric=False):
    if value is None:
        return ""
    if isinstance(value, bool):
        return int(value)
    text = clean(value)
    if not numeric and text.startswith(("=", "+", "-", "@", "\t", "\r")):
        text = "'" + text
    return text


def markdown(value):
    text = html.escape(clean(value), quote=True)
    text = re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text)
    return text.replace("\r", " ").replace("\n", " · ")


def ranking(comments, top):
    # Unknown likes follow every known count (including zero); stable tie-break.
    return sorted(comments, key=lambda c: (-(c.like_count if c.like_count is not None else -1),
                                           c.created_at or "\uffff", c.comment_id))[:top]


def summary(videos, top):
    lines = ["# 评论采集摘要", "", "页面适配尚未真实校准；完整表示已走完所见分页，不保证平台展示全部评论。", ""]
    for video in videos:
        counts = video.summary()
        lines += [f"## 视频 {markdown(video.video_id or 'unresolved')}", "",
                  f"状态：{video.status}；原因：{video.reason}", "",
                  f"页面显示：{markdown(video.displayed) if video.displayed else '未知'}；"
                  f"接口报告：{video.reported if video.reported is not None else '未知'}；"
                  f"采集一级：{counts['top_count']}；回复：{counts['reply_count']}。", ""]
        if counts["partial_threads"]:
            lines += ["未完整的回复线程：", ""]
            for thread in counts["partial_threads"]:
                lines.append(f"- 根评论 {markdown(thread['root_comment_id'])}："
                             f"{thread['reason']}；报告 {thread['reported'] if thread['reported'] is not None else '未知'}，"
                             f"已采 {thread['collected']}，缺口 {thread['shortfall'] if thread['shortfall'] is not None else '未知'}")
            lines.append("")
        for level, title in ((1, "一级评论"), (2, "回复")):
            lines += [f"### {title} Top {top}", ""]
            for item in ranking([c for c in video.comments.values() if c.level == level], top):
                lines.append(_rank_line(video, item))
            lines.append("")
    lines += [f"## 跨视频 Top {top}", ""]
    all_items = [(video, item) for video in videos for item in video.comments.values()]
    ordered = sorted(all_items, key=lambda pair: (
        -(pair[1].like_count if pair[1].like_count is not None else -1),
        pair[1].created_at or "\uffff", pair[1].comment_id, pair[0].video_id))[:top]
    for video, item in ordered:
        lines.append(f"- 视频 {markdown(video.video_id)} " + _rank_line(video, item)[2:])
    return "\n".join(lines) + "\n"


def _rank_line(video, item):
    line = f"- 点赞 {item.like_count if item.like_count is not None else '未知'} · 评论 {markdown(item.comment_id)}：{markdown(item.text)}"
    if item.level == 2:
        root = video.comments.get(item.root_comment_id)
        line += f" — 根评论 {markdown(item.root_comment_id)}：{markdown(root.text) if root else '缺失'}"
    return line


def export(out, run_id, videos, code, reason, requests, top=30, metadata=None):
    out = private_dir(out)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    for video in videos:
        if not video.video_id:
            continue
        for item in video.comments.values():
            row = dict(asdict(item), run_id=run_id, video_id=video.video_id,
                       video_url=video.video_url, video_title=video.title)
            writer.writerow({key: cell(row[key], key in NUMERIC) for key in COLUMNS})
    atomic_text(out / "comments.csv", "\ufeff" + buffer.getvalue())
    atomic_text(out / "summary.md", summary(videos, top))
    result = dict(run_id=run_id, out=str(out), videos=[v.summary() for v in videos],
                  exit_code=code, exit_reason=reason, requests=requests,
                  site_calibrated=False)
    if metadata is not None:
        result["metadata"] = metadata
    # Only the summary's whitelisted fields, never templates, headers or raw JSON.
    for entry in result["videos"]:
        entry["displayed"] = clean(entry["displayed"])
    atomic_text(out / "run.json", json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


class Log:
    def __init__(self, out):
        self.stream = open(private_open(out / "run.log"), "w", encoding="utf-8", closefd=True)

    def event(self, code):
        # Callers pass a fixed event/reason code. Never log exception strings,
        # page content, request URLs, headers, or the browser's debug output.
        safe = code if re.fullmatch(r"[a-z0-9_]+", code) else "event_redacted"
        print(safe, file=sys.stderr, flush=True)
        print(safe, file=self.stream, flush=True)

    def close(self):
        self.stream.close()
