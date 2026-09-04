#!/usr/bin/env python3
"""Client for a teams-interface server. Standard library only.

Runs under any Python 3.8+ on any machine on the LAN -- it does not need this
repo's virtualenv, Playwright, or a browser. That is the point: the heavy half
lives on the machine with the signed-in Chrome, and an agent anywhere else just
talks HTTP.

    python tools/teams_interface.py next --wait 60      # claim a job (JSON)
    python tools/teams_interface.py reply <id> "done"   # answer and close it
    python tools/teams_interface.py say "haruna" "hi"   # unprompted message
    python tools/teams_interface.py docs                # the server's own docs

Exit codes: 0 ok | 1 error | 2 auth | 3 Teams said no | 4 nothing waiting
           | 5 cannot reach the server

It is also importable:

    from teams_interface import Client
    c = Client()
    job = c.next(wait=60)
    if job: c.reply(job["id"], "on it")
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://teams-interface.local:8787"
CONFIG_NAMES = ("teams-interface.json",)

OK, ERR, AUTH, TEAMS, EMPTY, UNREACHABLE = 0, 1, 2, 3, 4, 5


class ApiError(RuntimeError):
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload
        msg = payload.get("error") if isinstance(payload, dict) else str(payload)
        super().__init__("HTTP %s: %s" % (status, msg))


def _find_config():
    """Look for the server's own config file, for the common same-machine case."""
    env = os.environ.get("TEAMS_INTERFACE_CONFIG")
    if env and Path(env).exists():
        return Path(env)
    here = Path(__file__).resolve()
    for folder in (here.parent.parent, here.parent, Path.cwd()):
        for name in CONFIG_NAMES:
            candidate = folder / name
            if candidate.exists():
                return candidate
    return None


def resolve(url=None, token=None):
    """(url, token) from flags, then environment, then a local config file."""
    url = url or os.environ.get("TEAMS_INTERFACE_URL")
    token = token or os.environ.get("TEAMS_INTERFACE_TOKEN")
    if not url or not token:
        path = _find_config()
        if path:
            try:
                cfg = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cfg = {}
            token = token or cfg.get("token")
            if not url:
                host = cfg.get("hostname") or "teams-interface"
                port = cfg.get("port") or 8787
                url = "http://%s.local:%s" % (host, port)
    return (url or DEFAULT_URL).rstrip("/"), token


class Client:
    def __init__(self, url=None, token=None, timeout=400):
        self.url, self.token = resolve(url, token)
        self.timeout = timeout

    # --- transport --------------------------------------------------------
    def request(self, method, path, params=None, body=None, raw=False):
        target = self.url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                target += "?" + urllib.parse.urlencode(clean)
        data = None
        req = urllib.request.Request(target, method=method)
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        req.data = data
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = r.read()
                if r.status == 204 or not payload:
                    return None
                if raw:
                    return payload
                return json.loads(payload.decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body_text)
            except json.JSONDecodeError:
                parsed = {"error": body_text[:400]}
            raise ApiError(e.code, parsed)
        except urllib.error.URLError as e:
            raise ConnectionError("cannot reach %s (%s)" % (self.url, e.reason))

    # --- endpoints --------------------------------------------------------
    def docs(self):
        return self.request("GET", "/", raw=True).decode("utf-8")

    def health(self):
        return self.request("GET", "/health")

    def status(self):
        return self.request("GET", "/status")

    def chats(self, limit=25):
        return self.request("GET", "/chats", {"limit": limit})

    def read(self, chat, limit=20, history=0):
        return self.request("GET", "/messages",
                            {"chat": chat, "limit": limit, "history": history or None})

    def say(self, chat, text, dry_run=False, prefix=None):
        return self.request("POST", "/messages",
                            body={"chat": chat, "text": text,
                                  "dry_run": dry_run, "prefix": prefix})

    def search(self, query, limit=20):
        return self.request("GET", "/search", {"q": query, "limit": limit})

    def mentions(self, state=None, chat=None, limit=50, wait=0):
        return self.request("GET", "/mentions",
                            {"state": state, "chat": chat, "limit": limit,
                             "wait": wait or None})

    def next(self, wait=0, chat=None, worker="claude"):
        """Claim the oldest pending job, or None. Blocks up to `wait` seconds."""
        return self.request("POST", "/mentions/next",
                            body={"wait": wait, "chat": chat, "worker": worker})

    def reply(self, mention_id, text, prefix=None):
        return self.request("POST", "/mentions/%s/reply" % mention_id,
                            body={"text": text, "prefix": prefix})

    def ack(self, mention_id, note=None):
        return self.request("POST", "/mentions/%s/ack" % mention_id,
                            body={"note": note})

    def fail(self, mention_id, error=None):
        return self.request("POST", "/mentions/%s/fail" % mention_id,
                            body={"error": error})

    def release(self, mention_id):
        return self.request("POST", "/mentions/%s/release" % mention_id, body={})

    def watch(self, patch=None):
        if patch is None:
            return self.request("GET", "/watch")
        return self.request("PUT", "/watch", body=patch)

    def poll(self):
        return self.request("POST", "/watch/poll", body={})

    def events(self, limit=50):
        return self.request("GET", "/events", {"limit": limit})

    def screenshot(self, path, full=False):
        png = self.request("GET", "/screenshot",
                           {"full": "1" if full else None}, raw=True)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(png)
        return path


