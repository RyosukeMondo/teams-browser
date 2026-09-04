#!/usr/bin/env bash
# Look at the browser this tool drives, so you can sign in to Teams by hand.
#
# On a headless Linux box the browser lives on a virtual display nobody can
# see. This puts a viewer in front of it -- x11vnc for the screen, noVNC so the
# viewer is an ordinary web page and you need no VNC client at all.
#
#   ./login-vnc.sh start      # start the viewer, print how to reach it
#   ./login-vnc.sh status
#   ./login-vnc.sh stop       # stop it again once you are signed in
#
# Both halves bind to 127.0.0.1 only, and there is deliberately no password:
# the security boundary is the SSH tunnel you open to reach them, not a VNC
# password typed over a LAN. A signed-in Teams session is never exposed to the
# network. Leave it stopped when you are not using it.
#
# Knobs: TEAMS_DISPLAY (default: the display service.sh gave the browser),
#        VNC_PORT (5921), WEB_PORT (6081)
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

VNC_PORT="${VNC_PORT:-5921}"
WEB_PORT="${WEB_PORT:-6081}"
NAME="${TEAMS_SERVICE_NAME:-teams-interface}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

die() { printf 'login-vnc.sh: %s\n' "$*" >&2; exit 1; }

# The display the *service* uses, so we are certain to look at the right one.
resolve_display() {
    if [ -n "${TEAMS_DISPLAY:-}" ]; then echo "$TEAMS_DISPLAY"; return; fi
    local unit="$UNIT_DIR/$NAME.service"
    if [ -f "$unit" ]; then
        local d
        d="$(sed -n 's/^Environment=DISPLAY=//p' "$unit" | head -1)"
        if [ -n "$d" ]; then echo "$d"; return; fi
    fi
    echo "${DISPLAY:-:120}"
}

novnc_root() {
    for d in /usr/share/novnc /usr/share/webapps/novnc /opt/novnc; do
        [ -d "$d" ] && { echo "$d"; return; }
    done
    return 1
}

pids_for() { pgrep -f -- "$1" 2>/dev/null || true; }

VNC_MATCH="x11vnc.*-rfbport $VNC_PORT"
WEB_MATCH="websockify.*$WEB_PORT"

case "${1:-status}" in
start)
    display="$(resolve_display)"
    command -v x11vnc >/dev/null || die "x11vnc not installed -- sudo apt install x11vnc"
    web="$(novnc_root)" || die "noVNC not installed -- sudo apt install novnc websockify"
    command -v websockify >/dev/null || die "websockify not installed -- sudo apt install websockify"
    DISPLAY="$display" xdpyinfo >/dev/null 2>&1 \
        || die "nothing is serving display $display -- is teams-xvfb running? (./service.sh status)"

    if [ -z "$(pids_for "$VNC_MATCH")" ]; then
        # -localhost so the screen never leaves this machine; -nopw is safe
        # only *because* of that, and -shared lets you reconnect.
        x11vnc -display "$display" -rfbport "$VNC_PORT" -localhost -nopw \
               -shared -forever -bg -quiet -o "$here/out/x11vnc.log" >/dev/null
    fi
    if [ -z "$(pids_for "$WEB_MATCH")" ]; then
        mkdir -p "$here/out"
        nohup websockify --web="$web" "127.0.0.1:$WEB_PORT" "127.0.0.1:$VNC_PORT" \
              >> "$here/out/novnc.log" 2>&1 &
        disown || true
    fi
    sleep 1
    [ -n "$(pids_for "$WEB_MATCH")" ] || die "websockify did not start -- see out/novnc.log"

    cat <<END

Viewer is up on this machine only. From your own machine:

    ssh -N -L $WEB_PORT:127.0.0.1:$WEB_PORT $(whoami)@$(hostname -I 2>/dev/null | awk '{print $1}')

then open

    http://127.0.0.1:$WEB_PORT/vnc.html?autoconnect=1&resize=remote

Sign in to Teams in that window. When the chat list has rendered, run:

    ./teams wait-login
    ./login-vnc.sh stop
END
    ;;

stop)
    for m in "$VNC_MATCH" "$WEB_MATCH"; do
        p="$(pids_for "$m")"
        [ -n "$p" ] && kill $p 2>/dev/null || true
    done
    echo "viewer stopped (the browser and the bridge keep running)"
    ;;

status)
    display="$(resolve_display)"
    printf 'display   %s (%s)\n' "$display" \
        "$(DISPLAY="$display" xdpyinfo >/dev/null 2>&1 && echo alive || echo 'not serving')"
    printf 'x11vnc    %s\n' "$(pids_for "$VNC_MATCH" | tr '\n' ' ' | sed 's/ $//' || true)"
    printf 'novnc     %s  http://127.0.0.1:%s/vnc.html\n' \
        "$(pids_for "$WEB_MATCH" | tr '\n' ' ' | sed 's/ $//' || true)" "$WEB_PORT"
    ;;

*)
    sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
