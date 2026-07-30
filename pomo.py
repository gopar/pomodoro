#!/usr/bin/env python3
"""Pomodoro CLI. Drop-in replacement for the old `pomo` bash script.

UX preserved:
    pomo <minutes>        Start a pomodoro
    pomo break <minutes>  Start a break
    pomo clear            Stop & clear (prompts for a break)

Behavior:
  - Writes the new session to the local cache immediately (works offline,
    keeps tmux/legacy file live) and pushes to the server; if the server is
    unreachable the push is queued in the outbox for the agent to flush.
  - Fires the *initiating* machine's immediate side effects directly
    (Focus On for a pomodoro; Focus Off + launch Emacs for a break/clear),
    matching the original script. The agent owns only the overtime timer.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import agent as agentmod  # reuse side-effect helpers  # noqa: E402


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
    agentmod._focus(True, cfg)
    session = common.new_session("pomodoro", int(time.time()), mins * 60,
                                 cfg["machine_name"])
    common.write_cache(session)
    _push("session", session)
    print(f"Pomodoro started for {mins} minute(s) 🍅")


def start_break(mins: int) -> None:
    cfg = _cfg()
    agentmod._focus(False, cfg)
    agentmod._launch_emacs(cfg)
    session = common.new_session("break", int(time.time()), mins * 60,
                                 cfg["machine_name"])
    common.write_cache(session)
    _push("session", session)
    print(f"Break started for {mins} minute(s) ☕")


def stop(session: dict | None, launch_emacs: bool) -> None:
    cfg = _cfg()
    agentmod._focus(False, cfg)
    if launch_emacs:
        agentmod._launch_emacs(cfg)
    end = session or common.new_session("ended", int(time.time()), 0,
                                        cfg["machine_name"])
    end = dict(end)
    end["state"] = "ended"
    end["updated_at"] = time.time()
    end["ended_at"] = time.time()
    common.clear_cache()
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
        stop(active, launch_emacs=False)
    start_pomodoro(mins)


def cmd_break(args: list[str]) -> None:
    if len(args) != 1:
        usage()
    mins = _require_int(args[0], "minutes")
    if not _confirm_overwrite():
        print("Aborted.")
        return
    start_break(mins)


def cmd_clear() -> None:
    active = _current_active()
    # Already in a break -> just stop it, no prompt, no Emacs.
    if active and active["state"] in ("break", "break-overtime"):
        stop(active, launch_emacs=False)
        print("Break cleared 🧹")
        return
    try:
        brk = input("Break minutes? (empty to skip): ").strip()
    except EOFError:
        brk = ""
    if not brk:
        stop(active, launch_emacs=True)
        print("Pomodoro cleared 🧹")
        return
    mins = _require_int(brk, "break minutes")
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
