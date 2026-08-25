"""Helpers for driving the Windows common file-open / file-save dialog.

Why a dedicated module: the OS file dialog is a separate top-level window
(class ``#32770``) that pywinauto can't reach via the parent app's tree,
because it isn't owned by the app in the UIA sense. The reliable pattern,
in this order, is:

  1. Find the new top-level window whose class is ``#32770`` and whose
     process matches the app.
  2. Focus it.
  3. ``Alt+N`` to put focus on the "Dateiname:" / "File name:" edit.
  4. ``Ctrl+A {DEL}`` to clear it (the dialog may have prefilled it).
  5. ``Ctrl+V`` to paste a clipboard-stashed absolute path.
  6. ``{ENTER}`` to confirm.

``send_keys`` to type the path character-by-character fails on long paths
or paths with spaces — Windows races the focus and drops characters.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import pywinauto
from pywinauto.keyboard import send_keys

from delphi_remote.testing.helpers import set_clipboard_text

log = logging.getLogger(__name__)

FILE_DIALOG_CLASS = "#32770"


def find_modal_top(app: pywinauto.Application, exclude_handle: int) -> Any | None:
    """Return the first ``#32770`` top-level window owned by ``app``.

    ``exclude_handle`` should be the main window of the app (so we don't
    accidentally return it). Returns ``None`` if no file dialog is up yet.
    """
    try:
        handles = pywinauto.findwindows.find_windows(
            process=app.process, top_level_only=True,
        )
    except Exception as e:
        log.debug("find_windows failed: %r", e)
        return None
    for h in handles:
        if h == exclude_handle:
            continue
        try:
            w = app.window(handle=h)
            if (w.class_name() or "") == FILE_DIALOG_CLASS:
                return w
        except Exception:
            continue
    return None


def wait_for_modal(app: pywinauto.Application, exclude_handle: int,
                   timeout: float = 5.0) -> Any | None:
    """Poll for a file dialog to appear; returns it or ``None`` on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dlg = find_modal_top(app, exclude_handle)
        if dlg is not None:
            return dlg
        time.sleep(0.1)
    return None


def wait_for_no_modal(app: pywinauto.Application, exclude_handle: int,
                      timeout: float = 10.0) -> bool:
    """Wait until no file dialog is open. Returns True on success, False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if find_modal_top(app, exclude_handle) is None:
            return True
        time.sleep(0.1)
    return False


def paste_path(dialog: Any, path: Path | str, *, settle_seconds: float = 0.15) -> None:
    """Type ``path`` into the dialog's filename edit and press Enter.

    Uses ``Alt+N`` to focus the edit, which is the standard Windows shortcut
    for "Dateiname:" / "File name:" — works on both DE and EN localized
    builds. Pastes via clipboard to avoid send_keys' per-char races.
    """
    dialog.set_focus()
    time.sleep(settle_seconds)
    set_clipboard_text(str(path))
    send_keys("%n")  # Alt+N
    time.sleep(settle_seconds)
    send_keys("^a{DEL}")
    time.sleep(0.05)
    send_keys("^v")
    time.sleep(settle_seconds)
    send_keys("{ENTER}")


class FileDialog:
    """High-level wrapper around an open Windows file dialog.

    Construct via ``DelphiApp.wait_for_file_dialog()`` rather than directly.
    """

    def __init__(self, app: pywinauto.Application, dialog: Any, main_handle: int) -> None:
        self._app = app
        self._dialog = dialog
        self._main_handle = main_handle

    @property
    def title(self) -> str:
        try:
            return self._dialog.window_text() or ""
        except Exception:
            return ""

    def paste_path(self, path: Path | str) -> None:
        """Inject ``path`` into the dialog's filename edit and submit."""
        paste_path(self._dialog, path)

    def cancel(self) -> None:
        try:
            self._dialog.set_focus()
            time.sleep(0.1)
            send_keys("{ESC}")
        except Exception as e:
            log.warning("Could not cancel file dialog: %r", e)

    def wait_until_closed(self, timeout: float = 10.0) -> bool:
        """Block until the file dialog disappears (e.g. after paste_path)."""
        return wait_for_no_modal(self._app, self._main_handle, timeout=timeout)
