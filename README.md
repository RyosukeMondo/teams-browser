# teams-browser

Read and write Microsoft Teams by driving a **real Chrome** with Playwright
over CDP. No Graph API, no app registration, no admin consent, no tenant
permissions — it uses the Teams *web* client with your own signed-in session.

Runs on **Windows and Linux** (macOS should work; untested). The commands are
identical; only the wrapper differs — `.\teams.ps1` or `./teams`. On a headless
Linux server it drives Chrome on a virtual display and you sign in once through
a browser tab, which makes it a reasonable thing to leave running on a machine
that is always on.

**First run — once, with a visible browser window.** You sign in yourself; the
tool never handles credentials.

```powershell
git clone https://github.com/RyosukeMondo/teams-browser
cd teams-browser
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Launch
# a Chrome window opens -- sign in to Teams there (password, MFA, all of it)
.\teams.ps1 wait-login
.\teams.ps1 chats
```

On Linux or macOS:

```bash
git clone https://github.com/RyosukeMondo/teams-browser
cd teams-browser
./setup.sh
./teams launch            # a Chrome window opens -- sign in to Teams there
./teams wait-login
./teams chats
```

`setup.ps1` assumes **nothing** is installed. It finds or installs Python (via
winget), creates the virtual environment, installs dependencies, and checks you
have Chrome or Edge. `setup.sh` does the same except that it never installs
anything for you — it prints the `apt`/`dnf`/`brew` line and stops, because a
setup script should not be calling sudo behind your back. Both are safe to
re-run; every step is skipped if already done.

