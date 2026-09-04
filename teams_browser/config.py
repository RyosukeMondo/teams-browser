"""Configuration for the REST service.

One JSON file at the repo root (``teams-interface.json``), created on first
run.  It holds the API token, so it is gitignored -- never commit it.

Precedence for every value: CLI flag > environment > config file > default.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("TEAMS_INTERFACE_CONFIG", REPO / "teams-interface.json"))
STATE_PATH = Path(os.environ.get("TEAMS_INTERFACE_STATE", REPO / "teams-interface.state.json"))

# The chat this machine watches by default, and the anchor that makes a message
# a job for Claude.  Both are overridable per watch entry.
DEFAULT_CHAT = "miyachi haruna"
DEFAULT_ANCHOR = "@claude"
DEFAULT_INTERVAL = 45
DEFAULT_PORT = 8787
DEFAULT_HOSTNAME = "teams-interface"
# Every reply the bridge sends is stamped with this, so the human can tell a
# Claude answer from a human one -- and so the watcher can ignore its own
# messages instead of answering them.
DEFAULT_REPLY_PREFIX = "[claude-code]"

DEFAULTS = {
    "host": "0.0.0.0",
    "port": DEFAULT_PORT,
    "hostname": DEFAULT_HOSTNAME,
    "mdns": True,
    "auth": True,
    "token": None,               # generated on first run when auth is on
    "reply_prefix": DEFAULT_REPLY_PREFIX,
    "interval": DEFAULT_INTERVAL,
    "watch": [{"chat": DEFAULT_CHAT, "anchor": DEFAULT_ANCHOR, "from": None}],
    "read_limit": 20,
    "browser": {
        "mode": "cdp",
        "port": int(os.environ.get("TEAMS_BROWSER_PORT", "9223")),
        "profile": None,          # None -> teams_browser.browser.PROFILE_DIR
        "prefer": "chrome",
        "headless": False,
        "minimized": True,
        "autostart": True,
        "idle_close": 0,          # seconds; 0 = keep the CDP attachment open
    },
}

_ENV = {
    "host": "TEAMS_INTERFACE_HOST",
    "port": "TEAMS_INTERFACE_PORT",
    "hostname": "TEAMS_INTERFACE_HOSTNAME",
    "token": "TEAMS_INTERFACE_TOKEN",
    "interval": "TEAMS_INTERFACE_INTERVAL",
    "reply_prefix": "TEAMS_INTERFACE_REPLY_PREFIX",
}
_INT_KEYS = ("port", "interval", "read_limit")


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def normalise_watch(entries) -> list:
    """Accept a bare string, a list of strings, or full dicts."""
    if entries is None:
        return []
    if isinstance(entries, (str, dict)):
        entries = [entries]
    out = []
    for e in entries:
        if isinstance(e, str):
            e = {"chat": e}
        chat = (e.get("chat") or "").strip()
        if not chat:
            continue
        anchor = e.get("anchor", DEFAULT_ANCHOR)
        out.append({
            "chat": chat,
            # "" or null means "every incoming message is a job"
            "anchor": (anchor or "").strip(),
            "from": (e.get("from") or None),
            "enabled": bool(e.get("enabled", True)),
            # True: the signed-in account's own "@claude" messages are jobs
            # too (self-driven use). The reply prefix still stops echo loops.
            "include_mine": bool(e.get("include_mine", False)),
        })
    return out


def load(path: Path = None) -> dict:
    path = Path(path or CONFIG_PATH)
    raw = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise SystemExit("teams-interface: cannot read %s: %s" % (path, e))
    cfg = _merge(DEFAULTS, raw)
    for key, env in _ENV.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = val
    for key in _INT_KEYS:
        try:
            cfg[key] = int(cfg[key])
        except (TypeError, ValueError):
            cfg[key] = DEFAULTS[key]
    cfg["watch"] = normalise_watch(cfg.get("watch"))
    cfg["_path"] = str(path)
    return cfg


def ensure_token(cfg: dict, path: Path = None) -> dict:
    """Mint a token on first run and persist it, so restarts keep the same one."""
    if not cfg.get("auth"):
        cfg["token"] = None
        return cfg
    if cfg.get("token"):
        return cfg
    cfg["token"] = secrets.token_urlsafe(24)
    save(cfg, path)
    return cfg


def save(cfg: dict, path: Path = None) -> Path:
    path = Path(path or cfg.get("_path") or CONFIG_PATH)
    out = {k: v for k, v in cfg.items() if not k.startswith("_")}
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:  # the token lives here: keep it off other accounts on this machine
        path.chmod(0o600)
    except OSError:
        pass
    return path


def public(cfg: dict) -> dict:
    """Config as served over HTTP -- never leaks the token."""
    out = {k: v for k, v in cfg.items() if k not in ("token", "_path")}
    out["auth"] = bool(cfg.get("auth"))
    out["config_path"] = cfg.get("_path")
    return out
