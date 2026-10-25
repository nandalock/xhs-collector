"""Durable bounded task queue, independent of the MediaCrawler runtime."""
import asyncio
import json
import re
import secrets
import time

from .common import CollectError, atomic_json, digest, now, redact
from .media import ImageDownloader

HALT = {"auth_required", "blocked", "rate_limited", "challenge", "budget_exhausted"}


def search_id():
    value = (int(time.time() * 1000) << 64) + secrets.randbelow(2147483647)
    digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    result = ""
    while value:
        value, digit = divmod(value, 36)
        result = digits[digit] + result
    return result


def initialize(store, spec):
    if store.meta("spec") is not None:
        raise ValueError("Run already exists. Use resume, or choose a new directory.")
    with store.db:
        store.set_meta("spec", spec)
        store.set_meta("status", "ready")
        store.set_meta("created_at", now())
        if spec["mode"] == "search":
            for keyword in spec["keywords"]:
                store.enqueue(f"search:{keyword}:1", "search", {
                    "keyword": keyword, "page": 1, "search_id": search_id()})
        elif spec["mode"] == "detail":
            for ref in spec["refs"]:
                add_note(store, ref, "specified", spec)
        else:
            ref = spec["refs"][0]
            store.enqueue(f"creator:{ref['id']}:", "creator", {"ref": ref, "cursor": "", "page": 1})
    atomic_json(store.root / "run.json", {"schema": 1, "created_at": store.meta("created_at"),
        "mode": spec["mode"], "keywords": spec.get("keywords", []),
        "max_notes": spec["max_notes"], "max_comments": spec["max_comments"],
        "sub_comments": spec["sub_comments"], "max_pages": spec["max_pages"], "sort": spec["sort"],
        "download_images": spec.get("download_images", False)})


def add_note(store, ref, source, spec):
    if not re.fullmatch(r"[0-9a-fA-F]{24}", str(ref.get("id", ""))):
        return False
    exists = store.db.execute("SELECT 1 FROM tasks WHERE key=?", (f"note:{ref['id']}",)).fetchone()
    if not exists and store.note_slots() >= spec["max_notes"]:
        return False
    store.db.execute("INSERT OR IGNORE INTO sources VALUES (?,?)", (ref["id"], source))
    if not ref.get("token"):
        store.event("missing_token", "Discovery item lacks xsec_token; item skipped")
        return False
    return store.enqueue(f"note:{ref['id']}", "note", ref)


def put_comment(store, note_id, item, limit, parent=""):
    identifier = item.get("id")
    if not isinstance(identifier, str) or not identifier or store.comment_count(note_id) >= limit:
        return
    record = {"id": identifier, "note_id": note_id, "text": str(item.get("content") or ""),
              "parent_comment_id": parent or (item.get("target_comment") or {}).get("id", ""),
              "published_at": item.get("create_time"), "like_count": item.get("like_count", 0),
              "source_url": f"https://www.xiaohongshu.com/explore/{note_id}", "fetched_at": now()}
    store.db.execute("INSERT OR IGNORE INTO comments VALUES (?,?,?)",
                     (identifier, note_id, json.dumps(record, ensure_ascii=False)))


