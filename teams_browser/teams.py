"""Page object for Microsoft Teams on the web (teams.microsoft.com).

Teams' DOM churns, so everything here works off *lists* of candidate selectors
and reports which one matched.  `probe()` dumps what actually exists on the
page, which is how you re-tune the lists when Microsoft ships a redesign.
"""
from __future__ import annotations

import re
import time

TEAMS_URL = "https://teams.microsoft.com/v2/"

# --- selector candidates -------------------------------------------------
CHAT_LIST_ITEM = [
    '[data-tid="chat-list-item"]',
    '[data-tid^="chat-list-item"]',
    'div[data-tid="chat-list"] [role="treeitem"]',
    'ul[data-tid="chat-list"] > li',
    # Fluent tree: level 1 rows are section headers ("Favourites", "Chat"),
    # level 2 rows are the actual conversations.
    '[role="tree"] [role="treeitem"][aria-level="2"]',
    '[role="tree"][aria-label] [role="treeitem"]',
]
MESSAGE_ITEM = [
    # The body of one message; author/timestamp live on the surrounding
    # [data-tid="chat-pane-item"] group, which the extractor walks up to.
    '[data-tid="chat-pane-message"]',
    '[data-tid="message-pane-list-viewport"] [role="listitem"]',
    'div[role="list"] div[data-tid^="message"]',
    '.ui-chat__item',
]
COMPOSER = [
    'div[data-tid="ckeditor-replyBox"] div[contenteditable="true"]',
    'div[role="textbox"][contenteditable="true"][data-tid*="ckeditor"]',
    'div[contenteditable="true"][role="textbox"]',
    'div.ck-editor__editable[contenteditable="true"]',
]
SEND_BUTTON = [
    'button[data-tid="newMessageCommands-send"]',
    'button[data-tid="sendMessageCommands-send"]',
    'button[name="send"]',
    'button[aria-label*="Send" i]',
]
SEARCH_BOX = [
    '[data-tid="AUTOSUGGEST_INPUT"]',
    'input[data-tid="search-box"]',
    '[role="search"] input',
    'div[role="combobox"] input',
]
SEARCH_RESULT_CARD = [
    '[data-tid="search-card"]',
    '[data-tid*="search-result" i]',
]
SIGNED_OUT_HINTS = ["login.microsoftonline.com", "login.live.com", "/_#/login"]

# App-bar entries are keyed by stable app GUIDs, so this survives UI language
# changes (the aria-labels are localised, the ids are not).
APP_IDS = {
    "chat": "86fcd49b-61a2-4701-b771-54728cd291fb",
    "meetings": "40472f6e-248f-4599-842c-ff3ed8f0ae34",
    "contacts": "40472f6e-248f-4599-842c-ff3ed8f0ae35",
    "communities": "3b95919f-59d8-4441-a975-2a8c2643c852",
    "calendar": "ef56c0de-36fc-4ef8-b417-3d82ba9d073c",
    "activity": "14d6962d-6eeb-4f48-8890-de55454bb136",
}


class TeamsError(RuntimeError):
    pass


