"""Find and answer modal dialogs that sit *beside* the IDE main window.

Delphi's modal dialogs are top-level windows owned by TAppBuilder, not children
of it. The UIA-based search in `DialogWatchdog` walks down from the main window
with ``max_depth=2`` and therefore never sees them. The visible symptom is
brutal and misleading: a dialog blocks the main window, every following build
dies with ``ElementNotEnabled``, and ``delphi-remote inspect`` cheerfully
reports ``pending_dialogs: []`` — because it looks in the same wrong place.

This module searches the process's top-level windows through plain Win32 and
answers them by posting BM_CLICK to a button. No UIA, no wrapper objects, so it
also works while the main window is disabled — which is exactly the situation
it exists for.

It is deliberately independent of the rules file: what belongs here are the
dialogs that must be cleared *before* a build can even start, and their answers
are not a matter of configuration. Everything that appears mid-build stays with
the watchdog.
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# Dialog class -> button captions to try, in order. The order matters:
#
#   TReadErrorDlg   The form designer cannot create a component, usually
#                   because its package is not installed in the IDE. During a
#                   port that is the normal case. Answer it with Cancel, which
#                   abandons loading the form and leaves the file alone.
#
#                   NOT "Ignore All", however tempting: that loads the form
#                   *without* the components it could not create and marks it
#                   modified. Delphi saves modified files before it builds, so
#                   the stripped form goes to disk — hundreds of components and
#                   their field declarations gone, from a build that was only
#                   ever meant to read the .pas. This has cost two forms in
#                   this project already; both had to be restored from git.
#
#   TMessageForm    In this context the follow-up errors of the same problem,
#                   one per failing component. On a form with hundreds of them
#                   answering "Yes" just brings the next one, so Cancel comes
#                   first: it aborts loading the form altogether.
#
# CAUTION: answering these dialogs leaves the IDE holding a form that lost
# components. Saving it then writes the damage to disk. Nothing here saves
# anything, but a caller that later triggers a save must know this.
KNOWN: dict[str, list[str]] = {
    "TReadErrorDlg": ["Abbrechen", "Cancel"],
    "TMessageForm": ["Abbrechen", "Cancel", "Alle Ja", "Yes to All", "Ja", "Yes", "OK"],
}

# A TMessageForm asking whether to save something must NOT be cancelled and must
# NOT be confirmed: cancelling aborts whatever the caller was doing, confirming
# writes files the caller never meant to touch. During a port that second case
# is the dangerous one - the IDE would save a form it just loaded without the
# components it could not create. "No" is the only safe answer.
_SAVE_PROMPT_WORDS = ("speichern", "save", "sichern")
_SAVE_PROMPT_BUTTONS = ["Nein", "No"]


def _buttons_for(cls: str, title: str, body: str) -> list[str]:
    """Answers to try for this dialog, most appropriate first."""
    if cls == "TMessageForm":
        haystack = (title + " " + body).lower()
        if any(w in haystack for w in _SAVE_PROMPT_WORDS):
            return _SAVE_PROMPT_BUTTONS
    return KNOWN[cls]


def _dialog_text(hwnd_dialog: int) -> str:
    """Concatenated captions of the dialog's static children."""
    _, win32gui, _ = _win32()
    parts: list[str] = []

    def visit(hwnd: int, _: Any) -> None:
        try:
            if win32gui.GetClassName(hwnd) in ("TLabel", "Static", "TStaticText"):
                parts.append(win32gui.GetWindowText(hwnd))
        except Exception:                                   # noqa: BLE001
            return

    try:
        win32gui.EnumChildWindows(hwnd_dialog, visit, None)
    except Exception:                                       # noqa: BLE001
        pass
    return " ".join(parts)

_MAX_PASSES = 40


def _win32():
    import win32con
    import win32gui
    import win32process
    return win32con, win32gui, win32process


def find_blocking(pid: int) -> list[tuple[int, str, str]]:
    """[(handle, class_name, title)] of visible known dialogs owned by `pid`."""
    _, win32gui, win32process = _win32()
    found: list[tuple[int, str, str]] = []

    def visit(hwnd: int, _: Any) -> None:
        try:
            if win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
                return
            if not win32gui.IsWindowVisible(hwnd):
                return
            cls = win32gui.GetClassName(hwnd)
            if cls in KNOWN:
                found.append((hwnd, cls, win32gui.GetWindowText(hwnd)))
        except Exception:                                   # noqa: BLE001
            return

    win32gui.EnumWindows(visit, None)
    return found


def _click(hwnd_dialog: int, captions: list[str]) -> str | None:
    """Post BM_CLICK to the first button whose caption matches. Returns it."""
    win32con, win32gui, _ = _win32()
    buttons: list[tuple[int, str]] = []

    def visit(hwnd: int, _: Any) -> None:
        try:
            if win32gui.GetClassName(hwnd) in ("TButton", "Button"):
                buttons.append((hwnd, win32gui.GetWindowText(hwnd).replace("&", "").strip()))
        except Exception:                                   # noqa: BLE001
            return

    win32gui.EnumChildWindows(hwnd_dialog, visit, None)
    for wanted in captions:
        for hwnd, caption in buttons:
            if caption.lower() == wanted.lower():
                win32gui.SendMessage(hwnd, win32con.BM_CLICK, 0, 0)
                return caption
    log.warning("Dialog %r offers none of %r, only %r",
                win32gui.GetWindowText(hwnd_dialog), captions,
                [c for _, c in buttons])
    return None


def clear_blocking(pid: int, settle_seconds: float = 0.5) -> list[dict[str, str]]:
    """Answer blocking dialogs until none are left. Returns what was answered.

    Loops because these dialogs come in cascades — one per component the
    designer could not create. The pass limit is a backstop against a dialog
    that reappears no matter what we click; hitting it is logged, not raised,
    because a build attempt against a stuck IDE still gives the caller a more
    useful error than an exception from here.
    """
    answered: list[dict[str, str]] = []
    for _ in range(_MAX_PASSES):
        blocking = find_blocking(pid)
        if not blocking:
            return answered
        progress = False
        for hwnd, cls, title in blocking:
            caption = _click(hwnd, _buttons_for(cls, title, _dialog_text(hwnd)))
            if caption is None:
                continue
            progress = True
            answered.append({"class_name": cls, "title": title, "button": caption})
            log.info("Cleared %s %r with %r", cls, title, caption)
            time.sleep(settle_seconds)
        if not progress:
            break
    log.warning("Still blocked after %d passes; %d dialog(s) answered",
                _MAX_PASSES, len(answered))
    return answered
