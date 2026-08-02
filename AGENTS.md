# AGENTS.md

Multi-machine pomodoro sync service. Python **stdlib only** — no third-party
deps, no package manager, no build/lint/test config. Requires Python 3.11+
(`tomllib`); developed/run on macOS.

Tests live in `tests/` (stdlib `unittest`, no deps). Run them with:

    python3 -m unittest discover -s tests -t tests

Add tests under `tests/` to verify changes; for anything that can't be covered
by a test, run the processes directly (see below).
Side effects (`shortcuts`, `say`, `afplay`, Emacs) are macOS-specific.

CI (`.github/workflows/ci.yml`) runs the `unittest` suite on Python 3.11–3.14
plus a `compileall` syntax gate (Ubuntu). Only the **server** is containerized
(`Dockerfile` / `docker-compose.yml`, DB on the `/data` volume); agents and the
CLI are host processes by design.

## Architecture (3 processes, shared `common.py`)

- `server.py` — HTTP/JSON source of truth (SQLite, last-write-wins). One instance
  on a "home-base" machine. Endpoints: `GET /current`, `GET /health`,
  `POST /sessions`, `POST /sessions/end`.
- `agent.py` — per-machine daemon. Polls `/current`, owns the countdown→overtime
  timer, fires side effects, flushes the offline outbox.
- `pomo.py` — the CLI (`pomo <min>`, `pomo break <min>`, `pomo clear`). Writes the
  local cache immediately, then pushes (or queues to outbox if offline).

`common.py` is imported by all three (each does `sys.path.insert` on its own dir,
so run scripts directly, e.g. `python3 agent.py`, not as a `-m` package).

## Critical invariants — easy to break

- **LWW by `updated_at`**: every session mutation must set `updated_at = time.time()`
  or the server/agent will silently drop it as stale. See `apply_session` in
  `server.py` and `tick_timer` in `agent.py`. On the server this is race-safe:
  the history insert + a WHERE-guarded pointer UPDATE (`? >= updated_at`) run in
  one `BEGIN IMMEDIATE` transaction (WAL + `busy_timeout`), so concurrent writers
  can't lose the newest write.
- **`ended` is a real state, not deletion**: stops propagate as an `ended` record
  (a file deletion can't sync). Keep it in `ALL_STATES`; `is_idle()` treats
  `idle`/`ended`/`None` as idle.
- **Cache writes are atomic** (temp file + `replace`) so concurrent readers
  never see partial data. Preserve this pattern.
- States: `pomodoro`/`break` → `overtime`/`break-overtime` (via `OVERTIME_OF`) → `ended`.

## Side effects & hooks

- **All side effects are hooks** — the daemon ships no built-in effects and is
  OS-agnostic. Both CLI and agent fire events via `hooks.dispatch()` in
  `hooks.py` (the single entry point, so `pomo.py` and `agent.py` don't import
  each other). Hooks are best-effort and must never crash the loop/CLI.
- User hooks: executables in `~/.config/pomo/hooks/<event>.d/*` (`hooks.py`), run in
  lexical order, killed after `hooks.timeout`. Events: `pomodoro_start`,
  `break_start`, `pomodoro_overtime`, `break_overtime`, `session_stop`. Ready-made examples
  (macOS + Linux, Windows stub) in `hooks/examples/<event>.d/`.
- `run_for_remote_sessions` (top-level config) gates whether adopting a
  remote-started session fires hooks (`on_remote_adopt` in `agent.py`).

## Config & paths

- Config: `~/.config/pomo/agent.toml` (see `agent.toml.sample`), merged over
  `_DEFAULT_CONFIG` in `common.py`. Only `[hooks]` is deep-merged; other keys
  replace. Agent re-reads config every loop.
- Env overrides: `POMO_SERVER_URL` (agent/CLI), `POMO_TOKEN` (bearer auth, both
  ends), `POMO_PORT`/`POMO_HOST`/`POMO_DB_PATH` (server).
- Paths in `common.py`: cache `~/.cache/pomo/`, DB `~/.local/share/pomo/pomo.db`.

## Testing Philosophy
- When adding tests, do your best to minimize mock usage.
- Prefer tests that test the behavior and not the internals.
- When fixing a bug, write a test to verify existing bug and then re-run it to verify it has been fix.
- Tests isolate all state onto a temp dir via `tests/_util.py` (patches path
  globals in `common`/`server`); they never touch real `~`.`
- When adding tests use Gherkin style comments (Given, When, Then, etc)

See `README.md` for the setup/launchd flow and hook details.
