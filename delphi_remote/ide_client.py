"""IDE client — wraps pywinauto for control of a running Delphi RAD Studio IDE.

Backend: UIA (UI Automation). The Win32 backend cannot reach modern Delphi
panels reliably, so UIA is the only viable option even though it's slower.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pywinauto import Application
from pywinauto.application import ProcessNotFoundError
from pywinauto.findwindows import ElementAmbiguousError, ElementNotFoundError

log = logging.getLogger(__name__)

IDE_EXECUTABLE = "bds.exe"
IDE_MAIN_WINDOW_CLASS = "TAppBuilder"


class BridgeError(RuntimeError):
    """Bridge failed to reach or operate the IDE."""


@dataclass
class IDEHandle:
    process_id: int
    main_window_title: str
    main_window_class: str


class DelphiIDE:
    """Thin wrapper around a running bds.exe process."""

    def __init__(self) -> None:
        self._app: Application | None = None
        self._main: Any = None  # pywinauto WindowSpecification

    def attach(self, timeout: float = 5.0) -> IDEHandle:
        """Attach to a running Delphi IDE. Raises BridgeError if not found."""
        try:
            self._app = Application(backend="uia").connect(
                path=IDE_EXECUTABLE, timeout=timeout
            )
        except (ElementNotFoundError, ProcessNotFoundError) as e:
            raise BridgeError(
                f"Delphi IDE ({IDE_EXECUTABLE}) not running — start it and retry"
            ) from e
        except ElementAmbiguousError as e:
            raise BridgeError(
                f"Multiple {IDE_EXECUTABLE} processes found — pid selection not yet supported"
            ) from e

        # Specifically pick the TAppBuilder window — top_window() can return
        # the splash screen during cold boot, which then disappears and leaves
        # a stale handle that reads empty titles.
        # Enumerate windows directly rather than going through window().element_info
        # so we don't pay pywinauto's default 5-second window-find timeout when
        # TAppBuilder isn't up yet.
        main = self._find_main_window_now()
        if main is None:
            raise BridgeError(
                f"IDE main window ({IDE_MAIN_WINDOW_CLASS}) not yet visible "
                f"(splash screen still up?). Retry shortly."
            )
        try:
            self._main = main
            info = main.element_info
            handle = IDEHandle(
                process_id=self._app.process,
                main_window_title=info.name or "",
                main_window_class=info.class_name or "",
            )
        except Exception as e:
            raise BridgeError(f"Could not enumerate IDE main window: {e!r}") from e

        log.info("Attached to %s pid=%s title=%r", IDE_EXECUTABLE, handle.process_id,
                 handle.main_window_title)
        return handle

    def is_attached(self) -> bool:
        return self._app is not None

    @property
    def main_window(self) -> Any:
        if self._main is None:
            raise BridgeError("Not attached — call attach() first")
        return self._main

    @property
    def process_id(self) -> int:
        if self._app is None:
            raise BridgeError("Not attached — call attach() first")
        return int(self._app.process)

    def read_main_window_title(self) -> str:
        if self._main is None:
            raise BridgeError("Not attached — call attach() first")
        try:
            return self._main.window_text() or ""
        except Exception as e:
            raise BridgeError(f"Could not read main window title: {e!r}") from e

    def focus_main_window(self) -> None:
        if self._main is None:
            raise BridgeError("Not attached — call attach() first")
        try:
            self._main.set_focus()
        except Exception as e:
            raise BridgeError(f"Could not focus main window: {e!r}") from e

    def restore_if_minimized(self) -> bool:
        """Restore the IDE main window if it's minimized to the taskbar.

        type_keys(set_foreground=True) brings a window forward but does not
        un-minimize it — keystrokes sent to a minimized HWND are dropped.

        Returns True if the window was restored, False if it was already
        in a normal/maximized state. Callers can use this to decide whether
        to re-minimize after their work.
        """
        if self._main is None:
            raise BridgeError("Not attached — call attach() first")
        try:
            import win32con
            import win32gui

            hwnd = self._main.element_info.handle
            if not hwnd:
                return False
            if win32gui.IsIconic(hwnd):
                log.info("IDE main window is minimized — restoring (hwnd=%s)", hwnd)
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.4)
                return True
            return False
        except Exception as e:
            log.warning("Could not check/restore minimized state: %r", e)
            return False

    def force_to_foreground(self, settle_ms: int = 300) -> bool:
        """Bring the IDE main window genuinely to the foreground via Win32.

        pywinauto's ``set_foreground=True`` on ``type_keys`` is a soft
        activation that often fails to fire Delphi's WM_ACTIVATEAPP-driven
        external-file-changed check. Doing the activation through plain
        ShowWindow + BringWindowToTop + SetForegroundWindow makes Delphi
        notice disk edits made between builds, so the resulting "Neu laden?"
        prompt opens *now* rather than mid-compile.

        Returns True if the window was minimized and had to be restored.
        """
        if self._main is None:
            raise BridgeError("Not attached — call attach() first")
        try:
            import win32con
            import win32gui

            hwnd = self._main.element_info.handle
            if not hwnd:
                return False
            was_minimized = bool(win32gui.IsIconic(hwnd))
            if was_minimized:
                log.info("IDE main window minimized — restoring (hwnd=%s)", hwnd)
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.2)
            try:
                win32gui.BringWindowToTop(hwnd)
            except Exception as e:
                log.debug("BringWindowToTop refused: %r", e)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception as e:
                # Win10+ can refuse foreground steals from inactive callers.
                # BringWindowToTop alone is usually enough to fire WM_ACTIVATE.
                log.debug("SetForegroundWindow refused: %r", e)
            time.sleep(settle_ms / 1000.0)
            return was_minimized
        except Exception as e:
            log.warning("Could not force IDE to foreground: %r", e)
            return False

    def minimize_main_window(self) -> None:
        """Minimize the IDE main window — used to undo a restore_if_minimized()."""
        if self._main is None:
            raise BridgeError("Not attached — call attach() first")
        try:
            import win32con
            import win32gui

            hwnd = self._main.element_info.handle
            if not hwnd:
                return
            log.info("Re-minimizing IDE main window (hwnd=%s)", hwnd)
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        except Exception as e:
            log.warning("Could not minimize IDE main window: %r", e)

    def ensure_project_loaded(
        self,
        dproj: Path,
        *,
        startup_timeout: float = 90.0,
        load_timeout: float = 60.0,
    ) -> IDEHandle:
        """Make sure the IDE is running with `dproj` as the active project.

        - If no IDE is running, ``bds.exe`` is launched via Windows file
          association on the dproj (same as double-clicking it in Explorer).
        - If an IDE is running with a different project, the same file
          association is used to ask the running IDE to load the dproj.
        - If the right project is already loaded, this returns immediately.

        Raises BridgeError on timeout or if the IDE never reaches the expected
        state.
        """
        project_name = dproj.stem
        expected_prefix = f"{project_name} - "

        try:
            handle = self.attach(timeout=2.0)
        except BridgeError:
            log.info("IDE not running — launching it with %s", dproj.name)
            self._launch_via_association(dproj)
            handle = self._poll_for_attach(deadline_seconds=startup_timeout)

        if not handle.main_window_title.startswith(expected_prefix):
            # A modal dialog beside the main window (the progress window a
            # previous build left open, above all) would swallow the
            # project-open keystroke: answer those first.
            from delphi_remote.toplevel import clear_blocking
            for entry in clear_blocking(handle.process_id):
                log.info("Before the project switch: cleared %s %r with %r",
                         entry["class_name"], entry["title"], entry["button"])
            log.info("Active project differs — asking IDE to load %s", dproj.name)
            self._load_project_replacing(dproj)

        if not handle.main_window_title.startswith(expected_prefix):
            handle = self._wait_for_project(expected_prefix, dproj, load_timeout)

        return handle

    def _load_project_replacing(self, dproj: Path, retry: bool = True) -> None:
        """Load `dproj` into the running IDE, replacing the current project.

        Do NOT use the file association here. os.startfile on a .dproj is a
        double-click in Explorer, and a running IDE answers that by *adding*
        the project to the existing project group. Two projects in one group
        means the IDE asks modally whether to save the group on every switch —
        which blocks the bridge, surfaces as "Could not read Meldungen panel",
        and leaves the caller wondering what happened.

        File → Projekt öffnen (Strg+F11) is the operation that actually
        replaces the active project. Tried twice; when the dialog never
        appears, a BridgeError says so (no association fallback any more).
        """
        try:
            import pywinauto
            from delphi_remote.testing.file_dialog import paste_path, wait_for_modal

            main = self.main_window
            app = pywinauto.Application(backend="uia").connect(
                process=self.process_id,
            )
            # A prompt still open (e.g. "Neu laden?" left by a build) disables
            # the main window: activating it or typing into it then fails with
            # ElementNotEnabled, and the association fallback below adds the
            # project to a group. Answer it before anything else.
            self._answer_pending_prompts(wait=0.0)
            self.force_to_foreground()
            # Activating the IDE makes it check its open files: a unit changed
            # on disk while open in the editor brings up "... geändert. Neu
            # laden?" right now, and it would swallow Strg+F11.
            self._answer_pending_prompts(wait=1.0)
            self._close_all()
            self.force_to_foreground()
            main.type_keys("^{F11}", set_foreground=True)

            dialog = wait_for_modal(app, main.handle, timeout=8.0)
            if dialog is None:
                # Strg+F11 is swallowed now and then (a prompt that came only
                # now, an editor state such as the .dproj as the active tab).
                # Once more, after the prompts and an Esc for the editor.
                log.warning("Strg+F11 did not open the project dialog - trying once more.")
                self._answer_pending_prompts(wait=0.0)
                main.type_keys("{ESC}", set_foreground=True)
                time.sleep(0.3)
                self.force_to_foreground()
                main.type_keys("^{F11}", set_foreground=True)
                dialog = wait_for_modal(app, main.handle, timeout=8.0)
            if dialog is None:
                # No file association here: a running IDE adds the project to
                # a project group, which then asks "ProjectGroup1 speichern
                # unter" before every build and blocks the IDE until someone
                # closes the group by hand (2026-10-04, twice). An error the
                # caller can act on is the lesser evil.
                raise BridgeError(
                    f"Strg+F11 did not open the open-project dialog for {dproj.name}. "
                    "Open the project in the IDE (Datei -> Projekt oeffnen) and "
                    "build again."
                )
            paste_path(dialog, dproj)
        except BridgeError:
            raise
        except Exception as e:                                  # noqa: BLE001
            # Typically ElementNotEnabled: a modal (a prompt, the CE licence
            # reminder, a save dialog) disables the main window. The file
            # association is no way out here: with the IDE blocked it only
            # queues the project as a *second* one of a project group, which
            # then asks "ProjectGroup1 speichern unter" before every build and
            # blocks the IDE for good. Answer the prompts and try once more;
            # failing that, say so plainly.
            log.warning("Open-project dialog failed (%r) — answering pending "
                        "prompts and trying once more.", e)
            if not retry:
                raise BridgeError(
                    f"Could not open {dproj.name} in the IDE: {e!r}. A modal "
                    "dialog the bridge does not know probably blocks the main "
                    "window; close it in the IDE and build again."
                ) from e
            self._answer_pending_prompts(wait=1.0)
            self._load_project_replacing(dproj, retry=False)

    def _close_all(self, timeout: float = 15.0) -> None:
        """Datei -> Alle schliessen before a project switch - the user's rule,
        the clean way: nothing of the old project stays open (no reload
        prompts for its units, no editor tab that swallows Strg+F11, no
        project group).

        The main menu (TActionMainMenuBar) is reachable neither through UIA,
        MSAA nor WM_COMMAND, only by keyboard: Alt+D opens "Datei", "h" is the
        accelerator of "Alle sc_h_liessen" (Delphi 12, German; "c" would be
        "Schliessen", the current file only). Plain key events (keybd_event)
        with 300 ms for the menu to build up - pywinauto's type_keys "%dh" in
        one go left the menu open, and it swallowed Strg+F11 afterwards.
        Save prompts are answered "Nein" (clear_blocking): the bridge builds
        what is on disk; unsaved edits typed into the IDE editor are lost.
        Waits until the title shows no project; else the menu is closed with
        Esc so it cannot eat the next keys.
        """
        import win32api
        import win32con
        import win32gui

        hwnd = self._main.element_info.handle

        def key(vk: int) -> None:
            win32api.keybd_event(vk, 0, 0, 0)
            win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)

        log.info("Before the project switch: Datei -> Alle schliessen")
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        key(ord("D"))
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.3)
        key(ord("H"))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.3)
            self._answer_pending_prompts(wait=0.0)
            title = win32gui.GetWindowText(hwnd)
            if title and " - " not in title:  # "Delphi 12 Community Edition"
                log.info("All closed (title %r)", title)
                return
        log.warning("Alle schliessen: the IDE still shows a project after %.0f s - "
                    "closing the menu", timeout)
        key(win32con.VK_ESCAPE)
        key(win32con.VK_ESCAPE)

    def _answer_pending_prompts(self, wait: float = 1.0) -> bool:
        """Answer the IDE's message boxes standing in the way, e.g. the reload
        prompt for a unit changed on disk ("Alle Ja": reload, so the build
        sees the file as it is on disk) or a "save?" prompt ("Nein").

        `wait`: how long a prompt may take to appear (the IDE checks its files
        when it is activated). True if anything was answered.
        """
        try:
            from delphi_remote.toplevel import clear_blocking, find_blocking
        except Exception as e:                              # noqa: BLE001
            log.debug("toplevel helper unavailable: %r", e)
            return False
        deadline = time.monotonic() + wait
        while True:
            if find_blocking(self.process_id, ("TMessageForm",)):
                answered = clear_blocking(self.process_id, classes=("TMessageForm",))
                for entry in answered:
                    log.info("Answered %s %r with %r", entry["class_name"],
                             entry["title"], entry["button"])
                return bool(answered)
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)

    @staticmethod
    def _launch_via_association(dproj: Path) -> None:
        try:
            os.startfile(str(dproj))
        except OSError as e:
            raise BridgeError(
                f"Could not open {dproj} via file association: {e!r}. "
                f"Verify .dproj is registered to bds.exe in Windows."
            ) from e

    def _poll_for_attach(self, *, deadline_seconds: float) -> IDEHandle:
        deadline = time.monotonic() + deadline_seconds
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return self.attach(timeout=1.0)
            except BridgeError as e:
                last_err = e
                time.sleep(0.5)
        raise BridgeError(
            f"Delphi IDE did not become attachable within {deadline_seconds:.0f}s "
            f"(last error: {last_err!r})"
        )

    def _wait_for_project(self, expected_prefix: str, dproj: Path,
                          timeout: float) -> IDEHandle:
        deadline = time.monotonic() + timeout
        last_title = ""
        last_log = 0.0
        last_class = ""
        while time.monotonic() < deadline:
            # Re-resolve the main window each iteration. The window we attached
            # to during boot may have been the splash, which is now closed.
            main = self._find_main_window_now()
            if main is not None:
                try:
                    info = main.element_info
                    self._main = main
                    last_title = info.name or ""
                    last_class = info.class_name or ""
                except Exception as e:
                    log.debug("Could not read IDE title yet: %r", e)
                    last_title = ""

            if time.monotonic() - last_log > 5.0:
                log.info("Waiting for project %r to load (current title: %r)",
                         dproj.name, last_title)
                last_log = time.monotonic()

            if last_title.startswith(expected_prefix):
                return IDEHandle(
                    process_id=self.process_id,
                    main_window_title=last_title,
                    main_window_class=last_class,
                )
            # a reload prompt of the old project's units (or a "save?") can
            # still come up while the switch is under way and stop it
            self._answer_pending_prompts(wait=0.0)
            time.sleep(0.5)
        raise BridgeError(
            f"Project {dproj.name!r} did not become active within {timeout:.0f}s "
            f"(last title: {last_title!r})"
        )

    def _find_main_window_now(self) -> Any | None:
        """Return the TAppBuilder window if it currently exists, else None.

        Avoids pywinauto's default window-find timeout — fast even when missing.
        """
        if self._app is None:
            return None
        try:
            for handle in self._app.windows():
                try:
                    if handle.class_name() == IDE_MAIN_WINDOW_CLASS:
                        return handle
                except Exception:
                    continue
        except Exception:
            return None
        return None

    def list_top_level_windows(self) -> list:
        if self._app is None:
            raise BridgeError("Not attached — call attach() first")
        return list(self._app.windows())

    def dump_window_tree(self, depth: int = 4, max_children: int = 50) -> dict[str, Any]:
        """Return the full top-level window tree as a JSON-serializable dict."""
        if self._app is None:
            raise BridgeError("Not attached — call attach() first")
        windows = self.list_top_level_windows()
        return {
            "process_id": self._app.process,
            "top_level_window_count": len(windows),
            "top_level_windows": [
                _serialize(w.element_info, depth=depth, max_children=max_children)
                for w in windows
            ],
        }

    def find_first(self, *, class_name: str | None = None,
                   name: str | None = None,
                   max_depth: int | None = None) -> Any | None:
        """Depth-first search the tree for the first element matching name/class.

        ``max_depth`` caps how deep into each top-level window's child tree the
        search descends. Pass 2 for modal dialogs (TMessageForm/TProgressForm),
        which always sit as direct children of TAppBuilder — turns a multi-second
        full-tree DFS into ~50ms. Leave None for unbounded search (Meldungen
        panel etc., which can be docked several levels deep).

        Returns the raw ElementInfo, or None if not found.
        """
        if self._app is None:
            raise BridgeError("Not attached — call attach() first")
        for w in self.list_top_level_windows():
            hit = _dfs_match(w.element_info, class_name=class_name, name=name,
                             max_depth=max_depth, _depth=0)
            if hit is not None:
                return hit
        return None

    def dump_subtree(self, *, class_name: str | None = None, name: str | None = None,
                     depth: int = -1, max_children: int = -1) -> dict[str, Any]:
        """Dump the subtree rooted at the first element matching class_name/name."""
        root = self.find_first(class_name=class_name, name=name)
        if root is None:
            raise BridgeError(
                f"No element found matching class_name={class_name!r} name={name!r}"
            )
        return _serialize(root, depth=depth, max_children=max_children)


def _dfs_match(info: Any, *, class_name: str | None, name: str | None,
               max_depth: int | None = None, _depth: int = 0) -> Any | None:
    try:
        cn = info.class_name or ""
        nm = info.name or ""
    except Exception:
        cn, nm = "", ""
    cls_ok = class_name is None or cn == class_name
    name_ok = name is None or nm == name
    if cls_ok and name_ok and (class_name is not None or name is not None):
        return info
    if max_depth is not None and _depth >= max_depth:
        return None
    try:
        for child in info.children():
            hit = _dfs_match(child, class_name=class_name, name=name,
                             max_depth=max_depth, _depth=_depth + 1)
            if hit is not None:
                return hit
    except Exception:
        pass
    return None


def _serialize(info: Any, depth: int, max_children: int) -> dict[str, Any]:
    """Recursively convert a pywinauto ElementInfo into a JSON-friendly dict."""
    node: dict[str, Any] = {}
    try:
        node["class_name"] = info.class_name or ""
    except Exception as e:
        node["class_name_error"] = repr(e)
    try:
        node["name"] = info.name or ""
    except Exception as e:
        node["name_error"] = repr(e)
    try:
        ctrl = getattr(info, "control_type", None)
        node["control_type"] = str(ctrl) if ctrl else ""
    except Exception as e:
        node["control_type_error"] = repr(e)
    try:
        aid = getattr(info, "automation_id", None)
        node["automation_id"] = aid or ""
    except Exception:
        pass
    try:
        rect = info.rectangle
        node["rectangle"] = [rect.left, rect.top, rect.right, rect.bottom]
    except Exception:
        node["rectangle"] = None

    try:
        children = list(info.children())
    except Exception as e:
        node["children_error"] = repr(e)
        return node

    truncated = 0
    if max_children >= 0 and len(children) > max_children:
        truncated = len(children) - max_children
        children = children[:max_children]

    if depth != 0 and children:
        node["children"] = [
            _serialize(c, depth=depth - 1, max_children=max_children) for c in children
        ]
    elif children:
        node["child_count"] = len(children)

    if truncated:
        node["children_truncated_count"] = truncated
    return node
