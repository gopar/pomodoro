#!/usr/bin/env python3
"""Pomodoro local agent (one per machine).

Long-running daemon that keeps this machine in sync with the server and owns
the countdown timer.

Responsibilities:
  1. Poll  GET /current every `poll_interval` seconds. If the server's session
     is newer than our cache, adopt it (update cache) and, when
     configured, fire lifecycle hooks for remote-originated sessions.
  2. Local timer: when the active session passes start+duration, transition
     pomodoro->overtime / break->break-overtime, fire hooks, and push
     the transition to the server.
  3. Outbox flush: pending pushes queued by the CLI while offline are sent on
     each loop; last-write-wins on the server resolves conflicts.

Side effects are entirely hook-driven (see hooks.py), so the daemon itself is
OS-agnostic. Timer stays local so everything works offline. Stdlib only.
"""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import hooks  # noqa: E402

OVERTIME_OF = {"pomodoro": "overtime", "break": "break-overtime"}


def on_remote_adopt(session: dict, cfg: dict) -> None:
    """Fire hooks when we adopt a session that started on another machine."""
    if not cfg.get("run_for_remote_sessions"):
        return
    state = session.get("state")
    if state == "pomodoro":
        hooks.dispatch("pomodoro_start", session, cfg, remote=True)
    elif state == "break":
        hooks.dispatch("break_start", session, cfg, remote=True)


# ---------------------------------------------------------------------------
# Sync helpers
# ---------------------------------------------------------------------------

def _updated_at(session: dict | None) -> float:
    if not session:
        return 0.0
    return float(session.get("updated_at") or 0.0)


def flush_outbox(cfg: dict) -> None:
    items = common.read_outbox()
    if not items:
        return
    remaining: list[dict] = []
    for item in items:
        try:
            if item["action"] == "end":
                common.post_end(cfg["server_url"], item["session"])
            else:
                common.post_session(cfg["server_url"], item["session"])
        except common.ServerUnavailable:
            remaining.append(item)  # keep for next attempt
    common.rewrite_outbox(remaining)


def poll_server(cfg: dict) -> None:
    """Adopt the server's session if it is newer than our cache."""
    try:
        remote = common.get_current(cfg["server_url"])
    except common.ServerUnavailable:
        return
    local = common.read_cache()
    if common.is_idle(remote):
        # Server says idle. Clear only if our cache isn't a newer active session
        # that simply hasn't been pushed yet.
        if local and not common.is_idle(local):
            # local pending newer session -> keep; outbox will push it
            return
        if local is not None:
            common.clear_cache()
        return
    if _updated_at(remote) > _updated_at(local):
        remote_started_elsewhere = (
            not local or remote.get("id") != (local or {}).get("id")
        ) and remote.get("origin_machine") != cfg["machine_name"]
        common.write_cache(remote)
        if remote_started_elsewhere:
            on_remote_adopt(remote, cfg)


def tick_timer(cfg: dict) -> None:
    """Advance the local session into overtime when its duration elapses."""
    session = common.read_cache()
    if common.is_idle(session):
        return
    state = session.get("state")
    overtime_state = OVERTIME_OF.get(state)
    if overtime_state is None:
        return  # already in an overtime state; nothing to do
    start_epoch = session.get("start_epoch")
    duration = session.get("duration")
    if not isinstance(start_epoch, (int, float)) or not isinstance(duration, (int, float)):
        return  # malformed cache; read_cache normally filters this out
    elapsed = time.time() - start_epoch
    if elapsed < duration:
        return
    # Transition to overtime locally, fire side effects, push.
    session["state"] = overtime_state
    session["updated_at"] = time.time()
    common.write_cache(session)
    end_event = "break_end" if overtime_state == "break-overtime" else "pomodoro_end"
    hooks.dispatch(end_event, session, cfg)
    try:
        common.post_session(cfg["server_url"], session)
    except common.ServerUnavailable:
        common.enqueue_outbox("session", session)


def loop() -> None:
    cfg = common.load_config()
    interval = float(cfg.get("poll_interval", 5))
    sys.stderr.write(
        f"pomo-agent: machine={cfg['machine_name']} server={cfg['server_url']} "
        f"interval={interval}s\n"
    )
    while True:
        cfg = common.load_config()  # re-read so config edits take effect live
        try:
            flush_outbox(cfg)
            poll_server(cfg)
            tick_timer(cfg)
        except Exception:  # noqa: BLE001 - a bad tick must not kill the daemon
            # KeyboardInterrupt/SystemExit derive from BaseException and still
            # propagate, so Ctrl-C and shutdown work. Everything else is logged
            # and the loop continues (self-heals on the next iteration).
            sys.stderr.write("pomo-agent: iteration error:\n" + traceback.format_exc())
        time.sleep(float(cfg.get("poll_interval", 5)))


if __name__ == "__main__":
    try:
        loop()
    except KeyboardInterrupt:
        pass
