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

## Architecture (3 processes in the `pomo` package)

- `pomo/server.py` — HTTP/JSON source of truth (SQLite, last-write-wins). One instance
  on a "home-base" machine. Endpoints: `GET /health`, `GET /current`,
  `GET /sessions` (optional `?project=`), `GET /projects`, `POST /sessions`,
  `POST /sessions/end`.
- `pomo/agent.py` — per-machine daemon. Polls `/current`, owns the countdown→overtime
  timer, fires side effects, flushes the offline outbox.
- `pomo/cli.py` — the CLI (`pomo start <min>`, `pomo break <min>`, `pomo clear`). Writes the
  local cache immediately, then pushes (or queues to outbox if offline).

`pomo/common.py` is imported by all three. Each script adds the repo root to
`sys.path` on startup so it can be run directly (e.g. `python3 pomo/cli.py`)
or as a module (e.g. `python3 -m pomo.cli`).

## Critical invariants — easy to break

- **LWW by `updated_at`**: every session mutation must set `updated_at = time.time()`
  or the server/agent will silently drop it as stale. See `apply_session` in
  `pomo/server.py` and `tick_timer` in `pomo/agent.py`. On the server this is race-safe:
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
  `pomo/hooks.py` (the single entry point, so `pomo/cli.py` and `pomo/agent.py` don't import
  each other). Hooks are best-effort and must never crash the loop/CLI.
- User hooks: executables in `~/.config/pomo/hooks/<event>.d/*` (`pomo/hooks.py`), run in
  lexical order, killed after `hooks.timeout`. Events: `pomodoro_start`,
  `break_start`, `pomodoro_overtime`, `break_overtime`, `session_stop`. Ready-made examples
  (macOS + Linux, Windows stub) in `hooks/examples/<event>.d/`.
- `run_for_remote_sessions` (top-level config) gates whether adopting a
  remote-started session fires hooks (`on_remote_adopt` in `pomo/agent.py`).

## Config & paths

- Config: `~/.config/pomo/agent.toml` (see `agent.toml.sample`), merged over
  `_DEFAULT_CONFIG` in `pomo/common.py`. Only `[hooks]` is deep-merged; other keys
  replace. Agent re-reads config every loop.
- Env overrides: `POMO_SERVER_URL` (agent/CLI), `POMO_TOKEN` (bearer auth, both
  ends), `POMO_PORT`/`POMO_HOST`/`POMO_DB_PATH` (server).
- Paths in `pomo/common.py`: cache `~/.cache/pomo/`, DB `~/.local/share/pomo/pomo.db`.

## Testing Philosophy
- When adding tests, do your best to minimize mock usage.
- Prefer tests that test the behavior and not the internals.
- When fixing a bug, write a test to verify existing bug and then re-run it to verify it has been fix.
- Tests isolate all state onto a temp dir via `tests/_util.py` (patches path
  globals in `common`/`server`); they never touch real `~`.`
- When adding tests use Gherkin style comments (Given, When, Then, etc)

## Keeping README in sync

`README.md` is the user-facing documentation. It must stay in sync with the
code. When you add, change, or remove any of these, update `README.md`:

- **CLI flags/subcommands** — caught by snapshot tests in
  `tests/test_snapshots.py` (CI fails on mismatch)
- **Hook environment variables** — caught by `test_build_env_has_expected_pomo_keys`
  in `tests/test_hooks.py` (CI fails on mismatch)
- **Server API endpoints** — no automated check; update manually

## Versioning

When bumping the version:

- Update the `VERSION` file
- Commit as `vX.Y.Z`
- (optional) `git tag vX.Y.Z`

See `README.md` for the setup/launchd flow and hook details.
