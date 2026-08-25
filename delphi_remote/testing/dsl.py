"""High-level test DSL for native VCL applications.

The DSL is intentionally thin — it covers the patterns that show up in
every UI smoke test (launch, attach, fill edits, click buttons, paste paths
into file dialogs, close gracefully) and otherwise lets you reach the raw
pywinauto window via ``app.main_window`` / ``app.window(...)`` if you need
to do something the DSL doesn't model.

Usage::

    with delphi_app(r"C:\\path\\to\\MyApp.exe") as app:
        app.fill_edit_in_group(" Bandname ", "DIFFRACTING WAVES")
        app.click_button("Bild laden...")
        dlg = app.wait_for_file_dialog()
        dlg.paste_path(r"C:\\path\\to\\image.jpg")
        dlg.wait_until_closed()

Locating elements: VCL forms expose their controls in the UIA tree by their
``Caption`` property (for buttons, labels) or by their position within a
``TGroupBox`` named after its caption. Use ``find_class`` / ``found_index``
on ``app.main_window`` for raw pywinauto access when needed.
"""
from __future__ import annotations

import contextlib
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Iterator

import pywinauto
from pywinauto.keyboard import send_keys

from delphi_remote.testing.file_dialog import (
    FileDialog,
    find_modal_top,
    wait_for_modal,
)
from delphi_remote.testing.helpers import enable_dpi_awareness, graceful_close

log = logging.getLogger(__name__)


class DelphiAppError(RuntimeError):
    """Raised when the DSL cannot launch or reach a running VCL application."""


class DelphiApp:
    """A running VCL application, attached via pywinauto's UIA backend.

    Don't instantiate directly — use ``delphi_app()`` as a context manager
    so the process is closed gracefully when the test exits.
    """

    def __init__(
        self,
        process: subprocess.Popen[bytes] | None,
        app: pywinauto.Application,
        main_window: Any,
    ) -> None:
        self._process = process
        self._app = app
        self._main = main_window

    # ── Process / window properties ─────────────────────────────────────────

    @property
    def pid(self) -> int:
        return int(self._app.process)

    @property
    def main_window(self) -> Any:
        """The top-level pywinauto WindowSpecification for the app's main form."""
        return self._main

    @property
    def main_handle(self) -> int:
        return int(self._main.element_info.handle)

    # ── Window discovery ────────────────────────────────────────────────────

    def window(self, *, title: str | None = None, title_re: str | None = None,
               class_name: str | None = None) -> Any:
        """Return a pywinauto WindowSpecification matching the given filters.

        Same arguments pywinauto's ``Application.window()`` accepts. Raises
        a ``DelphiAppError`` if the window doesn't exist by the time it's
        used (lazy — pywinauto will raise on first access).
        """
        return self._app.window(title=title, title_re=title_re, class_name=class_name)

    def wait_for_window(self, title_re: str, timeout: float = 10.0,
                        ready_state: str = "exists") -> Any:
        """Block until a top-level window matching ``title_re`` appears."""
        deadline = time.monotonic() + timeout
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                w = self._app.window(title_re=title_re)
                w.wait(ready_state, timeout=0.5)
                return w
            except Exception as e:
                last_err = e
                time.sleep(0.2)
        raise DelphiAppError(
            f"No window matching title_re={title_re!r} appeared within "
            f"{timeout:.1f}s (last error: {last_err!r})"
        )

    # ── Common element actions ──────────────────────────────────────────────

    def click_button(self, caption_or_re: str, *, use_regex: bool = False) -> None:
        """Click a button on the main form by its caption.

        Set ``use_regex=True`` to interpret ``caption_or_re`` as a regex
        (useful for buttons whose caption embeds a parameter, e.g.
        ``"Als 3000x3000 speichern..."``).
        """
        kwargs: dict[str, Any] = {"control_type": "Button"}
        if use_regex:
            kwargs["title_re"] = caption_or_re
        else:
            kwargs["title"] = caption_or_re
        self._main.child_window(**kwargs).click_input()

    def fill_edit_in_group(self, group_caption: str, text: str,
                           *, edit_index: int = 0,
                           edit_class: str = "TEdit") -> None:
        """Clear and type into a ``TEdit`` inside a named ``TGroupBox``.

        The VCL pattern is: a ``TGroupBox`` with a visible caption containing
        one or more ``TEdit`` children. Pywinauto exposes the groupbox as a
        ``Pane`` whose title is the caption (often padded with spaces, e.g.
        ``" Bandname "``). ``edit_index`` picks among multiple edits in the
        same group.
        """
        group = self._main.child_window(title=group_caption, control_type="Pane")
        edit = group.child_window(class_name=edit_class, found_index=edit_index)
        edit.click_input()
        time.sleep(0.1)
        send_keys("^a{DEL}")
        time.sleep(0.1)
        send_keys(text, with_spaces=True, pause=0.005)

    def read_edit_in_group(self, group_caption: str, *,
                           edit_index: int = 0,
                           edit_class: str = "TEdit") -> str:
        """Read text from a TEdit by copy-paste round-trip (UIA can't read it directly)."""
        from delphi_remote.testing.helpers import get_clipboard_text, set_clipboard_text

        sentinel = f"__delphi_remote_edit_sentinel_{int(time.time()*1000)}__"
        set_clipboard_text(sentinel)
        group = self._main.child_window(title=group_caption, control_type="Pane")
        edit = group.child_window(class_name=edit_class, found_index=edit_index)
        edit.click_input()
        time.sleep(0.1)
        send_keys("^a^c")
        time.sleep(0.2)
        text = get_clipboard_text()
        return "" if text == sentinel else text

    def read_status_bar(self) -> str:
        """Return the text of the first ``TStatusBar`` panel on the main form."""
        try:
            sb = self._main.child_window(class_name="TStatusBar")
            for child in sb.children():
                try:
                    t = (child.window_text() or "").strip()
                    if t:
                        return t
                except Exception:
                    continue
        except Exception as e:
            log.debug("Could not read status bar: %r", e)
        return ""

    # ── File dialogs ────────────────────────────────────────────────────────

    def wait_for_file_dialog(self, timeout: float = 5.0) -> FileDialog:
        """Wait for an OS file-open / file-save dialog to appear."""
        dlg = wait_for_modal(self._app, self.main_handle, timeout=timeout)
        if dlg is None:
            raise DelphiAppError(
                f"No OS file dialog appeared within {timeout:.1f}s. "
                f"Did the button click actually open one?"
            )
        return FileDialog(self._app, dlg, self.main_handle)

    def has_open_dialog(self) -> bool:
        """True if any ``#32770`` dialog is currently visible."""
        return find_modal_top(self._app, self.main_handle) is not None

    def dismiss_stray_dialog(self) -> bool:
        """Send Esc to any open dialog. Useful in test ``setup`` blocks."""
        dlg = find_modal_top(self._app, self.main_handle)
        if dlg is None:
            return False
        try:
            dlg.set_focus()
            time.sleep(0.2)
            send_keys("{ESC}")
            time.sleep(0.4)
            return True
        except Exception as e:
            log.warning("Could not Esc stray dialog: %r", e)
            return False

    # ── Geometry helpers ────────────────────────────────────────────────────

    def set_window_rect(self, x: int, y: int, w: int, h: int) -> None:
        """Force the main form to a known geometry (handy for canvas-pixel tests)."""
        import win32gui

        hwnd = self.main_handle
        win32gui.ShowWindow(hwnd, 1)  # SW_SHOWNORMAL
        time.sleep(0.2)
        win32gui.MoveWindow(hwnd, x, y, w, h, True)
        time.sleep(0.3)

    # ── Cleanup ─────────────────────────────────────────────────────────────

    def close(self, *, timeout: float = 5.0) -> bool:
        """Send WM_CLOSE to the main form; kill the process if it hangs."""
        return graceful_close(self._main, self.pid, timeout=timeout)


