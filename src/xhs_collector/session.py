"""Explicit login in a dedicated browser profile, never in an unrelated profile."""
import asyncio
import json
import os
import platform
import sys
from importlib.metadata import version
from pathlib import Path

from .api import WEB_HOST, XhsClient
from .common import CollectError, atomic_json, now
from .storage import run_lock


def state_dir():
    if os.environ.get("XHS_COLLECTOR_HOME"):
        return Path(os.environ["XHS_COLLECTOR_HOME"]).expanduser().resolve()
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return base / "xhs-collector"


def load_session():
    path = state_dir() / "session.json"
    if not path.exists():
        raise CollectError("auth_required", "No local session; run login and complete browser sign-in")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("cookie"), str) or not data.get("user_agent"):
            raise ValueError()
        values = dict(piece.strip().split("=", 1) for piece in data["cookie"].split(";") if "=" in piece)
        if not values.get("web_session") or not values.get("a1"):
            raise ValueError()
        return data
    except (ValueError, KeyError, TypeError) as exc:
        raise CollectError("auth_required", "Session file is invalid; run login again") from exc


def browser_channel(requested):
    if requested != "auto":
        return None if requested == "chromium" else requested
    if platform.system() == "Windows":
        candidates = [(Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe", "chrome"),
                      (Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe", "msedge")]
        for path, channel in candidates:
            if path.exists():
                return channel
    return None


async def login(browser="auto", timeout=180):
    from playwright.async_api import async_playwright, Error as PlaywrightError
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    with run_lock(root / "login-lock"):
        try:
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    str(root / "browser-profile"), channel=browser_channel(browser), headless=False)
                try:
                    page = await context.new_page()
                    await page.goto(WEB_HOST, wait_until="domcontentloaded", timeout=45000)
                    # Do not trigger SMS or perform account-changing actions. The user signs in.
                    deadline = asyncio.get_running_loop().time() + timeout
                    while asyncio.get_running_loop().time() < deadline:
                        cookies = await context.cookies([WEB_HOST])
                        by_name = {item["name"]: item["value"] for item in cookies}
                        if by_name.get("web_session") and by_name.get("a1"):
                            data = {"cookie": "; ".join(f"{key}={value}" for key, value in by_name.items()),
                                    "user_agent": await page.evaluate("navigator.userAgent"), "saved_at": now()}
                            async with XhsClient(data, max_requests=3, max_seconds=15) as client:
                                try:
                                    await client.verify()
                                except CollectError as exc:
                                    if exc.status in {"blocked", "rate_limited", "challenge"}:
                                        raise
                                else:
                                    path = root / "session.json"
                                    atomic_json(path, data)
                                    os.chmod(path, 0o600)
                                    return {"status": "authenticated", "saved_at": data["saved_at"], "state_dir": str(root)}
                        await asyncio.sleep(5)
                    raise CollectError("auth_required", "Login timed out; run login again to continue in the same profile")
                finally:
                    await context.close()
        except PlaywrightError as exc:
            raise CollectError("browser_error", "Browser could not open or was closed. Use doctor --browser-check and select chrome/msedge/chromium.") from exc


async def doctor(browser="auto", *, browser_check=False, online=False):
    info = {"status": "ok", "version": version("xhs-collector"),
            "runtime_python": sys.executable, "package_path": str(Path(__file__).resolve().parent),
            "dependencies": {name: version(name) for name in ("httpx", "playwright", "xhshow")},
            "state_dir": str(state_dir()), "browser_channel": browser_channel(browser) or "chromium",
            "session_present": (state_dir() / "session.json").exists(), "authentication": "not_checked"}
    if browser_check:
        from playwright.async_api import async_playwright, Error as PlaywrightError
        try:
            async with async_playwright() as playwright:
                instance = await playwright.chromium.launch(channel=browser_channel(browser), headless=True)
                try:
                    page = await instance.new_page()
                    await page.set_content("<title>xhs-collector local check</title><main>ready</main>")
                    info["browser_check"] = await page.title() == "xhs-collector local check"
                finally:
                    await instance.close()
        except PlaywrightError:
            info.update(status="browser_missing", browser_check=False)
    if online:
        try:
            async with XhsClient(load_session(), max_requests=3, max_seconds=30) as client:
                await client.verify()
            info["authentication"] = "valid"
        except CollectError as exc:
            info.update(status=exc.status, authentication=exc.status)
    return info
