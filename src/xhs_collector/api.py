"""XHS web API adapter, adapted from MediaCrawler (relakkes@gmail.com).

NON-COMMERCIAL LEARNING LICENSE 1.1. Interface mapping and xhshow integration
follow MediaCrawler commit 8f58399. No dependency on a MediaCrawler checkout.
"""
import asyncio
import json
import time
from urllib.parse import quote, urlencode

import httpx

from .common import CollectError, initial_note

API_HOST = "https://edith.xiaohongshu.com"
WEB_HOST = "https://www.xiaohongshu.com"


class XhsClient:
    def __init__(self, session, *, delay=2, max_requests=100, max_seconds=300,
                 timeout=30, transport=None, signer=None):
        self.cookie = session["cookie"]
        self.headers = {"Cookie": self.cookie, "User-Agent": session["user_agent"],
                        "Origin": WEB_HOST, "Referer": WEB_HOST + "/",
                        "Content-Type": "application/json;charset=UTF-8"}
        self.delay = delay
        self.max_requests = max_requests
        self.deadline = time.monotonic() + max_seconds
        self.timeout = timeout
        self.requests = 0
        self.last_request = 0.0
        self.signer = signer
        self.http = httpx.AsyncClient(transport=transport, follow_redirects=False)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.http.aclose()

    async def acquire_request(self):
        """Share the request budget and spacing with the separate image client."""
        if self.requests >= self.max_requests:
            raise CollectError("budget_exhausted", "Request budget reached; resume this run to continue")
        remaining = self.deadline - time.monotonic()
        pause = max(0, self.last_request + self.delay - time.monotonic())
        if remaining <= pause:
            raise CollectError("budget_exhausted", "Time budget reached; resume this run to continue")
        await asyncio.sleep(pause)
        self.requests += 1
        self.last_request = time.monotonic()
        return min(self.timeout, max(.01, self.deadline - time.monotonic()))

    async def request(self, method, path, data=None, *, html=False):
        data = data or {}
        for attempt in range(3):
            timeout = await self.acquire_request()
            headers = dict(self.headers)
            if not html:
                if self.signer is None:
                    from xhshow import Xhshow
                    self.signer = Xhshow()
                if method == "POST":
                    signed = self.signer.sign_headers_post(uri=path, cookies=self.cookie, payload=data)
                else:
                    signed = self.signer.sign_headers_get(uri=path, cookies=self.cookie, params=data)
                headers.update(signed)
            url = (WEB_HOST if html else API_HOST) + path
            if method == "GET" and data:
                url += "?" + urlencode(data, quote_via=quote, safe=",")
            try:
                async with self.http.stream(method, url, headers=headers,
                        content=json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode() if method == "POST" else None,
                        timeout=timeout) as response:
                    code = response.status_code
                    if code in (401, 403, 429, 461, 471):
                        status = {401: "auth_required", 403: "blocked", 429: "rate_limited", 461: "challenge", 471: "challenge"}[code]
                        raise CollectError(status, f"Platform returned HTTP {code}; stop and inspect account/browser state",
                                           retry_after=response.headers.get("Retry-After"))
                    if code == 404:
                        raise CollectError("not_found", "Content does not exist")
                    if code >= 500:
                        raise CollectError("server_error", f"Platform returned HTTP {code}")
                    if code != 200:
                        raise CollectError("http_error", f"Unexpected HTTP {code}")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 10 * 1024 * 1024:
                            raise CollectError("response_too_large", "Response exceeds 10 MiB")
                    text = body.decode("utf-8", errors="replace")
                try:
                    payload = json.loads(text)
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    business_code = str(payload.get("code", ""))
                    if business_code in {"300011", "300012"}:
                        raise CollectError("blocked", f"Platform access restriction ({business_code})")
                    if business_code in {"-510000", "-510001"}:
                        raise CollectError("not_found", "Note missing or unavailable")
                if html:
                    return text
                if not isinstance(payload, dict):
                    raise CollectError("parse_error", "Expected JSON response")
                if not payload.get("success"):
                    raise CollectError("api_error", f"API failed (code={payload.get('code', 'unknown')})")
                result = payload.get("data")
                if not isinstance(result, dict):
                    raise CollectError("parse_error", "Expected an object in API data")
                return result
            except (httpx.TimeoutException, httpx.TransportError):
                error = CollectError("network_error", "HTTP transport failed; no credentials logged")
            except CollectError as exc:
                if exc.status != "server_error":
                    raise
                error = exc
            if attempt == 2:
                raise error
            self.last_request = time.monotonic() + (2 ** attempt)

    async def verify(self):
        try:
            result = await self.request("GET", "/api/sns/web/v1/user/selfinfo")
        except CollectError as exc:
            if exc.status == "api_error":
                raise CollectError("auth_required", "Session verification rejected; run login to re-check the account") from exc
            raise
        user_result = result.get("result")
        if not isinstance(user_result, dict) or not user_result.get("success"):
            raise CollectError("auth_required", "Login is missing or expired; run login")
        return True

    async def search(self, keyword, page, search_id, sort):
        return await self.request("POST", "/api/sns/web/v1/search/notes", {
            "keyword": keyword, "page": page, "page_size": 20, "search_id": search_id,
            "sort": sort, "note_type": 0})

    async def creator(self, ref, cursor):
        return await self.request("GET", "/api/sns/web/v1/user_posted", {
            "user_id": ref["id"], "num": 30, "cursor": cursor,
            "image_formats": "jpg,webp,avif", "xsec_token": ref["token"], "xsec_source": ref["source"]})

    async def note(self, ref):
        try:
            result = await self.request("POST", "/api/sns/web/v1/feed", {
                "source_note_id": ref["id"], "image_formats": ["jpg", "webp", "avif"],
                "extra": {"need_body_topic": 1}, "xsec_token": ref["token"], "xsec_source": ref["source"]})
            items = result.get("items", [])
            if isinstance(items, list) and items and isinstance(items[0], dict) and isinstance(items[0].get("note_card"), dict):
                return items[0]["note_card"], "api"
        except CollectError as exc:
            if exc.status not in {"parse_error", "api_error", "server_error", "network_error"}:
                raise
        html = await self.request("GET", f"/explore/{ref['id']}", {
            "xsec_token": ref["token"], "xsec_source": ref["source"]}, html=True)
        return initial_note(html, ref["id"]), "embedded_state"

    async def comments(self, ref, cursor, parent=""):
        data = {"note_id": ref["id"], "cursor": cursor, "image_formats": "jpg,webp,avif",
                "top_comment_id": "", "xsec_token": ref["token"]}
        path = "/api/sns/web/v2/comment/page"
        if parent:
            path = "/api/sns/web/v2/comment/sub/page"
            data.update(root_comment_id=parent, num="10")
        return await self.request("GET", path, data)
