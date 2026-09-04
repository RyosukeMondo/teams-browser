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

# Where an installed Chrome/Edge lives, per platform.  Order matters: the
# first hit wins, so the system-wide install is preferred over a per-user one.
_CANDIDATES = {
    "win32": {
        "chrome": [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ],
        "edge": [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ],
    },
    "linux": {
        "chrome": [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/opt/google/chrome/chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ],
        "edge": [
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            "/opt/microsoft/msedge/msedge",
        ],
    },
    "darwin": {
        "chrome": [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser(
                "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ],
        "edge": [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ],
    },
}
# Names to try on PATH when no known install path matched.  Chromium counts:
# Teams web only refuses Firefox, and any Chromium build drives it fine.
_ON_PATH = {
    "chrome": ["google-chrome", "google-chrome-stable", "chrome",
               "chromium", "chromium-browser"],
    "edge": ["microsoft-edge", "microsoft-edge-stable", "msedge"],
}


def _platform_key() -> str:
    if sys.platform == "win32":
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"          # every other POSIX behaves the same way here


# Kept as module-level names because the CLI and tests refer to them.
CHROME_CANDIDATES = _CANDIDATES[_platform_key()]["chrome"]
EDGE_CANDIDATES = _CANDIDATES[_platform_key()]["edge"]


def find_browser(prefer: str = "chrome") -> str:
    order = ["chrome", "edge"] if prefer != "edge" else ["edge", "chrome"]
    fam = _CANDIDATES[_platform_key()]
    for kind in order:
        for path in fam.get(kind, ()):
            if path and Path(path).exists():
                return path
    for kind in order:
        for name in _ON_PATH[kind]:
            found = shutil.which(name)
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


def profile_has_session(profile: Path = PROFILE_DIR) -> bool:
    """Rough check that *someone has signed in with a visible window* before.

    Headless Chrome cannot show a sign-in form the user can type into, so a
    headless launch on a never-used profile would just sit on a login page.
    A cookie store of any real size means a session was established once.
    """
    for rel in ("Default/Network/Cookies", "Default/Cookies"):
        f = profile / rel
        if f.exists() and f.stat().st_size > 20000:
            return True
    return False


def launch_browser(port: int = DEFAULT_PORT, profile: Path = PROFILE_DIR,
                   prefer: str = "chrome", browser_path: str | None = None,
                   url: str | None = None, wait: float = 30.0,
                   headless: bool = False, minimized: bool = False) -> dict:
    """Start a debuggable browser on our own profile (no-op if already up).

    `headless` needs a profile that has already been signed in interactively --
    see profile_has_session(); the caller is expected to have checked.
    """
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
    if headless:
        # Headless still needs a real viewport: Teams virtualises its lists.
        args += ["--headless=new", "--window-size=1440,960"]
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
            if minimized and not headless:
                _minimize_first_window(port)
            return info
        time.sleep(0.4)
    raise RuntimeError(f"Browser did not expose CDP on port {port} within {wait}s")


def _minimize_first_window(port: int):
    """Best-effort: tuck the freshly launched window away."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0] if browser.contexts else None
            if ctx and ctx.pages:
                set_window_state(ctx.pages[0], "minimized")
            browser.close()
    except Exception:
        pass


def window_state(page):
    """'normal' | 'minimized' | 'maximized' | 'fullscreen', or None if unknown."""
    try:
        cdp = page.context.new_cdp_session(page)
        wid = cdp.send("Browser.getWindowForTarget")["windowId"]
        return cdp.send("Browser.getWindowBounds", {"windowId": wid})["bounds"]["windowState"]
    except Exception:
        return None


def set_window_state(page, state: str = "minimized") -> bool:
    try:
        cdp = page.context.new_cdp_session(page)
        wid = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send("Browser.setWindowBounds",
                 {"windowId": wid, "bounds": {"windowState": state}})
        return True
    except Exception:
        return False


def ensure_window_size(page, width: int = 1440, height: int = 960):
    """Teams virtualises its lists, so a small window renders few messages.

    A minimized window is left alone: it keeps rendering at its last size, and
    resizing it would yank it back onto the user's desktop mid-task.
    """
    if window_state(page) == "minimized":
        return "minimized"
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
