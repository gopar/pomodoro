"""Behavior tests for common.py: session model, cache, outbox, and config merge."""

from __future__ import annotations

import json

import pytest

from pomo import common


class TestSessionModel:
    def test_version_returns_value(self):
        # Given: a VERSION file
        # When: version() is called
        v = common.version()
        # Then: it returns a non-empty, non-unknown value
        assert v
        assert v != "unknown"

    def test_is_idle(self):
        # Given: no state, idle markers, ended markers, and active states
        # When / Then: only active states are not idle
        assert common.is_idle(None)
        assert common.is_idle({"state": "idle"})
        assert common.is_idle({"state": "ended"})
        assert common.is_idle({})
        for state in ("pomodoro", "overtime", "break", "break-overtime"):
            assert not common.is_idle({"state": state})

    def test_new_session_fields(self):
        # When: a new pomodoro session is created
        s = common.new_session("pomodoro", 1000, 60, "laptop")
        # Then: all fields are populated correctly
        assert s["state"] == "pomodoro"
        assert s["start_epoch"] == 1000
        assert s["duration"] == 60
        assert s["origin_machine"] == "laptop"
        assert s["ended_at"] is None
        assert s["updated_at"] > 0
        assert s["id"]

    def test_new_session_rejects_invalid_state(self):
        # When / Then: creating a session with a bogus state raises ValueError
        with pytest.raises(ValueError):
            common.new_session("bogus", 0, 0, "laptop")

    def test_new_session_name_defaults_to_none(self):
        # When: a new session is created without a name
        s = common.new_session("pomodoro", 1000, 60, "laptop")
        # Then: name is None
        assert s["name"] is None

    def test_new_session_name_is_stored(self):
        # When: a new session is created with a name
        s = common.new_session("pomodoro", 1000, 60, "laptop", name="project-x")
        # Then: name is stored
        assert s["name"] == "project-x"

    def test_new_session_project_defaults_to_none(self):
        # When: a new session is created without a project
        s = common.new_session("pomodoro", 1000, 60, "laptop")
        # Then: project is None
        assert s["project"] is None

    def test_new_session_project_is_stored(self):
        # When: a new session is created with a project
        s = common.new_session("pomodoro", 1000, 60, "laptop", project="website")
        # Then: project is stored
        assert s["project"] == "website"

    def test_new_session_kind_is_pomodoro(self):
        # When: a pomodoro session is created
        s = common.new_session("pomodoro", 1000, 60, "laptop")
        # Then: kind is "pomodoro"
        assert s["kind"] == "pomodoro"

    def test_new_session_kind_is_break(self):
        # When: a break session is created
        s = common.new_session("break", 1000, 60, "laptop")
        # Then: kind is "break"
        assert s["kind"] == "break"

    def test_new_session_overtime_kind_is_pomodoro(self):
        # When: a pomodoro transitions to overtime via the agent
        # Then: the kind should be derived from the original state
        s = common.new_session("pomodoro", 1000, 60, "laptop")
        s["state"] = "overtime"
        # kind is still "pomodoro" because it was set at creation
        assert s["kind"] == "pomodoro"

    def test_new_session_break_overtime_kind_is_break(self):
        # When: a break enters overtime
        s = common.new_session("break", 1000, 60, "laptop")
        s["state"] = "break-overtime"
        # kind is still "break"
        assert s["kind"] == "break"


class TestCache:
    def test_write_then_read_roundtrip(self):
        # Given: a new session
        s = common.new_session("pomodoro", 1000, 60, "laptop")
        # When: written to cache then read back
        common.write_cache(s)
        # Then: the read session matches the original
        assert common.read_cache() == s

    def test_read_cache_missing_returns_none(self):
        # Given: no cache file exists
        # When / Then: reading returns None
        assert common.read_cache() is None

    def test_read_cache_corrupt_returns_none(self):
        # Given: a corrupt JSON cache file
        common.ensure_dirs()
        common.CACHE_FILE.write_text("{not json", encoding="utf-8")
        # When / Then: reading returns None instead of raising
        assert common.read_cache() is None

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
        assert common.read_cache() is None

    def test_read_cache_rejects_non_dict(self):
        # Given: cache is a JSON array instead of an object
        common.ensure_dirs()
        common.CACHE_FILE.write_text("[1, 2, 3]", encoding="utf-8")
        # When / Then: reading returns None
        assert common.read_cache() is None

    def test_read_cache_accepts_idle_marker(self):
        # Given: cache contains an idle state marker
        common.ensure_dirs()
        common.CACHE_FILE.write_text(json.dumps({"state": "idle"}), encoding="utf-8")
        # When / Then: the idle marker is read back as-is
        assert common.read_cache() == {"state": "idle"}

    def test_clear_cache_removes_file(self):
        # Given: an active session written to cache
        common.write_cache(common.new_session("pomodoro", 1, 60, "laptop"))
        # When: cache is cleared
        common.clear_cache()
        # Then: reading returns None (file is gone)
        assert common.read_cache() is None


