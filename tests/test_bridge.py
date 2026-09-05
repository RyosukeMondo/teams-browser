"""Offline checks for the Teams -> Claude bridge.

No browser, no network, no pytest:

    .\\.venv\\Scripts\\python.exe tests\\test_bridge.py   # Windows
    ./.venv/bin/python tests/test_bridge.py                # Linux, macOS

`teams selftest` covers the browser half. This covers the half that decides
*which* messages become jobs -- which you cannot exercise by hand without
spamming a real person, and where a mistake means either answering the same
message twice or answering your own reply in a loop.
"""
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from teams_browser.watcher import (MentionStore, Watcher, gather_attachments,
                                   match_anchor, newer)

fails = []


def eq(name, got, want):
    if got != want:
        fails.append("%s: got %r want %r" % (name, got, want))
        print("FAIL %s" % name)
    else:
        print("PASS %s" % name)


# --- anchor matching -----------------------------------------------------
eq("anchor found", match_anchor("hey @claude do X", "@claude"), (True, "hey do X"))
eq("anchor at start", match_anchor("@claude do X", "@claude"), (True, "do X"))
eq("anchor case-insensitive", match_anchor("@Claude go", "@claude")[0], True)
eq("anchor absent", match_anchor("just chatting", "@claude"), (False, ""))
eq("empty anchor takes everything", match_anchor(" hi ", ""), (True, "hi"))
eq("multi-line body keeps its shape",
   match_anchor("@claude fix:\n  - a\n  - b", "@claude")[1], "fix:\n  - a\n  - b")
eq("id ordering is numeric", newer("1787000000010", "1787000000009"), True)
eq("id ordering vs None", newer("1", None), True)


# --- the polling rules ---------------------------------------------------
class FakeWorker:
    """Replays canned message batches instead of driving a browser."""

    def __init__(self, batches):
        self.batches = batches
        self.calls = 0

    def submit(self, fn, timeout=0):
        i = min(self.calls, len(self.batches) - 1)
        self.calls += 1
        return {"title": "miyachi haruna", "messages": self.batches[i]}


def msg(mid, text, author="miyachi haruna", mine=False):
    return {"id": mid, "text": text, "author": author, "mine": mine, "time": None}


def store_in_tmp():
    return MentionStore(Path(tempfile.mkdtemp()) / "state.json")


store = store_in_tmp()
cfg = {"watch": [{"chat": "miyachi haruna", "anchor": "@claude", "from": None,
                  "enabled": True}],
       "interval": 45, "reply_prefix": "[claude-code]", "read_limit": 20}

batches = [
    # First poll sees a backlog that must NOT be answered, only cursored.
    [msg("1787000000001", "@claude old request from yesterday")],
    # Then: one real hit, one non-anchored line, our own reply echoed back by
    # Teams, and an anchored message we sent ourselves.
    [msg("1787000000001", "@claude old request from yesterday"),
     msg("1787000000002", "morning!"),
     msg("1787000000003", "@claude please check the build"),
     msg("1787000000004", "[claude-code] build is green", author="me"),
     msg("1787000000005", "@claude and this one too", author="me", mine=True)],
]
w = Watcher(FakeWorker(batches), store, cfg)

eq("first poll queues nothing", w.poll_once(), [])
eq("first poll sets the cursor", store.cursor("miyachi haruna"), "1787000000001")

second = w.poll_once()
eq("second poll queues exactly the new anchor hit", len(second), 1)
eq("only the un-prefixed, not-mine, anchored message wins",
   [m["message_id"] for m in second], ["1787000000003"])
eq("body has the anchor stripped", second[0]["body"], "please check the build")
eq("cursor advanced past everything seen",
   store.cursor("miyachi haruna"), "1787000000005")

eq("re-polling the same batch does not duplicate", w.poll_once(), [])
eq("queue holds exactly one pending job", store.counts()["pending"], 1)

eq("a job without pictures still carries an attachments list",
   second[0]["attachments"], [])

# --- attachments ---------------------------------------------------------
# A screenshot pasted into Teams is often its own bubble, right before or
# after the words. The job must pick those up, each tagged with the message
# it lives in, and never hand one bubble to two jobs.
def pic(mid, author="miyachi haruna", n=1):
    m = msg(mid, "", author=author)
    m["attachments"] = [{"index": i, "kind": "image", "src": "blob:%s-%d" % (mid, i)}
                        for i in range(n)]
    return m


def withpic(mid, text, author="miyachi haruna"):
    m = msg(mid, text, author=author)
    m["attachments"] = [{"index": 0, "kind": "image", "src": "blob:%s" % mid}]
    return m


store4 = store_in_tmp()
w4 = Watcher(FakeWorker([
    [msg("200", "seed")],
    [msg("200", "seed"),
     pic("201"),                                        # before, same author
     withpic("202", "@claude does this look right?"),   # the job, own picture
     pic("203", n=2),                                   # after, two pictures
     pic("204", author="Someone Else"),                 # after, other author
     msg("205", "@claude second job"),
     pic("206")],
]), store4, cfg)
w4.poll_once()
jobs = w4.poll_once()
eq("two jobs from the batch", [j["message_id"] for j in jobs], ["202", "205"])
first, second_job = jobs
eq("job absorbs its own and the adjacent same-author picture bubbles",
   [(a["message_id"], a["slot"]) for a in first["attachments"]],
   [("201", 0), ("202", 0), ("203", 0), ("203", 1)])
