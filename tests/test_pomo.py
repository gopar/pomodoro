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
from datetime import datetime

from unittest.mock import patch

from _util import isolate

import common
import hooks
import pomo


class Base(unittest.TestCase):
    """Shared test setup: isolated paths, recorded events, no network."""

    def setUp(self):
        isolate(self)
        self.events: list[str] = []
        self.event_sessions: list[dict] = []

        p = patch.object(hooks, "dispatch",
                         side_effect=lambda e, s, c, **kw: (
                             self.events.append(e),
                             self.event_sessions.append(s),
                         ))
        p.start(); self.addCleanup(p.stop)

        p = patch.object(common, "post_session", return_value={})
        p.start(); self.addCleanup(p.stop)
        p = patch.object(common, "post_end", return_value={})
        p.start(); self.addCleanup(p.stop)
        p = patch.object(common, "enqueue_outbox", return_value=None)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(pomo, "_confirm_overwrite", return_value=True)
        p.start(); self.addCleanup(p.stop)

        self._stdout = io.StringIO()
        self._stderr = io.StringIO()
        p = patch.object(sys, "stdout", self._stdout)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(sys, "stderr", self._stderr)
        p.start(); self.addCleanup(p.stop)

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


# ---------------------------------------------------------------------------
# Tests documenting current (broken) behaviour
# ---------------------------------------------------------------------------

class CmdBreakTests(Base):
    """pomo break N — behaviour with an active pomodoro."""

    def test_break_with_active_pomodoro_stops_first(self):
        # Given: an active pomodoro
        self._active("pomodoro")
        # When: pomo break 5 is run
        pomo.cmd_break(5)
        # Then: session_stop fires first, then break_start
        self.assertEqual(self.events, [hooks.SESSION_STOP, hooks.BREAK_START])
        # Then: cache has the break session
        self._assert_cache_state("break")


class CmdClearTests(Base):
    """pomo clear — stop pomodoro, optionally start a break."""

    @patch.object(builtins, "input", side_effect=["5"])
    def test_clear_with_break_stops_pomodoro_first(self, _mock):
        # Given: an active pomodoro, user enters break minutes "5"
        self._active("pomodoro")
        # When: pomo clear is run
        pomo.cmd_clear()
        # Then: session_stop fires first, then break_start
        self.assertEqual(self.events, [hooks.SESSION_STOP, hooks.BREAK_START])
        # Then: cache has the break session
        self._assert_cache_state("break")

    @patch.object(builtins, "input", side_effect=["x", "5"])
    def test_clear_invalid_then_valid_break(self, _mock):
        # Given: an active pomodoro
        # When: user enters invalid "x" (re-prompted), then valid "5"
        self._active("pomodoro")
        pomo.cmd_clear()
        # Then: session_stop and break_start fire; cache has break session
        self.assertEqual(self.events, [hooks.SESSION_STOP, hooks.BREAK_START])
        self._assert_cache_state("break")

    @patch.object(builtins, "input", side_effect=["x", "y", ""])
    def test_clear_invalid_then_empty_clears_pomodoro(self, _mock):
        # Given: an active pomodoro
        # When: user enters invalid "x" (re-prompted), "y" (re-prompted),
        #       then "" (skip)
        self._active("pomodoro")
        pomo.cmd_clear()
        # Then: session_stop fires, no break, cache cleared
        self.assertEqual(self.events, [hooks.SESSION_STOP])
        self._assert_cache_state(None)

    @patch.object(builtins, "input", side_effect=[""])
    def test_clear_no_break_stops_pomodoro(self, _mock):
        # Given: an active pomodoro, user enters empty (no break)
        self._active("pomodoro")
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

    @patch.object(builtins, "input", side_effect=[""])
    def test_clear_when_idle_is_noop(self, _mock):
        # Given: no active session (cache is empty/idle)
        common.clear_cache()
        # When: pomo clear is run with empty input (skip break)
        pomo.cmd_clear()
        # Then: no hooks fire, no pushes, no crash
        self.assertEqual(self.events, [])
        self._assert_cache_state(None)


class StopFunctionTests(Base):
    """stop() — low-level session teardown."""

    def test_stop_with_none_is_noop(self):
        # Given: nothing active
        common.clear_cache()
        # When: stop(None) is called
        pomo.stop(None)
        # Then: no hooks fire, no pushes, cache unchanged
        self.assertEqual(self.events, [])
        self._assert_cache_state(None)

    def test_stop_active_session_clears_and_fires_hook(self):
        # Given: an active pomodoro
        self._active("pomodoro")
        session = common.read_cache()
        # When: stop is called with the active session
        pomo.stop(session)
        # Then: session_stop fires, cache cleared
        self.assertEqual(self.events, [hooks.SESSION_STOP])
        self._assert_cache_state(None)


