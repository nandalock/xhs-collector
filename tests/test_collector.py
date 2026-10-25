import asyncio
import json

import httpx
import pytest

from xhs_collector.api import XhsClient
from xhs_collector.collector import collect, initialize
from xhs_collector.common import CollectError, initial_note, parse_link
from xhs_collector.demo import FixtureSite, IDS, SESSION, ref, spec
from xhs_collector.session import load_session
from xhs_collector.storage import Store, run_lock


class Signer:
    def __init__(self):
        self.calls = []

    def sign_headers_post(self, **kwargs):
        self.calls.append(("POST", kwargs))
        return {"x-s": "synthetic-signature"}

    def sign_headers_get(self, **kwargs):
        self.calls.append(("GET", kwargs))
        return {"x-s": "synthetic-signature"}


def run(store, site, *, requests=100, retry_failed=False):
    async def inner():
        async with XhsClient(SESSION, delay=0, max_requests=requests,
                             signer=Signer(), transport=httpx.MockTransport(site.handle)) as client:
            return await collect(store, client, retry_failed=retry_failed)
    return asyncio.run(inner())


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "run", create=True)
    yield instance
    instance.close()


@pytest.mark.parametrize("limit,expected", [(1, 1), (2, 2), (3, 3), (5, 3)])
def test_final_page_and_exact_note_limit(store, limit, expected):
    initialize(store, spec(max_notes=limit, max_comments=0))
    report = run(store, FixtureSite())
    assert report["status"] == "completed"
    assert report["notes"] == expected
    assert report["discovered_notes"] == expected


def test_multiple_keywords_share_budget_and_keep_provenance(store):
    initialize(store, spec(keywords=["帐篷", "露营"], max_notes=3, max_comments=0))
    run(store, FixtureSite())
    assert store.note_slots() == 3
    assert store.db.execute("SELECT count(*) FROM sources WHERE note_id=?", (IDS[0],)).fetchone()[0] == 2


def test_resume_skips_completed_notes_and_regenerates_exports(store):
    initialize(store, spec(max_comments=0))
    first = FixtureSite()
    report = run(store, first, requests=3)
    assert report["status"] == "budget_exhausted"
    assert report["notes"] == 1
    store.db.close()
    store.db = __import__("sqlite3").connect(store.root / "state.sqlite3")
    store.db.row_factory = __import__("sqlite3").Row
    second = FixtureSite()
    report = run(store, second)
    assert report["status"] == "completed"
    assert report["notes"] == 3
    assert sum(path.endswith("/feed") for _, path, _ in second.calls) == 2
    assert len((store.root / "notes.jsonl").read_text(encoding="utf-8").splitlines()) == 3
    before = second.calls.copy()
    run(store, second)
    assert second.calls == before


@pytest.mark.parametrize("limit", [1, 2, 3, 4])
def test_comment_cap_includes_embedded_and_paginated_replies(store, limit):
    initialize(store, spec(max_notes=1, max_comments=limit))
    report = run(store, FixtureSite())
    assert report["comments"] == limit
    comments = list(store.records("comments"))
    assert len({comment["id"] for comment in comments}) == limit
    for comment in comments:
        if "reply" in comment["id"]:
            assert comment["parent_comment_id"] == IDS[0] + "-c1"


def test_creator_final_page_and_duplicate_notes(store):
    initialize(store, spec(mode="creator", refs=[ref(IDS[3])], max_notes=5, max_comments=0))
    report = run(store, FixtureSite())
    assert report["notes"] == 3


@pytest.mark.parametrize("status,classification", [(401, "auth_required"), (403, "blocked"),
        (429, "rate_limited"), (461, "challenge"), (471, "challenge")])
def test_access_errors_stop_without_html_fallback(store, status, classification):
    initialize(store, spec(mode="detail", refs=[ref(IDS[0])]))

    class Blocked(FixtureSite):
        def handle(self, request):
            if request.url.path.endswith("/feed"):
                self.calls.append((request.method, request.url.path, {}))
                return httpx.Response(status, headers={"Retry-After": "60"})
            return super().handle(request)

    site = Blocked()
    report = run(store, site)
    assert report["status"] == classification
    assert report["tasks"]["pending"] == 1
    assert len(site.calls) == 2  # auth + detail, no retries or HTML fallback
    if status == 429:
        assert report["last_event"]["retry_after"] == "60"


def test_html_fallback_preserves_literal_undefined(store):
    initialize(store, spec(mode="detail", refs=[ref(IDS[0])], max_comments=0))

    class Html(FixtureSite):
        def handle(self, request):
            if request.url.path.endswith("/feed"):
                return httpx.Response(200, json={"success": True, "data": {"items": []}})
            if request.url.path.startswith("/explore/"):
                text = 'window.__INITIAL_STATE__={"note":{"noteDetailMap":{"' + IDS[0] + '":{"note":{"noteId":"' + IDS[0] + '","desc":"undefined is text","title":"测试","extra":undefined}}}}};</script>'
                return httpx.Response(200, text=text)
            return super().handle(request)

    report = run(store, Html())
    assert report["notes"] == 1
    note = next(store.records("notes"))
    assert note["text"] == "undefined is text"
    assert note["extraction_method"] == "embedded_state"


def test_bad_embedded_js_is_not_executed():
    with pytest.raises(CollectError):
        initial_note('window.__INITIAL_STATE__=fetch("https://example.com")</script>', IDS[0])


