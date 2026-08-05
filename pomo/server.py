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

import contextlib
import json
import os
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if sys.version_info < (3, 11):
    sys.exit(f"Error: Python 3.11+ required (current: {sys.version.split()[0]})")

from pomo import common

DB_PATH = Path(os.environ.get("POMO_DB_PATH", str(common.DB_FILE)))
PORT = int(os.environ.get("POMO_PORT", "8787"))
HOST = os.environ.get("POMO_HOST", "0.0.0.0")
TOKEN = os.environ.get("POMO_TOKEN") or None

# Reject request bodies larger than this (sessions are ~200 bytes). Guards the
# open LAN endpoint against unbounded reads / memory exhaustion.
MAX_BODY_BYTES = 65536


class RequestTooLarge(Exception):
    """Raised when a request body exceeds MAX_BODY_BYTES."""


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None -> autocommit; we drive transactions explicitly with
    # BEGIN IMMEDIATE so the LWW read-modify-write cannot interleave.
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL improves read/write concurrency; busy_timeout blocks (instead of
    # instantly erroring) when another writer holds the lock. synchronous=NORMAL
    # is the safe pairing for WAL (durable across app crashes).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    with contextlib.closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id             TEXT NOT NULL,
                state          TEXT NOT NULL,
                start_epoch    INTEGER NOT NULL,
                duration       INTEGER NOT NULL,
                origin_machine TEXT NOT NULL,
                updated_at     REAL NOT NULL,
                ended_at       REAL,
                PRIMARY KEY (id, updated_at)
            )
            """
        )
        # `current` holds the id of the active session (single row, id=0).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS current (singleton INTEGER PRIMARY KEY "
            "CHECK (singleton = 0), session_id TEXT, updated_at REAL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO current (singleton, session_id, updated_at) VALUES (0, NULL, 0)"
        )
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ALTER TABLE sessions ADD COLUMN name TEXT")
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ALTER TABLE sessions ADD COLUMN project TEXT")
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ALTER TABLE sessions ADD COLUMN kind TEXT")


def _row_to_session(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "state": row["state"],
        "start_epoch": row["start_epoch"],
        "duration": row["duration"],
        "origin_machine": row["origin_machine"],
        "updated_at": row["updated_at"],
        "ended_at": row["ended_at"],
        "name": row["name"],
        "project": row["project"],
        "kind": row["kind"],
    }


def _current_session_locked(conn: sqlite3.Connection) -> dict:
    """Read the current session using an already-open connection.

    Shared by the public getter and apply_session (so the returned `current`
    can be read inside the same transaction, closing the read-after-commit gap).
    """
    row = conn.execute("SELECT session_id FROM current WHERE singleton = 0").fetchone()
    if not row or not row["session_id"]:
        return common.idle_session()
    srow = conn.execute(
        "SELECT * FROM sessions WHERE id = ? ORDER BY updated_at DESC LIMIT 1", (row["session_id"],)
    ).fetchone()
    if not srow:
        return common.idle_session()
    session = _row_to_session(srow)
    if session["state"] == "ended":
        return {"state": "idle", "updated_at": session["updated_at"], "session_id": session["id"]}
    return session


def get_current_session() -> dict:
    with contextlib.closing(_connect()) as conn:
        return _current_session_locked(conn)


def apply_session(session: dict) -> tuple[bool, dict]:
    """Insert/replace current session under last-write-wins.

    Returns (applied, current_session). If the incoming updated_at is older
    than the stored current pointer, the write is ignored (applied=False).

    The history insert and the guarded pointer UPDATE run inside a single
    BEGIN IMMEDIATE transaction. The LWW comparison lives in the UPDATE's
    WHERE clause (`? >= updated_at`), so it is atomic and cannot lose an
    update under concurrent writers.
    """
    required = ("id", "state", "start_epoch", "duration", "origin_machine", "updated_at")
    for key in required:
        if key not in session:
            raise ValueError(f"missing field: {key}")
    if session["state"] not in common.ALL_STATES:
        raise ValueError(f"invalid state: {session['state']!r}")

    with contextlib.closing(_connect()) as conn:
        incoming = float(session["updated_at"])
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Always record history (even losers) for a faithful log.
            conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(id, state, start_epoch, duration, origin_machine, "
                "updated_at, ended_at, name, project, kind) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session["id"],
                    session["state"],
                    int(session["start_epoch"]),
                    int(session["duration"]),
                    session["origin_machine"],
                    incoming,
                    session.get("ended_at"),
                    session.get("name"),
                    session.get("project"),
                    session.get("kind"),
                ),
            )
            cur = conn.execute(
                "UPDATE current SET session_id = ?, updated_at = ? "
                "WHERE singleton = 0 AND ? >= updated_at",
                (session["id"], incoming, incoming),
            )
            applied = cur.rowcount == 1
            current = _current_session_locked(conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return applied, current


def end_current(session: dict) -> tuple[bool, dict]:
    """Mark the current session ended under LWW using the provided record."""
    ended = dict(session)
    ended["state"] = "ended"
    ended.setdefault("ended_at", time.time())
    ended["ended_at"] = ended["ended_at"] or time.time()
    return apply_session(ended)


def get_today_sessions(project: str | None = None) -> list[dict]:
    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        params: list = []
        sql = """
            SELECT s.*
            FROM sessions s
            INNER JOIN (
                SELECT id, MAX(updated_at) AS max_updated
                FROM sessions
                WHERE date(start_epoch, 'unixepoch') = date('now')
                {} GROUP BY id
            ) latest ON s.id = latest.id AND s.updated_at = latest.max_updated
            WHERE date(s.start_epoch, 'unixepoch') = date('now')
            {} ORDER BY s.start_epoch ASC
        """
        project_inner = ""
        project_outer = ""
        if project is not None:
            project_inner = "AND project = ?"
            project_outer = "AND s.project = ?"
            params = [project, project]
        rows = conn.execute(sql.format(project_inner, project_outer), params).fetchall()
    return [_row_to_session(r) for r in rows]


def get_projects() -> list[dict]:
    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT DISTINCT project
            FROM sessions
            WHERE project IS NOT NULL
            ORDER BY project ASC
            """
        ).fetchall()
    return [{"project": r["project"]} for r in rows]


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
        if length > MAX_BODY_BYTES:
            # Reject before allocating/reading the oversized body.
            raise RequestTooLarge(f"{length} > {MAX_BODY_BYTES}")
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        if not self._authorized():
            return self._send_json({"error": "unauthorized"}, 401)
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path == "/health":
            return self._send_json({"ok": True})
        if path == "/version":
            return self._send_json({"version": common.version()})
        if path == "/current":
            return self._send_json(get_current_session())
        if path == "/sessions":
            project = qs.get("project", [None])[0]
            return self._send_json(get_today_sessions(project=project))
        if path == "/projects":
            return self._send_json(get_projects())
        return self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authorized():
            return self._send_json({"error": "unauthorized"}, 401)
        try:
            payload = self._read_json()
        except RequestTooLarge:
            return self._send_json({"error": "request too large"}, 413)
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
    sys.stderr.write(f"pomo-server v{common.version()} listening on {HOST}:{PORT} (db={DB_PATH})\n")
    if TOKEN:
        sys.stderr.write("auth: bearer token REQUIRED\n")
    else:
        sys.stderr.write("auth: NONE (network-level only)\n")
    with contextlib.suppress(KeyboardInterrupt):
        httpd.serve_forever()


if __name__ == "__main__":
    main()
