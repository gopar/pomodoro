"""Behavior tests for pomo.py: CLI commands, state transitions, and tear-down.

Tests document current behavior; after bug fixes, expectations are updated
to the correct behaviour and re-verified.
"""

from __future__ import annotations

import builtins
import io
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


if __name__ == "__main__":
    unittest.main()
