"""Synthetic, offline transport fixtures shared by demo and behavior tests."""
import json

import httpx

from .api import XhsClient
from .collector import collect, initialize
from .common import atomic_json
from .storage import Store, run_lock

IDS = [f"{index:024x}" for index in range(1, 5)]
SESSION = {"cookie": "a1=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef; web_session=synthetic-fixture-only",
           "user_agent": "Mozilla/5.0 xhs-collector-offline-fixture"}


def spec(**overrides):
    return {"mode": "search", "keywords": ["合成露营样本"], "refs": [], "max_notes": 3,
            "max_comments": 4, "sub_comments": True, "max_pages": 5, "sort": "general", **overrides}


def ref(identifier):
    return {"id": identifier, "token": "synthetic-access-token", "source": "pc_search"}


class FixtureSite:
    def __init__(self):
        self.calls = []

    def handle(self, request):
        self.calls.append((request.method, request.url.path, dict(request.url.params)))
        path = request.url.path
        if path.endswith("selfinfo"):
            result = {"result": {"success": True}}
        elif path.endswith("search/notes"):
            page = json.loads(request.content)["page"]
            ids = IDS[:2] if page == 1 else IDS[1:3]
            result = {"items": [{"id": item, "xsec_token": "synthetic-access-token", "model_type": "note"} for item in ids],
                      "has_more": page == 1}
        elif path.endswith("user_posted"):
            ids = IDS[:2] if not request.url.params.get("cursor") else IDS[1:3]
            result = {"notes": [{"note_id": item, "xsec_token": "synthetic-access-token"} for item in ids],
                      "cursor": "creator-page-2", "has_more": not bool(request.url.params.get("cursor"))}
        elif path.endswith("/feed"):
            identifier = json.loads(request.content)["source_note_id"]
            note = {"note_id": identifier, "title": "合成样本：露营装备 " + identifier[-1],
                    "desc": "这是离线测试资料，不是真实小红书数据。准备帐篷、防潮垫和照明。",
                    "type": "normal", "time": 1700000000000,
                    "user": {"user_id": "synthetic-private-id", "nickname": "测试用户"},
                    "xsec_token": "synthetic-access-token", "interact_info": {"liked_count": "3"},
                    "tag_list": [{"type": "topic", "name": "露营"}], "image_list": []}
            result = {"items": [{"note_card": note}]}
        elif path.endswith("comment/sub/page"):
            note_id = request.url.params["note_id"]
            result = {"comments": [{"id": note_id + "-reply-2", "content": "合成回复第二页"}], "has_more": False, "cursor": ""}
        elif path.endswith("comment/page"):
            note_id = request.url.params["note_id"]
            if not request.url.params.get("cursor"):
                items = [{"id": note_id + "-c1", "content": "合成评论一",
                          "sub_comments": [{"id": note_id + "-reply-1", "content": "合成内嵌回复"}],
                          "sub_comment_has_more": True, "sub_comment_cursor": "reply-page-2"}]
                result = {"comments": items, "has_more": True, "cursor": "page-2"}
            else:
                result = {"comments": [{"id": note_id + "-c1", "content": "重复评论"},
                                       {"id": note_id + "-c2", "content": "合成评论二"}], "has_more": False, "cursor": ""}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"success": True, "data": result})


async def run_demo(root):
    with run_lock(root):
        store = Store(root, create=True)
        try:
            initialize(store, spec())
            with store.db:
                store.set_meta("data_origin", "synthetic_fixture")
            site = FixtureSite()
            async with XhsClient(SESSION, delay=0, transport=httpx.MockTransport(site.handle)) as client:
                report = await collect(store, client)
            report["data_origin"] = "synthetic_fixture"
            atomic_json(store.root / "report.json", report)
            return report
        finally:
            store.close()
