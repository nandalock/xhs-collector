"""Small JSON output for agents; bulk data always goes to disk."""
import argparse
import asyncio
import json
import sys
import secrets
from datetime import datetime
from pathlib import Path

from .api import XhsClient
from .collector import collect, initialize
from .common import CollectError, parse_link
from .session import doctor, load_session, login
from .storage import Store, run_lock


def bounded(low, high):
    def parse(text):
        value = int(text)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"Must be between {low} and {high}")
        return value
    return parse


def default_run_dir(command):
    root = Path.home() / "Documents" / "xhs-collector" / "runs"
    return root / f"{command}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def parser():
    app = argparse.ArgumentParser(description="Standalone Xiaohongshu collector for non-commercial learning")
    sub = app.add_subparsers(dest="command", required=True)
    check = sub.add_parser("doctor", help="Check dependencies, browser and optional login validity")
    check.add_argument("--browser-check", action="store_true")
    check.add_argument("--online", action="store_true")
    auth = sub.add_parser("login", help="Open a dedicated browser; complete login manually")
    auth.add_argument("--timeout", type=bounded(15, 600), default=180)
    for child in (check, auth):
        child.add_argument("--browser", choices=["auto", "chrome", "msedge", "chromium"], default="auto")
    for name in ("search", "detail", "creator", "resume", "download-images"):
        child = sub.add_parser(name)
        child.add_argument("--run", required=name in {"resume", "download-images"}, type=Path,
                           help="Output directory; new runs default to ~/Documents/xhs-collector/runs/")
        child.add_argument("--max-requests", type=bounded(1, 1000), default=100,
                           help="Per invocation, includes retries and auth checks")
        child.add_argument("--max-seconds", type=bounded(1, 3600), default=300)
        child.add_argument("--delay", type=bounded(1, 60), default=2)
        if name in {"resume", "download-images"}:
            child.add_argument("--retry-failed", action="store_true")
            continue
        child.add_argument("--download-images", action=argparse.BooleanOptionalAction, default=True,
                           help="Save images in clean/images beside Markdown (default: enabled)")
        child.add_argument("--max-notes", type=bounded(1, 100), default=10)
        child.add_argument("--max-comments", type=bounded(0, 100), default=0,
                           help="Total comments per note, including replies")
        child.add_argument("--sub-comments", action="store_true")
        child.add_argument("--max-pages", type=bounded(1, 50), default=5)
        child.add_argument("--sort", choices=["general", "popularity_descending", "time_descending"], default="general")
        if name == "search":
            child.add_argument("--keyword", required=True, action="append")
        else:
            child.add_argument("--url", required=True, action="append" if name == "detail" else "store")
    for name in ("report", "export", "show"):
        child = sub.add_parser(name)
        child.add_argument("--run", required=True, type=Path)
        if name == "show":
            child.add_argument("--doc", required=True)
            child.add_argument("--offset", type=bounded(0, 10000000), default=0)
            child.add_argument("--max-chars", type=bounded(1, 8000), default=3000)
    fixture = sub.add_parser("demo", help="Offline fixture demo, not real platform data")
    fixture.add_argument("--run", type=Path)
    return app


async def execute(args):
    if args.command == "doctor":
        return await doctor(args.browser, browser_check=args.browser_check, online=args.online)
    if args.command == "login":
        return await login(args.browser, args.timeout)
    if args.run is None:
        args.run = default_run_dir(args.command)
    if args.command == "demo":
        from .demo import run_demo
        return await run_demo(args.run)
    is_new = args.command in {"search", "detail", "creator"}
    if not is_new and not (args.run / "state.sqlite3").exists():
        raise ValueError("Run not found")
    with run_lock(args.run):
        store = Store(args.run, create=is_new)
        try:
            if args.command == "report":
                return store.report()
            if args.command == "show":
                return store.show(args.doc, args.offset, args.max_chars)
            if args.command == "export":
                return store.export()
            if args.command == "download-images":
                with store.db:
                    for note in store.records("notes"):
                        store.enqueue_images(note)
            if is_new:
                refs = [] if args.command == "search" else [parse_link(url, "creator" if args.command == "creator" else "note")
                            for url in ([args.url] if args.command == "creator" else args.url)]
                keywords = list(dict.fromkeys(k.strip() for k in getattr(args, "keyword", []) if k.strip()))
                if args.command == "search" and not keywords:
                    raise ValueError("At least one non-empty keyword is required")
                initialize(store, {"mode": args.command, "refs": refs, "keywords": keywords,
                    "max_notes": args.max_notes, "max_comments": args.max_comments,
                    "sub_comments": args.sub_comments, "max_pages": args.max_pages, "sort": args.sort,
                    "download_images": args.download_images})
            needs_auth = args.command != "download-images" and store.db.execute(
                "SELECT 1 FROM tasks WHERE kind!='image' AND (status='pending' OR (status='failed' AND ?)) LIMIT 1",
                (getattr(args, "retry_failed", False),)).fetchone()
            session = {"cookie": "", "user_agent": "Mozilla/5.0 xhs-collector"}
            if needs_auth:
                try:
                    session = load_session()
                except CollectError as exc:
                    with store.db:
                        store.set_meta("status", exc.status)
                        store.event(exc.status, str(exc))
                    return store.export()
            async with XhsClient(session, delay=args.delay, max_requests=args.max_requests,
                                 max_seconds=args.max_seconds) as client:
                return await collect(store, client, retry_failed=getattr(args, "retry_failed", False),
                                     max_seconds=args.max_seconds, images_only=args.command == "download-images")
        finally:
            store.close()


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        result = asyncio.run(execute(args))
        print(json.dumps(result, ensure_ascii=False))
        status = result.get("status", "ok")
        return 0 if status in {"ok", "completed", "authenticated"} else 2
    except CollectError as exc:
        print(json.dumps({"status": exc.status, "message": str(exc), "retry_after": exc.retry_after}, ensure_ascii=False))
        return 2
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "invalid_input", "message": str(exc)}, ensure_ascii=False))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted", "message": "Checkpoint saved for active collection; use resume"}))
        return 130
    except Exception as exc:
        print(json.dumps({"status": "internal_error", "message": type(exc).__name__}))
        return 1

