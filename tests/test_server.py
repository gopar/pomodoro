"""Behavior tests for server.py: last-write-wins, history, and end/idle.

Includes an intentionally hard-red concurrency test that documents the
non-atomic read-modify-write of the `current` pointer (track B hardening).
"""

from __future__ import annotations

import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from _util import isolate, patch_attr

import common
import server


def _session(updated_at: float, state: str = "pomodoro", sid: str | None = None) -> dict:
    s = common.new_session(state, 1000, 60, "laptop")
    if sid is not None:
        s["id"] = sid
    s["updated_at"] = updated_at
    return s


class LWWTests(unittest.TestCase):
    def setUp(self):
        tmp = isolate(self)
        patch_attr(self, server, "DB_PATH", tmp / "data" / "pomo.db")
        server.init_db()

    def test_apply_missing_field_raises(self):
        with self.assertRaises(ValueError):
            server.apply_session({"id": "x"})

    def test_apply_invalid_state_raises(self):
        bad = _session(1.0)
        bad["state"] = "bogus"
        with self.assertRaises(ValueError):
            server.apply_session(bad)

    def test_newer_write_wins(self):
        applied, current = server.apply_session(_session(100.0, sid="a"))
        self.assertTrue(applied)
        self.assertEqual(current["id"], "a")

        applied, current = server.apply_session(_session(200.0, sid="b"))
        self.assertTrue(applied)
        self.assertEqual(current["id"], "b")

    def test_older_write_loses_but_is_recorded_in_history(self):
        server.apply_session(_session(200.0, sid="winner"))
        applied, current = server.apply_session(_session(100.0, sid="loser"))

        self.assertFalse(applied)
        self.assertEqual(current["id"], "winner")

        # Loser must still be in history even though it lost the pointer.
        with sqlite3.connect(server.DB_PATH) as conn:
            ids = {r[0] for r in conn.execute("SELECT id FROM sessions")}
        self.assertIn("loser", ids)
        self.assertIn("winner", ids)

    def test_ended_pointer_reports_idle(self):
        server.apply_session(_session(100.0, sid="a"))
        server.end_current(_session(200.0, sid="a"))
        self.assertTrue(common.is_idle(server.get_current_session()))

    def test_end_current_sets_ended_at(self):
        server.apply_session(_session(100.0, sid="a"))
        applied, _ = server.end_current(_session(200.0, sid="a"))
        self.assertTrue(applied)
        with sqlite3.connect(server.DB_PATH) as conn:
            row = conn.execute(
                "SELECT state, ended_at FROM sessions WHERE id = 'a'"
            ).fetchone()
        self.assertEqual(row[0], "ended")
        self.assertIsNotNone(row[1])


class ConcurrencyTests(unittest.TestCase):
    """EXPECTED TO FAIL until server WAL/atomic-update hardening (track B).

    The read-modify-write of the `current` pointer in apply_session spans two
    statements on separate connections, so concurrent writers can lose the
    highest-updated_at winner or hit `database is locked`. This test documents
    that race and should stay RED until the server is hardened.
    """

    def setUp(self):
        tmp = isolate(self)
        patch_attr(self, server, "DB_PATH", tmp / "data" / "pomo.db")
        server.init_db()

    def test_concurrent_apply_keeps_highest_updated_at(self):
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

        with ThreadPoolExecutor(max_workers=n) as pool:
            list(pool.map(worker, sessions))

        self.assertEqual(errors, [], f"apply_session raised under load: {errors}")

        current = server.get_current_session()
        self.assertEqual(
            current["id"], f"s{n - 1:02d}",
            "current pointer is not the highest-updated_at session",
        )

        with sqlite3.connect(server.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.assertEqual(count, n, "not all sessions were recorded in history")


if __name__ == "__main__":
    unittest.main()
