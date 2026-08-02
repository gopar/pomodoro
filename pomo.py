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

import argparse
import json
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


def _fmt_time(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


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

def cmd_start(mins: int) -> None:
    if not _confirm_overwrite():
        print("Aborted.")
        return
    active = _current_active()
    if active and active["state"] in ("pomodoro", "overtime"):
        stop(active)
    start_pomodoro(mins)


def cmd_break(mins: int) -> None:
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


def cmd_status(json_output: bool = False) -> None:
    session = common.read_cache()
    if common.is_idle(session):
        if json_output:
            print(json.dumps({"state": "idle", "display": "No active session"}))
        else:
            print("No active session")
        return

    now = time.time()
    cache_state = session["state"]
    start = int(session["start_epoch"])
    duration = int(session["duration"])
    elapsed = int(now - start)
    remaining = duration - elapsed

    overtime_of = {"pomodoro": "overtime", "break": "break-overtime"}
    effective_state = overtime_of.get(cache_state, cache_state) if remaining <= 0 else cache_state

    icon = {"pomodoro": "🍅", "overtime": "⏰", "break": "☕", "break-overtime": "☕"}.get(effective_state, "")
    time_str = _fmt_time(-remaining) if remaining < 0 else _fmt_time(remaining)
    if remaining < 0:
        time_str = f"+{time_str}"
    display = f"{icon} {time_str}"

    if json_output:
        print(json.dumps({
            "state": effective_state,
            "start_epoch": start,
            "duration": duration,
            "elapsed": elapsed,
            "remaining": remaining,
            "display": display,
        }))
    else:
        print(display)


def _argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pomo",
        description="Start, stop, and track pomodoro sessions.",
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("start", help="Start a pomodoro for N minutes")
    p.add_argument("minutes", type=int, help="Duration in minutes")

    p = sub.add_parser("break", help="Start a break for N minutes")
    p.add_argument("minutes", type=int, help="Duration in minutes")

    sub.add_parser("clear", help="Stop current session, optionally start a break")

    p = sub.add_parser("status", help="Show current session status")
    p.add_argument("--json", action="store_true", help="Output as JSON")

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _argparser()
    args = parser.parse_args(argv)

    if args.command == "start":
        cmd_start(args.minutes)
    elif args.command == "break":
        cmd_break(args.minutes)
    elif args.command == "clear":
        cmd_clear()
    elif args.command == "status":
        cmd_status(json_output=args.json)
    else:
        parser.print_help()


if __name__ == "__main__":
    main(sys.argv[1:])
