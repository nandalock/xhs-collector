import asyncio
import json

import httpx
import pytest

from xhs_collector.api import XhsClient
from xhs_collector.cli import default_run_dir, execute, parser
from xhs_collector.collector import collect, initialize
from xhs_collector.common import CollectError
from xhs_collector.demo import FixtureSite, IDS, SESSION, ref, spec
from xhs_collector.markdown import render_note_markdown
from xhs_collector.media import ImageDownloader
from xhs_collector.storage import Store

PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic-image-bytes"
IMAGE_URL = "https://sns-webpic-qc.xhscdn.com/example.png"


class Signer:
    def sign_headers_get(self, **kwargs):
        return {}

    def sign_headers_post(self, **kwargs):
        return {}


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "run", create=True)
    yield instance
    instance.close()


def image_site(request):
    response = FixtureSite().handle(request)
    if request.url.path.endswith("/feed"):
        payload = response.json()
        payload["data"]["items"][0]["note_card"]["image_list"] = [{"url_default": IMAGE_URL}]
        return httpx.Response(200, json=payload)
    return response


def collect_images(store, image_handler, *, requests=100):
    async def exercise():
        async with XhsClient(SESSION, delay=0, max_requests=requests, signer=Signer(),
                             transport=httpx.MockTransport(image_site)) as client:
            return await collect(store, client, media_transport=httpx.MockTransport(image_handler))
    return asyncio.run(exercise())


def test_images_export_in_markdown_images_folder_without_credentials(store):
    initialize(store, spec(mode="detail", refs=[ref(IDS[0])], max_comments=0, download_images=True))
    seen = []

    def image(request):
        seen.append(request)
        assert "cookie" not in request.headers
        assert "x-s" not in request.headers
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    report = collect_images(store, image)
    assert report["status"] == "completed"
    assert report["images"] == {"downloaded": 1, "pending": 0, "failed": 0}
    assert report["requests_total"] == 3
    image_path = store.root / "clean" / "images" / f"{IDS[0]}-001.png"
    assert image_path.read_bytes() == PNG
    markdown = (store.root / "clean" / f"{IDS[0]}.md").read_text(encoding="utf-8")
    assert f"![图片1](<images/{image_path.name}>)" in markdown
    assert IMAGE_URL not in markdown
    exported = json.loads((store.root / "notes.jsonl").read_text(encoding="utf-8"))
    assert exported["images"] == [IMAGE_URL]
    assert exported["image_files"][0]["path"] == "clean/images/" + image_path.name
    assert collect_images(store, image)["requests_total"] == 3
    assert len(seen) == 1
    image_path.unlink()
    with store.db:
        store.enqueue_images(next(store.records("notes")))
    assert collect_images(store, image)["images"]["downloaded"] == 1
    assert len(seen) == 2


def test_image_budget_keeps_saved_note_and_pending_image(store):
    initialize(store, spec(mode="detail", refs=[ref(IDS[0])], max_comments=0, download_images=True))
    handler = lambda request: httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    first = collect_images(store, handler, requests=2)
    assert first["status"] == "budget_exhausted"
    assert first["notes"] == 1
    assert first["images"]["pending"] == 1
    second = collect_images(store, handler, requests=1)
    assert second["status"] == "completed"
    assert second["requests_total"] == 3  # image-only resume does not reverify login


def test_image_rate_limit_stops_without_losing_note(store):
    initialize(store, spec(mode="detail", refs=[ref(IDS[0])], max_comments=0, download_images=True))
    report = collect_images(store, lambda request: httpx.Response(429, headers={"retry-after": "60"}))
    assert report["status"] == "rate_limited"
    assert report["notes"] == 1
    assert report["images"]["pending"] == 1
    assert report["last_event"]["retry_after"] == "60"


def test_error_page_is_not_saved_as_an_image(store):
    initialize(store, spec(mode="detail", refs=[ref(IDS[0])], max_comments=0, download_images=True))
    report = collect_images(store, lambda request: httpx.Response(200, content=b"<html>error</html>",
                                                                headers={"content-type": "image/png"}))
    assert report["status"] == "partial"
    assert report["images"]["failed"] == 1
    assert report["notes"] == 1
    assert not list((store.root / "clean").rglob("*.png"))
    assert not list((store.root / "clean").rglob("*.part"))


@pytest.mark.parametrize("url", ["http://127.0.0.1/secret", "file:///etc/passwd", "https://xhscdn.com.evil.test/a"])
def test_untrusted_image_hosts_are_rejected_before_request(store, url):
    async def exercise():
        async with XhsClient(SESSION, delay=0) as budget:
            async with ImageDownloader(store.root, budget) as downloader:
                with pytest.raises(CollectError, match="CDN"):
                    await downloader.download({"note_id": IDS[0], "position": 1, "url": url})
                assert budget.requests == 0
    asyncio.run(exercise())


def test_redirect_host_is_checked(store):
    async def exercise():
        async with XhsClient(SESSION, delay=0) as budget:
            transport = httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "http://localhost/secret"}))
            async with ImageDownloader(store.root, budget, transport=transport) as downloader:
                with pytest.raises(CollectError, match="CDN"):
                    await downloader.download({"note_id": IDS[0], "position": 1, "url": IMAGE_URL})
                assert budget.requests == 1
    asyncio.run(exercise())


def test_download_images_command_needs_no_session_and_preserves_other_tasks(store, monkeypatch):
    initialize(store, spec())
    note = {"id": IDS[0], "note_id": IDS[0], "title": "Example", "text": "Body", "images": [],
            "source_url": "https://example.com/note", "fetched_at": "fixture"}
    with store.db:
        store.db.execute("INSERT INTO notes VALUES (?,?)", (IDS[0], json.dumps(note)))
    monkeypatch.setattr("xhs_collector.cli.load_session", lambda: pytest.fail("Image-only command loaded credentials"))
    args = parser().parse_args(["download-images", "--run", str(store.root)])
    result = asyncio.run(execute(args))
    assert result["status"] == "ready"
    assert result["tasks"]["pending"] == 1


def test_default_output_is_outside_source_and_explicit_run_still_works(monkeypatch, tmp_path):
    monkeypatch.setattr("xhs_collector.cli.Path.home", lambda: tmp_path)
    generated = default_run_dir("search")
    assert generated.parent == tmp_path / "Documents" / "xhs-collector" / "runs"
    assert generated != default_run_dir("search")
    args = parser().parse_args(["search", "--keyword", "computer"])
    assert args.run is None and args.download_images
    args = parser().parse_args(["search", "--keyword", "computer", "--run", str(tmp_path), "--no-download-images"])
    assert args.run == tmp_path and not args.download_images


def test_markdown_falls_back_to_remote_images_when_local_file_is_missing():
    note = {"images": [IMAGE_URL], "image_files": [{"position": 1, "path": "../outside.png"}]}
    assert IMAGE_URL in render_note_markdown(note)
    assert "outside.png" not in render_note_markdown(note)
