"""The one thread allowed to touch the browser.

Playwright's sync API is thread-affine: objects created on one thread cannot be
used from another, and the HTTP server is inherently multi-threaded.  So every
handler and the watcher submit a callable here and block on the result, and
this thread does the driving.  That also serialises Teams itself -- a read and
a send racing on the same DOM would fight over which chat is open.

The CDP attachment is kept open between jobs (reconnecting costs ~1-2 s) but is
torn down and rebuilt whenever a job fails, so a browser the user closed, or a
`shutdown` followed by a `launch`, heals on the next request instead of
poisoning the process.
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
from pathlib import Path

from .browser import (PROFILE_DIR, _pick_page, cdp_version, ensure_window_size,
                      launch_browser, profile_has_session)
from .teams import TEAMS_URL, Teams, TeamsError


class BrowserUnavailable(RuntimeError):
    """No browser to attach to, and we may not (or cannot) start one."""


class _Job:
    __slots__ = ("fn", "needs_teams", "done", "value", "error")

    def __init__(self, fn, needs_teams=True):
        self.fn = fn
        self.needs_teams = needs_teams
        self.done = threading.Event()
        self.value = None
        self.error = None


class BrowserWorker(threading.Thread):
    def __init__(self, cfg: dict):
        super().__init__(name="browser", daemon=True)
        b = cfg.get("browser", {})
        self.mode = b.get("mode", "cdp")
        self.port = int(b.get("port", 9223))
        self.profile = Path(b.get("profile") or PROFILE_DIR)
        self.prefer = b.get("prefer", "chrome")
        self.headless = bool(b.get("headless", False))
        self.minimized = bool(b.get("minimized", True))
        self.autostart = bool(b.get("autostart", True))
        self.idle_close = float(b.get("idle_close", 0) or 0)

        self._q = queue.Queue()
        self._stop = threading.Event()
        self._pw = None
        self._browser = None
        self._page = None
        self._last_used = 0.0
        self.stats = {"jobs": 0, "failures": 0, "reconnects": 0, "last_error": None}

    # --- public API ------------------------------------------------------
    def submit(self, fn, timeout: float = 120.0, needs_teams: bool = True):
        """Run `fn(teams)` on the browser thread and return its value.

        Re-raises whatever `fn` raised, so handlers can map TeamsError to HTTP
        409 the same way the CLI maps it to exit code 3.
        """
        if not self.is_alive():
            raise BrowserUnavailable("browser worker is not running")
        job = _Job(fn, needs_teams)
        self._q.put(job)
        if not job.done.wait(timeout):
            raise TimeoutError("browser job did not finish within %ss" % timeout)
        if job.error is not None:
            raise job.error
        return job.value

    def stop(self):
        self._stop.set()
        self._q.put(_Job(lambda _: None, needs_teams=False))

    def running(self) -> bool:
        """Is a debuggable browser listening? Cheap -- no Playwright involved."""
        return bool(cdp_version(self.port))

    def describe(self) -> dict:
        return {
            "mode": self.mode,
            "port": self.port,
            "profile": str(self.profile),
            "headless": self.headless,
            "attached": self._page is not None,
            "browser_running": self.running(),
            "profile_signed_in_once": profile_has_session(self.profile),
            "stats": dict(self.stats),
        }

    # --- thread body -----------------------------------------------------
    def run(self):
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=1.0)
            except queue.Empty:
                self._maybe_idle_close()
                continue
            if self._stop.is_set():
                job.done.set()
                break
            self._run_job(job)
        self._teardown()

    def _run_job(self, job: _Job):
        self.stats["jobs"] += 1
        try:
            job.value = self._invoke(job)
        except (TeamsError, BrowserUnavailable) as e:
            # Expected, caller-facing failures: do not churn the connection.
            job.error = e
            self.stats["failures"] += 1
            self.stats["last_error"] = str(e)
        except Exception as e:
            # Anything else may mean the attachment is dead -- drop it and give
            # the job exactly one more chance on a fresh connection.
            self.stats["last_error"] = "%s: %s" % (type(e).__name__, e)
            self._teardown()
            self.stats["reconnects"] += 1
            try:
                job.value = self._invoke(job)
            except Exception as e2:
                job.error = e2
                job.value = None
                self.stats["failures"] += 1
                self.stats["last_error"] = "%s: %s | %s" % (
                    type(e2).__name__, e2, traceback.format_exc(limit=2))
                self._teardown()
        finally:
            self._last_used = time.time()
            job.done.set()

    def _invoke(self, job: _Job):
        if not job.needs_teams:
            return job.fn(None)
        return job.fn(Teams(self._ensure_page()))

    # --- connection ------------------------------------------------------
    def _ensure_page(self):
        if self._page is not None:
            try:
                self._page.evaluate("() => 1")   # liveness probe, ~1 ms
                return self._page
            except Exception:
                self._teardown()

        if self.mode == "cdp" and not cdp_version(self.port):
            if not self.autostart:
                raise BrowserUnavailable(
                    "nothing listening on CDP port %d and autostart is off; "
                    "POST /launch or run `teams launch`" % self.port)
            if self.headless and not profile_has_session(self.profile):
                raise BrowserUnavailable(
                    "profile %s has never been signed in, so a headless launch "
                    "would sit on the login page; run `teams launch` with a "
                    "window and sign in once" % self.profile)
            launch_browser(self.port, self.profile, self.prefer,
                           headless=self.headless, minimized=self.minimized)

        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        if self.mode == "cdp":
            self._browser = self._pw.chromium.connect_over_cdp(
                "http://127.0.0.1:%d" % self.port)
            ctx = (self._browser.contexts[0] if self._browser.contexts
                   else self._browser.new_context())
        else:
            self.profile.mkdir(parents=True, exist_ok=True)
            ctx = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(self.profile), headless=self.headless,
                args=["--no-first-run", "--no-default-browser-check"])
            self._browser = ctx
        self._page = _pick_page(ctx, "teams")
        ensure_window_size(self._page)
        if "teams." not in self._page.url:
            Teams(self._page).open(TEAMS_URL)
        return self._page

    def _maybe_idle_close(self):
        if not self.idle_close or self._page is None:
            return
        if time.time() - self._last_used > self.idle_close:
            self._teardown()

    def _teardown(self):
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw = self._browser = self._page = None

    # --- convenience wrappers used by the HTTP layer ---------------------
    def launch(self, headless=None, minimized=None) -> dict:
        headless = self.headless if headless is None else bool(headless)
        minimized = self.minimized if minimized is None else bool(minimized)
        if headless and not profile_has_session(self.profile):
            raise BrowserUnavailable(
                "profile %s has never been signed in; launch with a window "
                "once and sign in" % self.profile)
        info = launch_browser(self.port, self.profile, self.prefer,
                              headless=headless, minimized=minimized)
        return {"launched": True, "browser": info.get("Browser"),
                "port": self.port, "headless": headless, "minimized": minimized}

    def shutdown_browser(self) -> dict:
        if not cdp_version(self.port):
            return {"closed": False,
                    "detail": "nothing running on port %d" % self.port}

        def kill(t):
            cdp = t.page.context.new_cdp_session(t.page)
            cdp.send("Browser.close")
            return True

        try:
            self.submit(kill, timeout=30)
        except Exception:
            pass
        # Drop our now-dangling attachment (on the browser thread, the only one
        # allowed to close Playwright objects) so the next job reconnects.
        self.submit(lambda _: self._teardown(), needs_teams=False, timeout=30)
        return {"closed": True, "detail": "session persists in %s" % self.profile}
