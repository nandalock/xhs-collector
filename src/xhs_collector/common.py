"""Shared data contracts; no platform credentials in exported records."""
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class CollectError(Exception):
    def __init__(self, status, message, *, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def atomic_json(path: Path, data):
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def parse_link(url: str, kind="note"):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"www.xiaohongshu.com", "xiaohongshu.com"}:
        raise ValueError("Use a full HTTPS xiaohongshu.com link, not a short/share link.")
    pattern = r"/(?:explore|discovery/item|search_result)/([a-fA-F0-9]{24})/?" if kind == "note" else r"/user/profile/([a-fA-F0-9]{24})/?"
    match = re.fullmatch(pattern, parsed.path)
    if not match:
        raise ValueError(f"Invalid {kind} link path")
    query = parse_qs(parsed.query)
    token = query.get("xsec_token", [""])[0]
    if not token:
        raise ValueError("The link needs xsec_token; copy the full website link.")
    return {"id": match[1], "token": token, "source": query.get("xsec_source", ["pc_search"])[0]}


def redact(value):
    """Snapshots preserve content fields, omit user profiles and access credentials."""
    private = {"cookie", "cookies", "authorization", "xsec_token", "xsectoken", "user", "user_info", "userinfo", "ip_location", "iplocation"}
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items() if key.lower() not in private}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def snake_keys(value):
    if isinstance(value, dict):
        return {re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower(): snake_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [snake_keys(item) for item in value]
    return value


def initial_note(html, note_id):
    """Parse state without evaluating arbitrary JavaScript from the page."""
    match = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html)
    if not match:
        raise CollectError("parse_error", "No embedded note state found in HTML")
    text = html[match.end():].split("</script>", 1)[0]
    # Replace JS undefined outside quoted JSON strings, preserving text content.
    text = re.sub(r'"(?:\\.|[^"\\])*"|\bundefined\b', lambda m: "null" if m[0] == "undefined" else m[0], text)
    try:
        state, _ = json.JSONDecoder().raw_decode(text.lstrip())
        return snake_keys(state)["note"]["note_detail_map"][note_id]["note"]
    except (ValueError, KeyError, TypeError) as exc:
        raise CollectError("parse_error", "Embedded note state has an unsupported format") from exc