async def process(store, client, task, spec, image_downloader=None):
    kind, payload = task["kind"], task["payload"]
    if kind in {"search", "creator"}:
        if store.note_slots() >= spec["max_notes"]:
            return
        if kind == "search":
            data = await client.search(payload["keyword"], payload["page"], payload["search_id"], spec["sort"])
            items = data.get("items")
            source = "keyword:" + payload["keyword"]
        else:
            data = await client.creator(payload["ref"], payload["cursor"])
            items = data.get("notes")
            source = "creator:" + digest(payload["ref"]["id"])[:16]
        if not isinstance(items, list):
            raise CollectError("parse_error", "Discovery response does not contain a list")
        for item in items:
            if not isinstance(item, dict) or item.get("model_type") in {"rec_query", "hot_query"}:
                continue
            add_note(store, {"id": item.get("note_id") or item.get("id"),
                "token": item.get("xsec_token", ""), "source": item.get("xsec_source") or "pc_search"}, source, spec)
        # Always process items on the final page, even when has_more is false.
        if data.get("has_more") and store.note_slots() < spec["max_notes"]:
            if payload["page"] >= spec["max_pages"]:
                store.event("discovery_limit", "Source page limit reached", task["id"])
                return
            next_payload = {**payload, "page": payload["page"] + 1}
            if kind == "search":
                key = f"search:{payload['keyword']}:{next_payload['page']}"
            else:
                cursor = data.get("cursor", "")
                if not cursor or cursor == payload["cursor"]:
                    store.event("pagination_stalled", "Creator cursor did not advance", task["id"])
                    return
                next_payload["cursor"] = cursor
                key = f"creator:{payload['ref']['id']}:{cursor}"
            if not store.enqueue(key, kind, next_payload):
                store.event("pagination_stalled", "Discovery cursor repeated", task["id"])
    elif kind == "note":
        note, method = await client.note(payload)
        if not isinstance(note, dict) or (note.get("note_id") and note["note_id"] != payload["id"]):
            raise CollectError("parse_error", "Note response has an unexpected ID or structure")
        text = str(note.get("desc") or "").strip()
        title = str(note.get("title") or text[:80]).strip()
        if not (text or title or note.get("image_list") or note.get("video")):
            raise CollectError("empty_content", "Note contains no usable content")
        identifier = payload["id"]
        record = {"id": identifier, "note_id": identifier, "title": title, "text": text,
            "type": note.get("type", "normal"), "source_url": f"https://www.xiaohongshu.com/explore/{identifier}",
            "published_at": note.get("time"), "updated_at": note.get("last_update_time"), "fetched_at": now(),
            "tags": [item.get("name") for item in note.get("tag_list", []) if isinstance(item, dict) and item.get("type") == "topic"],
            "images": [item.get("url_default") or item.get("url") for item in note.get("image_list", []) if isinstance(item, dict)],
            "interactions": note.get("interact_info", {}), "extraction_method": method,
            "content_hash": digest(title + "\n" + text)}
        atomic_json(store.root / "snapshots" / f"{identifier}.json", redact(note))
        store.db.execute("INSERT OR REPLACE INTO notes VALUES (?,?)", (identifier, json.dumps(record, ensure_ascii=False)))
        if spec.get("download_images", False):
            store.enqueue_images(record)
        if spec["max_comments"]:
            store.enqueue(f"comments:{identifier}::", "comments", {"ref": payload, "cursor": "", "parent": ""})
    elif kind == "comments":
        ref, parent = payload["ref"], payload["parent"]
        if store.comment_count(ref["id"]) >= spec["max_comments"]:
            return
        data = await client.comments(ref, payload["cursor"], parent)
        items = data.get("comments")
        if not isinstance(items, list):
            raise CollectError("parse_error", "Comments response does not contain a list")
        for item in items:
            if not isinstance(item, dict):
                continue
            put_comment(store, ref["id"], item, spec["max_comments"], parent)
            if spec["sub_comments"] and not parent and item.get("id"):
                for child in item.get("sub_comments") or []:
                    if isinstance(child, dict):
                        put_comment(store, ref["id"], child, spec["max_comments"], item["id"])
                if item.get("sub_comment_has_more"):
                    cursor = item.get("sub_comment_cursor") or ""
                    store.enqueue(f"comments:{ref['id']}:{item['id']}:{cursor}", "comments",
                                  {"ref": ref, "cursor": cursor, "parent": item["id"]})
        if data.get("has_more") and store.comment_count(ref["id"]) < spec["max_comments"]:
            cursor = data.get("cursor") or ""
            if not cursor or cursor == payload["cursor"]:
                store.event("pagination_stalled", "Comment cursor did not advance", task["id"])
                return
            key = f"comments:{ref['id']}:{parent}:{cursor}"
            if not store.enqueue(key, "comments", {**payload, "cursor": cursor}):
                store.event("pagination_stalled", "Comment cursor repeated", task["id"])
    elif kind == "image":
        asset = await image_downloader.download(payload)
        store.db.execute("INSERT OR REPLACE INTO images VALUES (?,?,?,?,?,?)",
                         tuple(asset[key] for key in ("note_id", "position", "url", "path", "content_type", "size")))


async def collect(store, client, *, retry_failed=False, max_seconds=300, verify=True,
                  images_only=False, media_transport=None):
    spec = store.meta("spec")
    if spec is None:
        raise ValueError("Run has no collection specification")
    with store.db:
        if retry_failed:
            store.db.execute("UPDATE tasks SET status='pending',error=NULL WHERE status='failed'"
                             + (" AND kind='image'" if images_only else ""))
        store.set_meta("status", "running")
    task = None
    image_downloader = None
    try:
        async with asyncio.timeout(max_seconds):
            if verify and not images_only and store.db.execute(
                    "SELECT 1 FROM tasks WHERE status='pending' AND kind!='image' LIMIT 1").fetchone():
                await client.verify()
            while (task := store.next_task("image" if images_only else None)) is not None:
                try:
                    if task["kind"] == "image" and image_downloader is None:
                        image_downloader = ImageDownloader(store.root, client, transport=media_transport)
                    # Results, pagination position and task completion commit together.
                    with store.db:
                        await process(store, client, task, spec, image_downloader)
                        store.db.execute("UPDATE tasks SET status='done',error=NULL WHERE id=?", (task["id"],))
                except CollectError as exc:
                    with store.db:
                        store.event(exc.status, str(exc), task["id"], exc.retry_after)
                        if exc.status in HALT:
                            store.set_meta("status", exc.status)
                        else:
                            store.db.execute("UPDATE tasks SET status='failed',error=? WHERE id=?", (exc.status, task["id"]))
                    if exc.status in HALT:
                        break
            else:
                failed = store.db.execute("SELECT count(*) FROM tasks WHERE status='failed'").fetchone()[0]
                with store.db:
                    pending = store.db.execute("SELECT count(*) FROM tasks WHERE status='pending'").fetchone()[0]
                    store.set_meta("status", "partial" if failed else "ready" if pending else "completed")
    except CollectError as exc:
        with store.db:
            store.event(exc.status, str(exc), retry_after=exc.retry_after)
            store.set_meta("status", exc.status)
    except TimeoutError:
        with store.db:
            store.set_meta("status", "budget_exhausted")
            store.event("budget_exhausted", "Time limit reached; unfinished task remains pending")
    except asyncio.CancelledError:
        with store.db:
            store.set_meta("status", "interrupted")
            store.event("interrupted", "Interrupted; unfinished task remains pending")
        raise
    except Exception as exc:
        with store.db:
            store.set_meta("status", "internal_error")
            store.event("internal_error", f"Unexpected {type(exc).__name__}; unfinished task remains pending")
    finally:
        if image_downloader is not None:
            await image_downloader.http.aclose()
        with store.db:
            store.set_meta("requests_total", store.meta("requests_total", 0) + client.requests)
            store.set_meta("updated_at", now())
        store.export()
    return store.report()
