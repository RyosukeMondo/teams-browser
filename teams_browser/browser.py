"""Browser connection layer.

Two ways to get a Playwright page pointed at Teams:

* ``cdp`` (default) -- attach to a real Chrome/Edge running with
  ``--remote-debugging-port``.  We launch it ourselves on a dedicated profile
  the first time, so the user can log in (incl. MFA) once by hand and the
  session survives between runs.  Because it is an ordinary browser window the
  user can watch and take over at any time.
* ``persistent`` -- let Playwright own a persistent context.  Handy for
  headless/unattended runs once the profile is already logged in.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROFILE_DIR = Path(os.environ.get("TEAMS_BROWSER_PROFILE", REPO / "profile"))
DEFAULT_PORT = int(os.environ.get("TEAMS_BROWSER_PORT", "9223"))

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_browser(prefer: str = "chrome") -> str:
    order = CHROME_CANDIDATES + EDGE_CANDIDATES
    if prefer == "edge":
        order = EDGE_CANDIDATES + CHROME_CANDIDATES
    for path in order:
        if path and Path(path).exists():
            return path
    found = shutil.which("chrome") or shutil.which("msedge")
    if found:
        return found
    raise RuntimeError("No Chrome or Edge found; install one or pass --browser-path")


def cdp_version(port: int, timeout: float = 1.0):
    """Return the /json/version payload if something is listening, else None."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


def launch_browser(port: int = DEFAULT_PORT, profile: Path = PROFILE_DIR,
                   prefer: str = "chrome", browser_path: str | None = None,
                   url: str | None = None, wait: float = 30.0) -> dict:
    """Start a debuggable browser on our own profile (no-op if already up)."""
    info = cdp_version(port)
    if info:
        return info

    exe = browser_path or find_browser(prefer)
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        "--remote-allow-origins=*",
    ]
    if url:
        args.append(url)

    creation = 0
    if sys.platform == "win32":
        creation = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(args, creationflags=creation, close_fds=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.time() + wait
    while time.time() < deadline:
        info = cdp_version(port)
        if info:
            return info
        time.sleep(0.4)
    raise RuntimeError(f"Browser did not expose CDP on port {port} within {wait}s")


def ensure_window_size(page, width: int = 1440, height: int = 960):
    """Teams virtualises its lists, so a small window renders few messages."""
    try:
        vp = page.evaluate("() => [window.outerWidth, window.outerHeight]")
    except Exception:
        return None
    if vp and vp[0] >= width - 40 and vp[1] >= height - 40:
        return vp
    try:  # a CDP-attached page has a real OS window, so resize that
        cdp = page.context.new_cdp_session(page)
        target = cdp.send("Browser.getWindowForTarget")
        cdp.send("Browser.setWindowBounds", {
            "windowId": target["windowId"],
            "bounds": {"windowState": "normal", "width": width, "height": height},
        })
    except Exception:
        try:
            page.set_viewport_size({"width": width, "height": height})
        except Exception:
            return vp
    page.wait_for_timeout(400)
    try:
        return page.evaluate("() => [window.outerWidth, window.outerHeight]")
    except Exception:
        return vp


def _pick_page(context, url_hint: str = "teams"):
    """Prefer an already-open Teams tab, else the first/blank tab, else new."""
    pages = [p for p in context.pages if not p.url.startswith("devtools://")]
    for p in pages:
        if url_hint in p.url:
            return p
    for p in pages:
        if p.url in ("about:blank", "chrome://newtab/", ""):
            return p
    return pages[0] if pages else context.new_page()


@contextmanager
def session(mode: str = "cdp", port: int = DEFAULT_PORT, profile: Path = PROFILE_DIR,
            headless: bool = False, prefer: str = "chrome",
            browser_path: str | None = None, channel: str | None = None,
            autostart: bool = True, url_hint: str = "teams"):
    """Yield a Playwright ``Page`` ready to drive Teams."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        if mode == "cdp":
            if not cdp_version(port):
                if not autostart:
                    raise RuntimeError(
                        f"Nothing listening on CDP port {port}. Run `teams launch` first.")
                launch_browser(port, profile, prefer, browser_path)
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = _pick_page(context, url_hint)
            ensure_window_size(page)
            try:
                yield page
            finally:
                browser.close()  # detaches CDP; the real browser keeps running
        elif mode == "persistent":
            profile.mkdir(parents=True, exist_ok=True)
            kwargs = dict(user_data_dir=str(profile), headless=headless,
                          args=["--no-first-run", "--no-default-browser-check"])
            if channel:
                kwargs["channel"] = channel
            elif browser_path:
                kwargs["executable_path"] = browser_path
            context = pw.chromium.launch_persistent_context(**kwargs)
            page = _pick_page(context, url_hint)
            try:
                yield page
            finally:
                context.close()
        else:
            raise ValueError(f"unknown mode: {mode!r}")
