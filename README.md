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
- `hooks.py`   — runs user-defined executables on lifecycle events.
- `agent.toml.sample` — per-machine config (server URL, name, side-effect flags).

State cache: `~/.cache/pomo/current.json`.

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

### Hooks (extend what happens on each event)

The built-in effects (Focus mode, `say`, alarm, launch Emacs) are just the
shipped defaults, toggled by `[side_effects]`. To run your own actions, drop
executable scripts into per-machine (local) directories:

```
~/.config/pomo/hooks/<event>.d/*     # chmod +x
```

Events: `pomodoro_start`, `break_start`, `pomodoro_end`, `break_end`,
`session_stop`. Every executable in the matching `<event>.d/` runs, in lexical
filename order (prefix with `10-`, `20-`, … to control ordering).

Each script gets context two ways:

- Env vars: `POMO_EVENT`, `POMO_STATE`, `POMO_START_EPOCH`, `POMO_DURATION`,
  `POMO_MACHINE`, `POMO_ORIGIN_MACHINE`, `POMO_REMOTE` (`0`/`1`),
  `POMO_SESSION_ID`.
- The full session as JSON on stdin.

Hooks are best-effort: a failing, missing, or slow hook (killed after
`hooks.timeout` seconds) never affects the timer or CLI. See
`hooks/examples/` for a starter script, and `[hooks]` in `agent.toml.sample`
to tune `enabled` / `timeout` / `dir`.

