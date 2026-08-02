"""Behavior tests for server.py: last-write-wins, history, and end/idle.

Includes concurrency tests that exercise the WHERE-guarded pointer UPDATE under
concurrent writers (WAL + busy_timeout).
"""

from __future__ import annotations

import datetime
import sqlite3
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from unittest.mock import patch

from _util import isolate

import common
import server


def _session(updated_at: float, state: str = "pomodoro", sid: str | None = None) -> dict:
    s = common.new_session(state, 1000, 60, "laptop")
    if sid is not None:
        s["id"] = sid
    s["updated_at"] = updated_at
    return s


class LWWTests(unittest.TestCase):
    """Tests for apply_session / end_current: LWW semantics and state handling."""

    def setUp(self):
        tmp = isolate(self)
        p = patch.object(server, "DB_PATH", tmp / "data" / "pomo.db")
        p.start()
        self.addCleanup(p.stop)
        server.init_db()

    def test_apply_missing_field_raises(self):
        # When: apply_session is called with a dict missing required fields
        # Then: ValueError is raised
        with self.assertRaises(ValueError):
            server.apply_session({"id": "x"})

    def test_apply_invalid_state_raises(self):
        # Given: a session with a bogus state
        bad = _session(1.0)
        bad["state"] = "bogus"
        # When: apply_session is called
        # Then: ValueError is raised
        with self.assertRaises(ValueError):
            server.apply_session(bad)

    def test_newer_write_wins(self):
        # Given: an older session "a" is applied
        applied, current = server.apply_session(_session(100.0, sid="a"))
        self.assertTrue(applied)
        self.assertEqual(current["id"], "a")
        # When: a newer session "b" is applied
        applied, current = server.apply_session(_session(200.0, sid="b"))
        # Then: "b" wins the current pointer
        self.assertTrue(applied)
        self.assertEqual(current["id"], "b")

    def test_older_write_loses_but_is_recorded_in_history(self):
        # Given: a newer session "winner" is applied
        server.apply_session(_session(200.0, sid="winner"))
        # When: an older session "loser" is applied
        applied, current = server.apply_session(_session(100.0, sid="loser"))
        # Then: the write loses the pointer (applied=False)
        self.assertFalse(applied)
        self.assertEqual(current["id"], "winner")
        # Then: loser is still recorded in history even though it lost
        with sqlite3.connect(server.DB_PATH) as conn:
            ids = {r[0] for r in conn.execute("SELECT id FROM sessions")}
        self.assertIn("loser", ids)
        self.assertIn("winner", ids)

    def test_ended_pointer_reports_idle(self):
        # Given: an active session applied, then ended
        server.apply_session(_session(100.0, sid="a"))
        server.end_current(_session(200.0, sid="a"))
        # When / Then: get_current_session reports idle
        self.assertTrue(common.is_idle(server.get_current_session()))

    def test_ended_pointer_idle_response_includes_timestamp_and_session_id(self):
        # Given: an active session applied, then ended
        s = _session(200.0, sid="a")
        server.apply_session(s)
        server.end_current(_session(300.0, sid="a"))
        # When: get_current_session reports idle because the session ended
        current = server.get_current_session()
        # Then: the idle response carries updated_at and session_id so
        # agents can compare whether the remote-end is newer than local
        self.assertTrue(common.is_idle(current))
        self.assertIn("updated_at", current)
        self.assertIn("session_id", current)
        self.assertEqual(current["session_id"], "a")

    def test_end_current_sets_ended_at(self):
        # Given: an active session
        server.apply_session(_session(100.0, sid="a"))
        # When: end_current is called
        applied, _ = server.end_current(_session(200.0, sid="a"))
        # Then: session is marked ended with ended_at set
        self.assertTrue(applied)
        with sqlite3.connect(server.DB_PATH) as conn:
            row = conn.execute(
                "SELECT state, ended_at FROM sessions WHERE id = 'a' "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row[0], "ended")
        self.assertIsNotNone(row[1])

    def test_stale_write_does_not_overwrite_newer_history_row(self):
        # Given: session "a" applied at t=200
        server.apply_session(_session(200.0, sid="a"))
        # When: same session "a" re-applied at t=100 (stale)
        applied, _ = server.apply_session(_session(100.0, sid="a"))
        # Then: write loses the pointer
        self.assertFalse(applied)
        # Then: both rows exist in history — the stale write didn't overwrite
        # the newer one (composite PK prevents collision)
        with sqlite3.connect(server.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT updated_at, state FROM sessions "
                "WHERE id = 'a' ORDER BY updated_at DESC"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["updated_at"], 200.0)  # newer row intact
        self.assertEqual(rows[1]["updated_at"], 100.0)  # stale row also present

    def test_name_survives_roundtrip(self):
        # Given: a session is created with a name
        s = _session(100.0, sid="named")
        s["name"] = "project-x"
        # When: it is applied and read back
        server.apply_session(s)
        current = server.get_current_session()
        # Then: name is preserved
        self.assertEqual(current["name"], "project-x")

    def test_get_today_sessions_returns_today_only(self):
        # Given: a session from today and one from yesterday
        today_epoch = int(time.time())
        yesterday_epoch = today_epoch - 90000
        s_today = common.new_session("pomodoro", today_epoch, 60, "laptop")
        s_yesterday = common.new_session("pomodoro", yesterday_epoch, 60, "laptop")
        server.apply_session(s_today)
        server.apply_session(s_yesterday)
        # When: get_today_sessions is called
        sessions = server.get_today_sessions()
        # Then: only today's session is returned
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["id"], s_today["id"])

    def test_get_today_sessions_deduplicates_to_latest(self):
        # Given: the same session id written twice today
        now = int(time.time())
        start_epoch = now - 60
        s1 = common.new_session("pomodoro", start_epoch, 60, "laptop")
        s1["updated_at"] = now - 1
        s2 = dict(s1)
        s2["state"] = "ended"
        s2["updated_at"] = now
        s2["ended_at"] = now
        server.apply_session(s1)
        server.apply_session(s2)
        # When: get_today_sessions is called
        sessions = server.get_today_sessions()
        # Then: only the latest row (ended) is returned
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["state"], "ended")

    def test_get_today_sessions_returns_empty_when_none(self):
        # Given: no sessions exist
        # When: get_today_sessions is called
        sessions = server.get_today_sessions()
        # Then: empty list
        self.assertEqual(sessions, [])