class Teams:
    """Thin wrapper over a Playwright Page that speaks Teams."""

    def __init__(self, page, timeout: int = 30000):
        self.page = page
        self.timeout = timeout

    # --- plumbing --------------------------------------------------------
    @property
    def frames(self):
        """Main frame first, then child frames (Teams sometimes nests apps)."""
        return [self.page.main_frame] + [
            f for f in self.page.frames if f is not self.page.main_frame
        ]

    def _first(self, frame, selectors):
        for sel in selectors:
            try:
                loc = frame.locator(sel)
                if loc.count():
                    return sel, loc
            except Exception:
                continue
        return None, None

    def find(self, selectors):
        """(frame, selector, locator) for the first candidate that exists."""
        for frame in self.frames:
            sel, loc = self._first(frame, selectors)
            if loc is not None:
                return frame, sel, loc
        return None, None, None

    # --- navigation ------------------------------------------------------
    def open(self, url: str = TEAMS_URL, wait: float = 20.0, app: str = "chat"):
        if "teams." not in self.page.url:
            self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
        self.wait_ready(wait)
        if app:
            self.goto_app(app)
        return self.page.url

    def goto_app(self, app: str = "chat", wait: float = 10.0) -> bool:
        """Click an app-bar entry (chat/calendar/...) and wait for it to render."""
        app_id = APP_IDS.get(app, app)
        btn = self.page.locator('[id="%s"], [data-tid="%s"]' % (app_id, app_id))
        try:
            if not btn.count():
                return False
            if btn.first.get_attribute("aria-selected") != "true":
                btn.first.click()
        except Exception:
            return False
        if app != "chat":
            self.page.wait_for_timeout(800)
            return True
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.find(CHAT_LIST_ITEM)[2] is not None:
                return True
            self.page.wait_for_timeout(400)
        return False

    def signed_in(self) -> bool:
        url = self.page.url
        if any(h in url for h in SIGNED_OUT_HINTS):
            return False
        _, _, loc = self.find(CHAT_LIST_ITEM)
        if loc is not None:
            return True
        try:
            return self.page.locator(
                '[data-tid="app-bar"], #app-bar, [role="navigation"]'
            ).count() > 0
        except Exception:
            return False

    def wait_ready(self, wait: float = 20.0) -> bool:
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.signed_in():
                return True
            self.page.wait_for_timeout(500)
        return self.signed_in()

    # --- reading ---------------------------------------------------------
    def ensure_chat_view(self):
        """Make sure the Chat app is the one on screen; returns (frame, selector)."""
        frame, sel, _ = self.find(CHAT_LIST_ITEM)
        if frame is None:
            self.goto_app("chat")
            frame, sel, _ = self.find(CHAT_LIST_ITEM)
        return frame, sel

    def list_chats(self, limit: int = 25):
        frame, sel = self.ensure_chat_view()
        if frame is None:
            raise TeamsError("chat list not found -- run `teams probe` and check sign-in")
        js = r"""([sel, limit]) => Array.from(document.querySelectorAll(sel)).slice(0, limit).map((el, i) => {
            const t = el.innerText.split(String.fromCharCode(10)).map(s => s.trim()).filter(Boolean);
            const label = el.getAttribute('aria-label') || '';
            return {
                index: i,
                id: el.getAttribute('id') || el.getAttribute('data-tid') || null,
                title: (label || t[0] || '').trim(),
                preview: t.slice(1, 3).join(' | '),
                // Structural signals only -- see README: this could not be
                // verified against a genuinely unread chat, so treat a false
                // as "no unread marker found", not as "definitely read".
                unread: /unread|未読/i.test(label)
                        || !!el.querySelector('[data-tid*="unread" i]')
                        || Array.from(el.querySelectorAll('[aria-label]')).some(b =>
                             /^\s*\d+\s*$/.test(b.getAttribute('aria-label'))
                             || /unread|未読/i.test(b.getAttribute('aria-label'))),
            };
        })"""
        return frame.evaluate(js, [sel, limit])

    def read_messages(self, limit: int = 20):
        frame, sel, _ = self.find(MESSAGE_ITEM)
        if frame is None:
            raise TeamsError("no message list on screen -- open a chat first")
        js = """([sel, limit]) => {
            const nodes = Array.from(document.querySelectorAll(sel));
            return nodes.slice(-limit).map(el => {
                const group = el.closest('[data-tid="chat-pane-item"]') || el.parentElement || el;
                const t = group.querySelector('time');
                const authorEl = group.querySelector('[data-tid="message-author-name"]')
                              || group.querySelector('[data-tid="messageAuthorName"]')
                              || group.querySelector('.ui-chat__message__author');
                const mine = !!group.querySelector('[id^="read-status-icon"]');
                const idAttr = el.getAttribute('id') || '';
                return {
                    id: idAttr.replace(/^message-body-/, '') || null,
                    author: authorEl ? authorEl.innerText.trim() : (mine ? '(me)' : null),
                    mine: mine,
                    time: t ? (t.getAttribute('datetime') || t.getAttribute('title') || t.innerText.trim()) : null,
                    text: (el.innerText || '').trim(),
                };
            });
        }"""
        msgs = frame.evaluate(js, [sel, limit])
        return [m for m in msgs if m["text"]]

    def current_chat_title(self):
        js = """() => {
            // Try in priority order: a selector list would match in document
            // order and pick up the left-pane heading instead.
            const sels = ['[data-tid="chat-title"]', '[data-tid="chat-header-title"]',
                          '[data-tid="threadHeaderTitle"]', 'h2[id*="title" i]',
                          '[role="heading"][aria-level="1"]'];
            for (const s of sels) {
                const el = document.querySelector(s);
                const txt = el ? (el.innerText || '').trim() : '';
                if (txt) return txt;
            }
            return null;
        }"""
        for frame in self.frames:
            try:
                v = frame.evaluate(js)
                if v:
                    return v
            except Exception:
                continue
        return None

    # --- history ---------------------------------------------------------
    SCROLLER = ('[data-tid="message-pane-list-viewport"]',
                '[data-tid="message-pane-body"]')

    def _scroll_state(self):
        js = """(sels) => {
            for (const s of sels) {
                const e = document.querySelector(s);
                if (e && e.scrollHeight > 0) {
                    return {sel: s, top: e.scrollTop, height: e.scrollHeight,
                            client: e.clientHeight};
                }
            }
            return null;
        }"""
        return self.page.main_frame.evaluate(js, list(self.SCROLLER))

    def load_history(self, rounds: int = 10, settle_ms: int = 900,
                     stop_after_idle: int = 2) -> dict:
        """Scroll the message pane up so Teams fetches older messages.

        Teams virtualises the list, so `read_messages` only ever sees what is
        rendered.  Returns what actually changed, so callers can tell whether
        they reached the top of the conversation or just ran out of rounds.
        """
        js_scroll = """(sels) => {
            for (const s of sels) {
                const e = document.querySelector(s);
                if (e && e.scrollHeight > 0) { e.scrollTop = 0; return true; }
            }
            return false;
        }"""
        before = len(self.read_messages(10000))
        idle = 0
        for i in range(rounds):
            if not self.page.main_frame.evaluate(js_scroll, list(self.SCROLLER)):
                break
            self.page.wait_for_timeout(settle_ms)
            now = len(self.read_messages(10000))
            if now > before:
                before, idle = now, 0
            else:
                idle += 1
                if idle >= stop_after_idle:
                    break
        state = self._scroll_state()
        return {"messages": before, "rounds_used": i + 1 if rounds else 0,
                "at_top": bool(state and state["top"] <= 1)}

    # --- navigating to a chat -------------------------------------------
    def open_chat(self, name: str, limit: int = 60):
        """Click the first chat-list entry whose title matches `name`."""
        frame, sel = self.ensure_chat_view()
        if frame is None:
            raise TeamsError("chat list not found")
        chats = self.list_chats(limit=limit)
        pat = re.compile(re.escape(name), re.I)
        match = next((c for c in chats if pat.search(c["title"] or "")), None)
        if match is None:
            raise TeamsError(
                "no chat matching %r; visible: %s"
                % (name, ", ".join(repr((c["title"] or "")[:40]) for c in chats[:15]))
            )
        frame.locator(sel).nth(match["index"]).click()
        self.page.wait_for_timeout(1200)
        return match

    # --- search ----------------------------------------------------------
    def search_messages(self, query: str, limit: int = 20, wait: float = 8.0):
        """Search messages across every conversation.

        This is the only way to reach content the chat list has not rendered,
        and it is read-only: results are scraped, never clicked.
        """
        frame, sel, loc = self.find(SEARCH_BOX)
        if frame is None:
            raise TeamsError("search box not found")
        box = loc.first
        box.click()
        self.page.keyboard.press("Control+A")
        self.page.keyboard.press("Delete")
        self.page.keyboard.type(query, delay=25)
        self.page.wait_for_timeout(900)
        self.page.keyboard.press("Enter")

        # The results view is tabbed; the Messages tab is what we want.
        deadline = time.time() + wait
        while time.time() < deadline:
            tab = self.page.locator('[data-tid="messages-tab"]')
            if tab.count():
                try:
                    if tab.first.get_attribute("aria-selected") != "true":
                        tab.first.click()
                except Exception:
                    pass
                break
            self.page.wait_for_timeout(300)

        deadline = time.time() + wait
        while time.time() < deadline:
            if self.find(SEARCH_RESULT_CARD)[2] is not None:
                break
            self.page.wait_for_timeout(300)

        frame, sel, _ = self.find(SEARCH_RESULT_CARD)
        if frame is None:
            return []
        js = r"""([sel, limit]) => Array.from(document.querySelectorAll(sel)).slice(0, limit).map(card => {
            const NL = String.fromCharCode(10);
            const labelled = card.querySelector('[aria-label]');
            const serp = card.querySelector('[id^="serp-message-card-content"]');
            // The header block holds the author name and the timestamp as
            // sibling spans in no guaranteed order -- pick the timestamp by
            // shape (12:34, 07/20, 2022/12/01 20:28) and the author is the rest.
            const serpLines = serp ? (serp.innerText || '').split(NL)
                .map(x => x.trim()).filter(Boolean) : [];
            const isStamp = x => /\d{1,2}:\d{2}/.test(x) || /\d{1,4}\/\d{1,2}/.test(x);
            const time = serpLines.find(isStamp) || null;
            const author = serpLines.find(x => !isStamp(x)) || null;
            // Matched terms are wrapped in [data-tid="highlighted"]; the div
            // holding them is the message body. span[title] in the header is
            // the *account* label ("me"), not the conversation.
            let bodyEl = null;
            const hl = card.querySelector('[data-tid="highlighted"]');
            if (hl) bodyEl = hl.closest('div');
            if (!bodyEl && labelled) {
                bodyEl = Array.from(labelled.children).filter(k => k.tagName === 'DIV')
                    .reverse().find(k => (k.innerText || '').trim()
                        && !k.querySelector('[data-tid="message-app-card-header"]')) || null;
            }
            return {
                author: author,
                time: time,
                text: bodyEl ? (bodyEl.innerText || '').trim() : '',
                // The a11y label names the conversation the hit lives in, then
                // appends keyboard instructions -- keep only the first sentence.
                context: labelled ? labelled.getAttribute('aria-label').split(/[.。]/)[0].trim() : null,
            };
        })"""
        return frame.evaluate(js, [sel, limit])

    def clear_search(self):
        frame, sel, loc = self.find(SEARCH_BOX)
        if frame is None:
            return False
        try:
            loc.first.click()
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Delete")
            self.page.keyboard.press("Escape")
        except Exception:
            return False
        self.page.wait_for_timeout(300)
        return True

    # --- writing ---------------------------------------------------------
    def clear_composer(self) -> bool:
        """Empty the composer (a leftover draft would otherwise be typed into)."""
        frame, sel, loc = self.find(COMPOSER)
        if frame is None:
            return False
        box = loc.first
        box.click()
        self.page.wait_for_timeout(100)
        if not (box.inner_text() or "").strip():
            return True
        self.page.keyboard.press("Control+A")
        self.page.keyboard.press("Delete")
        self.page.wait_for_timeout(150)
        return not (box.inner_text() or "").strip()

    def compose(self, text: str, clear: bool = True) -> str:
        """Type `text` into the composer without sending. Returns selector used."""
        frame, sel, loc = self.find(COMPOSER)
        if frame is None:
            raise TeamsError("message composer not found -- is a chat open?")
        box = loc.first
        if clear:
            self.clear_composer()
        box.click()
        self.page.wait_for_timeout(150)
        for i, line in enumerate(text.split("\n")):
            if i:
                self.page.keyboard.press("Shift+Enter")  # newline, not send
            self.page.keyboard.type(line, delay=8)
        self.page.wait_for_timeout(200)
        return sel

    def composer_text(self) -> str:
        frame, sel, loc = self.find(COMPOSER)
        if frame is None:
            return ""
        try:
            return (loc.first.inner_text() or "").strip()
        except Exception:
            return ""

    def send(self, text: str, use_button: bool = True):
        """Type and actually send. Side-effectful: only on explicit request."""
        self.compose(text)
        frame, sel, loc = self.find(SEND_BUTTON)
        sent_via = None
        if use_button and loc is not None:
            try:
                loc.first.click()
                sent_via = "button %s" % sel
            except Exception:
                sent_via = None
        if sent_via is None:
            self.page.keyboard.press("Enter")
            sent_via = "Enter"
        self.page.wait_for_timeout(1200)
        return {"sent_via": sent_via, "composer_now": self.composer_text()}

    # --- diagnostics -----------------------------------------------------
    def probe(self):
        groups = {
            "chat_list_item": CHAT_LIST_ITEM,
            "message_item": MESSAGE_ITEM,
            "composer": COMPOSER,
            "send_button": SEND_BUTTON,
            "search_box": SEARCH_BOX,
        }
        out = {
            "url": self.page.url,
            "title": self.page.title(),
            "frames": [f.url for f in self.frames],
            "matches": {},
        }
        for name, sels in groups.items():
            hits = []
            for frame in self.frames:
                for s in sels:
                    try:
                        n = frame.locator(s).count()
                    except Exception:
                        continue
                    if n:
                        hits.append({"frame": frame.url[:80], "selector": s, "count": n})
            out["matches"][name] = hits
        return out

    def sniff_tids(self, top: int = 40):
        """Most common data-tid values on the page -- for re-tuning selectors."""
        js = """(top) => {
            const c = {};
            document.querySelectorAll('[data-tid]').forEach(e => {
                const k = e.getAttribute('data-tid');
                c[k] = (c[k] || 0) + 1;
            });
            return Object.entries(c).sort((a, b) => b[1] - a[1]).slice(0, top);
        }"""
        return self.page.main_frame.evaluate(js, top)
