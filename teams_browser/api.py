"""HTTP front end.

Stdlib only, deliberately: this repo bootstraps onto a machine with nothing
installed, and a web framework would be another thing for `setup.ps1` to get
right.  `ThreadingHTTPServer` gives each request its own thread, which is what
lets `/mentions/next?wait=60` block without stalling everything else; the
actual browser work is funnelled onto one thread by BrowserWorker.
"""
from __future__ import annotations

import hmac
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import apidocs, config as cfgmod
from .mdns import Advertiser, lan_ip
from .teams import TeamsError
from .watcher import ANSWERED, FAILED, MentionStore, Watcher
from .worker import BrowserUnavailable, BrowserWorker

MAX_BODY = 1 << 20          # 1 MiB is far more than any Teams message needs


class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


class Service:
    """Everything the handlers need, assembled once and shared."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.started = time.time()
        self.worker = BrowserWorker(cfg)
        self.store = MentionStore()
        self.watcher = Watcher(self.worker, self.store, cfg)
        self.advertiser = None
        self.base_url = "http://127.0.0.1:%s" % cfg.get("port")
        self.urls = [self.base_url]

    # --- auth ------------------------------------------------------------
    def authorised(self, headers, query) -> bool:
        if not self.cfg.get("auth"):
            return True
        token = self.cfg.get("token") or ""
        given = ""
        auth = headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            given = auth[7:].strip()
        given = given or headers.get("X-Api-Token", "") or _one(query, "token", "")
        return bool(given) and hmac.compare_digest(given, token)

    # --- browser-backed operations ---------------------------------------
    def chats(self, limit: int) -> list:
        return self.worker.submit(lambda t: t.list_chats(limit), timeout=120)

    def messages(self, chat, limit: int, history: int) -> dict:
        def run(t):
            if chat:
                t.open_chat(chat)
            info = t.load_history(rounds=history) if history else None
            return {"chat": t.current_chat_title(),
                    "messages": t.read_messages(limit),
                    "history": info}
        return self.worker.submit(run, timeout=240)

    def send(self, chat, text, dry_run=False, prefix=None) -> dict:
        body = "%s %s" % (prefix, text) if prefix else text

        def run(t):
            if chat:
                t.open_chat(chat)
            if dry_run:
                sel = t.compose(body)
                return {"sent": False, "dry_run": True, "composed_into": sel,
                        "chat": t.current_chat_title(), "text": body,
                        "composer_now": t.composer_text()}
            res = t.send(body)
            return {"sent": True, "dry_run": False, "chat": t.current_chat_title(),
                    "text": body, "sent_via": res["sent_via"],
                    "composer_now": res["composer_now"]}
        out = self.worker.submit(run, timeout=240)
        if out["sent"] and out.get("composer_now"):
            raise HttpError(502, "send may not have gone through: the composer "
                                 "still holds text", detail=out)
        self.store.event("send" if not dry_run else "send-dry",
                         "%s: %s" % (out.get("chat"), body[:120]))
        return out

    def search(self, q, limit) -> list:
        def run(t):
            hits = t.search_messages(q, limit=limit)
            t.clear_search()
            return hits
        return self.worker.submit(run, timeout=240)

    def screenshot(self, full=False) -> bytes:
        return self.worker.submit(
            lambda t: t.page.screenshot(full_page=full), timeout=120)

    # --- composites -------------------------------------------------------
    def status(self) -> dict:
        signed_in = None
        detail = None
        if self.worker.running():
            try:
                signed_in = bool(self.worker.submit(lambda t: t.signed_in(), timeout=90))
            except Exception as e:
                detail = "%s: %s" % (type(e).__name__, e)
        return {
            "service": "teams-interface",
            "uptime_seconds": round(time.time() - self.started, 1),
            "signed_in": signed_in,
            "browser": self.worker.describe(),
            "watcher": self.watcher.status(),
            "mdns": self.advertiser.status() if self.advertiser else {"advertised": False},
            "config_path": self.cfg.get("_path"),
            "detail": detail,
        }

    def reply_to(self, mid: str, text: str, prefix=None) -> dict:
        m = self.store.get(mid)
        if m is None:
            raise HttpError(404, "no mention %r" % mid)
        if not (text or "").strip():
            raise HttpError(400, "reply text is empty")
        prefix = self.cfg.get("reply_prefix") if prefix is None else prefix
        sent = self.send(m["chat"], text, dry_run=False, prefix=prefix)
        return {"mention": self.store.finish(mid, ANSWERED, reply=sent["text"]),
                "send": sent}

    def apply_watch(self, patch: dict) -> dict:
        if "watch" in patch:
            self.cfg["watch"] = cfgmod.normalise_watch(patch["watch"])
        if "interval" in patch:
            self.cfg["interval"] = max(5, int(patch["interval"]))
        if "reply_prefix" in patch:
            self.cfg["reply_prefix"] = patch["reply_prefix"] or ""
        if "read_limit" in patch:
            self.cfg["read_limit"] = max(1, int(patch["read_limit"]))
        cfgmod.save(self.cfg)
        self.watcher.poke()
        return self.watcher.status()

    # --- lifecycle --------------------------------------------------------
    def start_background(self):
        self.worker.start()
        self.watcher.start()
        if self.cfg.get("mdns"):
            self.advertiser = Advertiser(self.cfg.get("hostname"), self.cfg["port"])
            self.advertiser.start()

    def stop_background(self):
        self.watcher.stop()
        self.worker.stop()
        if self.advertiser:
            self.advertiser.stop()


def _one(query: dict, key: str, default=None):
    vals = query.get(key)
    return vals[0] if vals else default


def _int(query: dict, key: str, default: int) -> int:
    try:
        return int(_one(query, key, default))
    except (TypeError, ValueError):
        return default


def _flag(query: dict, key: str, default=False) -> bool:
    raw = _one(query, key)
    if raw is None:
        return default
    return raw.lower() not in ("0", "false", "no", "")


class Handler(BaseHTTPRequestHandler):
    server_version = "teams-interface/1.0"
    protocol_version = "HTTP/1.1"
    service: Service = None      # set by serve()

    # --- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):
        if self.server.quiet:
            return
        print("%s  %s" % (self.log_date_time_string(), fmt % args), flush=True)

    def _send(self, status: int, body: bytes, ctype: str, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, X-Api-Token, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"),
                   "application/json; charset=utf-8")

    def text(self, body: str, status=200, ctype="text/markdown; charset=utf-8"):
        self._send(status, body.encode("utf-8"), ctype)

    def fail(self, status: int, message: str, **extra):
        payload = {"error": message, "status": status}
        payload.update(extra)
        self.json(payload, status)

    def body_json(self, query) -> dict:
        """Body as a dict. Accepts JSON; falls back to the query string.

        The fallback is there so `curl -X POST '.../reply?text=done'` works,
        which is a lot easier to type by hand than a JSON body.
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise HttpError(413, "body too large")
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        if not raw.strip():
            return {k: v[0] for k, v in query.items() if k != "token"}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise HttpError(400, "body is not valid JSON: %s" % e)
        if not isinstance(data, dict):
            raise HttpError(400, "body must be a JSON object")
        return data

    # --- dispatch ---------------------------------------------------------
    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        path = unquote(parsed.path).rstrip("/") or "/"
        query = parse_qs(parsed.query)
        svc = self.service
        try:
            if path not in ("/", "/health") and not svc.authorised(self.headers, query):
                raise HttpError(401, "missing or wrong API token; see "
                                     "teams-interface.json on the server, or GET / "
                                     "for how to send it")
            handler = self._route(method, path)
            if handler is None:
                raise HttpError(404, "no route for %s %s -- GET / lists them" %
                                (method, path))
            handler(query, path)
        except HttpError as e:
            self.fail(e.status, e.message, **e.extra)
        except TeamsError as e:
            # The Teams DOM said no: bad chat name, nothing on screen, ...
            self.fail(409, str(e), kind="TeamsError")
        except BrowserUnavailable as e:
            self.fail(503, str(e), kind="BrowserUnavailable")
        except TimeoutError as e:
            self.fail(504, str(e), kind="Timeout")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The client hung up -- routine when a long poll is cancelled, and
            # there is no socket left to report it on.
            self.close_connection = True
        except Exception as e:
            try:
                self.fail(500, "%s: %s" % (type(e).__name__, e))
            except OSError:
                self.close_connection = True

    def _route(self, method: str, path: str):
        table = {
            ("GET", "/"): self.h_root,
            ("GET", "/health"): self.h_health,
            ("GET", "/status"): self.h_status,
            ("GET", "/config"): self.h_config,
            ("POST", "/launch"): self.h_launch,
            ("POST", "/minimize"): self.h_minimize,
            ("POST", "/shutdown"): self.h_shutdown,
            ("GET", "/chats"): self.h_chats,
            ("GET", "/messages"): self.h_messages,
            ("POST", "/messages"): self.h_send,
            ("GET", "/search"): self.h_search,
            ("GET", "/mentions"): self.h_mentions,
            ("POST", "/mentions/next"): self.h_next,
            ("GET", "/watch"): self.h_watch,
            ("PUT", "/watch"): self.h_watch_put,
            ("POST", "/watch"): self.h_watch_put,
            ("POST", "/watch/poll"): self.h_poll,
            ("GET", "/events"): self.h_events,
            ("GET", "/screenshot"): self.h_screenshot,
        }
        if (method, path) in table:
            return table[(method, path)]
        # /chats/<name>/messages -- convenient, though ?chat= avoids having to
        # URL-encode titles that are in Japanese or hold full-width spaces.
        m = re.fullmatch(r"/chats/(.+)/messages", path)
        if m and method == "GET":
            return lambda q, p: self.h_messages(q, p, chat=m.group(1))
        if m and method == "POST":
            return lambda q, p: self.h_send(q, p, chat=m.group(1))
        m = re.fullmatch(r"/mentions/([^/]+)(?:/(reply|ack|fail|release))?", path)
        if m and m.group(1) != "next":
            mid, verb = m.group(1), m.group(2)
            if method == "GET" and not verb:
                return lambda q, p: self.h_mention(q, p, mid)
            if method == "POST" and verb:
                return lambda q, p: self.h_mention_verb(q, p, mid, verb)
        return None

    # --- handlers ---------------------------------------------------------
    def h_root(self, query, path):
        svc = self.service
        fmt = _one(query, "format")
        if fmt == "json" or (fmt is None and
                             "application/json" in (self.headers.get("Accept") or "")):
            return self.json({
                "service": "teams-interface",
                "description": "REST bridge between Microsoft Teams DMs and a "
                               "Claude Code session",
                "base_url": svc.base_url,
                "urls": svc.urls,
                "auth_required": bool(svc.cfg.get("auth")),
                "watch": svc.cfg.get("watch"),
                "interval_seconds": svc.cfg.get("interval"),
                "reply_prefix": svc.cfg.get("reply_prefix"),
                "routes": apidocs.route_table(),
            })
        doc = apidocs.render(svc.cfg, svc.base_url, svc.urls)
        ctype = ("text/plain; charset=utf-8" if fmt == "text"
                 else "text/markdown; charset=utf-8")
        return self.text(doc, ctype=ctype)

    def h_health(self, query, path):
        svc = self.service
        return self.json({
            "ok": True,
            "service": "teams-interface",
            "uptime_seconds": round(time.time() - svc.started, 1),
            "browser_running": svc.worker.running(),
            "watcher_running": svc.watcher.is_alive(),
            "queue": svc.store.counts(),
            "auth_required": bool(svc.cfg.get("auth")),
        })

    def h_status(self, query, path):
        return self.json(self.service.status())

    def h_config(self, query, path):
        return self.json(cfgmod.public(self.service.cfg))

    def h_launch(self, query, path):
        body = self.body_json(query)
        return self.json(self.service.worker.launch(
            headless=body.get("headless"), minimized=body.get("minimized")))

    def h_minimize(self, query, path):
        from .browser import set_window_state, window_state

        def run(t):
            state = window_state(t.page)
            if state is None:
                return {"minimized": False, "detail": "no OS window (headless?)"}
            if state == "minimized":
                return {"minimized": True, "detail": "already minimized"}
            return {"minimized": set_window_state(t.page, "minimized")}
        return self.json(self.service.worker.submit(run, timeout=60))

    def h_shutdown(self, query, path):
        return self.json(self.service.worker.shutdown_browser())

    def h_chats(self, query, path):
        return self.json(self.service.chats(_int(query, "limit", 25)))

    def h_messages(self, query, path, chat=None):
        chat = chat or _one(query, "chat")
        limit = _int(query, "limit", self.service.cfg.get("read_limit", 20))
        return self.json(self.service.messages(chat, limit, _int(query, "history", 0)))

    def h_send(self, query, path, chat=None):
        body = self.body_json(query)
        chat = chat or body.get("chat") or _one(query, "chat")
        text = body.get("text")
        if not chat:
            raise HttpError(400, "which chat? pass {\"chat\": \"...\"}")
        if not (text or "").strip():
            raise HttpError(400, "nothing to send: pass {\"text\": \"...\"}")
        dry = bool(body.get("dry_run") or body.get("dryRun"))
        if isinstance(body.get("dry_run"), str):
            dry = body["dry_run"].lower() not in ("0", "false", "no", "")
        return self.json(self.service.send(chat, text, dry_run=dry,
                                           prefix=body.get("prefix")))

    def h_search(self, query, path):
        q = _one(query, "q") or _one(query, "query")
        if not q:
            raise HttpError(400, "pass ?q=<term>")
        return self.json(self.service.search(q, _int(query, "limit", 20)))

    def h_mentions(self, query, path):
        svc = self.service
        wait = min(_int(query, "wait", 0), 300)
        state = _one(query, "state")
        chat = _one(query, "chat")
        limit = _int(query, "limit", 50)
        since = _one(query, "since")
        deadline = time.time() + wait
        while True:
            found = svc.store.list(state=state, chat=chat, limit=limit, since=since)
            if found or time.time() >= deadline:
                return self.json({"mentions": found, "counts": svc.store.counts()})
            time.sleep(min(2.0, max(0.2, deadline - time.time())))

    def h_next(self, query, path):
        svc = self.service
        body = self.body_json(query)
        wait = min(float(body.get("wait", _int(query, "wait", 0))), 300.0)
        m = svc.store.claim(worker=body.get("worker") or "claude",
                            chat=body.get("chat") or _one(query, "chat"),
                            wait=wait)
        if m is None:
            # 204 rather than 404: "nothing waiting" is the normal outcome of a
            # listener loop, not an error to be retried differently.
            return self._send(204, b"", "application/json")
        return self.json(m)

    def h_mention(self, query, path, mid):
        m = self.service.store.get(mid)
        if m is None:
            raise HttpError(404, "no mention %r" % mid)
        return self.json(m)

    def h_mention_verb(self, query, path, mid, verb):
        svc = self.service
        body = self.body_json(query)
        if verb == "reply":
            return self.json(svc.reply_to(mid, body.get("text") or "",
                                          prefix=body.get("prefix")))
        if svc.store.get(mid) is None:
            raise HttpError(404, "no mention %r" % mid)
        if verb == "ack":
            return self.json(svc.store.finish(mid, ANSWERED,
                                              note=body.get("note")))
        if verb == "fail":
            return self.json(svc.store.finish(mid, FAILED,
                                              error=body.get("error")))
        return self.json(svc.store.release(mid))

    def h_watch(self, query, path):
        return self.json(self.service.watcher.status())

    def h_watch_put(self, query, path):
        return self.json(self.service.apply_watch(self.body_json(query)))

    def h_poll(self, query, path):
        found = self.service.watcher.poll_once()
        return self.json({"polled": True, "new": found,
                          "counts": self.service.store.counts()})

    def h_events(self, query, path):
        limit = _int(query, "limit", 50)
        return self.json(self.service.store.log[-limit:])

    def h_screenshot(self, query, path):
        png = self.service.screenshot(full=_flag(query, "full"))
        return self._send(200, png, "image/png")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    quiet = False
    # On Windows SO_REUSEADDR does not mean "reuse a TIME_WAIT port", it means
    # "bind a port someone else is already listening on" -- so a second `serve`
    # would start happily, and the two would drive the same browser and answer
    # the same message twice. Elsewhere it keeps its usual, useful meaning.
    allow_reuse_address = sys.platform != "win32"


