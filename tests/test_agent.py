"""Behavior tests for agent.py: local overtime timer and server adoption.

Side-effect dispatch and the HTTP client are replaced with recorders/stubs so
tests exercise real transition logic without touching the network or macOS.
"""

from __future__ import annotations

import io
import time
import unittest

from _util import isolate, patch_attr

import agent
import common
import hooks


class TickTimerTests(unittest.TestCase):
    def setUp(self):
        isolate(self)
        self.events: list[str] = []
        patch_attr(self, hooks, "dispatch",
                   lambda event, session, cfg, **kw: self.events.append(event))
        # Server push is a no-op success by default.
        patch_attr(self, common, "post_session", lambda url, s: {})
        self.cfg = {"server_url": "http://x", "machine_name": "laptop"}

    def _active(self, state: str, elapsed: int, duration: int = 60) -> dict:
        start = int(time.time()) - elapsed
        s = common.new_session(state, start, duration, "laptop")
        common.write_cache(s)
        return s

    def test_no_transition_before_duration(self):
        self._active("pomodoro", elapsed=10, duration=60)
        agent.tick_timer(self.cfg)
        self.assertEqual(common.read_cache()["state"], "pomodoro")
        self.assertEqual(self.events, [])

    def test_pomodoro_transitions_to_overtime(self):
        before = self._active("pomodoro", elapsed=61, duration=60)
        agent.tick_timer(self.cfg)
        after = common.read_cache()
        self.assertEqual(after["state"], "overtime")
        self.assertGreaterEqual(after["updated_at"], before["updated_at"])
        self.assertEqual(self.events, ["pomodoro_overtime"])

    def test_break_transitions_to_break_overtime(self):
        self._active("break", elapsed=61, duration=60)
        agent.tick_timer(self.cfg)
        self.assertEqual(common.read_cache()["state"], "break-overtime")
        self.assertEqual(self.events, ["break_overtime"])

    def test_already_overtime_is_noop(self):
        self._active("overtime", elapsed=999, duration=60)
        agent.tick_timer(self.cfg)
        self.assertEqual(common.read_cache()["state"], "overtime")
        self.assertEqual(self.events, [])

    def test_idle_is_noop(self):
        agent.tick_timer(self.cfg)
        self.assertEqual(self.events, [])

    def test_malformed_cache_is_noop(self):
        # Active state on disk but missing numeric fields -> must not raise or
        # fire events (read_cache filters it; tick_timer is defensive too).
        import json
        common.ensure_dirs()
        common.CACHE_FILE.write_text(
            json.dumps({"state": "pomodoro", "id": "x", "updated_at": 1.0}),
            encoding="utf-8",
        )
        agent.tick_timer(self.cfg)
        self.assertEqual(self.events, [])

    def test_offline_push_queues_outbox(self):
        def boom(url, s):
            raise common.ServerUnavailable("offline")

        patch_attr(self, common, "post_session", boom)
        self._active("pomodoro", elapsed=61, duration=60)
        agent.tick_timer(self.cfg)
        outbox = common.read_outbox()
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["session"]["state"], "overtime")


class PollServerTests(unittest.TestCase):
    def setUp(self):
        isolate(self)
        self.adopted: list[dict] = []
        patch_attr(self, agent, "on_remote_adopt",
                   lambda session, cfg: self.adopted.append(session))
        self.cfg = {"server_url": "http://x", "machine_name": "laptop"}

    def _remote(self, session):
        patch_attr(self, common, "get_current", lambda url: session)

    def test_adopts_newer_remote_session(self):
        remote = common.new_session("pomodoro", 1000, 60, "desktop")
        remote["updated_at"] = 500.0
        self._remote(remote)
        agent.poll_server(self.cfg)
        self.assertEqual(common.read_cache()["id"], remote["id"])
        self.assertEqual(len(self.adopted), 1)

    def test_keeps_local_when_remote_older(self):
        local = common.new_session("pomodoro", 1000, 60, "laptop")
        local["updated_at"] = 900.0
        common.write_cache(local)
        remote = common.new_session("pomodoro", 1000, 60, "desktop")
        remote["updated_at"] = 100.0
        self._remote(remote)
        agent.poll_server(self.cfg)
        self.assertEqual(common.read_cache()["id"], local["id"])

    def test_server_idle_clears_stale_local(self):
        local = common.new_session("ended", 1000, 0, "laptop")
        common.write_cache(local)  # ended -> cache file removed already
        common.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Simulate a stale idle cache present but idle.
        self._remote(common.idle_session())
        agent.poll_server(self.cfg)
        self.assertTrue(common.is_idle(common.read_cache()))

    def test_local_pending_active_kept_when_server_idle(self):
        local = common.new_session("pomodoro", 1000, 60, "laptop")
        common.write_cache(local)
        self._remote(common.idle_session())
        agent.poll_server(self.cfg)
        # Unpushed local active session must not be clobbered by server idle.
        self.assertEqual(common.read_cache()["id"], local["id"])


class _StopLoop(Exception):
    """Sentinel used to break agent.loop() deterministically in tests."""


class LoopResilienceTests(unittest.TestCase):
    def setUp(self):
        isolate(self)
        # Config with a tiny interval; loop re-reads config each iteration.
        patch_attr(self, common, "load_config",
                   lambda: {"server_url": "http://x", "machine_name": "laptop",
                            "poll_interval": 0})
        # Neutralize the network-y steps by default.
        patch_attr(self, agent, "flush_outbox", lambda cfg: None)
        patch_attr(self, agent, "poll_server", lambda cfg: None)

    def test_loop_survives_iteration_error_and_continues(self):
        self.calls = 0

        def flaky(cfg):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")  # first iteration blows up

        patch_attr(self, agent, "tick_timer", flaky)
        # Break out on the 2nd sleep so we prove the loop kept going past the error.
        def sleeper(_):
            if self.calls >= 2:
                raise _StopLoop
        patch_attr(self, agent, "time",
                   type("T", (), {"sleep": staticmethod(sleeper)}))
        # Swallow the expected traceback the loop logs to stderr.
        patch_attr(self, agent.sys, "stderr", io.StringIO())

        with self.assertRaises(_StopLoop):
            agent.loop()
        self.assertGreaterEqual(self.calls, 2)  # survived the RuntimeError

    def test_loop_does_not_swallow_keyboard_interrupt(self):
        def interrupt(cfg):
            raise KeyboardInterrupt
        patch_attr(self, agent, "tick_timer", interrupt)
        patch_attr(self, agent, "time",
                   type("T", (), {"sleep": staticmethod(lambda _: None)}))
        with self.assertRaises(KeyboardInterrupt):
            agent.loop()


if __name__ == "__main__":
    unittest.main()
