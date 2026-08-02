"""Shared test helpers.

Isolates all filesystem/db state onto a per-test temp dir by monkeypatching
the module-level path globals (they are resolved from ``Path.home()`` at import
time, so overriding the globals is enough — no env vars needed).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import common  # noqa: E402


def isolate(test) -> Path:
    """Point common/server path globals at a fresh temp dir for one test.

    Registers cleanup via ``test.addCleanup`` so originals are restored even on
    failure. Returns the temp dir root.
    """
    tmp = Path(tempfile.mkdtemp())
    test.addCleanup(_rmtree, tmp)

    overrides = {
        "CONFIG_DIR": tmp / "config",
        "CACHE_DIR": tmp / "cache",
        "DATA_DIR": tmp / "data",
        "CONFIG_FILE": tmp / "config" / "agent.toml",
        "CACHE_FILE": tmp / "cache" / "current.json",
        "OUTBOX_FILE": tmp / "cache" / "outbox.jsonl",
        "DB_FILE": tmp / "data" / "pomo.db",
        "HOOKS_DIR": tmp / "config" / "hooks",
    }
    for name, value in overrides.items():
        _patch(test, common, name, value)

    return tmp


def _patch(test, module, name: str, value) -> None:
    original = getattr(module, name)
    setattr(module, name, value)
    test.addCleanup(setattr, module, name, original)



def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)
