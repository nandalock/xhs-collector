"""Bounded image downloads in the Markdown directory's images subfolder."""
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from .common import CollectError

IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/avif": ".avif",
}
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def image_signature(data, extension):
    if extension == ".jpg":
        return data.startswith(b"\xff\xd8\xff")
    if extension == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if extension == ".gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if extension == ".webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if extension == ".avif":
        return data[4:8] == b"ftyp" and (b"avif" in data[8:32] or b"avis" in data[8:32])
    return False


def image_url(url):
    """Only use the platform's image CDN; never attach account credentials."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        valid = (parsed.scheme in {"http", "https"} and parsed.port in {None, 80, 443}
                 and not parsed.username and not parsed.password
                 and (host == "xhscdn.com" or host.endswith(".xhscdn.com")))
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise CollectError("image_url_invalid", "Image URL is not a supported platform CDN address")
    return url


class ImageDownloader:
    def __init__(self, root, budget, *, transport=None):
        self.root = Path(root).resolve()
        self.budget = budget
        self.http = httpx.AsyncClient(transport=transport, follow_redirects=False,
            headers={"User-Agent": budget.headers.get("User-Agent", "xhs-collector"),
                     "Referer": "https://www.xiaohongshu.com/", "Accept-Encoding": "identity"})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.http.aclose()

    def _record(self, payload, path, content_type):
        return {"note_id": payload["note_id"], "position": payload["position"],
                "url": payload["url"], "path": path.relative_to(self.root).as_posix(),
                "content_type": content_type, "size": path.stat().st_size}

    async def download(self, payload):
        note_id, position = payload["note_id"], payload["position"]
        if not re.fullmatch(r"[0-9a-fA-F]{24}", str(note_id)) or not isinstance(position, int) or position < 1:
            raise CollectError("image_task_invalid", "Invalid image task identifier")
        url = image_url(payload["url"])
        directory = self.root / "clean" / "images"
        directory.mkdir(parents=True, exist_ok=True)
        stem = f"{note_id}-{position:03d}"
        for content_type, extension in IMAGE_EXTENSIONS.items():
            path = directory / (stem + extension)
            if path.is_file() and 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
                with path.open("rb") as stream:
                    if image_signature(stream.read(32), extension):
                        return self._record(payload, path, content_type)

        for _ in range(4):
            timeout = await self.budget.acquire_request()
            # A CDN Set-Cookie response must not turn later requests into authenticated requests.
            self.http.cookies.clear()
            try:
                async with self.http.stream("GET", url, timeout=timeout) as response:
                    status = response.status_code
                    if status in {401, 403, 429, 461, 471}:
                        category = {401: "auth_required", 403: "blocked", 429: "rate_limited",
                                    461: "challenge", 471: "challenge"}[status]
                        raise CollectError(category, f"Image server returned HTTP {status}; stopped",
                                           retry_after=response.headers.get("Retry-After"))
                    if status in {301, 302, 303, 307, 308}:
                        url = image_url(urljoin(url, response.headers.get("location", "")))
                        continue
                    if status != 200:
                        raise CollectError("image_http_error", f"Image server returned HTTP {status}")
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    extension = IMAGE_EXTENSIONS.get(content_type)
                    if extension is None:
                        raise CollectError("image_type_invalid", "Image response is not a supported image type")
                    fd, temporary = tempfile.mkstemp(prefix=f".{stem}-", suffix=".part", dir=directory)
                    try:
                        size, head = 0, bytearray()
                        with os.fdopen(fd, "wb") as stream:
                            async for chunk in response.aiter_bytes():
                                size += len(chunk)
                                if size > MAX_IMAGE_BYTES:
                                    raise CollectError("image_too_large", "Image exceeds the 20 MiB limit")
                                if len(head) < 32:
                                    head.extend(chunk[:32 - len(head)])
                                stream.write(chunk)
                            if not image_signature(head, extension):
                                raise CollectError("image_type_invalid", "Response bytes do not match the image type")
                            stream.flush()
                            os.fsync(stream.fileno())
                        path = directory / (stem + extension)
                        os.replace(temporary, path)
                        return self._record(payload, path, content_type)
                    finally:
                        if os.path.exists(temporary):
                            os.unlink(temporary)
            except httpx.TransportError as exc:
                raise CollectError("image_network_error", "Image transport failed; task can be retried") from exc
        raise CollectError("image_redirect_limit", "Image redirect limit reached")
