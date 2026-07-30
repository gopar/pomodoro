#!/usr/bin/env python3
"""Pomodoro sync server. Stdlib only (http.server + sqlite3).

Single source of truth for the current pomodoro session across machines.
Stores an append-only history; "current" is the latest non-ended session.
Conflict resolution is last-write-wins by `updated_at`.

Endpoints:
    GET  /health          -> {"ok": true}
    GET  /current         -> current session JSON or {"state": "idle"}
    POST /sessions        -> upsert current (LWW), append to history
    POST /sessions/end    -> mark current ended (LWW)

Auth: none by default. If POMO_TOKEN is set in the environment, all requests
must send `Authorization: Bearer <token>`.

Config via env:
    POMO_PORT      (default 8787)
    POMO_HOST      (default 0.0.0.0)
    POMO_DB_PATH   (default ~/.local/share/pomo/pomo.db)
    POMO_TOKEN     (optional; enables bearer auth when set)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

DB_PATH = Path(os.environ.get("POMO_DB_PATH", str(common.DB_FILE)))
PORT = int(os.environ.get("POMO_PORT", "8787"))
HOST = os.environ.get("POMO_HOST", "0.0.0.0")
TOKEN = os.environ.get("POMO_TOKEN") or None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id             TEXT PRIMARY KEY,
                state          TEXT NOT NULL,
                start_epoch    INTEGER NOT NULL,
                duration       INTEGER NOT NULL,
                origin_machine TEXT NOT NULL,
                updated_at     REAL NOT NULL,
                ended_at       REAL
            )
            """
        )
        # `current` holds the id of the active session (single row, id=0).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS current (singleton INTEGER PRIMARY KEY "
            "CHECK (singleton = 0), session_id TEXT, updated_at REAL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO current (singleton, session_id, updated_at) "
            "VALUES (0, NULL, 0)"
        )


def _row_to_session(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "state": row["state"],
        "start_epoch": row["start_epoch"],
        "duration": row["duration"],
        "origin_machine": row["origin_machine"],
        "updated_at": row["updated_at"],
        "ended_at": row["ended_at"],
    }


def get_current_session() -> dict:
    with _connect() as conn:
        cur = conn.execute("SELECT session_id FROM current WHERE singleton = 0")
        row = cur.fetchone()
        if not row or not row["session_id"]:
            return common.idle_session()
        srow = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (row["session_id"],)
        ).fetchone()
        if not srow:
            return common.idle_session()
        session = _row_to_session(srow)
        if session["state"] == "ended":
            return common.idle_session()
        return session


def _current_updated_at(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT updated_at FROM current WHERE singleton = 0"
    ).fetchone()
    return row["updated_at"] if row and row["updated_at"] is not None else 0.0


def apply_session(session: dict) -> tuple[bool, dict]:
    """Insert/replace current session under last-write-wins.

    Returns (applied, current_session). If the incoming updated_at is older
    than the stored current pointer, the write is ignored (applied=False).
    """
    required = ("id", "state", "start_epoch", "duration",
                "origin_machine", "updated_at")
    for key in required:
        if key not in session:
            raise ValueError(f"missing field: {key}")
    if session["state"] not in common.ALL_STATES:
        raise ValueError(f"invalid state: {session['state']!r}")

    with _connect() as conn:
        incoming = float(session["updated_at"])
        stored = _current_updated_at(conn)
        # Always record history (even losers) for a faithful log.
        conn.execute(
            "INSERT OR REPLACE INTO sessions "
            "(id, state, start_epoch, duration, origin_machine, updated_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session["id"], session["state"], int(session["start_epoch"]),
                int(session["duration"]), session["origin_machine"],
                incoming, session.get("ended_at"),
            ),
        )
        if incoming >= stored:
            conn.execute(
                "UPDATE current SET session_id = ?, updated_at = ? WHERE singleton = 0",
                (session["id"], incoming),
            )
            applied = True
        else:
            applied = False
    return applied, get_current_session()


def end_current(session: dict) -> tuple[bool, dict]:
    """Mark the current session ended under LWW using the provided record."""
    ended = dict(session)
    ended["state"] = "ended"
    ended.setdefault("ended_at", time.time())
    ended["ended_at"] = ended["ended_at"] or time.time()
    return apply_session(ended)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "pomo/1.0"

    # -- helpers ----------------------------------------------------------
    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {TOKEN}"

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        if not self._authorized():
            return self._send_json({"error": "unauthorized"}, 401)
        if self.path == "/health":
            return self._send_json({"ok": True})
        if self.path == "/current":
            return self._send_json(get_current_session())
        return self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authorized():
            return self._send_json({"error": "unauthorized"}, 401)
        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            return self._send_json({"error": "invalid json"}, 400)

        try:
            if self.path == "/sessions":
                applied, current = apply_session(payload)
                return self._send_json({"applied": applied, "current": current})
            if self.path == "/sessions/end":
                applied, current = end_current(payload)
                return self._send_json({"applied": applied, "current": current})
        except ValueError as exc:
            return self._send_json({"error": str(exc)}, 400)
        return self._send_json({"error": "not found"}, 404)


def main() -> None:
    init_db()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    sys.stderr.write(f"pomo-server listening on {HOST}:{PORT} (db={DB_PATH})\n")
    if TOKEN:
        sys.stderr.write("auth: bearer token REQUIRED\n")
    else:
        sys.stderr.write("auth: NONE (network-level only)\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
