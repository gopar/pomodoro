#!/usr/bin/env python3
"""Pomodoro local agent (one per machine).

Long-running daemon that keeps this machine in sync with the server and owns
the countdown timer + side effects.

Responsibilities:
  1. Poll  GET /current every `poll_interval` seconds. If the server's session
     is newer than our cache, adopt it (update cache + legacy file) and, when
     configured, fire side effects for remote-originated sessions.
  2. Local timer: when the active session passes start+duration, transition
     pomodoro->overtime / break->break-overtime, fire side effects, and push
     the transition to the server.
  3. Outbox flush: pending pushes queued by the CLI while offline are sent on
     each loop; last-write-wins on the server resolves conflicts.

Timer stays local so everything works offline. Stdlib only.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

ALARM_FILE = common.HOME / ".config" / "media" / "alarm.mp3"
EMACS_BIN = "/Applications/Emacs.app/Contents/MacOS/Emacs"
EMACS_INIT = common.HOME / ".config" / "emacs.d" / "init.minimal.gui.el"

OVERTIME_OF = {"pomodoro": "overtime", "break": "break-overtime"}


# ---------------------------------------------------------------------------
# Side effects (best-effort; never crash the loop)
# ---------------------------------------------------------------------------

def _run(cmd: list[str], background: bool = False) -> None:
    try:
        if background:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        else:
            subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    except (OSError, ValueError):
        pass


def _focus(on: bool, cfg: dict) -> None:
    if not cfg["side_effects"].get("focus_mode"):
        return
    _run(["shortcuts", "run", "Focus On" if on else "Focus Off"])


def _say(text: str, cfg: dict) -> None:
    if cfg["side_effects"].get("say"):
        _run(["say", text])


def _alarm(cfg: dict) -> None:
    if cfg["side_effects"].get("alarm") and ALARM_FILE.exists():
        _run(["afplay", str(ALARM_FILE)], background=True)


def _launch_emacs(cfg: dict) -> None:
    if cfg["side_effects"].get("launch_emacs") and Path(EMACS_BIN).exists():
        _run([EMACS_BIN, "-q", "-l", str(EMACS_INIT)], background=True)


def on_overtime(state: str, cfg: dict) -> None:
    """Fire when a session crosses into overtime locally."""
    text = "Break Overtime" if state == "break-overtime" else "Pomodoro Overtime"
    _say(text, cfg)
    _alarm(cfg)


def on_remote_adopt(session: dict, cfg: dict) -> None:
    """Fire when we adopt a session that started on another machine."""
    if not cfg["side_effects"].get("run_for_remote_sessions"):
        return
    state = session.get("state")
    if state == "pomodoro":
        _focus(True, cfg)
    elif state == "break":
        _focus(False, cfg)
        _launch_emacs(cfg)


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
    state = session["state"]
    overtime_state = OVERTIME_OF.get(state)
    if overtime_state is None:
        return  # already in an overtime state; nothing to do
    elapsed = time.time() - session["start_epoch"]
    if elapsed < session["duration"]:
        return
    # Transition to overtime locally, fire side effects, push.
    session["state"] = overtime_state
    session["updated_at"] = time.time()
    common.write_cache(session)
    on_overtime(overtime_state, cfg)
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
        flush_outbox(cfg)
        poll_server(cfg)
        tick_timer(cfg)
        time.sleep(float(cfg.get("poll_interval", 5)))


if __name__ == "__main__":
    try:
        loop()
    except KeyboardInterrupt:
        pass
