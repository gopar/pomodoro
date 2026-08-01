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
    """Tests for tick_timer: countdown expiry, overtime transitions, and push."""

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
        # Given: an active pomodoro with time remaining (10s elapsed, 60s total)
        self._active("pomodoro", elapsed=10, duration=60)
        # When: the timer ticks
        agent.tick_timer(self.cfg)
        # Then: state stays pomodoro, no events are fired
        self.assertEqual(common.read_cache()["state"], "pomodoro")
        self.assertEqual(self.events, [])

    def test_pomodoro_transitions_to_overtime(self):
        # Given: a pomodoro past its duration (61s elapsed, 60s total)
        before = self._active("pomodoro", elapsed=61, duration=60)
        # When: the timer ticks
        agent.tick_timer(self.cfg)
        # Then: state becomes overtime, updated_at advances, overtime event fires
        after = common.read_cache()
        self.assertEqual(after["state"], "overtime")
        self.assertGreaterEqual(after["updated_at"], before["updated_at"])
        self.assertEqual(self.events, ["pomodoro_overtime"])

    def test_break_transitions_to_break_overtime(self):
        # Given: a break past its duration
        self._active("break", elapsed=61, duration=60)
        # When: the timer ticks
        agent.tick_timer(self.cfg)
        # Then: state becomes break-overtime, break_overtime event fires
        self.assertEqual(common.read_cache()["state"], "break-overtime")
        self.assertEqual(self.events, ["break_overtime"])

    def test_already_overtime_is_noop(self):
        # Given: already in overtime state
        self._active("overtime", elapsed=999, duration=60)
        # When: the timer ticks
        agent.tick_timer(self.cfg)
        # Then: no state change, no events fired
        self.assertEqual(common.read_cache()["state"], "overtime")
        self.assertEqual(self.events, [])

    def test_idle_is_noop(self):
        # Given: no active session (idle)
        # When: the timer ticks
        agent.tick_timer(self.cfg)
        # Then: no events fired
        self.assertEqual(self.events, [])

    def test_malformed_cache_is_noop(self):
        # Given: cache has active state but missing required numeric fields
        import json
        common.ensure_dirs()
        common.CACHE_FILE.write_text(
            json.dumps({"state": "pomodoro", "id": "x", "updated_at": 1.0}),
            encoding="utf-8",
        )
        # When: the timer ticks
        agent.tick_timer(self.cfg)
        # Then: no events fired (self-heals, no crash)
        self.assertEqual(self.events, [])

    def test_offline_push_queues_outbox(self):
        # Given: an expired pomodoro, and the server is unreachable
        def boom(url, s):
            raise common.ServerUnavailable("offline")

        patch_attr(self, common, "post_session", boom)
        self._active("pomodoro", elapsed=61, duration=60)
        # When: the timer ticks and attempts to push
        agent.tick_timer(self.cfg)
        # Then: the overtime session is queued in the outbox
        outbox = common.read_outbox()
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["session"]["state"], "overtime")