# --- CLI -----------------------------------------------------------------
def _print(obj):
    if obj is None:
        return
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(obj)


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="teams_interface",
        description="Talk to a teams-interface server (Teams <-> Claude Code).")
    p.add_argument("--url", default=None, help="server base URL")
    p.add_argument("--token", default=None, help="API token")
    p.add_argument("--timeout", type=float, default=400)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("docs", help="print the server's markdown documentation")
    sub.add_parser("health", help="is the server alive")
    sub.add_parser("status", help="browser, watcher and queue state")

    s = sub.add_parser("chats", help="list chat threads")
    s.add_argument("--limit", type=int, default=25)

    s = sub.add_parser("read", help="read a chat")
    s.add_argument("chat")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--history", type=int, default=0)

    s = sub.add_parser("say", help="send a message (side-effectful)")
    s.add_argument("chat")
    s.add_argument("text", nargs="?", help="omit to read stdin")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--raw", action="store_true",
                   help="send without the [claude-code] prefix")

    s = sub.add_parser("search", help="search every chat")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=20)

    s = sub.add_parser("peek", help="list queued jobs without claiming one")
    s.add_argument("--state", default="pending")
    s.add_argument("--chat", default=None)
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--wait", type=int, default=0)

    s = sub.add_parser("next", help="claim the oldest job; exit 4 if none")
    s.add_argument("--wait", type=int, default=0, help="block up to N seconds")
    s.add_argument("--chat", default=None)
    s.add_argument("--worker", default="claude")

    s = sub.add_parser("reply", help="answer a job in its chat and close it")
    s.add_argument("id")
    s.add_argument("text", nargs="?", help="omit to read stdin")

    s = sub.add_parser("ack", help="close a job without replying")
    s.add_argument("id")
    s.add_argument("--note", default=None)

    s = sub.add_parser("fail", help="mark a job failed")
    s.add_argument("id")
    s.add_argument("--error", default=None)

    s = sub.add_parser("release", help="put a claimed job back on the queue")
    s.add_argument("id")

    s = sub.add_parser("watch", help="show or change what is monitored")
    s.add_argument("--chat", action="append", default=None,
                   help="replace the watch list; repeatable")
    s.add_argument("--anchor", default=None)
    s.add_argument("--interval", type=int, default=None)
    s.add_argument("--reply-prefix", default=None)

    sub.add_parser("poll", help="force a poll right now")

    s = sub.add_parser("events", help="recent server activity")
    s.add_argument("--limit", type=int, default=50)

    s = sub.add_parser("shot", help="save a screenshot of the Teams tab")
    s.add_argument("path", nargs="?", default="out/teams-interface.png")
    s.add_argument("--full", action="store_true")

    a = p.parse_args(argv)
    c = Client(a.url, a.token, a.timeout)

    def stdin_or(value):
        return value if value is not None else sys.stdin.read().rstrip("\n")

    try:
        if a.cmd == "docs":
            _print(c.docs())
        elif a.cmd == "health":
            _print(c.health())
        elif a.cmd == "status":
            _print(c.status())
        elif a.cmd == "chats":
            _print(c.chats(a.limit))
        elif a.cmd == "read":
            _print(c.read(a.chat, a.limit, a.history))
        elif a.cmd == "say":
            _print(c.say(a.chat, stdin_or(a.text), dry_run=a.dry_run,
                         prefix="" if a.raw else None))
        elif a.cmd == "search":
            _print(c.search(a.query, a.limit))
        elif a.cmd == "peek":
            _print(c.mentions(a.state, a.chat, a.limit, a.wait))
        elif a.cmd == "next":
            job = c.next(a.wait, a.chat, a.worker)
            if job is None:
                return EMPTY
            _print(job)
        elif a.cmd == "reply":
            _print(c.reply(a.id, stdin_or(a.text)))
        elif a.cmd == "ack":
            _print(c.ack(a.id, a.note))
        elif a.cmd == "fail":
            _print(c.fail(a.id, a.error))
        elif a.cmd == "release":
            _print(c.release(a.id))
        elif a.cmd == "watch":
            patch = {}
            if a.chat:
                patch["watch"] = [{"chat": ch, "anchor": a.anchor} if a.anchor
                                  is not None else {"chat": ch} for ch in a.chat]
            if a.anchor is not None and "watch" not in patch:
                current = c.watch().get("watch", [])
                patch["watch"] = [dict(w, anchor=a.anchor) for w in current]
            if a.interval is not None:
                patch["interval"] = a.interval
            if a.reply_prefix is not None:
                patch["reply_prefix"] = a.reply_prefix
            _print(c.watch(patch or None))
        elif a.cmd == "poll":
            _print(c.poll())
        elif a.cmd == "events":
            _print(c.events(a.limit))
        elif a.cmd == "shot":
            print(c.screenshot(a.path, a.full))
    except ApiError as e:
        print(json.dumps(e.payload, ensure_ascii=False, indent=2), file=sys.stderr)
        if e.status == 401:
            return AUTH
        if e.status == 409:
            return TEAMS
        return ERR
    except ConnectionError as e:
        print("%s\nIs the server up? On its machine: .\\teams.ps1 serve" % e,
              file=sys.stderr)
        return UNREACHABLE
    except KeyboardInterrupt:
        return ERR
    return OK


if __name__ == "__main__":
    sys.exit(main())