def serve(cfg: dict, quiet: bool = False) -> int:
    svc = Service(cfg)
    ip = lan_ip()
    host, port = cfg["host"], int(cfg["port"])
    loopback_only = host in ("127.0.0.1", "localhost", "::1")
    svc.urls = []
    if cfg.get("hostname") and cfg.get("mdns") and not loopback_only:
        svc.urls.append("http://%s.local:%d" % (cfg["hostname"], port))
    if not loopback_only:
        svc.urls.append("http://%s:%d" % (ip, port))
    svc.urls.append("http://127.0.0.1:%d" % port)
    svc.base_url = svc.urls[0]

    Handler.service = svc
    try:
        httpd = Server((host, port), Handler)
    except OSError as e:
        print("cannot bind %s:%d -- %s\n"
              "Another teams-interface is probably already running. Check with\n"
              "    curl http://127.0.0.1:%d/health\n"
              "and either use it, stop it, or pass --api-port for a second one."
              % (host, port, e, port), file=sys.stderr, flush=True)
        return 1
    httpd.quiet = quiet
    svc.start_background()

    # Flushed: the token line is the one thing the operator must see, and
    # stdout is block-buffered the moment this is redirected into a log.
    def say(msg=""):
        print(msg, flush=True)

    say("teams-interface listening on %s:%d" % (host, port))
    for u in svc.urls:
        say("  %s" % u)
    if svc.advertiser and svc.advertiser.zc:
        say("mDNS: %s -> %s (registered)" % (svc.advertiser.fqdn, svc.advertiser.ip))
    elif cfg.get("mdns"):
        say("mDNS: NOT advertised (%s)" % (svc.advertiser.error if svc.advertiser
                                             else "disabled"))
    if cfg.get("auth"):
        say("token: %s   (also in %s)" % (cfg["token"], cfg["_path"]))
    else:
        say("WARNING: auth is off -- anyone on this LAN can send Teams "
              "messages as you")
    watched = ", ".join("%s [%s]" % (w["chat"], w["anchor"] or "any message")
                        for w in cfg.get("watch", [])) or "(nothing)"
    say("watching: %s  every %ss  replying with %r"
          % (watched, cfg["interval"], cfg.get("reply_prefix")))
    say("docs: %s/  (Ctrl-C to stop)" % svc.urls[0])

    thread = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        httpd.shutdown()
        svc.stop_background()
    return 0
