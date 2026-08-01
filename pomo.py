#!/usr/bin/env python3
"""Pomodoro CLI. Drop-in replacement for the old `pomo` bash script.

UX preserved:
    pomo <minutes>        Start a pomodoro
    pomo break <minutes>  Start a break
    pomo clear            Stop & clear (prompts for a break)

Behavior:
  - Writes the new session to the local cache immediately (works offline)
    and pushes to the server; if the server is
    unreachable the push is queued in the outbox for the agent to flush.
  - Fires lifecycle hooks for the event (pomodoro_start / break_start /
    session_stop). All side effects live in hooks (see hooks.py); the agent
    owns only the overtime timer.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import hooks  # noqa: E402


def _cfg() -> dict:
    return common.load_config()


def _require_int(value: str, name: str) -> int:
    if not value.isdigit():
        sys.stderr.write(f"Error: {name} must be an integer\n")
        sys.exit(1)
    return int(value)


def _push(action: str, session: dict) -> None:
    cfg = _cfg()
    try:
        if action == "end":
            common.post_end(cfg["server_url"], session)
        else:
            common.post_session(cfg["server_url"], session)
    except common.ServerUnavailable:
        common.enqueue_outbox(action, session)


def _current_active() -> dict | None:
    session = common.read_cache()
    if common.is_idle(session):
        return None
    return session


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

def start_pomodoro(mins: int) -> None:
    cfg = _cfg()
    session = common.new_session("pomodoro", int(time.time()), mins * 60,
                                 cfg["machine_name"])
    common.write_cache(session)
    hooks.dispatch(hooks.POMODORO_START, session, cfg)
    _push("session", session)
    print(f"Pomodoro started for {mins} minute(s) 🍅")


def start_break(mins: int) -> None:
    cfg = _cfg()
    session = common.new_session("break", int(time.time()), mins * 60,
                                 cfg["machine_name"])
    common.write_cache(session)
    hooks.dispatch(hooks.BREAK_START, session, cfg)
    _push("session", session)
    print(f"Break started for {mins} minute(s) ☕")


def stop(session: dict | None) -> None:
    cfg = _cfg()
    end = session or common.new_session("ended", int(time.time()), 0,
                                        cfg["machine_name"])
    end = dict(end)
    end["state"] = "ended"
    end["updated_at"] = time.time()
    end["ended_at"] = time.time()
    common.clear_cache()
    hooks.dispatch(hooks.SESSION_STOP, end, cfg)
    _push("end", end)


def _confirm_overwrite() -> bool:
    if _current_active() is None:
        return True
    try:
        answer = input("Pomodoro/break already running. Overwrite? [y/N]: ")
    except EOFError:
        answer = ""
    return answer.strip().lower() in ("y", "yes")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_start(args: list[str]) -> None:
    if len(args) != 1:
        usage()
    mins = _require_int(args[0], "minutes")
    if not _confirm_overwrite():
        print("Aborted.")
        return
    active = _current_active()
    if active and active["state"] in ("pomodoro", "overtime"):
        stop(active)
    start_pomodoro(mins)


def cmd_break(args: list[str]) -> None:
    if len(args) != 1:
        usage()
    mins = _require_int(args[0], "minutes")
    if not _confirm_overwrite():
        print("Aborted.")
        return
    active = _current_active()
    if active and active["state"] in ("pomodoro", "overtime"):
        stop(active)
    start_break(mins)


def cmd_clear() -> None:
    active = _current_active()
    # Already in a break -> just stop it, no prompt.
    if active and active["state"] in ("break", "break-overtime"):
        stop(active)
        print("Break cleared 🧹")
        return
    try:
        brk = input("Break minutes? (empty to skip): ").strip()
    except EOFError:
        brk = ""
    if not brk:
        stop(active)
        print("Pomodoro cleared 🧹")
        return
    mins = _require_int(brk, "break minutes")
    stop(active)
    start_break(mins)


def usage() -> None:
    sys.stderr.write(
        "Usage:\n"
        "  pomo <minutes>        Start pomodoro\n"
        "  pomo break <minutes>  Start a break\n"
        "  pomo clear            Stop & clear pomodoro (prompts for a break)\n"
    )
    sys.exit(1)


def main(argv: list[str]) -> None:
    if not argv:
        usage()
    cmd = argv[0]
    if cmd == "clear":
        cmd_clear()
    elif cmd == "break":
        cmd_break(argv[1:])
    else:
        cmd_start(argv)


if __name__ == "__main__":
    main(sys.argv[1:])
