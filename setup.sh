#!/usr/bin/env bash
# One-shot Linux/macOS setup for teams-browser -- the POSIX twin of setup.ps1.
#
# Assumes only that a Python 3.9+ interpreter exists. Creates the virtual
# environment, installs dependencies, and checks that the things this tool
# drives are present: a Chromium-based browser, and -- on a headless Linux box
# -- a virtual display to put it on.
#
# Safe to re-run: every step is skipped if it is already done.
#
#   ./setup.sh                  # set everything up
#   ./setup.sh --with-browser   # also fetch Playwright's Chromium (~150 MB;
#                               # only needed for --mode persistent)
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

with_browser=0
for arg in "$@"; do
    case "$arg" in
        --with-browser|--with-playwright-browser) with_browser=1 ;;
        -h|--help) sed -n '2,16p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "setup.sh: unknown argument: $arg" >&2; exit 2 ;;
    esac
done

step() { printf '\n==> %s\n' "$*"; }
ok()   { printf '    OK  %s\n' "$*"; }
warn() { printf '    !!  %s\n' "$*" >&2; }

# --- 1. Python ------------------------------------------------------------
step "Checking for Python >= 3.9"
python_exe=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 python; do
    exe="$(command -v "$cand" 2>/dev/null || true)"
    [ -n "$exe" ] || continue
    if "$exe" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
        python_exe="$exe"; break
    fi
done
if [ -z "$python_exe" ]; then
    warn "No Python >= 3.9 found."
    echo "    Debian/Ubuntu: sudo apt install python3 python3-venv" >&2
    echo "    Fedora:        sudo dnf install python3" >&2
    echo "    macOS:         brew install python" >&2
    exit 1
fi
ok "$("$python_exe" -V) at $python_exe"

# --- 2. Virtual environment ----------------------------------------------
step "Setting up the virtual environment"
venv_py="$here/.venv/bin/python"
if [ -x "$venv_py" ]; then
    ok "Reusing $here/.venv"
else
    if ! "$python_exe" -m venv "$here/.venv" 2>/dev/null; then
        # Debian splits the venv module into its own package, and the error it
        # prints on failure is easy to miss in the middle of a build log.
        warn "Could not create a virtualenv. On Debian/Ubuntu that means:"
        echo "    sudo apt install python3-venv" >&2
        exit 1
    fi
    ok "Created $here/.venv"
fi

# --- 3. Dependencies ------------------------------------------------------
step "Installing dependencies"
"$venv_py" -m pip install --upgrade pip --quiet
"$venv_py" -m pip install -r "$here/requirements.txt" --quiet
ver="$("$venv_py" -c "import importlib.metadata as m; print(m.version('playwright'))" 2>/dev/null || true)"
[ -n "$ver" ] || { warn "playwright did not import from $venv_py"; exit 1; }
ok "playwright $ver"

if [ "$with_browser" = 1 ]; then
    step "Downloading Playwright's Chromium (only needed for --mode persistent)"
    "$venv_py" -m playwright install chromium || warn "Chromium download failed; CDP mode still works."
fi

# --- 4. A browser to drive ------------------------------------------------
step "Looking for Chrome, Chromium or Edge"
browser="$("$venv_py" -c 'from teams_browser.browser import find_browser; print(find_browser())' 2>/dev/null || true)"
if [ -n "$browser" ]; then
    ok "$browser"
else
    warn "No Chromium-based browser found. Teams web needs one (Firefox is blocked)."
    echo "    Debian/Ubuntu: sudo apt install chromium-browser   # or install Google Chrome" >&2
fi

# --- 5. Somewhere to put its window --------------------------------------
# CDP mode drives a *real* window, which on a headless server means a virtual
# display. Without one, Chrome exits immediately and `launch` times out with a
# misleading "did not expose CDP" -- so say so now, while it is still cheap.
if [ "$(uname -s)" = "Linux" ] && [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    step "No DISPLAY in this session (headless)"
    if command -v Xvfb >/dev/null 2>&1; then
        ok "Xvfb is installed -- ./service.sh install will run one for you"
    else
        warn "Install Xvfb so the browser has a screen to draw on:"
        echo "    sudo apt install xvfb x11vnc novnc websockify" >&2
        echo "    (x11vnc + novnc are how you sign in to Teams by hand: ./login-vnc.sh)" >&2
    fi
fi

# --- 6. Done --------------------------------------------------------------
step "Setup complete"
cat <<'NEXT'

Next steps -- unattended service (recommended on a Linux box):
    ./service.sh install      # Xvfb + the REST bridge, as systemd --user units
    ./login-vnc.sh start      # then sign in to Teams once, by hand
    ./service.sh status

Or drive it by hand:
    ./teams launch            # a debuggable browser on its own profile
    (sign in to Teams in that window -- this tool never handles credentials)
    ./teams wait-login        # blocks until the chat list renders
    ./teams chats             # list your conversations
NEXT
