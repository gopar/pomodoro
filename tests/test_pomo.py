"""Behavior tests for pomo.py: CLI commands, state transitions, and tear-down.

Tests document current behavior; after bug fixes, expectations are updated
to the correct behaviour and re-verified.
"""

from __future__ import annotations

import builtins
import io
import json
import sys
import time
import unittest

from _util import isolate, patch_attr

import common
import hooks
import pomo


class Base(unittest.TestCase):
    """Shared test setup: isolated paths, recorded events, no network."""

    def setUp(self):
        isolate(self)
        self.events: list[str] = []
        self.event_sessions: list[dict] = []
        patch_attr(self, hooks, "dispatch",
                   lambda e, s, c, **kw: (
                       self.events.append(e),
                       self.event_sessions.append(s),
                   ))
        patch_attr(self, common, "post_session", lambda url, s: {})
        patch_attr(self, common, "post_end", lambda url, s: {})
        patch_attr(self, common, "enqueue_outbox", lambda a, s: None)
        patch_attr(self, pomo, "_confirm_overwrite", lambda: True)
        self._stdout = io.StringIO()
        self._stderr = io.StringIO()
        patch_attr(self, sys, "stdout", self._stdout)
        patch_attr(self, sys, "stderr", self._stderr)

    def _active(self, state: str = "pomodoro") -> dict:
        s = common.new_session(state, int(time.time()), 25 * 60, "laptop")
        common.write_cache(s)
        return s

    def _assert_cache_state(self, expected_state: str | None):
        session = common.read_cache()
        if expected_state is None:
            self.assertIsNone(session, f"expected no cache, got {session}")
        else:
            self.assertIsNotNone(session, "expected active session but cache is None")
            self.assertEqual(session["state"], expected_state)

    def _mock_input(self, *responses: str):
        """Replace builtins.input with a callable that pops from responses."""
        responses = list(responses)

        def fake_input(prompt=""):
            return responses.pop(0)

        patch_attr(self, builtins, "input", fake_input)


# ---------------------------------------------------------------------------
# Tests documenting current (broken) behaviour
# ---------------------------------------------------------------------------

class CmdBreakTests(Base):
    """pomo break N — behaviour with an active pomodoro."""

    def test_break_with_active_pomodoro_stops_first(self):
        # Given: an active pomodoro
        self._active("pomodoro")
        # When: pomo break 5 is run
        pomo.cmd_break(["5"])
        # Then: session_stop fires first, then break_start
        self.assertEqual(self.events, [hooks.SESSION_STOP, hooks.BREAK_START])
        # Then: cache has the break session
        self._assert_cache_state("break")


class CmdClearTests(Base):
    """pomo clear — stop pomodoro, optionally start a break."""

    def test_clear_with_break_stops_pomodoro_first(self):
        # Given: an active pomodoro, user enters break minutes "5"
        self._active("pomodoro")
        self._mock_input("5")
        # When: pomo clear is run
        pomo.cmd_clear()
        # Then: session_stop fires first, then break_start
        self.assertEqual(self.events, [hooks.SESSION_STOP, hooks.BREAK_START])
        # Then: cache has the break session
        self._assert_cache_state("break")

    def test_clear_invalid_break_input_exits_and_keeps_pomodoro(self):
        # Given: an active pomodoro, user enters non-numeric "x"
        self._active("pomodoro")
        self._mock_input("x")
        # When / Then: SystemExit raised, pomodoro stays active
        with self.assertRaises(SystemExit):
            pomo.cmd_clear()
        self.assertEqual(self.events, [])
        self._assert_cache_state("pomodoro")

    def test_clear_no_break_stops_pomodoro(self):
        # Given: an active pomodoro, user enters empty (no break)
        self._active("pomodoro")
        self._mock_input("")
        # When: pomo clear is run
        pomo.cmd_clear()
        # Then: session_stop fires, cache cleared (this was already correct)
        self.assertEqual(self.events, [hooks.SESSION_STOP])
        self._assert_cache_state(None)

    def test_clear_active_break_stops_it(self):
        # Given: an active break
        self._active("break")
        # When: pomo clear is run
        pomo.cmd_clear()
        # Then: session_stop fires, cache cleared (this was already correct)
        self.assertEqual(self.events, [hooks.SESSION_STOP])
        self._assert_cache_state(None)


class CmdStartTests(Base):
    """pomo N — start pomodoro, overwriting existing session."""

    def test_start_overwrites_active_pomodoro(self):
        # Given: an active pomodoro
        self._active("pomodoro")
        # When: pomo 25 is run
        pomo.cmd_start(["25"])
        # Then: session_stop fires, then pomodoro_start (this was already correct)
        self.assertEqual(self.events, [hooks.SESSION_STOP, hooks.POMODORO_START])
        self._assert_cache_state("pomodoro")