class ConcurrencyTests(unittest.TestCase):
    """LWW must hold under concurrent writers.

    apply_session runs the history insert and a WHERE-guarded pointer UPDATE
    inside one BEGIN IMMEDIATE transaction (WAL + busy_timeout), so concurrent
    writers cannot lose the highest-updated_at winner.
    """

    def setUp(self):
        tmp = isolate(self)
        p = patch.object(server, "DB_PATH", tmp / "data" / "pomo.db")
        p.start()
        self.addCleanup(p.stop)
        server.init_db()

    def test_concurrent_apply_keeps_highest_updated_at(self):
        # Given: n sessions with sequential updated_at timestamps
        n = 25
        sessions = [_session(float(i + 1), sid=f"s{i:02d}") for i in range(n)]

        errors: list[Exception] = []
        barrier = threading.Barrier(n)

        def worker(s: dict) -> None:
            try:
                barrier.wait()
                server.apply_session(s)
            except Exception as exc:  # noqa: BLE001 - collected for assertion
                errors.append(exc)

        # When: all sessions are applied concurrently from n threads
        with ThreadPoolExecutor(max_workers=n) as pool:
            list(pool.map(worker, sessions))

        # Then: no errors, highest-updated_at wins, all n sessions in history
        self.assertEqual(errors, [], f"apply_session raised under load: {errors}")

        current = server.get_current_session()
        self.assertEqual(
            current["id"], f"s{n - 1:02d}",
            "current pointer is not the highest-updated_at session",
        )

        with sqlite3.connect(server.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.assertEqual(count, n, "not all sessions were recorded in history")

    def test_concurrent_mixed_order_selects_global_max(self):
        # Given: updated_at values in shuffled order across threads
        # The winner must be the global maximum regardless of arrival order.
        pairs = [(f"s{i:02d}", float(v)) for i, v in
                 enumerate([50, 10, 99, 30, 70, 5, 88, 42, 60, 15])]
        max_id = max(pairs, key=lambda p: p[1])[0]

        errors: list[Exception] = []
        barrier = threading.Barrier(len(pairs))

        def worker(pair) -> None:
            sid, ts = pair
            try:
                barrier.wait()
                server.apply_session(_session(ts, sid=sid))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        # When: all sessions are applied concurrently in shuffled order
        with ThreadPoolExecutor(max_workers=len(pairs)) as pool:
            list(pool.map(worker, pairs))

        # Then: no errors, global max wins, all sessions in history
        self.assertEqual(errors, [], f"apply_session raised under load: {errors}")
        self.assertEqual(server.get_current_session()["id"], max_id)

        with sqlite3.connect(server.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.assertEqual(count, len(pairs))


if __name__ == "__main__":
    unittest.main()
