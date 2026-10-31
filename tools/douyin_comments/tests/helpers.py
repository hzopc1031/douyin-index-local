import json


def comment(cid, replies=0, **extra):
    return dict(cid=str(cid), text="合成评论😀", create_time=1700000000,
                digg_count=0, reply_comment_total=replies,
                user={"uid": "900000000000000001", "nickname": "合成昵称"},
                ip_label="合成属地", **extra)


def response(comments=(), cursor=0, more=False, **extra):
    return {"status": 200, "text": json.dumps(dict(
        status_code=0, comments=list(comments), cursor=cursor,
        has_more=int(more), **extra), ensure_ascii=False)}


class Transport:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def fetch(self, video_id, root, cursor):
        self.calls.append((root, cursor))
        result = self.pages[(root, cursor)]
        if isinstance(result, list):
            return result.pop(0)
        return result
