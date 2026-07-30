# pomo

[![CI](https://github.com/gopar/pomodoro/actions/workflows/ci.yml/badge.svg)](https://github.com/gopar/pomodoro/actions/workflows/ci.yml)

A pomodoro timer that keeps your current session in sync across all your
machines and still works offline. Pure standard-library Python — no third-party
dependencies, no package manager.

Start a pomodoro on your laptop and your desktop knows about it; when it runs
over, whichever machine you're on can announce it. Each machine decides for
itself what happens on each event (notifications, Focus mode, sounds, …) through
simple hook scripts.

## How it works

Three small processes share `common.py`:

- **`server.py`** — the source of truth. An HTTP/JSON service backed by SQLite
  with an append-only history; conflicts resolve last-write-wins by timestamp.
  Runs on one "home-base" machine (or a VPS / container).
- **`agent.py`** — a per-machine daemon. Polls the server, owns the local
  countdown→overtime timer, fires lifecycle hooks, and flushes an offline outbox
  when the server is unreachable.
- **`pomo.py`** — the CLI you actually type (`pomo 25`, `pomo break 5`,
  `pomo clear`). Writes the local cache immediately, then pushes to the server
  (or queues the push if offline).

A session moves through: `pomodoro`/`break` → `overtime`/`break-overtime` →
`ended`. Because the timer is local, everything keeps working with no network.

## Requirements

- Python **3.11+** (uses the stdlib `tomllib`), nothing else to install.
- The agent and CLI run on the host (macOS/Linux). The server runs anywhere,
  including Docker.

## Usage

The `pomo` command is `pomo.py`. Put it on your `PATH` (symlink or alias), e.g.
`ln -s ~/.config/pomo/pomo.py ~/.local/bin/pomo`, then:

```
pomo <minutes>        Start a pomodoro
pomo break <minutes>  Start a break
pomo clear            Stop & clear the pomodoro (prompts for a break)
```

## Setup

The repo is deployed to `~/.config/pomo` on each machine — that's where the
launchd/systemd service files expect to find `agent.py` / `server.py`.

1. Copy the sample config and edit it:

   ```
   cp agent.toml.sample agent.toml   # sets server_url + machine_name
   ```

   A Tailscale hostname for `server_url` is recommended. There is no app-level
   auth by default; set `POMO_TOKEN` on the server and every agent to enable
   bearer auth.

2. **Home-base only** — start the server:
   - macOS: `cp launchd/ai.pomo.server.plist ~/Library/LaunchAgents/` then
     `launchctl load` it.
   - Linux: `cp systemd/pomo-server.service ~/.config/systemd/user/` then
     `systemctl --user daemon-reload && systemctl --user enable --now pomo-server`.
   - Docker: see below.

3. **Every machine** — start the agent:
   - macOS: `cp launchd/ai.pomo.agent.plist ~/Library/LaunchAgents/` then
     `launchctl load` it.
   - Linux: `cp systemd/pomo-agent.service ~/.config/systemd/user/` then
     `systemctl --user daemon-reload && systemctl --user enable --now pomo-agent`.
     For persistence across logout/reboot: `loginctl enable-linger "$USER"`.
     Logs: `journalctl --user -u pomo-agent -f`.

Offline behavior: starts write the local cache immediately and queue the push;
the agent syncs on reconnect (last-write-wins by timestamp).

## Run the server with Docker (optional)

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

## Hooks (all side effects live here)

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
a Windows stub) live in `hooks/examples/<event>.d/`. Copy the ones you want:

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

## Config & paths

- **Config:** `~/.config/pomo/agent.toml` (see `agent.toml.sample`) — `server_url`,
  `machine_name`, `poll_interval`, `run_for_remote_sessions`, and `[hooks]`.
- **Cache:** `~/.cache/pomo/` (current session + offline outbox).
- **Server DB:** `~/.local/share/pomo/pomo.db` (override with `POMO_DB_PATH`).
- **Env overrides:** `POMO_SERVER_URL` (agent/CLI), `POMO_TOKEN` (bearer auth,
  both ends), `POMO_PORT` / `POMO_HOST` / `POMO_DB_PATH` (server).

Server endpoints: `GET /current`, `GET /health`, `POST /sessions`,
`POST /sessions/end`.

## Development

Tests are stdlib `unittest`, no dependencies:

```
python3 -m unittest discover -s tests -t tests
```

CI runs the suite on Python 3.11–3.14. See `AGENTS.md` for architecture notes
and invariants.

## License

MIT.
