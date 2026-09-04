#!/usr/bin/env bash
# Keep the teams-interface REST bridge running across reboots on Linux --
# the POSIX twin of service.ps1, using systemd --user instead of a Scheduled
# Task. No root needed: the units run as you, which is what the browser wants
# anyway.
#
#   ./service.sh install      # write the units, enable at boot, start now
#   ./service.sh status       # units, port holder, and does the API answer
#   ./service.sh restart
#   ./service.sh stop
#   ./service.sh log
#   ./service.sh uninstall    # remove the units (also stops the browser)
#
# Knobs (environment, read at install time and baked into the units):
#   TEAMS_DISPLAY        X display for the browser   (default: auto)
#   TEAMS_SCREEN         Xvfb geometry               (default 1600x1000x24)
#   TEAMS_BROWSER_PORT   the CDP port of the browser (default 9223)
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
NAME="${TEAMS_SERVICE_NAME:-teams-interface}"
XVFB_NAME="teams-xvfb"
LOG="$here/out/$NAME.log"
VENV_PY="$here/.venv/bin/python"
SCREEN="${TEAMS_SCREEN:-1600x1000x24}"
CDP_PORT="${TEAMS_BROWSER_PORT:-9223}"

action="${1:-status}"

die()  { printf 'service.sh: %s\n' "$*" >&2; exit 1; }
say()  { printf '%s\n' "$*"; }

api_port() {
    # The JSON file is the durable config; the flag default is only a fallback
    # for a machine that has not run the server even once.
    "$VENV_PY" -c 'import json,pathlib,sys; p=pathlib.Path("teams-interface.json"); print((json.loads(p.read_text(encoding="utf-8")).get("port") or 8787) if p.exists() else 8787)' 2>/dev/null || echo 8787
}

# Which display should the browser draw on? A real desktop session already has
# one; a headless server needs Xvfb, and then we own that too.
resolve_display() {
    if [ -n "${TEAMS_DISPLAY:-}" ]; then echo "$TEAMS_DISPLAY"; return; fi
    if [ -n "${DISPLAY:-}" ];      then echo "$DISPLAY";      return; fi
    echo ":120"     # headless: our own Xvfb, well clear of :0 and of the low
                    # numbers other tools tend to grab
}

needs_xvfb() {
    # We run an Xvfb only when we picked the display ourselves.
    [ -z "${DISPLAY:-}" ] && [ -z "${TEAMS_DISPLAY:-}" ]
}

# What the *installed* unit says, which is not always what this shell would
# choose. Reporting the shell's guess instead is how `status` ends up naming
# some other application's browser as ours.
unit_env() {
    local unit="$UNIT_DIR/$NAME.service"
    [ -f "$unit" ] || return 1
    local v
    v="$(sed -n "s/^Environment=$1=//p" "$unit" | head -1)"
    [ -n "$v" ] && echo "$v"
}

running_display()  { unit_env DISPLAY            || resolve_display; }
running_cdp_port() { unit_env TEAMS_BROWSER_PORT || echo "$CDP_PORT"; }
installed_xvfb()   { [ -f "$UNIT_DIR/$XVFB_NAME.service" ]; }

port_holder() {
    # ss is on every modern Linux; lsof is not.
    ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2
}

require_venv() {
    [ -x "$VENV_PY" ] || die "no virtualenv at $VENV_PY -- run ./setup.sh first"
}

write_units() {
    mkdir -p "$UNIT_DIR" "$here/out"
    local display
    display="$(resolve_display)"

    if needs_xvfb; then
        cat > "$UNIT_DIR/$XVFB_NAME.service" <<UNIT
[Unit]
Description=Virtual display for the teams-browser Chrome ($display)
Before=$NAME.service

[Service]
Type=simple
# -nolisten tcp: this display is for our own Chrome, not for the network.
ExecStart=/usr/bin/Xvfb $display -screen 0 $SCREEN -nolisten tcp
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
UNIT
        say "wrote $UNIT_DIR/$XVFB_NAME.service  ($display, $SCREEN)"
    else
        rm -f "$UNIT_DIR/$XVFB_NAME.service"
    fi

    local after=""
    if needs_xvfb; then
        after="After=$XVFB_NAME.service
Requires=$XVFB_NAME.service"
    fi

    cat > "$UNIT_DIR/$NAME.service" <<UNIT
[Unit]
Description=teams-interface REST bridge (Teams DM -> Claude Code)
$after
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=$here
Environment=DISPLAY=$display
Environment=TEAMS_BROWSER_PORT=$CDP_PORT
Environment=PYTHONUNBUFFERED=1
# --log, never a shell redirect: a redirect lives in a wrapper process, and
# killing that leaves the server alive holding the port with a dead stdout,
# unable to answer anything. The server opens this file itself.
ExecStart=$VENV_PY -X utf8 -m teams_browser serve --log $LOG
# KillMode=process is DELIBERATE. The browser is launched as a child of the
# server but is meant to outlive it: the Teams session stays warm across a
# restart and the next start just re-attaches over CDP. The default
# (control-group) would take Chrome down on every restart and force a slow
# reload of Teams.
KillMode=process
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT
    say "wrote $UNIT_DIR/$NAME.service"
}