class CmdStartTests(Base):
    """pomo N — start pomodoro, overwriting existing session."""

    def test_start_overwrites_active_pomodoro(self):
        # Given: an active pomodoro
        self._active("pomodoro")
        # When: pomo 25 is run
        pomo.cmd_start(25)
        # Then: session_stop fires, then pomodoro_start (this was already correct)
        self.assertEqual(self.events, [hooks.SESSION_STOP, hooks.POMODORO_START])
        self._assert_cache_state("pomodoro")


class CmdStatusTests(Base):
    """pomo status [--json] — read current session."""

    def test_status_json_idle_no_cache(self):
        pomo.cmd_status(json_output=True)
        self.assertEqual(json.loads(self._stdout.getvalue()),
                         {"state": "idle", "display": "No active session"})

    def test_status_json_idle_ended_cache(self):
        common.write_cache({"state": "ended"})
        pomo.cmd_status(json_output=True)
        self.assertEqual(json.loads(self._stdout.getvalue()),
                         {"state": "idle", "display": "No active session"})

    def test_status_json_idle_marker_cache(self):
        common.write_cache({"state": "idle"})
        pomo.cmd_status(json_output=True)
        self.assertEqual(json.loads(self._stdout.getvalue()),
                         {"state": "idle", "display": "No active session"})

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_json_pomodoro_countdown(self, _mock):
        now = 1722520000.0
        duration = 25 * 60
        s = common.new_session("pomodoro", int(now - 60), duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status(json_output=True)
        out = json.loads(self._stdout.getvalue())
        self.assertEqual(out["state"], "pomodoro")
        self.assertEqual(out["start_epoch"], int(now - 60))
        self.assertEqual(out["duration"], duration)
        self.assertEqual(out["elapsed"], 60)
        self.assertEqual(out["remaining"], duration - 60)
        self.assertEqual(out["display"], "🍅 24:00")

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_json_pomodoro_overtime(self, _mock):
        now = 1722520000.0
        duration = 25 * 60
        start = int(now - duration - 10)
        s = common.new_session("pomodoro", start, duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status(json_output=True)
        out = json.loads(self._stdout.getvalue())
        self.assertEqual(out["state"], "overtime")
        self.assertEqual(out["remaining"], -10)
        self.assertEqual(out["display"], "⏰ +00:10")

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_json_break_countdown(self, _mock):
        now = 1722520000.0
        duration = 5 * 60
        s = common.new_session("break", int(now - 30), duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status(json_output=True)
        out = json.loads(self._stdout.getvalue())
        self.assertEqual(out["state"], "break")
        self.assertEqual(out["remaining"], duration - 30)
        self.assertEqual(out["display"], "☕ 04:30")

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_json_break_overtime(self, _mock):
        now = 1722520000.0
        duration = 5 * 60
        start = int(now - duration - 5)
        s = common.new_session("break", start, duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status(json_output=True)
        out = json.loads(self._stdout.getvalue())
        self.assertEqual(out["state"], "break-overtime")
        self.assertEqual(out["remaining"], -5)
        self.assertEqual(out["display"], "☕ +00:05")

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_json_already_overtime_in_cache(self, _mock):
        now = 1722520000.0
        duration = 25 * 60
        start = int(now - duration - 30)
        s = common.new_session("overtime", start, duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status(json_output=True)
        out = json.loads(self._stdout.getvalue())
        self.assertEqual(out["state"], "overtime")
        self.assertEqual(out["remaining"], -30)
        self.assertEqual(out["display"], "⏰ +00:30")

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_display_key_matches_human_output(self, _mock):
        now = 1722520000.0
        duration = 25 * 60
        s = common.new_session("pomodoro", int(now - 60), duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status(json_output=True)
        json_display = json.loads(self._stdout.getvalue())["display"]

        self._stdout.truncate(0)
        self._stdout.seek(0)
        pomo.cmd_status()
        human = self._stdout.getvalue().strip()

        self.assertEqual(json_display, human)

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_human_pomodoro_countdown(self, _mock):
        now = 1722520000.0
        duration = 25 * 60
        s = common.new_session("pomodoro", int(now - 60), duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status()
        self.assertEqual(self._stdout.getvalue().strip(), "🍅 24:00")

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_human_overtime(self, _mock):
        now = 1722520000.0
        duration = 25 * 60
        start = int(now - duration - 65)
        s = common.new_session("pomodoro", start, duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status()
        self.assertEqual(self._stdout.getvalue().strip(), "⏰ +01:05")

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_human_break_countdown(self, _mock):
        now = 1722520000.0
        duration = 5 * 60
        s = common.new_session("break", int(now - 30), duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status()
        self.assertEqual(self._stdout.getvalue().strip(), "☕ 04:30")

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_human_break_overtime_uses_coffee(self, _mock):
        now = 1722520000.0
        duration = 5 * 60
        start = int(now - duration - 10)
        s = common.new_session("break", start, duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status()
        output = self._stdout.getvalue().strip()
        self.assertIn("☕", output)
        self.assertIn("+", output)
        self.assertEqual(output, "☕ +00:10")

    def test_status_human_idle(self):
        pomo.cmd_status()
        self.assertEqual(self._stdout.getvalue().strip(), "No active session")

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_named_session_display(self, _mock):
        # Given: a named pomodoro session
        now = 1722520000.0
        duration = 25 * 60
        s = common.new_session("pomodoro", int(now - 60), duration,
                                "laptop", name="project-x")
        common.write_cache(s)

        # When: status is requested
        pomo.cmd_status()
        # Then: name appears after the timer
        self.assertEqual(self._stdout.getvalue().strip(), "🍅 24:00 [project-x]")

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_named_session_json_includes_name(self, _mock):
        # Given: a named pomodoro session
        now = 1722520000.0
        duration = 25 * 60
        s = common.new_session("pomodoro", int(now - 60), duration,
                                "laptop", name="project-x")
        common.write_cache(s)

        # When: status --json is requested
        pomo.cmd_status(json_output=True)
        # Then: JSON output includes name
        out = json.loads(self._stdout.getvalue())
        self.assertEqual(out["name"], "project-x")
        self.assertEqual(out["display"], "🍅 24:00 [project-x]")


    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_with_project_display(self, _mock):
        # Given: a session with a project
        now = 1722520000.0
        duration = 25 * 60
        s = common.new_session("pomodoro", int(now - 60), duration,
                                "laptop", project="website")
        common.write_cache(s)
        # When: status is requested
        pomo.cmd_status()
        # Then: project is shown in brackets
        self.assertEqual(self._stdout.getvalue().strip(), "🍅 24:00 [website]")

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_with_project_and_name_display(self, _mock):
        # Given: a session with both project and name
        now = 1722520000.0
        duration = 25 * 60
        s = common.new_session("pomodoro", int(now - 60), duration,
                                "laptop", name="fix-auth", project="website")
        common.write_cache(s)
        # When: status is requested
        pomo.cmd_status()
        # Then: both are shown in brackets
        self.assertEqual(self._stdout.getvalue().strip(),
                         "🍅 24:00 [website] [fix-auth]")

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_with_project_json(self, _mock):
        # Given: a session with a project
        now = 1722520000.0
        duration = 25 * 60
        s = common.new_session("pomodoro", int(now - 60), duration,
                                "laptop", project="website")
        common.write_cache(s)
        # When: status --json is requested
        pomo.cmd_status(json_output=True)
        # Then: JSON includes project
        out = json.loads(self._stdout.getvalue())
        self.assertEqual(out["project"], "website")
        self.assertEqual(out["display"], "🍅 24:00 [website]")


class CmdClearInheritanceTests(Base):
    """cmd_clear inherits name and project from the cleared session."""

    @patch.object(builtins, "input", side_effect=["5"])
    def test_clear_inherits_name_and_project(self, _mock):
        # Given: an active pomodoro with name and project
        s = common.new_session("pomodoro", int(time.time()), 25 * 60,
                                "laptop", name="fix-auth", project="website")
        common.write_cache(s)
        # When: pomo clear starts a break
        pomo.cmd_clear()
        # Then: the break session inherits name and project
        session = common.read_cache()
        self.assertEqual(session["state"], "break")
        self.assertEqual(session["name"], "fix-auth")
        self.assertEqual(session["project"], "website")

    @patch.object(builtins, "input", side_effect=["5"])
    def test_clear_inherits_only_name(self, _mock):
        # Given: an active pomodoro with name but no project
        s = common.new_session("pomodoro", int(time.time()), 25 * 60,
                                "laptop", name="fix-auth")
        common.write_cache(s)
        # When: pomo clear starts a break
        pomo.cmd_clear()
        # Then: break inherits name, project is None
        session = common.read_cache()
        self.assertEqual(session["name"], "fix-auth")
        self.assertIsNone(session["project"])

    @patch.object(builtins, "input", side_effect=["5"])
    def test_clear_inherits_only_project(self, _mock):
        # Given: an active pomodoro with project but no name
        s = common.new_session("pomodoro", int(time.time()), 25 * 60,
                                "laptop", project="website")
        common.write_cache(s)
        # When: pomo clear starts a break
        pomo.cmd_clear()
        # Then: break inherits project, name is None
        session = common.read_cache()
        self.assertIsNone(session["name"])
        self.assertEqual(session["project"], "website")


class CmdHistoryTests(Base):
    """pomo history — today's session timeline."""

    @patch.object(common, "get_sessions", return_value=[])
    def test_history_human_output(self, get_sessions_mock):
        # Given: a named pomodoro session from today
        now = 1722520000.0
        s = common.new_session("pomodoro", int(now), 25 * 60, "laptop", name="fix-auth")
        s["ended_at"] = now + 25 * 60
        get_sessions_mock.return_value = [s]
        # When: pomo history is called
        pomo.cmd_history()
        output = self._stdout.getvalue()
        # Then: output contains expected date, icon, name, duration, and time range
        expected_date = datetime.fromtimestamp(int(now)).strftime("%Y-%m-%d")
        expected_start = datetime.fromtimestamp(int(now)).strftime("%H:%M")
        expected_end = datetime.fromtimestamp(int(now + 25 * 60)).strftime("%H:%M")
        self.assertIn(expected_date, output)
        self.assertIn("🍅", output)
        self.assertIn("[fix-auth]", output)
        self.assertIn("25:00", output)
        self.assertIn(expected_start, output)
        self.assertIn(expected_end, output)

    @patch.object(common, "get_sessions")
    def test_history_json_output(self, get_sessions_mock):
        # Given: sessions exist
        s = common.new_session("pomodoro", 1000, 60, "laptop", name="fix-auth")
        get_sessions_mock.return_value = [s]
        # When: pomo history --json is called
        pomo.cmd_history(json_output=True)
        out = json.loads(self._stdout.getvalue())
        # Then: session data is returned as JSON
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "fix-auth")

    @patch.object(common, "get_sessions", return_value=[])
    def test_history_empty(self, _mock):
        # Given: no sessions today
        # When: pomo history is called
        pomo.cmd_history()
        # Then: empty message shown
        self.assertIn("No sessions today", self._stdout.getvalue())

    @patch.object(common, "get_sessions",
                  side_effect=common.ServerUnavailable("offline"))
    def test_history_offline_errors(self, _mock):
        # Given: server is unreachable
        # When / Then: pomo history exits with an error
        with self.assertRaises(SystemExit):
            pomo.cmd_history()
        self.assertIn("unavailable", self._stderr.getvalue())

    @patch.object(common, "get_sessions")
    def test_history_passes_project_filter(self, get_sessions_mock):
        # Given: mock that returns empty list
        get_sessions_mock.return_value = []
        # When: pomo history --project website is called
        pomo.cmd_history(project="website")
        # Then: get_sessions was called with project filter
        get_sessions_mock.assert_called_once()
        self.assertEqual(get_sessions_mock.call_args[1]["project"], "website")

    @patch.object(common, "get_sessions", return_value=[])
    def test_history_with_project_in_output(self, get_sessions_mock):
        # Given: a session with a project
        now = 1722520000.0
        s = common.new_session("pomodoro", int(now), 25 * 60, "laptop",
                                project="website")
        s["ended_at"] = now + 25 * 60
        get_sessions_mock.return_value = [s]
        # When: pomo history is called
        pomo.cmd_history()
        output = self._stdout.getvalue()
        # Then: project is shown in output
        self.assertIn("[website]", output)


class CmdProjectsTests(Base):
    """pomo projects — list all defined projects."""

    @patch.object(common, "get_projects",
                  side_effect=common.ServerUnavailable("offline"))
    def test_projects_offline_errors(self, _mock):
        # Given: server is unreachable
        # When / Then: pomo projects exits with an error
        with self.assertRaises(SystemExit):
            pomo.cmd_projects()
        self.assertIn("unavailable", self._stderr.getvalue())

    @patch.object(common, "get_projects", return_value=[])
    def test_projects_empty(self, _mock):
        # Given: no projects defined
        # When: pomo projects is called
        pomo.cmd_projects()
        # Then: empty message shown
        self.assertIn("No projects defined", self._stdout.getvalue())

    @patch.object(common, "get_projects", return_value=[
        {"project": "backend"}, {"project": "website"}
    ])
    def test_projects_list(self, _mock):
        # Given: projects exist
        # When: pomo projects is called
        pomo.cmd_projects()
        # Then: each project name is printed on its own line
        output = self._stdout.getvalue()
        self.assertIn("backend", output)
        self.assertIn("website", output)
        lines = [l for l in output.split("\n") if l]
        self.assertEqual(lines, ["backend", "website"])

    @patch.object(common, "get_projects", return_value=[
        {"project": "backend"}, {"project": "website"}
    ])
    def test_projects_json(self, _mock):
        # Given: projects exist
        # When: pomo projects --json is called
        pomo.cmd_projects(json_output=True)
        # Then: projects returned as JSON array of objects
        out = json.loads(self._stdout.getvalue())
        self.assertEqual(out, [{"project": "backend"}, {"project": "website"}])


if __name__ == "__main__":
    unittest.main()