# ── Context manager ─────────────────────────────────────────────────────────

@contextlib.contextmanager
def delphi_app(
    exe: Path | str,
    *,
    args: list[str] | None = None,
    title_re: str | None = None,
    startup_timeout: float = 10.0,
    set_dpi_awareness: bool = True,
    settle_seconds: float = 0.3,
) -> Iterator[DelphiApp]:
    """Launch a VCL app and yield a ``DelphiApp`` wrapper; close on exit.

    ``title_re``:
      Regex the main form's caption must match. Defaults to ``"^<exe stem>$"``
      (case-sensitive). Override for apps whose caption differs from the EXE
      name or includes a version suffix.

    ``set_dpi_awareness``:
      Make the *test* process per-monitor DPI aware on entry so coordinates
      align with the app's pixels. Leave True unless your test has already
      set its own DPI policy.

    On exit the app is asked to close via WM_CLOSE (so its FormDestroy /
    autosave logic runs) with a hard SIGTERM fallback after 5 seconds.
    """
    exe = Path(exe)
    if not exe.is_file():
        raise DelphiAppError(f"Executable does not exist: {exe}")

    if set_dpi_awareness:
        enable_dpi_awareness()

    title_re = title_re or f"^{exe.stem}$"
    proc = subprocess.Popen([str(exe), *(args or [])])

    try:
        deadline = time.monotonic() + startup_timeout
        app: pywinauto.Application | None = None
        main_window: Any = None
        while time.monotonic() < deadline:
            handles = pywinauto.findwindows.find_windows(
                title_re=title_re, process=proc.pid,
            )
            if handles:
                app = pywinauto.Application(backend="uia").connect(handle=handles[0])
                main_window = app.window(handle=handles[0])
                break
            time.sleep(0.2)

        if app is None or main_window is None:
            proc.terminate()
            raise DelphiAppError(
                f"App {exe.name} did not show a window matching "
                f"title_re={title_re!r} within {startup_timeout:.1f}s"
            )

        main_window.set_focus()
        time.sleep(settle_seconds)

        wrapper = DelphiApp(proc, app, main_window)
        try:
            yield wrapper
        finally:
            wrapper.close()
    finally:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("Process %s still alive after WM_CLOSE — killing", proc.pid)
            try:
                proc.kill()
            except Exception as e:
                log.warning("Could not kill leftover process %s: %r", proc.pid, e)