check_linger() {
    # Without linger, systemd tears the whole user manager down at logout and
    # the bridge dies with it -- the Linux face of the "closing the console
    # kills the server" trap that service.ps1 warns about.
    if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
        say ""
        say "!! Linger is off for $USER: these units stop when your last login ends."
        say "   Fix (once, needs root):  sudo loginctl enable-linger $USER"
    fi
}

case "$action" in
install)
    require_venv
    command -v systemctl >/dev/null || die "systemd not available on this machine"
    if needs_xvfb && ! command -v Xvfb >/dev/null; then
        die "headless machine and no Xvfb -- sudo apt install xvfb (see ./setup.sh)"
    fi
    write_units
    systemctl --user daemon-reload
    if needs_xvfb; then systemctl --user enable --now "$XVFB_NAME.service"; fi
    systemctl --user enable --now "$NAME.service"
    check_linger
    say ""
    "$0" status || true
    ;;

uninstall)
    systemctl --user disable --now "$NAME.service" 2>/dev/null || true
    systemctl --user disable --now "$XVFB_NAME.service" 2>/dev/null || true
    rm -f "$UNIT_DIR/$NAME.service" "$UNIT_DIR/$XVFB_NAME.service"
    systemctl --user daemon-reload
    # KillMode=process left the browser running on purpose; on uninstall we do
    # mean to take it with us. Matching on the profile directory makes sure we
    # never touch a Chrome that belongs to something else on this machine.
    pkill -f -- "--user-data-dir=$here/profile" 2>/dev/null || true
    say "removed $NAME.service and $XVFB_NAME.service"
    ;;

start)
    systemctl --user start "$NAME.service"
    "$0" status
    ;;

stop)
    systemctl --user stop "$NAME.service" 2>/dev/null || true
    # Parity with service.ps1: stop means nothing is holding the port any more,
    # including a stray hand-started serve that systemd knows nothing about.
    p="$(api_port)"
    holder="$(port_holder "$p" || true)"
    if [ -n "${holder:-}" ]; then
        say "port $p still held by pid $holder -- terminating it"
        kill "$holder" 2>/dev/null || true
    fi
    say "stopped"
    ;;

restart)
    "$0" stop >/dev/null
    systemctl --user start "$NAME.service"
    "$0" status
    ;;

status)
    p="$(api_port)"
    cdp="$(running_cdp_port)"
    state="$(systemctl --user is-active "$NAME.service" 2>/dev/null || true)"
    printf 'unit      %s (%s)\n' "$NAME.service" "${state:-not installed}"
    if installed_xvfb; then
        printf 'display   %s (%s)\n' "$(running_display)" \
            "$(systemctl --user is-active "$XVFB_NAME.service" 2>/dev/null || echo 'no unit')"
    else
        printf 'display   %s (not ours)\n' "$(running_display)"
    fi
    printf 'api port  %s (held by pid %s)\n' "$p" "$(port_holder "$p" || echo none)"
    printf 'cdp port  %s (held by pid %s)\n' "$cdp" "$(port_holder "$cdp" || echo none)"
    body="$(curl -sS --max-time 5 "http://127.0.0.1:$p/health" 2>/dev/null || true)"
    if [ -n "$body" ]; then
        printf 'health    %s\n' "$body"
    else
        printf 'health    no answer on http://127.0.0.1:%s/health\n' "$p"
        exit 1
    fi
    ;;

log)
    [ -f "$LOG" ] || die "no log yet at $LOG"
    tail -n "${2:-40}" "$LOG"
    ;;

*)
    sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