No display? See [Running it on a headless Linux box](#running-it-on-a-headless-linux-box).

One thing that does **not** move between machines: the `profile/` directory.
Chrome encrypts its cookie store with a key belonging to the OS account that
created it — DPAPI on Windows, the login keyring on Linux — so copying a signed-in
profile to another machine gives you a profile that is signed out. Every new
machine costs exactly one human sign-in.

**Every run after that — no window on your desktop.** The sign-in is remembered
in `./profile`, so from now on you can run it invisibly:

```powershell
.\teams.ps1 launch --headless     # no window at all
.\teams.ps1 chats
.\teams.ps1 shutdown              # done for now; frees the memory
```

You can close that browser at any time — clicking X, or `shutdown`. Nothing is
lost: the session lives in `./profile`, not in the running browser, and the next
`launch` picks it straight back up (~15 s). You will only need a visible window
again when the session eventually expires.

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
| `launch` | start a debuggable browser on this project's own profile (`--minimized`, `--headless`) |
| `minimize` | tuck the running browser window away |
| `shutdown` | close the browser; frees ~1.4 GB, session persists |
| `wait-login` | block until you have signed in by hand and the chat list renders |
| `status` | is the browser up, signed in, and which chat is open |
| `chats` | list chat threads (`*` marks a possible unread — see caveats) |
| `read [chat]` | dump messages from a chat; `--history N` scrolls up for older ones |
| `search <term>` | search messages across every chat (read-only) |
| `send <chat> <text>` | **sends a message**; `--dry-run` types it without sending |
| `serve` | run the REST bridge on the LAN (see below); `.\teams-api.ps1` is a shortcut |
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

---

## Teams as a front end for Claude Code

The other half of this repo: a REST service that puts a Claude Code session on
the other end of a Teams DM. Someone messages you `@claude why is the build
red?`; the server notices, a Claude session picks the job up, works in the repo,
and answers in the same chat with `[claude-code]` in front so nobody mistakes it
for you.

```powershell
.\teams-api.ps1          # or: .\teams.ps1 serve
```

```
teams-interface listening on 0.0.0.0:8787
  http://teams-interface.local:8787
  http://192.168.11.14:8787
mDNS: teams-interface.local -> 192.168.11.14 (registered)
token: 3aP9…                (also in teams-interface.json)
watching: miyachi haruna [@claude]  every 45s  replying with '[claude-code]'
```

It advertises itself over mDNS, so **`http://teams-interface.local:8787/`
resolves from any machine on the LAN** — no DNS server, no hosts file. Open that
URL and the service documents itself: every endpoint, the watched chats, the
anchor, and a copy-pasteable listener loop, all rendered from the live config.

### Why the server polls and the agent does not

A Claude session polling Teams itself would spend tokens on every empty tick.
Here the browser poll is free, and the agent blocks on a claim that only returns
when a real message arrives:

```
Teams DM ──poll every 45s──> server ──long-poll──> Claude Code session
    ^                                                     |
    └──────────── reply, prefixed "[claude-code]" ─────────┘
```

### Driving it

```bash
python tools/teams_interface.py next --wait 60      # claim a job; exit 4 = none
python tools/teams_interface.py reply <id> "done"   # answer in chat, close job
python tools/teams_interface.py say "haruna" "hi"   # unprompted message
python tools/teams_interface.py status              # browser, watcher, queue
```

`tools/teams_interface.py` is standard-library only, so it runs on any machine
on the LAN with any Python — no venv, no Playwright, no browser. The heavy half
stays on the box with the signed-in Chrome.

A Claude Code session in this repo picks up the `teams-interface` skill on its
own; `/loop /teams-interface` turns it into a listener. See
`.claude/skills/teams-interface/SKILL.md`.

### Configuring it

`teams-interface.json` is written on first run and is gitignored — it holds the
API token and, in the state file beside it, real message text.

| key | default | meaning |
| --- | --- | --- |
| `watch` | `[{"chat": "miyachi haruna", "anchor": "@claude"}]` | chats to monitor; `chat` is a title substring, `from` optionally pins the author |
| `anchor` | `"@claude"` | what makes a message a job; `""` means *every* incoming message |
| `interval` | `45` | seconds between polls |
| `reply_prefix` | `"[claude-code]"` | stamped on every reply — and skipped on the way in, so the bridge never answers itself |
| `port` / `hostname` | `8787` / `teams-interface` | HTTP port and the `.local` name |
| `auth` / `token` | `true` / generated | see below |

Flags override the file for one run: `.\teams-api.ps1 --watch "Sam" --anchor
"@bot" --interval 30`.

**Authentication is on by default and should stay on.** This binds `0.0.0.0` and
can read and send your Teams messages, so anyone who can reach the port could
message your colleagues as you. The token is generated on first run, printed at
startup, and sent as `Authorization: Bearer <token>`. `--no-auth` exists for a
trusted network; the root page says so in red if you use it.

The first poll of a chat only records where the conversation currently ends, so
starting the server never fires off replies to a backlog of old `@claude`
messages. A job claimed by a session that then died goes back on the queue at
restart rather than being swallowed.

**Each poll clicks into the watched chat**, because reading Teams means reading
what it has rendered. On the shared browser that shows up as the view switching
every 45 s — harmless (Teams keeps per-chat drafts, so nothing you were typing
is lost) but visible if you were watching. `launch --minimized` or `--headless`
puts it out of sight; a second profile and port keeps it out of the way
entirely.

### Keeping it up

On Linux, `./service.sh install | status | log | restart | stop | uninstall`
does the same job through `systemd --user` — see
[Running it on a headless Linux box](#running-it-on-a-headless-linux-box).
On Windows:

```powershell
.\service.ps1 install     # run at every logon, no window; starts it now too
.\service.ps1 status      # task state, next watchdog run, is it answering
.\service.ps1 log         # tail out\teams-interface.log
.\service.ps1 restart
.\service.ps1 uninstall
```

No admin rights: the task runs as you, in your own session, which is where the
browser has to live anyway. It survives a reboot (it starts at logon) and a
crash (a watchdog trigger re-runs it every 5 minutes; while it is alive the
extra run is discarded, so the cost of a crash is at most five minutes).

Four details in there are load-bearing — each was a way the service looked fine
while being broken:

* it runs **`pythonw.exe`**, not `python.exe`. A console app started by a task
  in your session gets a real console window, and closing that window sends
  Ctrl-C and kills the server. `pythonw` has no console at all.
* `serve --log` opens the log file **itself**, rather than a shell `*>>`
  redirect. A redirect lives in a wrapper process; kill that and the server
  survives with a dead stdout — still holding the port, unable to answer.
  (It is also why `pythonw`, which has no stdout, is safe here.)
* `stop`/`restart` go by **who holds the port**, not by what the task launched:
  `Stop-ScheduledTask` only stops the latter.
* only one instance may run — see the note on `SO_REUSEADDR` below.

### No port in the URL

`teams-interface.json` can set `"port": 80`, so the address is just
**`http://teams-interface.local/`**. Windows lets a normal user bind 80, no
admin needed. Every URL the service prints and the client builds drops the
`:80`.

Linux will not let an unprivileged process bind 80 (anything below
`net.ipv4.ip_unprivileged_port_start`, normally 1024, needs root or
`CAP_NET_BIND_SERVICE`). There the tidy answer is to leave the service on 8787
and put a reverse proxy — Caddy, nginx — in front of it, which is also what
publishes the friendly name. Same URL, nothing running as root.

The shipped default stays `8787`, because port 80 collides with anything else
serving HTTP on the machine. If something does take it, `serve` now exits 1
saying so rather than starting a second, silently-conflicting server: on
Windows `SO_REUSEADDR` means "bind a port someone else is listening on", so
two servers would otherwise both drive the browser and answer the same message
twice.

### Checking it

```powershell
.\.venv\Scripts\python.exe tests\test_bridge.py   # offline: the queueing rules
.\teams.ps1 selftest                              # the browser half, 16 checks
```

---

## Running it on a headless Linux box

This is the interesting deployment: a machine that is always on, so the bridge
is always answering, with no desktop session to keep alive and nobody logged in.

```bash
./setup.sh
./service.sh install      # Xvfb + the bridge, as systemd --user units
./login-vnc.sh start      # a viewer on that virtual display; prints the URL
# ...sign in to Teams in that browser tab, once...
./teams wait-login
./login-vnc.sh stop
./service.sh status
```

How the pieces fit:

* **`Xvfb`** gives Chrome a screen to draw on. CDP does not need a *visible*
  window, but it does need a real one — headless Chrome cannot show a sign-in
  form a human can type into, and Teams virtualises its message list by
  viewport size, so an unrealistically small screen renders too few messages.
* **`systemd --user`** replaces the Scheduled Task. `Restart=always` covers the
  crash case and `WantedBy=default.target` covers the reboot case, which is what
  the Windows side needed two separate triggers to achieve.
* **linger** (`sudo loginctl enable-linger <user>`) is what keeps user units
  running when nobody is logged in. Without it systemd tears the user manager
  down at logout and takes the bridge with it. `service.sh` checks and tells you.
* **`login-vnc.sh`** is only for the sign-in. `x11vnc` and noVNC both bind
  `127.0.0.1`, so you reach them over an SSH tunnel and a signed-in Teams
  session is never exposed to the network. Stop it when you are done.

One flag in the browser launch is load-bearing on Linux:
**`--password-store=basic`**. Chrome normally asks the desktop keyring for the
key it encrypts cookies with, and on a box with nobody logged in that D-Bus
call has no answer — Chrome then hangs before the first navigation commits,
showing the URL in the omnibox over an `about:blank` document that fetched
nothing. It looks like a network fault and is not one. Keeping the key in the
profile is also what lets a signed-in session outlive the desktop session that
created it.

`KillMode=process` in the unit is deliberate: the browser is started by the
bridge but outlives it, so restarting the service re-attaches to a warm Teams
instead of reloading it. `service.sh uninstall` is the one that does take the
browser with it, matching on `--user-data-dir` so it can never hit somebody
else's Chrome.

If the box already runs `avahi-daemon`, set `"mdns": false` and let avahi
publish the name — two mDNS responders answering for one name is a bad time.

---

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

## Running it out of your way (measured, not guessed)

Everything works with the window **minimized** — reads, search, and sends. CDP
drives the page directly, so it needs neither focus nor a visible window, and
attaching no longer yanks a minimized window back onto your desktop.

```powershell
.\teams.ps1 launch --minimized   # start tucked away
.\teams.ps1 minimize             # tuck an already-running one away
.\teams.ps1 shutdown             # close it; the signed-in session survives
.\teams.ps1 launch --headless    # no window at all (see the caveat below)
```

Measured on a daily-use Windows 11 desktop, Chrome 151, same account and the
same 16-check selftest each time:

| how it runs | RAM | notes |
| --- | --- | --- |
| windowed, minimized | ~1.4 GB / 11 procs | full function, no desktop clutter |
| `--headless` | ~1.3 GB / 9 procs | ~10% less — not the win you would expect |
| not running | 0 | `shutdown`; ~15 s cold start back to a chat list |

**Headless is not a footprint fix.** Teams' web client costs over a gigabyte
however you run it. If idle RAM matters on a machine you actually work on, the
lever is not keeping it running: `shutdown` when done and pay ~15 s on the next
cold start. If you use it through the day, leave it minimized.

### Headless cannot sign you in

`--headless` refuses (exit 2) on a profile that has never been signed in, because
headless Chrome has no window in which you could type credentials or complete
MFA — it would sit on the login page forever. Sign in once with a real window:

```powershell
.\teams.ps1 launch
# sign in to Teams in that window
.\teams.ps1 wait-login
```

From then on the profile carries the session and `--headless` works. The same
applies whenever the session expires: you need a visible window again.

### What about a browser extension?

A Chrome extension would ride inside the Chrome you already have open, so the
marginal cost would be one tab rather than a second ~1.4 GB browser. That is the
only option here that genuinely lowers the footprint, but it means building an
extension plus a native-messaging host to reach this CLI.

Pointing this tool at your *everyday* Chrome by starting it with
`--remote-debugging-port` would avoid the second browser too — **don't**. That
port lets any local process drive every tab you have open, banking and mail
included. The separate profile this tool uses exists precisely to contain that.

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
