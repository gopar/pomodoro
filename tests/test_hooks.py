"""Behavior tests for hooks.py: dispatch/fire run user scripts with the right
context, and stay best-effort."""

from __future__ import annotations

import json
import stat
import unittest

from _util import isolate

import common
import hooks


def _write_hook(event_dir, name: str, body: str):
    event_dir.mkdir(parents=True, exist_ok=True)
    script = event_dir / name
    script.write_text(body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR)
    return script


class HooksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = isolate(self)
        self.cfg = {
            "machine_name": "laptop",
            "hooks": {"enabled": True, "timeout": 10, "dir": ""},
        }

    def _event_dir(self, event: str):
        return common.HOOKS_DIR / f"{event}.d"

    def test_dispatch_runs_matching_hook(self):
        out = self.tmp / "ran.txt"
        _write_hook(self._event_dir("pomodoro_start"), "10-x.sh",
                    f"#!/usr/bin/env bash\necho hi > {out}\n")
        session = common.new_session("pomodoro", 1, 60, "laptop")
        hooks.dispatch("pomodoro_start", session, self.cfg)
        self.assertTrue(out.exists())

    def test_hook_receives_env_and_stdin(self):
        out = self.tmp / "ctx.txt"
        _write_hook(
            self._event_dir("pomodoro_start"), "10-ctx.sh",
            "#!/usr/bin/env bash\n"
            f'printf "%s|%s|%s" "$POMO_EVENT" "$POMO_STATE" "$(cat)" > {out}\n',
        )
        session = common.new_session("pomodoro", 1, 60, "laptop")
        hooks.dispatch("pomodoro_start", session, self.cfg)
        event, state, stdin = out.read_text(encoding="utf-8").split("|", 2)
        self.assertEqual(event, "pomodoro_start")
        self.assertEqual(state, "pomodoro")
        self.assertEqual(json.loads(stdin)["id"], session["id"])

    def test_disabled_hooks_do_not_run(self):
        out = self.tmp / "nope.txt"
        _write_hook(self._event_dir("pomodoro_start"), "10-x.sh",
                    f"#!/usr/bin/env bash\ntouch {out}\n")
        self.cfg["hooks"]["enabled"] = False
        hooks.dispatch("pomodoro_start", None, self.cfg)
        self.assertFalse(out.exists())

    def test_missing_event_dir_is_noop(self):
        # No directory created -> must not raise.
        hooks.dispatch("break_end", None, self.cfg)

    def test_non_executable_file_is_skipped(self):
        out = self.tmp / "skip.txt"
        event_dir = self._event_dir("pomodoro_start")
        event_dir.mkdir(parents=True, exist_ok=True)
        script = event_dir / "10-x.sh"
        script.write_text(f"#!/usr/bin/env bash\ntouch {out}\n", encoding="utf-8")
        script.chmod(stat.S_IRUSR)  # readable, not executable
        hooks.dispatch("pomodoro_start", None, self.cfg)
        self.assertFalse(out.exists())

    def test_scripts_run_in_lexical_order(self):
        out = self.tmp / "order.txt"
        event_dir = self._event_dir("pomodoro_start")
        _write_hook(event_dir, "20-b.sh",
                    f"#!/usr/bin/env bash\nprintf b >> {out}\n")
        _write_hook(event_dir, "10-a.sh",
                    f"#!/usr/bin/env bash\nprintf a >> {out}\n")
        hooks.dispatch("pomodoro_start", None, self.cfg)
        self.assertEqual(out.read_text(encoding="utf-8"), "ab")


if __name__ == "__main__":
    unittest.main()
