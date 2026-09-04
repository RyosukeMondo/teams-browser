"""Command line front end: `python -m teams_browser <command>`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .browser import (DEFAULT_PORT, PROFILE_DIR, cdp_version, launch_browser,
                      profile_has_session, session, set_window_state, window_state)
from .teams import CHAT_LIST_ITEM, SEND_BUTTON, TEAMS_URL, Teams, TeamsError


def _out(obj, as_json: bool):
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    return obj


def cmd_launch(a):
    profile = Path(a.profile)
    if a.launch_headless and not profile_has_session(profile):
        msg = [
            "Refusing to launch headless: %s has no signed-in session yet." % profile,
            "Headless Chrome cannot show a sign-in form you can type into, so it",
            "would just sit on the login page. Do this once with a real window:",
            r"    .\teams.ps1 launch",
            "    (sign in to Teams in that window)",
            r"    .\teams.ps1 wait-login",
            "After that, headless reuses the session.",
        ]
        print("\n".join(msg), file=sys.stderr)
        return 2
    info = launch_browser(a.port, profile, a.prefer, a.browser_path, url=a.url,
                          headless=a.launch_headless, minimized=a.minimized)
    how = "headless" if a.launch_headless else ("minimized" if a.minimized else "windowed")
    print("CDP up on port %d (%s): %s" % (a.port, how, info.get("Browser")))
    print("Profile: %s" % a.profile)
    if not a.launch_headless:
        print("Sign in to Teams in that window if needed; the session persists.")


def cmd_minimize(a):
    with session("cdp", a.port, Path(a.profile), autostart=False) as page:
        state = window_state(page)
        if state is None:
            print("no OS window to minimize (headless?)", file=sys.stderr)
            return 1
        if state == "minimized":
            print("already minimized")
            return 0
        ok = set_window_state(page, "minimized")
        print("minimized" if ok else "could not minimize")
        return 0 if ok else 1


def cmd_shutdown(a):
    """Close the browser. Frees ~1 GB; the signed-in session survives."""
    if not cdp_version(a.port):
        print("nothing running on port %d" % a.port)
        return 0
    with session("cdp", a.port, Path(a.profile), autostart=False) as page:
        try:
            cdp = page.context.new_cdp_session(page)
            cdp.send("Browser.close")
        except Exception:
            pass
    print("browser closed (profile %s keeps the session)" % a.profile)
    return 0


def cmd_status(a):
    info = cdp_version(a.port)
    if not info:
        print("no debuggable browser on port %d (run: launch)" % a.port)
        return 1
    print("browser: %s" % info.get("Browser"))
    with session("cdp", a.port, Path(a.profile), autostart=False) as page:
        t = Teams(page)
        print("tab url: %s" % page.url)
        print("signed in: %s" % t.signed_in())
        title = t.current_chat_title()
        if title:
            print("open chat: %s" % title)
    return 0


def _with_teams(a, fn):
    with session(a.mode, a.port, Path(a.profile), headless=a.headless,
                 prefer=a.prefer, browser_path=a.browser_path,
                 channel=a.channel, autostart=not a.no_autostart) as page:
        t = Teams(page)
        if a.open:
            t.open(a.url or TEAMS_URL)
        if not t.signed_in():
            print("Not signed in. Run `launch`, sign in to Teams in that window, "
                  "then retry.", file=sys.stderr)
            return 2
        return fn(t)


def cmd_wait_login(a):
    """Poll until the profile is signed in to Teams (you sign in by hand)."""
    import time
    deadline = time.time() + a.timeout
    with session("cdp", a.port, Path(a.profile), autostart=not a.no_autostart) as page:
        t = Teams(page)
        if a.open and "teams." not in page.url:
            page.goto(a.url or TEAMS_URL, wait_until="domcontentloaded")
        while time.time() < deadline:
            if t.signed_in() and t.find(CHAT_LIST_ITEM)[2] is not None:
                print("signed in: %s" % page.url)
                return 0
            page.wait_for_timeout(2000)
    print("still not signed in after %ss (url above)" % a.timeout, file=sys.stderr)
    return 2


def cmd_chats(a):
    def run(t):
        chats = t.list_chats(a.limit)
        if a.json:
            _out(chats, True)
        else:
            for c in chats:
                mark = "*" if c["unread"] else " "
                print("%s %2d  %s" % (mark, c["index"], (c["title"] or "")[:90]))
        return 0
    return _with_teams(a, run)


def cmd_read(a):
    def run(t):
        if a.chat:
            t.open_chat(a.chat)
        if a.history:
            info = t.load_history(rounds=a.history)
            if not info["at_top"]:
                print("note: more history above (raise --history)", file=sys.stderr)
        msgs = t.read_messages(a.limit)
        if a.json:
            _out({"chat": t.current_chat_title(), "messages": msgs}, True)
        else:
            print("== %s ==" % (t.current_chat_title() or "(current chat)"))
            for m in msgs:
                head = " ".join(x for x in [m.get("author"), m.get("time")] if x)
                print("--- %s" % head)
                print(m["text"])
        return 0
    return _with_teams(a, run)


def cmd_send(a):
    def run(t):
        if a.chat:
            opened = t.open_chat(a.chat)
            print("chat: %s" % (opened["title"] or "")[:80], file=sys.stderr)
        text = a.text if a.text is not None else sys.stdin.read().rstrip("\n")
        if not text:
            print("nothing to send", file=sys.stderr)
            return 1
        if a.dry_run:
            sel = t.compose(text)
            print("composed (NOT sent) into %s: %r" % (sel, t.composer_text()))
            return 0
        res = t.send(text)
        print("sent via %s to %s" % (res["sent_via"], t.current_chat_title()))
        if res["composer_now"]:
            print("warning: composer still holds %r" % res["composer_now"], file=sys.stderr)
            return 1
        return 0
    return _with_teams(a, run)


def cmd_search(a):
    def run(t):
        hits = t.search_messages(a.query, limit=a.limit)
        if a.json:
            _out(hits, True)
        else:
            if not hits:
                print("no message hits for %r" % a.query)
            for h in hits:
                print("--- %s | %s | %s" % (h.get("context") or "?",
                                            h.get("author") or "?", h.get("time") or "?"))
                print(h["text"])
        if not a.keep:
            t.clear_search()
        return 0
    return _with_teams(a, run)


def cmd_selftest(a):
    """Exercise every path except the actual send. Safe to run any time."""
    def run(t):
        checks = []

        def chk(name, fn):
            try:
                val = fn()
                ok = bool(val)
            except Exception as e:
                ok, val = False, "%s: %s" % (type(e).__name__, e)
            checks.append((ok, name, val))
            return val

        chats = chk("chat list", lambda: t.list_chats(a.limit))
        if chats:
            target = a.chat or chats[0]["title"]
            chk("open chat %r" % target[:30], lambda: t.open_chat(target))
            chk("chat title", t.current_chat_title)
            msgs = chk("read messages", lambda: t.read_messages(5))
            if msgs:
                chk("message has timestamp", lambda: any(m["time"] for m in msgs))
                chk("message has author", lambda: any(m["author"] for m in msgs))
            chk("composer clears", t.clear_composer)
            chk("compose replaces draft", lambda: (
                t.compose("selftest draft A"),
                t.compose("selftest draft B"),
                t.composer_text() == "selftest draft B")[-1])
            chk("composer cleared after test", t.clear_composer)
            chk("send button present", lambda: t.find(SEND_BUTTON)[2] is not None)
            chk("history scroll reaches top",
                lambda: t.load_history(rounds=4)["at_top"])
            hits = chk("message search", lambda: t.search_messages(a.query, limit=5))
            if hits:
                chk("search hit has text", lambda: all(h["text"] for h in hits))
                chk("search hit has author", lambda: any(h["author"] for h in hits))
            chk("search cleared", t.clear_search)
            chk("back to chat view", lambda: t.ensure_chat_view()[0] is not None)

        for ok, name, val in checks:
            detail = "" if ok else "  <- %s" % (val,)
            print("%s %s%s" % ("PASS" if ok else "FAIL", name, detail))
        failed = sum(1 for ok, _, _ in checks if not ok)
        print("%d/%d passed" % (len(checks) - failed, len(checks)))
        return 1 if failed else 0
    return _with_teams(a, run)


def cmd_probe(a):
    def run(t):
        _out({"probe": t.probe(), "top_data_tids": t.sniff_tids()}, True)
        return 0
    # probe should work even when sign-in detection fails
    with session(a.mode, a.port, Path(a.profile), headless=a.headless,
                 prefer=a.prefer, browser_path=a.browser_path,
                 channel=a.channel, autostart=not a.no_autostart) as page:
        t = Teams(page)
        if a.open:
            try:
                t.open(a.url or TEAMS_URL)
            except Exception as e:
                print("open failed: %s" % e, file=sys.stderr)
        return run(t)


def cmd_shot(a):
    with session(a.mode, a.port, Path(a.profile), headless=a.headless,
                 prefer=a.prefer, browser_path=a.browser_path,
                 channel=a.channel, autostart=not a.no_autostart) as page:
        path = Path(a.path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), full_page=a.full_page)
        print(path)
        return 0


def cmd_serve(a):
    """Run the REST bridge: teams-interface.local on the LAN."""
    import os
    from . import config as cfgmod
    from .api import serve

    # Before anything that might print. Under pythonw.exe -- which is how the
    # scheduled task runs, because it has no console for a stray window to
    # appear in or for a stray Ctrl-C to kill -- sys.stdout is None, and the
    # first print() would be an AttributeError.
    if a.log:
        path = Path(a.log)
        path.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(path, "a", encoding="utf-8", buffering=1)
    elif sys.stdout is None or sys.stderr is None:
        sys.stdout = sys.stderr = open(os.devnull, "w")

    cfg = cfgmod.load(a.config)
    # Persist the file *before* the flags are folded in: the file is the
    # durable configuration, flags are for this run only. Without this, one
    # `--interval 30` would quietly become the new permanent setting.
    cfgmod.ensure_token(cfg)
    cfgmod.save(cfg)

    for key, val in (("host", a.host), ("port", a.api_port), ("hostname", a.hostname),
                     ("interval", a.interval), ("reply_prefix", a.reply_prefix)):
        if val is not None:
            cfg[key] = val
    if a.watch:
        cfg["watch"] = cfgmod.normalise_watch(
            [{"chat": c, "anchor": a.anchor if a.anchor is not None
              else cfgmod.DEFAULT_ANCHOR} for c in a.watch])
    elif a.anchor is not None:
        for w in cfg["watch"]:
            w["anchor"] = a.anchor
    if a.no_mdns:
        cfg["mdns"] = False
    if a.no_auth:
        cfg["auth"] = False
    # These belong to the browser the service drives, and the global flags are
    # how the user says which browser that is.
    cfg["browser"]["port"] = a.port
    cfg["browser"]["profile"] = a.profile
    cfg["browser"]["prefer"] = a.prefer
    cfg["browser"]["mode"] = a.mode
    if a.headless:
        cfg["browser"]["headless"] = True

    if a.log:
        # The log file is ours, not a shell redirect: a redirect lives in a
        # wrapper process, and killing that leaves the server alive with a dead
        # stdout -- still holding the port, unable to answer anything.
        from datetime import datetime
        print("\n=== teams-interface starting %s ==="
              % datetime.now().isoformat(timespec="seconds"))
    return serve(cfg, quiet=a.quiet)


def build_parser():
    p = argparse.ArgumentParser(prog="teams", description=__doc__)
    p.add_argument("--mode", choices=["cdp", "persistent"], default="cdp",
                   help="attach to a debuggable browser (default) or let Playwright own one")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--profile", default=str(PROFILE_DIR))
    p.add_argument("--prefer", choices=["chrome", "edge"], default="chrome")
    p.add_argument("--browser-path", default=None)
    p.add_argument("--channel", default=None, help="persistent mode: chrome|msedge|chromium")
    p.add_argument("--headless", action="store_true", help="persistent mode only")
    p.add_argument("--no-autostart", action="store_true",
                   help="fail instead of launching a browser")
    p.add_argument("--url", default=None)
    p.add_argument("--no-open", dest="open", action="store_false", default=True,
                   help="use the tab as-is instead of navigating to Teams")
    p.add_argument("--json", action="store_true")

    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("launch", help="start a debuggable browser on our profile")
    s.add_argument("--headless", dest="launch_headless", action="store_true",
                   help="no window at all; requires a profile already signed in")
    s.add_argument("--minimized", action="store_true",
                   help="launch windowed but tucked away (keeps sign-in possible)")
    s.set_defaults(func=cmd_launch)

    s = sub.add_parser("minimize", help="tuck the browser window away")
    s.set_defaults(func=cmd_minimize)

    s = sub.add_parser("shutdown", help="close the browser (frees memory; session persists)")
    s.set_defaults(func=cmd_shutdown)

    s = sub.add_parser("status", help="is the browser up and signed in?")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("wait-login", help="block until you have signed in by hand")
    s.add_argument("--timeout", type=int, default=300)
    s.set_defaults(func=cmd_wait_login)

    s = sub.add_parser("chats", help="list chat threads")
    s.add_argument("--limit", type=int, default=25)
    s.set_defaults(func=cmd_chats)

    s = sub.add_parser("read", help="read messages from a chat")
    s.add_argument("chat", nargs="?", help="substring of the chat title")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--history", type=int, default=0, metavar="ROUNDS",
                   help="scroll up N rounds first to load older messages")
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("send", help="send a message (side-effectful)")
    s.add_argument("chat", help="substring of the chat title")
    s.add_argument("text", nargs="?", help="message text; omit to read stdin")
    s.add_argument("--dry-run", action="store_true",
                   help="type into the composer but do not send")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("search", help="search messages across all chats (read-only)")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--keep", action="store_true", help="leave the search box populated")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("selftest", help="check every path except send (sends nothing)")
    s.add_argument("--chat", default=None, help="chat to exercise (default: first)")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--query", default="a", help="term to exercise message search with")
    s.set_defaults(func=cmd_selftest)

    s = sub.add_parser("probe", help="dump which selectors match right now")
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("serve", help="run the REST API on the LAN (teams-interface)")
    s.add_argument("--host", default=None, help="bind address (default 0.0.0.0)")
    s.add_argument("--api-port", type=int, default=None, metavar="PORT",
                   help="HTTP port (default 8787); --port is the browser's CDP port")
    s.add_argument("--hostname", default=None,
                   help="mDNS name, without .local (default teams-interface)")
    s.add_argument("--watch", action="append", metavar="CHAT",
                   help="chat title substring to monitor; repeatable")
    s.add_argument("--anchor", default=None,
                   help='what makes a message a job (default "@claude"; '
                        'empty string means every incoming message)')
    s.add_argument("--interval", type=int, default=None,
                   help="seconds between polls (default 45)")
    s.add_argument("--reply-prefix", default=None,
                   help='stamped on every reply (default "[claude-code]")')
    s.add_argument("--no-mdns", action="store_true", help="do not advertise on the LAN")
    s.add_argument("--no-auth", action="store_true",
                   help="serve without a token -- only ever on a trusted network")
    s.add_argument("--config", default=None, help="path to teams-interface.json")
    s.add_argument("--log", default=None, metavar="PATH",
                   help="append output to this file instead of the console "
                        "(use this for unattended runs, not a shell redirect)")
    s.add_argument("--quiet", action="store_true", help="no per-request log lines")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("shot", help="screenshot the tab")
    s.add_argument("path", nargs="?", default="out/teams.png")
    s.add_argument("--full-page", action="store_true")
    s.set_defaults(func=cmd_shot)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        return a.func(a) or 0
    except TeamsError as e:
        print("teams: %s" % e, file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
