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

from teams_browser.watcher import MentionStore, Watcher, match_anchor, newer

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

print()
if fails:
    print("FAILURES:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("all bridge checks passed")