class PollServerTests(unittest.TestCase):
    """Tests for poll_server: adopting remote sessions vs preserving local state."""

    def setUp(self):
        isolate(self)
        self.adopted: list[dict] = []
        patch_attr(self, agent, "on_remote_adopt",
                   lambda session, cfg: self.adopted.append(session))
        self.cfg = {"server_url": "http://x", "machine_name": "laptop"}

    def _remote(self, session):
        patch_attr(self, common, "get_current", lambda url: session)

    def test_adopts_newer_remote_session(self):
        # Given: a remote session from desktop newer than local (empty) cache
        remote = common.new_session("pomodoro", 1000, 60, "desktop")
        remote["updated_at"] = 500.0
        self._remote(remote)
        # When: the agent polls the server
        agent.poll_server(self.cfg)
        # Then: local cache is updated and on_remote_adopt is called
        self.assertEqual(common.read_cache()["id"], remote["id"])
        self.assertEqual(len(self.adopted), 1)

    def test_keeps_local_when_remote_older(self):
        # Given: a local session newer than the remote one
        local = common.new_session("pomodoro", 1000, 60, "laptop")
        local["updated_at"] = 900.0
        common.write_cache(local)
        remote = common.new_session("pomodoro", 1000, 60, "desktop")
        remote["updated_at"] = 100.0
        self._remote(remote)
        # When: the agent polls the server
        agent.poll_server(self.cfg)
        # Then: local session is kept (LWW favours the newer timestamp)
        self.assertEqual(common.read_cache()["id"], local["id"])

    def test_server_idle_clears_stale_local(self):
        # Given: a stale local cache (ended session) and server reports idle
        local = common.new_session("ended", 1000, 0, "laptop")
        common.write_cache(local)
        common.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._remote(common.idle_session())
        # When: the agent polls the server
        agent.poll_server(self.cfg)
        # Then: local cache is cleared (server idle wins over stale local)
        self.assertTrue(common.is_idle(common.read_cache()))

    def test_local_pending_active_kept_when_server_idle(self):
        # Given: a local active session not yet pushed, server reports idle
        local = common.new_session("pomodoro", 1000, 60, "laptop")
        common.write_cache(local)
        self._remote(common.idle_session())
        # When: the agent polls the server
        agent.poll_server(self.cfg)
        # Then: unpushed local active session is kept (not clobbered)
        self.assertEqual(common.read_cache()["id"], local["id"])


class _StopLoop(Exception):
    """Sentinel used to break agent.loop() deterministically in tests."""


class LoopResilienceTests(unittest.TestCase):
    """Tests for the agent main loop: error resilience and shutdown."""

    def setUp(self):
        isolate(self)
        # Config with a tiny interval; loop re-reads config each iteration.
        patch_attr(self, common, "load_config",
                   lambda: {"server_url": "http://x", "machine_name": "laptop",
                            "poll_interval": 0})
        # Neutralize the network-y steps by default.
        patch_attr(self, agent, "flush_outbox", lambda cfg: None)
        patch_attr(self, agent, "poll_server", lambda cfg: None)
        # Suppress agent startup log during tests.
        patch_attr(self, agent.sys, "stderr", io.StringIO())

    def test_loop_survives_iteration_error_and_continues(self):
        # Given: tick_timer raises RuntimeError on first call, succeeds after
        self.calls = 0

        def flaky(cfg):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")

        patch_attr(self, agent, "tick_timer", flaky)
        # Break out on the 2nd sleep so we prove the loop kept going past the error.
        def sleeper(_):
            if self.calls >= 2:
                raise _StopLoop
        patch_attr(self, agent, "time",
                   type("T", (), {"sleep": staticmethod(sleeper)}))
        # When: the loop runs
        # Then: it survives the RuntimeError and continues iterating
        with self.assertRaises(_StopLoop):
            agent.loop()
        self.assertGreaterEqual(self.calls, 2)

    def test_loop_does_not_swallow_keyboard_interrupt(self):
        # Given: tick_timer raises KeyboardInterrupt
        def interrupt(cfg):
            raise KeyboardInterrupt
        patch_attr(self, agent, "tick_timer", interrupt)
        patch_attr(self, agent, "time",
                   type("T", (), {"sleep": staticmethod(lambda _: None)}))
        # When / Then: KeyboardInterrupt propagates (the loop does not swallow it)
        with self.assertRaises(KeyboardInterrupt):
            agent.loop()

    def test_poll_interval_below_minimum_is_clamped(self):
        # Given: config has poll_interval=1 (below minimum of 5)
        patch_attr(self, common, "load_config",
                   lambda: {"server_url": "http://x", "machine_name": "laptop",
                            "poll_interval": 1})
        sleep_args: list[float] = []

        def capture_sleep(secs):
            if sleep_args:  # break after first sleep
                raise _StopLoop
            sleep_args.append(secs)

        patch_attr(self, agent, "time",
                   type("T", (), {"sleep": staticmethod(capture_sleep)}))
        # When: the loop runs
        with self.assertRaises(_StopLoop):
            agent.loop()
        # Then: sleep is called with 5.0 (clamped), stderr warns about override
        self.assertEqual(sleep_args[0], 5.0)
        self.assertIn("clamped", agent.sys.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
