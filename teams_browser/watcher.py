"""Turn watched Teams DMs into a queue of jobs for a Claude Code session.

The *server* does the polling, not the agent.  A Claude session that polled
Teams itself would spend tokens on every empty tick; here the browser poll is
free and the session blocks on `GET /mentions/next?wait=60`, which returns only
when a real message arrives.

State is persisted, so a restart neither replays old messages nor drops
mentions that were queued but not answered.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import STATE_PATH
from .teams import TeamsError

PENDING, CLAIMED, ANSWERED, FAILED = "pending", "claimed", "answered", "failed"
MAX_KEPT = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "chat"


def newer(a, b) -> bool:
    """Is message id `a` newer than `b`?

    Teams ids are epoch-millisecond strings, so they sort numerically; fall
    back to string order if a redesign ever changes their shape.
    """
    if b is None:
        return True
    if a is None:
        return False
    if str(a).isdigit() and str(b).isdigit():
        return int(a) > int(b)
    return str(a) > str(b)


class MentionStore:
    """Durable queue of anchor hits, plus the read cursor per watched chat."""

    def __init__(self, path: Path = None):
        self.path = Path(path or STATE_PATH)
        self.lock = threading.Lock()
        self.arrived = threading.Condition(self.lock)
        self.mentions = []          # newest last
        self.cursors = {}           # chat -> last message id seen
        self.log = []               # recent activity, for GET /events
        self._load()

    # --- persistence -----------------------------------------------------
    def _load(self):
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self.mentions = raw.get("mentions", [])
        self.cursors = raw.get("cursors", {})
        # A session that died mid-job left its mention claimed forever;
        # nothing is working on it now, so put it back on the queue.
        for m in self.mentions:
            if m.get("state") == CLAIMED:
                m["state"] = PENDING
                m["claimed_by"] = None

    def _save_locked(self):
        self.mentions = self.mentions[-MAX_KEPT:]
        tmp = self.path.with_suffix(".tmp")
        payload = {"mentions": self.mentions, "cursors": self.cursors,
                   "saved": _now()}
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def event(self, kind: str, detail: str):
        with self.lock:
            self.log.append({"at": _now(), "kind": kind, "detail": detail})
            self.log = self.log[-200:]

    # --- writing ---------------------------------------------------------
    def add(self, mention: dict) -> bool:
        with self.arrived:
            if any(m["id"] == mention["id"] for m in self.mentions):
                return False
            self.mentions.append(mention)
            self._save_locked()
            self.arrived.notify_all()
            return True

    def set_cursor(self, chat: str, last_id):
        with self.lock:
            self.cursors[chat] = last_id
            self._save_locked()

    def cursor(self, chat: str):
        with self.lock:
            return self.cursors.get(chat)

    # --- reading ---------------------------------------------------------
    def list(self, state=None, chat=None, limit=50, since=None):
        with self.lock:
            out = list(self.mentions)
        if state:
            wanted = set(state.split(",")) if isinstance(state, str) else set(state)
            out = [m for m in out if m["state"] in wanted]
        if chat:
            low = chat.lower()
            out = [m for m in out if low in (m.get("chat") or "").lower()]
        if since:
            out = [m for m in out if m["id"] > since]
        return out[-limit:]

    def get(self, mid: str):
        with self.lock:
            return next((m for m in self.mentions if m["id"] == mid), None)

    def claim(self, worker: str = "claude", chat=None, wait: float = 0.0):
        """Hand the oldest pending mention to exactly one caller.

        Blocks up to `wait` seconds -- this is what makes a Claude session cheap
        to keep listening: no polling, no tokens, until something shows up.
        """
        deadline = time.time() + max(0.0, wait)
        with self.arrived:
            while True:
                for m in self.mentions:
                    if m["state"] != PENDING:
                        continue
                    if chat and chat.lower() not in (m.get("chat") or "").lower():
                        continue
                    m["state"] = CLAIMED
                    m["claimed_by"] = worker
                    m["claimed_at"] = _now()
                    self._save_locked()
                    return m
                left = deadline - time.time()
                if left <= 0:
                    return None
                self.arrived.wait(min(left, 5.0))

    def finish(self, mid: str, state: str, **fields):
        with self.lock:
            m = next((x for x in self.mentions if x["id"] == mid), None)
            if m is None:
                return None
            m["state"] = state
            m["finished_at"] = _now()
            m.update(fields)
            self._save_locked()
            return m

    def release(self, mid: str):
        return self.finish(mid, PENDING, claimed_by=None)

    def counts(self) -> dict:
        with self.lock:
            out = {PENDING: 0, CLAIMED: 0, ANSWERED: 0, FAILED: 0}
            for m in self.mentions:
                out[m["state"]] = out.get(m["state"], 0) + 1
            out["total"] = len(self.mentions)
            return out


def match_anchor(text: str, anchor: str):
    """(matched, body) -- body is the message with the anchor stripped.

    An empty anchor means every incoming message counts, which is what you
    want for a DM dedicated to the agent.
    """
    text = text or ""
    if not anchor:
        return True, text.strip()
    idx = text.lower().find(anchor.lower())
    if idx < 0:
        return False, ""
    # Close the gap the anchor left behind without touching whitespace
    # elsewhere -- a multi-line message keeps its shape.
    head, tail = text[:idx].rstrip(), text[idx + len(anchor):].lstrip()
    return True, ("%s %s" % (head, tail)).strip() if head else tail.strip()


class Watcher(threading.Thread):
    """Polls each watched chat on an interval and files new anchor hits."""

    def __init__(self, worker, store: MentionStore, cfg: dict):
        super().__init__(name="watcher", daemon=True)
        self.worker = worker
        self.store = store
        self.cfg = cfg
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.last_poll = None
        self.last_error = None
        self.polls = 0

    def stop(self):
        self._stop.set()
        self._wake.set()

    def poke(self):
        """Ask for a poll right now instead of at the next tick."""
        self._wake.set()

    @property
    def interval(self) -> int:
        return max(5, int(self.cfg.get("interval") or 45))

    def status(self) -> dict:
        return {
            "running": self.is_alive() and not self._stop.is_set(),
            "interval_seconds": self.interval,
            "watch": self.cfg.get("watch", []),
            "reply_prefix": self.cfg.get("reply_prefix"),
            "last_poll": self.last_poll,
            "polls": self.polls,
            "last_error": self.last_error,
            "queue": self.store.counts(),
        }

    def run(self):
        while not self._stop.is_set():
            self._wake.wait(self.interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.poll_once()
            except Exception as e:
                self.last_error = "%s: %s" % (type(e).__name__, e)
                self.store.event("watch-error", self.last_error)

    def poll_once(self) -> list:
        found = []
        for entry in self.cfg.get("watch", []):
            if not entry.get("enabled", True):
                continue
            try:
                found += self._poll_chat(entry)
            except TeamsError as e:
                self.last_error = "%s: %s" % (entry.get("chat"), e)
                self.store.event("watch-error", self.last_error)
            else:
                self.last_error = None
        self.polls += 1
        self.last_poll = _now()
        return found

    def _poll_chat(self, entry: dict) -> list:
        chat = entry["chat"]
        anchor = entry.get("anchor") or ""
        want_author = (entry.get("from") or "").lower()
        limit = int(self.cfg.get("read_limit") or 20)
        prefix = self.cfg.get("reply_prefix") or ""

        def read(t):
            t.open_chat(chat)
            return {"title": t.current_chat_title() or chat,
                    "messages": t.read_messages(limit)}

        data = self.worker.submit(read, timeout=180)
        title = data["title"]
        cursor = self.store.cursor(chat)
        newest = cursor
        fresh = []

        for m in data["messages"]:
            mid = m.get("id") or ""
            if not mid:
                continue
            if newer(mid, newest):
                newest = mid
            # First poll on a chat only sets the cursor: replying to a backlog
            # of old "@claude" messages on startup would be a nasty surprise.
            if cursor is None or not newer(mid, cursor):
                continue
            if m.get("mine"):
                continue
            text = (m.get("text") or "").strip()
            if prefix and text.startswith(prefix):
                continue           # our own reply, echoed back by Teams
            if want_author and want_author not in (m.get("author") or "").lower():
                continue
            ok, body = match_anchor(text, anchor)
            if not ok:
                continue
            fresh.append({
                "id": "%s-%s" % (_slug(chat), mid),
                "message_id": mid,
                "chat": chat,
                "chat_title": title,
                "author": m.get("author"),
                "sent_at": m.get("time"),
                "queued_at": _now(),
                "anchor": anchor,
                "text": text,
                "body": body,
                "state": PENDING,
                "claimed_by": None,
            })

        if newest != cursor:
            self.store.set_cursor(chat, newest)
        added = [m for m in fresh if self.store.add(m)]
        if added:
            self.store.event("mentions", "%d new from %s" % (len(added), title))
        elif cursor is None:
            self.store.event("watch-start",
                             "cursor set for %s at %s" % (title, newest))
        return added