def test_snapshots_and_exports_omit_access_tokens_profiles(store):
    initialize(store, spec())
    run(store, FixtureSite())
    paths = list((store.root / "snapshots").glob("*.json")) + list(store.root.glob("*.jsonl")) + [store.root / "run.json"]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "synthetic-access-token" not in text
        assert "synthetic-private-id" not in text
        assert "synthetic-fixture-only" not in text
    documents = [json.loads(line) for line in (store.root / "documents.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {item["kind"] for item in documents} == {"note", "comment"}
    assert all(item["source_url"] and len(item["content_hash"]) == 64 for item in documents)
    record = store.show("xhs:note:" + IDS[0], 2, 5)
    assert len(record["text"]) == 5
    assert record["next_offset"] == 7


def test_link_validation_and_query_preservation():
    result = parse_link(f"https://www.xiaohongshu.com/explore/{IDS[0]}?xsec_token=a%2Bb%3D&xsec_source=pc_search")
    assert result["token"] == "a+b="
    for url in (f"http://localhost/explore/{IDS[0]}?xsec_token=a", f"https://www.xiaohongshu.com/explore/{IDS[0]}", "https://xhslink.com/a"):
        with pytest.raises(ValueError):
            parse_link(url)


def test_signing_matches_sent_payload_and_query():
    signer = Signer()
    seen = []

    def handler(request):
        seen.append(request)
        assert request.headers["x-s"] == "synthetic-signature"
        return httpx.Response(200, json={"success": True, "data": {}})

    async def exercise():
        async with XhsClient(SESSION, delay=0, signer=signer, transport=httpx.MockTransport(handler)) as client:
            await client.request("POST", "/test", {"keyword": "中文", "page": 1})
            await client.request("GET", "/test", {"cursor": "a+b =", "image_formats": "jpg,webp"})
    asyncio.run(exercise())
    assert json.loads(seen[0].content) == signer.calls[0][1]["payload"]
    assert dict(seen[1].url.params) == signer.calls[1][1]["params"]
    assert b"jpg,webp" in seen[1].url.query
    assert b"%2B" in seen[1].url.query


def test_request_limit_counts_auth(store):
    initialize(store, spec())
    site = FixtureSite()
    report = run(store, site, requests=1)
    assert report["requests_total"] == 1
    assert len(site.calls) == 1
    assert report["status"] == "budget_exhausted"


def test_retry_failed_after_not_found(store):
    initialize(store, spec(mode="detail", refs=[ref(IDS[0])], max_comments=0))

    class Missing(FixtureSite):
        def handle(self, request):
            if request.url.path.endswith("/feed"):
                return httpx.Response(200, json={"success": False, "code": -510000})
            return super().handle(request)

    assert run(store, Missing())["status"] == "partial"
    assert run(store, FixtureSite())["notes"] == 0
    assert run(store, FixtureSite(), retry_failed=True)["notes"] == 1


def test_deadline_leaves_current_task_pending(store):
    initialize(store, spec(max_comments=0))

    class Slow:
        requests = 1

        async def search(self, *args):
            await asyncio.sleep(1)

    report = asyncio.run(collect(store, Slow(), max_seconds=.01, verify=False))
    assert report["status"] == "budget_exhausted"
    assert report["tasks"]["pending"] == 1
    assert report["notes"] == 0


def test_concurrent_run_lock(tmp_path):
    with run_lock(tmp_path):
        with pytest.raises(CollectError, match="Another process"):
            with run_lock(tmp_path):
                pass


def test_page_limit_reports_restricted_discovery(store):
    initialize(store, spec(max_notes=5, max_pages=1, max_comments=0))
    report = run(store, FixtureSite())
    assert report["notes"] == 2
    assert report["last_event"]["status"] == "discovery_limit"


def test_missing_session_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setenv("XHS_COLLECTOR_HOME", str(tmp_path))
    with pytest.raises(CollectError) as exc:
        load_session()
    assert exc.value.status == "auth_required"


def test_new_scope_does_not_overwrite_existing_run(store):
    initialize(store, spec(keywords=["first"]))
    with pytest.raises(ValueError, match="already exists"):
        initialize(store, spec(keywords=["second"]))
    assert store.meta("spec")["keywords"] == ["first"]


@pytest.mark.parametrize("data", [[], {}, {"cookie": "a1=; web_session=", "user_agent": "test"}])
def test_invalid_session_has_typed_error(tmp_path, monkeypatch, data):
    monkeypatch.setenv("XHS_COLLECTOR_HOME", str(tmp_path))
    (tmp_path / "session.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CollectError) as exc:
        load_session()
    assert exc.value.status == "auth_required"


def test_rejected_session_preserves_all_tasks(store):
    initialize(store, spec())

    class LoggedOut(FixtureSite):
        def handle(self, request):
            self.calls.append(request.url.path)
            return httpx.Response(200, json={"success": False, "code": -100})

    site = LoggedOut()
    report = run(store, site)
    assert report["status"] == "auth_required"
    assert report["tasks"]["pending"] == 1
    assert len(site.calls) == 1


def test_repeated_creator_cursor_terminates_with_warning(store):
    initialize(store, spec(mode="creator", refs=[ref(IDS[3])], max_notes=5, max_comments=0))

    class Stalled(FixtureSite):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path.endswith("user_posted"):
                payload = response.json()
                payload["data"]["has_more"] = True
                return httpx.Response(200, json=payload)
            return response

    report = run(store, Stalled())
    assert report["status"] == "completed"
    assert report["warnings"] == {"pagination_stalled": 1}
    assert report["notes"] == 3
