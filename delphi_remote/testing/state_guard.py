"""Backup/restore helper for application state files used by UI tests.

A test that wants to inject a deterministic ``state.json`` (or any per-user
config) into ``%APPDATA%\\<App>\\state.json`` must NOT just ``unlink`` the
original — that destroys whatever the user had open. Use ``StateGuard`` so
the user's file is moved to a sibling backup at the start of the test and
restored at the end (or on crash via ``atexit``).
"""
from __future__ import annotations

import atexit
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class StateGuard:
    """Context manager that swaps a state file out for the duration of a test.

    ``with StateGuard(path)`` moves ``path`` → ``path.with_suffix(suffix)``
    on enter, deletes any test-written file on exit, and restores the
    backup. Idempotent: if the backup already exists (previous run crashed),
    it is left in place rather than overwritten.

    Example::

        STATE = Path(os.environ["APPDATA"]) / "MyApp" / "state.json"
        with StateGuard(STATE):
            STATE.write_text(json.dumps(my_test_state))
            run_my_test()
        # original state.json is back
    """

    DEFAULT_BACKUP_SUFFIX = ".user_backup"

    def __init__(self, state_path: Path, *, backup_suffix: str | None = None) -> None:
        self.state = Path(state_path)
        suffix = backup_suffix or self.DEFAULT_BACKUP_SUFFIX
        self.backup = self.state.with_suffix(suffix)

    def __enter__(self) -> "StateGuard":
        self.state.parent.mkdir(parents=True, exist_ok=True)
        if self.state.exists():
            self.state.replace(self.backup)
        # If backup exists but state doesn't, an earlier run crashed mid-test.
        # Don't overwrite the original — let the user inspect it.
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            self.state.unlink(missing_ok=True)
        except OSError as e:
            log.warning("Could not remove test state file %s: %r", self.state, e)
        if self.backup.exists():
            try:
                self.backup.replace(self.state)
            except OSError as e:
                log.warning("Could not restore state backup %s → %s: %r",
                            self.backup, self.state, e)


_GUARDED_PATHS: set[Path] = set()


def guard_module(state_path: Path, *, backup_suffix: str | None = None) -> None:
    """Top-level variant: take a backup now, restore via ``atexit``.

    Use this in scripts that don't have a clean ``main()`` block where a
    ``with`` statement would fit. Calling twice for the same path is a no-op.
    """
    state_path = Path(state_path)
    if state_path in _GUARDED_PATHS:
        return
    _GUARDED_PATHS.add(state_path)

    suffix = backup_suffix or StateGuard.DEFAULT_BACKUP_SUFFIX
    backup = state_path.with_suffix(suffix)

    state_path.parent.mkdir(parents=True, exist_ok=True)
    if state_path.exists():
        state_path.replace(backup)

    def _restore() -> None:
        try:
            state_path.unlink(missing_ok=True)
        except OSError:
            pass
        if backup.exists():
            try:
                backup.replace(state_path)
            except OSError as e:
                log.warning("atexit restore failed for %s: %r", state_path, e)

    atexit.register(_restore)
