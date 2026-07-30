"""Shared helpers for the pomodoro sync service.

Pure stdlib. Used by server.py, agent.py, and pomo.py (CLI).

Responsibilities:
- Canonical filesystem paths (config, cache, db, outbox).
- Session model helpers.
- Local cache read/write (JSON).
- Minimal HTTP JSON client (urllib) with optional bearer token.
- agent.toml config loading (tomllib, stdlib in 3.11+).
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

HOME = Path.home()
CONFIG_DIR = HOME / ".config" / "pomo"
CACHE_DIR = HOME / ".cache" / "pomo"
DATA_DIR = HOME / ".local" / "share" / "pomo"

CONFIG_FILE = CONFIG_DIR / "agent.toml"
CACHE_FILE = CACHE_DIR / "current.json"
OUTBOX_FILE = CACHE_DIR / "outbox.jsonl"
DB_FILE = DATA_DIR / "pomo.db"

# Per-machine hook scripts. Executables in HOOKS_DIR/<event>.d/ run on the
# matching lifecycle event (see hooks.py). Local to each machine.
HOOKS_DIR = CONFIG_DIR / "hooks"

# Valid session states. `ended` is explicit so stops can propagate over the
# network (a file deletion cannot be synced; an `ended` record can).
ACTIVE_STATES = ("pomodoro", "overtime", "break", "break-overtime")
ALL_STATES = ACTIVE_STATES + ("ended",)


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, CACHE_DIR, DATA_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Session model
# ---------------------------------------------------------------------------

def new_session(state: str, start_epoch: int, duration: int,
                origin_machine: str) -> dict:
    """Build a session record with a fresh id and updated_at."""
    if state not in ALL_STATES:
        raise ValueError(f"invalid state: {state!r}")
    return {
        "id": str(uuid.uuid4()),
        "state": state,
        "start_epoch": int(start_epoch),
        "duration": int(duration),
        "origin_machine": origin_machine,
        "updated_at": time.time(),
        "ended_at": None,
    }


def idle_session() -> dict:
    return {"state": "idle"}


def is_idle(session: dict | None) -> bool:
    return not session or session.get("state") in (None, "idle", "ended")


# ---------------------------------------------------------------------------
# Local cache (JSON)
# ---------------------------------------------------------------------------

def read_cache() -> dict | None:
    try:
        with CACHE_FILE.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_cache(session: dict) -> None:
    """Persist the session to the JSON cache.

    The write is atomic (temp file + rename) so concurrent readers never
    observe a half-written file.
    """
    ensure_dirs()
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(session, fh)
    tmp.replace(CACHE_FILE)


def clear_cache() -> None:
    try:
        CACHE_FILE.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Outbox (offline queue): newline-delimited JSON of pending pushes
# ---------------------------------------------------------------------------

def enqueue_outbox(action: str, session: dict) -> None:
    """Append a pending push. action is 'session' or 'end'."""
    ensure_dirs()
    with OUTBOX_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"action": action, "session": session}) + "\n")


def read_outbox() -> list[dict]:
    try:
        with OUTBOX_FILE.open("r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        return []


def rewrite_outbox(items: list[dict]) -> None:
    ensure_dirs()
    tmp = OUTBOX_FILE.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item) + "\n")
    tmp.replace(OUTBOX_FILE)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = {
    "server_url": "http://127.0.0.1:8787",
    "machine_name": socket.gethostname(),
    "poll_interval": 5,
    "side_effects": {
        "focus_mode": True,
        "alarm": True,
        "say": True,
        "launch_emacs": True,
        "run_for_remote_sessions": False,
    },
    "hooks": {
        "enabled": True,
        # Timeout (seconds) per hook script. Runaway scripts are killed.
        "timeout": 10,
        # Override the hooks directory. Empty -> HOOKS_DIR (~/.config/pomo/hooks).
        "dir": "",
    },
}


def load_config() -> dict:
    """Load agent.toml merged over defaults. Missing file -> defaults."""
    cfg = json.loads(json.dumps(_DEFAULT_CONFIG))  # deep copy
    if tomllib is not None and CONFIG_FILE.exists():
        with CONFIG_FILE.open("rb") as fh:
            user = tomllib.load(fh)
        for key, val in user.items():
            if key in ("side_effects", "hooks") and isinstance(val, dict):
                cfg[key].update(val)
            else:
                cfg[key] = val
    if not cfg.get("machine_name"):
        cfg["machine_name"] = socket.gethostname()
    # Env overrides (handy for launchd / testing)
    cfg["server_url"] = os.environ.get("POMO_SERVER_URL", cfg["server_url"])
    return cfg


# ---------------------------------------------------------------------------
# HTTP JSON client
# ---------------------------------------------------------------------------

class ServerUnavailable(Exception):
    """Raised when the server cannot be reached (offline)."""


def _token() -> str | None:
    tok = os.environ.get("POMO_TOKEN")
    return tok or None


def _request(method: str, url: str, payload: dict | None = None,
             timeout: float = 4.0) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    tok = _token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:  # server reachable, returned error
        raise ServerUnavailable(f"HTTP {exc.code}: {exc.reason}") from exc
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        raise ServerUnavailable(str(exc)) from exc
    if not body:
        return {}
    return json.loads(body)


def get_current(server_url: str) -> dict:
    return _request("GET", server_url.rstrip("/") + "/current")


def post_session(server_url: str, session: dict) -> dict:
    return _request("POST", server_url.rstrip("/") + "/sessions", session)


def post_end(server_url: str, session: dict) -> dict:
    return _request("POST", server_url.rstrip("/") + "/sessions/end", session)
