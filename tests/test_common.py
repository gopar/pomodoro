"""Behavior tests for common.py: session model, cache, outbox, and config
merge."""

from __future__ import annotations

import os
import unittest

from _util import isolate, patch_attr

import common


class SessionModelTests(unittest.TestCase):
    def test_is_idle(self):
        self.assertTrue(common.is_idle(None))
        self.assertTrue(common.is_idle({"state": "idle"}))
        self.assertTrue(common.is_idle({"state": "ended"}))
        self.assertTrue(common.is_idle({}))
        for state in ("pomodoro", "overtime", "break", "break-overtime"):
            self.assertFalse(common.is_idle({"state": state}), state)

    def test_new_session_fields(self):
        s = common.new_session("pomodoro", 1000, 60, "laptop")
        self.assertEqual(s["state"], "pomodoro")
        self.assertEqual(s["start_epoch"], 1000)
        self.assertEqual(s["duration"], 60)
        self.assertEqual(s["origin_machine"], "laptop")
        self.assertIsNone(s["ended_at"])
        self.assertGreater(s["updated_at"], 0)
        self.assertTrue(s["id"])

    def test_new_session_rejects_invalid_state(self):
        with self.assertRaises(ValueError):
            common.new_session("bogus", 0, 0, "laptop")


class CacheTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_write_then_read_roundtrip(self):
        s = common.new_session("pomodoro", 1000, 60, "laptop")
        common.write_cache(s)
        self.assertEqual(common.read_cache(), s)

    def test_read_cache_missing_returns_none(self):
        self.assertIsNone(common.read_cache())

    def test_read_cache_corrupt_returns_none(self):
        common.ensure_dirs()
        common.CACHE_FILE.write_text("{not json", encoding="utf-8")
        self.assertIsNone(common.read_cache())

    def test_clear_cache_removes_file(self):
        common.write_cache(common.new_session("pomodoro", 1, 60, "laptop"))
        common.clear_cache()
        self.assertIsNone(common.read_cache())


class CacheAtomicityTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_writes_leave_no_temp_files(self):
        common.write_cache(common.new_session("pomodoro", 1, 60, "laptop"))
        leftovers = list(common.CACHE_DIR.glob("*.tmp"))
        self.assertEqual(leftovers, [])


class OutboxTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_enqueue_then_read_roundtrip(self):
        s = common.new_session("pomodoro", 1, 60, "laptop")
        common.enqueue_outbox("session", s)
        common.enqueue_outbox("end", s)
        items = common.read_outbox()
        self.assertEqual([i["action"] for i in items], ["session", "end"])
        self.assertEqual(items[0]["session"], s)

    def test_read_outbox_missing_returns_empty(self):
        self.assertEqual(common.read_outbox(), [])

    def test_rewrite_outbox_replaces_contents(self):
        s = common.new_session("pomodoro", 1, 60, "laptop")
        common.enqueue_outbox("session", s)
        common.enqueue_outbox("session", s)
        common.rewrite_outbox([{"action": "end", "session": s}])
        items = common.read_outbox()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["action"], "end")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        isolate(self)
        # Ensure env override does not leak in from the host.
        self.addCleanup(os.environ.pop, "POMO_SERVER_URL", None)
        os.environ.pop("POMO_SERVER_URL", None)

    def test_defaults_when_no_file(self):
        cfg = common.load_config()
        self.assertEqual(cfg["poll_interval"], 5)
        self.assertTrue(cfg["side_effects"]["focus_mode"])
        self.assertTrue(cfg["machine_name"])

    def test_nested_tables_merge_not_replace(self):
        common.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        common.CONFIG_FILE.write_text(
            "[side_effects]\nfocus_mode = false\n", encoding="utf-8"
        )
        cfg = common.load_config()
        # overridden key
        self.assertFalse(cfg["side_effects"]["focus_mode"])
        # untouched sibling keys retain their defaults (merge, not replace)
        self.assertTrue(cfg["side_effects"]["alarm"])
        self.assertIn("timeout", cfg["hooks"])

    def test_env_override_wins(self):
        os.environ["POMO_SERVER_URL"] = "http://example:9999"
        cfg = common.load_config()
        self.assertEqual(cfg["server_url"], "http://example:9999")


if __name__ == "__main__":
    unittest.main()