class TestCacheAtomicity:
    def test_writes_leave_no_temp_files(self):
        # When: a session is written to cache
        common.write_cache(common.new_session("pomodoro", 1, 60, "laptop"))
        # Then: no temporary .tmp files are left behind (atomic write)
        leftovers = list(common.CACHE_DIR.glob("*.tmp"))
        assert leftovers == []


class TestOutbox:
    def test_enqueue_then_read_roundtrip(self):
        # Given: a session to enqueue
        s = common.new_session("pomodoro", 1, 60, "laptop")
        # When: two items are enqueued (session + end)
        common.enqueue_outbox("session", s)
        common.enqueue_outbox("end", s)
        # Then: both are read back in order with correct actions and payload
        items = common.read_outbox()
        assert [i["action"] for i in items] == ["session", "end"]
        assert items[0]["session"] == s

    def test_read_outbox_missing_returns_empty(self):
        # Given: no outbox file exists
        # When / Then: reading returns an empty list
        assert common.read_outbox() == []

    def test_rewrite_outbox_replaces_contents(self):
        # Given: two items enqueued
        s = common.new_session("pomodoro", 1, 60, "laptop")
        common.enqueue_outbox("session", s)
        common.enqueue_outbox("session", s)
        # When: outbox is rewritten with a single new item
        common.rewrite_outbox([{"action": "end", "session": s}])
        # Then: only the new item remains
        items = common.read_outbox()
        assert len(items) == 1
        assert items[0]["action"] == "end"

    def test_read_outbox_skips_corrupt_lines(self):
        # Given: an outbox file with a corrupt line between two valid ones
        s = common.new_session("pomodoro", 1, 60, "laptop")
        common.enqueue_outbox("session", s)
        common.ensure_dirs()
        with common.OUTBOX_FILE.open("a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        common.enqueue_outbox("end", s)
        # When: outbox is read
        items = common.read_outbox()
        # Then: the two valid items are returned, corrupt line is skipped
        assert [i["action"] for i in items] == ["session", "end"]


class TestConfig:
    def test_defaults_when_no_file(self, monkeypatch):
        # Given: no config file exists and POMO_SERVER_URL is not in the environment
        monkeypatch.delenv("POMO_SERVER_URL", raising=False)
        # When: config is loaded
        cfg = common.load_config()
        # Then: all defaults are present
        assert cfg["poll_interval"] == 5
        assert not cfg["run_for_remote_sessions"]
        assert cfg["hooks"]["enabled"]
        assert cfg["machine_name"]

    def test_nested_tables_merge_not_replace(self, monkeypatch):
        # Given: a config file overriding only hooks.timeout
        monkeypatch.delenv("POMO_SERVER_URL", raising=False)
        common.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        common.CONFIG_FILE.write_text("[hooks]\ntimeout = 3\n", encoding="utf-8")
        # When: config is loaded
        cfg = common.load_config()
        # Then: overridden key takes new value
        assert cfg["hooks"]["timeout"] == 3
        # Then: untouched sibling keys retain their defaults (merge, not replace)
        assert cfg["hooks"]["enabled"]
        assert "dir" in cfg["hooks"]

    def test_env_override_wins(self, monkeypatch):
        # Given: POMO_SERVER_URL env var set
        monkeypatch.setenv("POMO_SERVER_URL", "http://example:9999")
        # When: config is loaded
        cfg = common.load_config()
        # Then: env var wins over file/default
        assert cfg["server_url"] == "http://example:9999"