class CmdStatusTests(Base):
    """pomo status [--json] — read current session."""

    def _freeze_time(self, ts: float):
        patch_attr(self, time, "time", lambda: ts)

    def test_status_json_idle_no_cache(self):
        pomo.cmd_status(["--json"])
        self.assertEqual(json.loads(self._stdout.getvalue()),
                         {"state": "idle", "display": "No active session"})

    def test_status_json_idle_ended_cache(self):
        common.write_cache({"state": "ended"})
        pomo.cmd_status(["--json"])
        self.assertEqual(json.loads(self._stdout.getvalue()),
                         {"state": "idle", "display": "No active session"})

    def test_status_json_idle_marker_cache(self):
        common.write_cache({"state": "idle"})
        pomo.cmd_status(["--json"])
        self.assertEqual(json.loads(self._stdout.getvalue()),
                         {"state": "idle", "display": "No active session"})

    def test_status_json_pomodoro_countdown(self):
        now = 1722520000.0
        duration = 25 * 60
        s = common.new_session("pomodoro", int(now - 60), duration, "laptop")
        common.write_cache(s)
        self._freeze_time(now)

        pomo.cmd_status(["--json"])
        out = json.loads(self._stdout.getvalue())
        self.assertEqual(out["state"], "pomodoro")
        self.assertEqual(out["start_epoch"], int(now - 60))
        self.assertEqual(out["duration"], duration)
        self.assertEqual(out["elapsed"], 60)
        self.assertEqual(out["remaining"], duration - 60)
        self.assertEqual(out["display"], "🍅 24:00")

    def test_status_json_pomodoro_overtime(self):
        now = 1722520000.0
        duration = 25 * 60
        start = int(now - duration - 10)
        s = common.new_session("pomodoro", start, duration, "laptop")
        common.write_cache(s)
        self._freeze_time(now)

        pomo.cmd_status(["--json"])
        out = json.loads(self._stdout.getvalue())
        self.assertEqual(out["state"], "overtime")
        self.assertEqual(out["remaining"], -10)
        self.assertEqual(out["display"], "⏰ +0:10")

    def test_status_json_break_countdown(self):
        now = 1722520000.0
        duration = 5 * 60
        s = common.new_session("break", int(now - 30), duration, "laptop")
        common.write_cache(s)
        self._freeze_time(now)

        pomo.cmd_status(["--json"])
        out = json.loads(self._stdout.getvalue())
        self.assertEqual(out["state"], "break")
        self.assertEqual(out["remaining"], duration - 30)
        self.assertEqual(out["display"], "☕ 4:30")

    def test_status_json_break_overtime(self):
        now = 1722520000.0
        duration = 5 * 60
        start = int(now - duration - 5)
        s = common.new_session("break", start, duration, "laptop")
        common.write_cache(s)
        self._freeze_time(now)

        pomo.cmd_status(["--json"])
        out = json.loads(self._stdout.getvalue())
        self.assertEqual(out["state"], "break-overtime")
        self.assertEqual(out["remaining"], -5)
        self.assertEqual(out["display"], "☕ +0:05")

    def test_status_json_already_overtime_in_cache(self):
        now = 1722520000.0
        duration = 25 * 60
        start = int(now - duration - 30)
        s = common.new_session("overtime", start, duration, "laptop")
        common.write_cache(s)
        self._freeze_time(now)

        pomo.cmd_status(["--json"])
        out = json.loads(self._stdout.getvalue())
        self.assertEqual(out["state"], "overtime")
        self.assertEqual(out["remaining"], -30)
        self.assertEqual(out["display"], "⏰ +0:30")

    def test_status_display_key_matches_human_output(self):
        now = 1722520000.0
        duration = 25 * 60
        s = common.new_session("pomodoro", int(now - 60), duration, "laptop")
        common.write_cache(s)
        self._freeze_time(now)

        pomo.cmd_status(["--json"])
        json_display = json.loads(self._stdout.getvalue())["display"]

        self._stdout.truncate(0)
        self._stdout.seek(0)
        pomo.cmd_status([])
        human = self._stdout.getvalue().strip()

        self.assertEqual(json_display, human)

    def test_status_human_pomodoro_countdown(self):
        now = 1722520000.0
        duration = 25 * 60
        s = common.new_session("pomodoro", int(now - 60), duration, "laptop")
        common.write_cache(s)
        self._freeze_time(now)

        pomo.cmd_status([])
        self.assertEqual(self._stdout.getvalue().strip(), "🍅 24:00")

    def test_status_human_overtime(self):
        now = 1722520000.0
        duration = 25 * 60
        start = int(now - duration - 65)
        s = common.new_session("pomodoro", start, duration, "laptop")
        common.write_cache(s)
        self._freeze_time(now)

        pomo.cmd_status([])
        self.assertEqual(self._stdout.getvalue().strip(), "⏰ +1:05")

    def test_status_human_break_countdown(self):
        now = 1722520000.0
        duration = 5 * 60
        s = common.new_session("break", int(now - 30), duration, "laptop")
        common.write_cache(s)
        self._freeze_time(now)

        pomo.cmd_status([])
        self.assertEqual(self._stdout.getvalue().strip(), "☕ 4:30")

    def test_status_human_break_overtime_uses_coffee(self):
        now = 1722520000.0
        duration = 5 * 60
        start = int(now - duration - 10)
        s = common.new_session("break", start, duration, "laptop")
        common.write_cache(s)
        self._freeze_time(now)

        pomo.cmd_status([])
        output = self._stdout.getvalue().strip()
        self.assertIn("☕", output)
        self.assertIn("+", output)
        self.assertEqual(output, "☕ +0:10")

    def test_status_human_idle(self):
        pomo.cmd_status([])
        self.assertEqual(self._stdout.getvalue().strip(), "No active session")


if __name__ == "__main__":
    unittest.main()
