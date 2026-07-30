### Sync service (multi-machine)

The `pomo` command is a thin wrapper around a small stdlib-Python sync
service in `pomo/`, so pomodoros stay in sync across computers and still work offline.

Components:
- `server.py`  — HTTP/JSON source of truth (SQLite history, last-write-wins).
  Runs on one "home-base" machine (or a VPS). Endpoints: `GET /current`,
  `POST /sessions`, `POST /sessions/end`, `GET /health`.
- `agent.py`   — per-machine daemon: polls the server every 5s, owns the
  countdown/overtime timer + side effects, and flushes an offline outbox.
- `pomo.py`    — the CLI (`pomo <min>`, `pomo break <min>`, `pomo clear`).
- `common.py`  — shared cache/HTTP/config helpers.
- `agent.toml.sample` — per-machine config (server URL, name, side-effect flags).

State cache: `~/.cache/pomo/current.json` plus the legacy `/tmp/org-pomodoro`
line that the tmux status bar reads (unchanged format).

Setup:
1. Copy `pomo/agent.toml.sample` to `pomo/agent.toml`; set `server_url` and
   `machine_name`. A Tailscale hostname is recommended (there is no app-level
   auth by default; set `POMO_TOKEN` on server + agents to enable it).
2. Home-base only: `cp pomo/launchd/ai.pomo.server.plist ~/Library/LaunchAgents/`
   then `launchctl load` it.
3. Every machine: `cp pomo/launchd/ai.pomo.agent.plist ~/Library/LaunchAgents/`
   then `launchctl load` it.

Offline: starts write the local cache immediately and queue the push; the
agent syncs on reconnect (last-write-wins by timestamp).