eq("job-level index is contiguous",
   [a["index"] for a in first["attachments"]], [0, 1, 2, 3])
eq("a stranger's picture is not absorbed",
   any(a["message_id"] == "204" for a in first["attachments"]), False)
eq("the following bubble goes to the next job",
   [a["message_id"] for a in second_job["attachments"]], ["206"])

# A bubble sitting between two jobs belongs to the first, never both.
shared = [withpic("300", "@claude a"), pic("301"), msg("302", "@claude b")]
absorbed = set()
eq("picture between two jobs: first job takes it",
   [a["message_id"] for a in gather_attachments(shared, 0, absorbed)], ["300", "301"])
eq("picture between two jobs: second job does not",
   gather_attachments(shared, 2, absorbed), [])

# --- attachment filenames -----------------------------------------------
from teams_browser.api import attachment_filename

eq("filename: Teams' default alt text is not a name",
   attachment_filename({"content_type": "image/png",
                        "attachment": {"kind": "image", "name": "image"}}, "17", 0),
   "17-0.png")
eq("filename: a real name keeps itself and gains the extension",
   attachment_filename({"content_type": "image/jpeg",
                        "attachment": {"kind": "image", "name": "login screen"}}, "17", 1),
   "login_screen.jpg")
eq("filename: no path separators survive",
   attachment_filename({"content_type": "image/png",
                        "attachment": {"kind": "file", "name": "../../etc/passwd"}}, "17", 0),
   "etc_passwd.png")


# --- the `from` filter ---------------------------------------------------
store2 = store_in_tmp()
cfg2 = dict(cfg, watch=[{"chat": "miyachi haruna", "anchor": "@claude",
                         "from": "haruna", "enabled": True}])
w2 = Watcher(FakeWorker([[msg("100", "seed")],
                         [msg("100", "seed"),
                          msg("101", "@claude from a stranger", author="Someone Else"),
                          msg("102", "@claude from haruna", author="miyachi haruna")]]),
             store2, cfg2)
w2.poll_once()
eq("from-filter keeps only the named author",
   [m["message_id"] for m in w2.poll_once()], ["102"])

# --- claiming ------------------------------------------------------------
store3 = store_in_tmp()
store3.add({"id": "x", "chat": "c", "state": "pending", "claimed_by": None})
winners = []


def race():
    m = store3.claim(worker="t", wait=0)
    if m:
        winners.append(m)


threads = [threading.Thread(target=race) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
eq("exactly one caller wins a contested claim", len(winners), 1)

# A session that died holding a claim must not swallow the message.
store3.path.write_text(store3.path.read_text(encoding="utf-8"), encoding="utf-8")
reloaded = MentionStore(store3.path)
eq("a claim left behind by a dead session comes back as pending",
   reloaded.get("x")["state"], "pending")

# --- keep-alive and an unread request body -------------------------------
# A request rejected before its body is read (401, 404, 413) used to answer
# and leave that body sitting in the socket. On a keep-alive connection the
# next request then began parsing in the middle of it, and came back as
# `Bad request syntax ('{"wait": 60, ...}GET /health HTTP/1.1')`. It needs a
# real socket and two pipelined requests to reproduce, which is why it stayed
# hidden until the service went behind a connection-reusing reverse proxy.
import re
import socket

from teams_browser.api import Handler, Server


class _RejectingService:
    """Enough Service for the auth check to run and say no."""
    cfg = {"auth": True, "token": "the-right-token", "port": 0}

    def authorised(self, headers, query):
        return False


Handler.service = _RejectingService()
_srv = Server(("127.0.0.1", 0), Handler)
_srv.quiet = True
threading.Thread(target=_srv.serve_forever, daemon=True).start()

_body = b'{"wait": 60, "worker": "test"}'
_pipelined = (
    b"POST /mentions/next HTTP/1.1\r\nHost: t\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: " + str(len(_body)).encode() + b"\r\n\r\n" + _body +
    b"GET /status HTTP/1.1\r\nHost: t\r\n\r\n"
)

_sock = socket.create_connection(_srv.server_address[:2], timeout=10)
_sock.sendall(_pipelined)
_sock.settimeout(5)
_seen = b""
try:
    while _seen.count(b"HTTP/1.1 ") < 2:
        _chunk = _sock.recv(65536)
        if not _chunk:
            break
        _seen += _chunk
except socket.timeout:
    pass
_sock.close()
_srv.shutdown()
Handler.service = None

# Not splitlines(): a Content-Length body has no trailing newline, so the
# second response begins on the same line the first one ended on.
_codes = re.findall(r"HTTP/1\.1 (\d{3})", _seen.decode("latin-1"))
# Both are 401s: the point is that the second was *parsed*, not that it passed.
eq("an unread body does not corrupt the next request on the connection",
   _codes, ["401", "401"])
eq("no bad-request-syntax after a rejected body",
   b"Bad request syntax" in _seen, False)


print()
if fails:
    print("FAILURES:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("all bridge checks passed")
