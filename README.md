# teams-browser

Read and write Microsoft Teams from Windows by driving a **real Chrome** with
Playwright over CDP. No Graph API, no app registration, no admin consent, no
tenant permissions — it uses the Teams *web* client with your own signed-in
session.

```powershell
git clone https://github.com/RyosukeMondo/teams-browser
cd teams-browser
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Launch
# sign in to Teams in the window that opens, then:
.\teams.ps1 wait-login
.\teams.ps1 chats
```

`setup.ps1` assumes **nothing** is installed. It finds or installs Python (via
winget), creates the virtual environment, installs dependencies, and checks you
have Chrome or Edge. It is safe to re-run — every step is skipped if already
done.

---

## Setting this up with a coding agent

This repo is meant to be handed to a coding-agent CLI (Codex, Claude Code,
Cursor, ...) with no further explanation. Clone it, start the agent in the
folder, and give it this:

> Read README.md and AGENTS.md, then set this project up and show me my chats.

`AGENTS.md` is the machine-facing contract: exact commands, exit codes, JSON
shapes, and the rules the agent must not break (never touch credentials, never
send a message unless asked). Agents that auto-read `AGENTS.md` or `CLAUDE.md`
pick it up on their own.

---

## Commands

Global flags go **before** the subcommand: `.\teams.ps1 --json read "Sam"`.

| Command | What it does |
| --- | --- |
| `launch` | start a debuggable browser on this project's own profile |
| `wait-login` | block until you have signed in by hand and the chat list renders |
| `status` | is the browser up, signed in, and which chat is open |
| `chats` | list chat threads (`*` marks a possible unread — see caveats) |
| `read [chat]` | dump messages from a chat; `--history N` scrolls up for older ones |
| `search <term>` | search messages across every chat (read-only) |
| `send <chat> <text>` | **sends a message**; `--dry-run` types it without sending |
| `selftest` | 16 checks over every path except the actual send |
| `probe` | which candidate selectors match right now, per frame |
| `shot [path]` | screenshot the tab |

```powershell
.\teams.ps1 chats --limit 50
.\teams.ps1 read "Sam" --history 8      # pull in older messages first
.\teams.ps1 --json read "Sam" --limit 40
.\teams.ps1 search "invoice"
.\teams.ps1 send "Sam" "hello" --dry-run
Get-Content msg.txt | .\teams.ps1 send "Sam"    # body from stdin
```

Chats are matched by **case-insensitive substring** of their title, so
`read "Sam"` opens the first chat whose title contains "sam". If nothing
matches, the error lists the titles that were visible.

## Modes

| flag | behaviour |
| --- | --- |
| `--mode cdp` (default) | attach to the debuggable Chrome on `--port` (9223); auto-launches it if down |
| `--mode persistent` | Playwright owns the browser; add `--headless` for unattended runs (`--channel chrome` to use installed Chrome) |

CDP mode keeps a normal browser window you can watch or take over at any time.
For headless runs, install Playwright's Chromium first:
`.\setup.ps1 -WithPlaywrightBrowser`.

`--profile` and `--port` (or `TEAMS_BROWSER_PROFILE` / `TEAMS_BROWSER_PORT`) let
you keep several accounts side by side — a work account and a personal one need
their own profile *and* their own port:

```powershell
.\teams.ps1 --profile .\profile-work --port 9224 launch
```

The port defaults to 9223 to stay clear of 9222, which other CDP tooling
commonly claims.

## Sign-in

Sign-in is always manual, in the browser window, whenever a profile is new or
its session expires. **The tool never handles credentials** — it has no code
path that types into a password field. Once you have signed in, the profile
directory keeps the session across runs.

## How it works

* A dedicated Chrome profile (`./profile`, gitignored) is launched with
  `--remote-debugging-port`. Playwright attaches over CDP.
* The window is resized to 1440x960 on attach: Teams virtualises its lists, so a
  small window renders only a handful of messages.
* Teams' DOM changes often, so every lookup goes through a *list* of candidate
  selectors in `teams_browser/teams.py` — the only file selectors live in.

## Verified against (2026-08-23, Chrome 151)

Built against a consumer Microsoft account, so the app served
`teams.live.com/v2/` (work/school accounts stay on `teams.microsoft.com`), with
a non-English UI. What the live DOM actually looks like, and what the code
relies on:

* App-bar entries are keyed by app GUID (`APP_IDS` in `teams.py`), not by their
  localised `aria-label`, so `goto_app("chat")` works in any UI language.
* The chat list is a Fluent tree: `aria-level="1"` rows are section headers
  ("Favourites", "Chat"), `aria-level="2"` rows are the conversations.
* A message body is `[data-tid="chat-pane-message"]` with
  `id="message-body-<epoch-ms>"`; its author, `<time datetime>` and the
  sent-status icon live on the enclosing `[data-tid="chat-pane-item"]`, which
  the extractor walks up to. `mine` is inferred from that sent-status icon.
* `current_chat_title()` tries selectors one at a time on purpose — a CSS
  selector *list* matches in document order and picks up the left-pane heading
  instead of the chat header.
* `compose()` clears the composer first: Teams keeps per-chat drafts, and typing
  into a leftover draft interleaves the two texts.
* Search results are cards under `[data-tid="search-card"]`. Matched terms are
  wrapped in `[data-tid="highlighted"]`, and the div holding them is the message
  body — the `span[title]` in the card header is the *account* label ("me"), not
  the conversation. Author and timestamp sit as sibling spans in no fixed order,
  so the timestamp is picked by shape and the author is whatever is left.

### Not verified

* **`--history` pagination.** The scroll-back loop runs and correctly reports
  `at_top`, but every chat on the account it was built against fitted on one
  screen, so Teams was never made to fetch an older page. The mechanism is
  untested against a long conversation.
* **The `unread` flag.** No chat was unread while this was built, so the
  detection has never seen a positive. Read a `false` as "no unread marker
  found", not as "definitely read".
* **Channels and Teams (the tabs), threads, attachments, reactions, editing and
  deleting.** Not implemented — this covers 1:1 and group *chats* only.

## Troubleshooting

| symptom | fix |
| --- | --- |
| `running scripts is disabled on this system` | `powershell -ExecutionPolicy Bypass -File .\setup.ps1` |
| `Not signed in` | `.\teams.ps1 launch`, sign in in that window, then `.\teams.ps1 wait-login` |
| `no chat matching '...'` | the error lists visible titles — match a substring of one |
| `no debuggable browser on port 9223` | `.\teams.ps1 launch` |
| `chat list not found` | run `.\teams.ps1 probe`; Teams may have changed its DOM |
| everything suddenly fails | `.\teams.ps1 selftest`, then `.\teams.ps1 probe` |

## When Teams changes its DOM

```powershell
.\teams.ps1 probe        # which candidate selectors match, per frame, + top data-tid values
```

Add the winning selector to the **head** of the matching list in
`teams_browser/teams.py` (`CHAT_LIST_ITEM`, `MESSAGE_ITEM`, `COMPOSER`,
`SEND_BUTTON`, `SEARCH_BOX`, `SEARCH_RESULT_CARD`), then run
`.\teams.ps1 selftest` to confirm nothing else regressed.

## Notes and limits

* `send` is genuinely side-effectful; `--dry-run` stops one step short so you
  can eyeball the composer.
* Reads see what the web client has rendered. `--history N` scrolls up for more.
* Teams web blocks Firefox; use Chrome or Edge.
* The `profile/` directory holds a live signed-in session. It is gitignored —
  keep it that way, and treat it like a credential.

## Licence

MIT — see `LICENSE`.
