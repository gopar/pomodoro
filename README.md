### Sync service (multi-machine)

The `pomo` command is a thin wrapper around a small stdlib-Python sync
service in `pomo/`, so pomodoros stay in sync across computers and still work offline.

Components:
- `server.py`  — HTTP/JSON source of truth (SQLite history, last-write-wins).
  Runs on one "home-base" machine (or a VPS). Endpoints: `GET /current`,
  `POST /sessions`, `POST /sessions/end`, `GET /health`.
- `agent.py`   — per-machine daemon: polls the server every 5s, owns the
  countdown/overtime timer, fires lifecycle hooks, and flushes an offline outbox.
- `pomo.py`    — the CLI (`pomo <min>`, `pomo break <min>`, `pomo clear`).
- `common.py`  — shared cache/HTTP/config helpers.
- `hooks.py`   — runs user-defined executables on lifecycle events.
- `agent.toml.sample` — per-machine config (server URL, name, hooks settings).

State cache: `~/.cache/pomo/current.json`.

Setup:
1. Copy `pomo/agent.toml.sample` to `pomo/agent.toml`; set `server_url` and
   `machine_name`. A Tailscale hostname is recommended (there is no app-level
   auth by default; set `POMO_TOKEN` on server + agents to enable it).
2. Home-base only — start the server:
   - macOS: `cp pomo/launchd/ai.pomo.server.plist ~/Library/LaunchAgents/`
     then `launchctl load` it.
   - Linux: `cp pomo/systemd/pomo-server.service ~/.config/systemd/user/`
     then `systemctl --user daemon-reload && systemctl --user enable --now pomo-server`.
3. Every machine — start the agent:
   - macOS: `cp pomo/launchd/ai.pomo.agent.plist ~/Library/LaunchAgents/`
     then `launchctl load` it.
   - Linux: `cp pomo/systemd/pomo-agent.service ~/.config/systemd/user/`
     then `systemctl --user daemon-reload && systemctl --user enable --now pomo-agent`.
   - Linux persistence across logout/reboot: `loginctl enable-linger "$USER"`.
     Logs: `journalctl --user -u pomo-agent -f`.

Offline: starts write the local cache immediately and queue the push; the
agent syncs on reconnect (last-write-wins by timestamp).

### Run the server with Docker (optional)

Only the **server** is containerized — agents and the CLI stay on the host by
design (they fire OS-native hooks and write to `~/.config`/`~/.cache`). Use this
*instead of* `systemd/pomo-server.service` on the home-base (run one, not both).

```
docker compose up -d          # build + run, persistent volume "pomo-data"
docker compose logs -f
```

Or without compose:

```
docker build -t pomo-server .
docker run -d --name pomo-server -p 8787:8787 -v pomo-data:/data pomo-server
```

The SQLite DB (and its WAL sidecars) live in the `/data` volume so they survive
restarts. Set `POMO_TOKEN` (env / compose) to require bearer auth; point agents
at this host via their `server_url` / `POMO_SERVER_URL`.

### Hooks (all side effects live here)

Every side effect is a hook — the agent itself is OS-agnostic and ships with no
built-in effects. To run actions on lifecycle events, drop executable scripts
into per-machine (local) directories:

```
~/.config/pomo/hooks/<event>.d/*     # chmod +x
```

Events: `pomodoro_start`, `break_start`, `pomodoro_end`, `break_end`,
`session_stop`. Every executable in the matching `<event>.d/` runs, in lexical
filename order (prefix with `10-`, `20-`, … to control ordering).

Ready-made examples (macOS Focus/`say`/alarm/Emacs, with Linux equivalents and
a Windows stub) live in `hooks/examples/<event>.d/`. Copy the ones you want,
e.g.:

```
mkdir -p ~/.config/pomo/hooks/pomodoro_start.d
cp hooks/examples/pomodoro_start.d/10-focus-on.sh ~/.config/pomo/hooks/pomodoro_start.d/
chmod +x ~/.config/pomo/hooks/pomodoro_start.d/10-focus-on.sh
```

Each script gets context two ways:

- Env vars: `POMO_EVENT`, `POMO_STATE`, `POMO_START_EPOCH`, `POMO_DURATION`,
  `POMO_MACHINE`, `POMO_ORIGIN_MACHINE`, `POMO_REMOTE` (`0`/`1`),
  `POMO_SESSION_ID`.
- The full session as JSON on stdin.

Hooks are best-effort: a failing, missing, or slow hook (killed after
`hooks.timeout` seconds) never affects the timer or CLI. See
`hooks/examples/` for starter scripts, and `[hooks]` in `agent.toml.sample`
to tune `enabled` / `timeout` / `dir`.

