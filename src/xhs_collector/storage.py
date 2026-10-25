"""SQLite is the checkpoint; JSONL and Markdown are reproducible exports."""
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .common import CollectError, atomic_json, atomic_text, digest, now
from .markdown import render_note_markdown


@contextmanager
def run_lock(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise CollectError("run_locked", "Another process is using this run") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Store:
    def __init__(self, root, *, create=False):
        self.root = Path(root).resolve()
        path = self.root / "state.sqlite3"
        if not create and not path.exists():
            raise ValueError("Run not found (state.sqlite3 missing)")
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (
              id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
              payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', error TEXT);
            CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, record TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS comments (
              id TEXT PRIMARY KEY, note_id TEXT NOT NULL, record TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS images (
              note_id TEXT NOT NULL, position INTEGER NOT NULL, url TEXT NOT NULL,
              path TEXT NOT NULL, content_type TEXT NOT NULL, size INTEGER NOT NULL,
              PRIMARY KEY(note_id,position));
            CREATE TABLE IF NOT EXISTS sources (
              note_id TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY(note_id,source));
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY, time TEXT NOT NULL, status TEXT NOT NULL, task_id INTEGER,
              message TEXT NOT NULL, retry_after TEXT);
        """)

    def close(self):
        self.db.close()

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def enqueue(self, key, kind, payload):
        cursor = self.db.execute("INSERT OR IGNORE INTO tasks(key,kind,payload) VALUES (?,?,?)",
                                 (key, kind, json.dumps(payload, ensure_ascii=False)))
        return cursor.rowcount > 0

    def next_task(self, kind=None):
        query = "SELECT * FROM tasks WHERE status='pending'"
        row = self.db.execute(query + (" AND kind=?" if kind else "") + " ORDER BY id LIMIT 1",
                              (kind,) if kind else ()).fetchone()
        return dict(row, payload=json.loads(row["payload"])) if row else None

    def enqueue_images(self, note):
        for position, url in enumerate(note.get("images") or [], start=1):
            if isinstance(url, str) and url:
                key = f"image:{note['id']}:{position}"
                payload = {"note_id": note["id"], "position": position, "url": url}
                if not self.enqueue(key, "image", payload):
                    asset = self.db.execute("SELECT path,size FROM images WHERE note_id=? AND position=?",
                                            (note["id"], position)).fetchone()
                    path = (self.root / asset["path"]).resolve() if asset else None
                    valid = (path is not None and path.is_relative_to((self.root / "clean").resolve())
                             and path.is_file() and path.stat().st_size == asset["size"])
                    if not valid:
                        self.db.execute("DELETE FROM images WHERE note_id=? AND position=?", (note["id"], position))
                        self.db.execute("UPDATE tasks SET status='pending',error=NULL,payload=? WHERE key=? AND status='done'",
                                        (json.dumps(payload, ensure_ascii=False), key))

    def event(self, status, message, task_id=None, retry_after=None):
        self.db.execute("INSERT INTO events(time,status,task_id,message,retry_after) VALUES (?,?,?,?,?)",
                        (now(), status, task_id, message, retry_after))

    def note_slots(self):
        return self.db.execute("SELECT count(*) FROM tasks WHERE kind='note'").fetchone()[0]

    def comment_count(self, note_id):
        return self.db.execute("SELECT count(*) FROM comments WHERE note_id=?", (note_id,)).fetchone()[0]

    def records(self, table):
        if table not in {"notes", "comments"}:
            raise ValueError("Unsupported table")
        for row in self.db.execute(f"SELECT record FROM {table} ORDER BY id"):
            record = json.loads(row[0])
            if table == "notes":
                files = []
                for asset in self.db.execute("SELECT * FROM images WHERE note_id=? ORDER BY position", (record["id"],)):
                    path = (self.root / asset["path"]).resolve()
                    if path.is_relative_to((self.root / "clean").resolve()) and path.is_file() and path.stat().st_size == asset["size"]:
                        files.append(dict(asset))
                if files:
                    record["image_files"] = files
            yield record

    def report(self):
        counts = {table: self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("notes", "comments")}
        tasks = dict(self.db.execute("SELECT status,count(*) FROM tasks GROUP BY status"))
        failures = dict(self.db.execute("SELECT error,count(*) FROM tasks WHERE status='failed' GROUP BY error"))
        warnings = dict(self.db.execute("SELECT status,count(*) FROM events WHERE status IN ('discovery_limit','pagination_stalled','missing_token') GROUP BY status"))
        last = self.db.execute("SELECT time,status,message,retry_after FROM events ORDER BY id DESC LIMIT 1").fetchone()
        image_tasks = dict(self.db.execute("SELECT status,count(*) FROM tasks WHERE kind='image' GROUP BY status"))
        return {"schema": 1, "run": str(self.root), "status": self.meta("status", "ready"),
                "data_origin": self.meta("data_origin", "platform"),
                **counts, "tasks": tasks, "failures": failures, "warnings": warnings,
                "requests_total": self.meta("requests_total", 0),
                "discovered_notes": self.note_slots(), "updated_at": self.meta("updated_at"),
                "last_event": dict(last) if last else None,
                "images": {"downloaded": self.db.execute("SELECT count(*) FROM images").fetchone()[0],
                           "pending": image_tasks.get("pending", 0), "failed": image_tasks.get("failed", 0)},
                "resume_available": bool(tasks.get("pending") or tasks.get("failed")),
                "artifacts": ["notes.jsonl", "comments.jsonl", "documents.jsonl", "clean/", "report.json"]}

    def export(self):
        documents = []
        for table in ("notes", "comments"):
            records = list(self.records(table))
            atomic_text(self.root / f"{table}.jsonl", "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records))
            for row in records:
                is_note = table == "notes"
                title = row["title"] if is_note else "评论"
                text = f"{title}\n\n{row['text']}" if is_note else row["text"]
                doc_id = ("xhs:note:" if is_note else "xhs:comment:") + row["id"]
                documents.append({"schema": 1, "doc_id": doc_id, "kind": "note" if is_note else "comment",
                    "note_id": row["note_id"], "title": title, "text": text,
                    "source_url": row["source_url"], "fetched_at": row["fetched_at"],
                    "content_hash": digest(text), "parent_comment_id": row.get("parent_comment_id", "")})
                if is_note:
                    atomic_text(self.root / "clean" / f"{row['id']}.md",
                                render_note_markdown(row))
        atomic_text(self.root / "documents.jsonl", "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in documents))
        report = self.report()
        atomic_json(self.root / "report.json", report)
        return report

    def show(self, doc_id, offset, limit):
        table = "comments" if doc_id.startswith("xhs:comment:") else "notes"
        identifier = doc_id.rsplit(":", 1)[-1]
        row = self.db.execute(f"SELECT record FROM {table} WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise ValueError("Document not found")
        record = json.loads(row[0])
        text = record.pop("text")
        return {**record, "text": text[offset:offset + limit], "offset": offset,
                "total_chars": len(text), "next_offset": offset + limit if offset + limit < len(text) else None}
