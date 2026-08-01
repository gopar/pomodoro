"""Behavior tests for common.py: session model, cache, outbox, and config merge."""

from __future__ import annotations

import json
import os
import unittest

from _util import isolate, patch_attr

import common


class SessionModelTests(unittest.TestCase):
    def test_is_idle(self):
        # Given: no state, idle markers, ended markers, and active states
        # When / Then: only active states are not idle
        self.assertTrue(common.is_idle(None))
        self.assertTrue(common.is_idle({"state": "idle"}))
        self.assertTrue(common.is_idle({"state": "ended"}))
        self.assertTrue(common.is_idle({}))
        for state in ("pomodoro", "overtime", "break", "break-overtime"):
            self.assertFalse(common.is_idle({"state": state}), state)

    def test_new_session_fields(self):
        # When: a new pomodoro session is created
        s = common.new_session("pomodoro", 1000, 60, "laptop")
        # Then: all fields are populated correctly
        self.assertEqual(s["state"], "pomodoro")
        self.assertEqual(s["start_epoch"], 1000)
        self.assertEqual(s["duration"], 60)
        self.assertEqual(s["origin_machine"], "laptop")
        self.assertIsNone(s["ended_at"])
        self.assertGreater(s["updated_at"], 0)
        self.assertTrue(s["id"])

    def test_new_session_rejects_invalid_state(self):
        # When / Then: creating a session with a bogus state raises ValueError
        with self.assertRaises(ValueError):
            common.new_session("bogus", 0, 0, "laptop")


class CacheTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_write_then_read_roundtrip(self):
        # Given: a new session
        s = common.new_session("pomodoro", 1000, 60, "laptop")
        # When: written to cache then read back
        common.write_cache(s)
        # Then: the read session matches the original
        self.assertEqual(common.read_cache(), s)

    def test_read_cache_missing_returns_none(self):
        # Given: no cache file exists
        # When / Then: reading returns None
        self.assertIsNone(common.read_cache())

    def test_read_cache_corrupt_returns_none(self):
        # Given: a corrupt JSON cache file
        common.ensure_dirs()
        common.CACHE_FILE.write_text("{not json", encoding="utf-8")
        # When / Then: reading returns None instead of raising
        self.assertIsNone(common.read_cache())

    def test_read_cache_rejects_active_session_missing_fields(self):
        # Active state but no start_epoch/duration -> treat as no session so the
        # timer can't KeyError on it.
        # Given: cache has active state but missing required numeric fields
        common.ensure_dirs()
        common.CACHE_FILE.write_text(
            json.dumps({"state": "pomodoro", "id": "x", "updated_at": 1.0}),
            encoding="utf-8",
        )
        # When / Then: reading returns None (self-heals, no crash)
        self.assertIsNone(common.read_cache())

    def test_read_cache_rejects_non_dict(self):
        # Given: cache is a JSON array instead of an object
        common.ensure_dirs()
        common.CACHE_FILE.write_text("[1, 2, 3]", encoding="utf-8")
        # When / Then: reading returns None
        self.assertIsNone(common.read_cache())

    def test_read_cache_accepts_idle_marker(self):
        # Given: cache contains an idle state marker
        common.ensure_dirs()
        common.CACHE_FILE.write_text(json.dumps({"state": "idle"}), encoding="utf-8")
        # When / Then: the idle marker is read back as-is
        self.assertEqual(common.read_cache(), {"state": "idle"})

    def test_clear_cache_removes_file(self):
        # Given: an active session written to cache
        common.write_cache(common.new_session("pomodoro", 1, 60, "laptop"))
        # When: cache is cleared
        common.clear_cache()
        # Then: reading returns None (file is gone)
        self.assertIsNone(common.read_cache())


class CacheAtomicityTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_writes_leave_no_temp_files(self):
        # When: a session is written to cache
        common.write_cache(common.new_session("pomodoro", 1, 60, "laptop"))
        # Then: no temporary .tmp files are left behind (atomic write)
        leftovers = list(common.CACHE_DIR.glob("*.tmp"))
        self.assertEqual(leftovers, [])


class OutboxTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_enqueue_then_read_roundtrip(self):
        # Given: a session to enqueue
        s = common.new_session("pomodoro", 1, 60, "laptop")
        # When: two items are enqueued (session + end)
        common.enqueue_outbox("session", s)
        common.enqueue_outbox("end", s)
        # Then: both are read back in order with correct actions and payload
        items = common.read_outbox()
        self.assertEqual([i["action"] for i in items], ["session", "end"])
        self.assertEqual(items[0]["session"], s)

    def test_read_outbox_missing_returns_empty(self):
        # Given: no outbox file exists
        # When / Then: reading returns an empty list
        self.assertEqual(common.read_outbox(), [])

    def test_rewrite_outbox_replaces_contents(self):
        # Given: two items enqueued
        s = common.new_session("pomodoro", 1, 60, "laptop")
        common.enqueue_outbox("session", s)
        common.enqueue_outbox("session", s)
        # When: outbox is rewritten with a single new item
        common.rewrite_outbox([{"action": "end", "session": s}])
        # Then: only the new item remains
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
        # Given: no config file exists
        # When: config is loaded
        cfg = common.load_config()
        # Then: all defaults are present
        self.assertEqual(cfg["poll_interval"], 5)
        self.assertFalse(cfg["run_for_remote_sessions"])
        self.assertTrue(cfg["hooks"]["enabled"])
        self.assertTrue(cfg["machine_name"])

    def test_nested_tables_merge_not_replace(self):
        # Given: a config file overriding only hooks.timeout
        common.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        common.CONFIG_FILE.write_text(
            "[hooks]\ntimeout = 3\n", encoding="utf-8"
        )
        # When: config is loaded
        cfg = common.load_config()
        # Then: overridden key takes new value
        self.assertEqual(cfg["hooks"]["timeout"], 3)
        # Then: untouched sibling keys retain their defaults (merge, not replace)
        self.assertTrue(cfg["hooks"]["enabled"])
        self.assertIn("dir", cfg["hooks"])

    def test_env_override_wins(self):
        # Given: POMO_SERVER_URL env var set
        os.environ["POMO_SERVER_URL"] = "http://example:9999"
        # When: config is loaded
        cfg = common.load_config()
        # Then: env var wins over file/default
        self.assertEqual(cfg["server_url"], "http://example:9999")


if __name__ == "__main__":
    unittest.main()
