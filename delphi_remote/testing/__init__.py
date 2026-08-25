"""UI testing toolkit for native VCL applications produced by Delphi CE.

A thin layer over ``pywinauto`` UIA that captures the patterns the bridge
collected while testing real VCL apps (CoverGenerator was the pilot):

  * launch + attach with title-regex polling
  * graceful close via WM_CLOSE (triggers FormDestroy / autosave)
  * file-dialog path injection via clipboard paste (Alt+N → ^a{DEL} → ^v →
    {ENTER}) — the only reliable way across DE/EN locales
  * %APPDATA% state-file backup/restore for tests that need a clean config

Example::

    from delphi_remote.testing import delphi_app, StateGuard

    with StateGuard(my_state_json):
        with delphi_app(r"C:\\path\\to\\MyApp.exe") as app:
            app.fill_edit_in_group(" Bandname ", "DIFFRACTING WAVES")
            app.main_window.button("Bild laden...").click()
            dlg = app.wait_for_file_dialog()
            dlg.paste_path(r"C:\\path\\to\\image.jpg")
"""
from __future__ import annotations

from delphi_remote.testing.dsl import (
    DelphiApp,
    DelphiAppError,
    delphi_app,
)
from delphi_remote.testing.file_dialog import (
    FileDialog,
    find_modal_top,
    paste_path,
    wait_for_modal,
    wait_for_no_modal,
)
from delphi_remote.testing.helpers import (
    enable_dpi_awareness,
    get_clipboard_text,
    graceful_close,
    set_clipboard_text,
)
from delphi_remote.testing.state_guard import StateGuard, guard_module
from delphi_remote.testing.visual import (
    VisualDiff,
    capture_window,
    compare_images,
    matches_baseline,
)

__all__ = [
    "DelphiApp",
    "DelphiAppError",
    "FileDialog",
    "StateGuard",
    "VisualDiff",
    "capture_window",
    "compare_images",
    "delphi_app",
    "enable_dpi_awareness",
    "find_modal_top",
    "get_clipboard_text",
    "graceful_close",
    "guard_module",
    "matches_baseline",
    "paste_path",
    "set_clipboard_text",
    "wait_for_modal",
    "wait_for_no_modal",
]
