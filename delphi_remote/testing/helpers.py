"""Shared low-level helpers for the UI testing DSL.

Each helper here is a paper-thin wrapper around Win32 / pywinauto whose only
job is to capture a behavior the bridge has had to re-discover in multiple
ad-hoc tests (DPI quirks, clipboard races, WM_CLOSE vs hard kill).
"""
from __future__ import annotations

import logging
import os
import signal
import time
from typing import Any

log = logging.getLogger(__name__)


# ── DPI awareness ───────────────────────────────────────────────────────────

def enable_dpi_awareness() -> str:
    """Make the *current process* Per-Monitor V2 DPI aware.

    Without this, ``GetClientRect`` / ``ClientToScreen`` / mouse clicks land
    at the wrong physical pixels on scaled displays (125% / 150% / 175%) —
    everything looks fine on a 100% screen and silently misses targets by
    20-50 px on a real laptop. Returns the awareness level that was actually
    applied (``"per_monitor_v2"``, ``"system"`` or ``"none"``).
    """
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return "per_monitor_v2"
    except Exception:
        pass
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "none"


# ── Clipboard with retries ──────────────────────────────────────────────────

def _open_clipboard_with_retry(retries: int = 10, delay: float = 0.05) -> None:
    import win32clipboard

    last: Exception | None = None
    for _ in range(retries):
        try:
            win32clipboard.OpenClipboard()
            return
        except Exception as e:
            last = e
            time.sleep(delay)
    raise RuntimeError(f"Could not open clipboard after {retries} attempts: {last!r}")


def set_clipboard_text(text: str) -> None:
    """Place ``text`` on the clipboard, retrying around contention.

    The Windows clipboard is a shared resource — any other process can hold
    it for tens of milliseconds. Tests that race the clipboard against the
    IDE or the system tray see sporadic ``OSError(0x8001010E)`` without this
    retry loop.
    """
    import win32clipboard

    _open_clipboard_with_retry()
    try:
        win32clipboard.EmptyClipboard()
        if text:
            win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def get_clipboard_text() -> str:
    """Return current clipboard text, or empty string if it isn't text."""
    import win32clipboard

    _open_clipboard_with_retry()
    try:
        try:
            data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            return data or ""
        except (TypeError, OSError):
            return ""
    finally:
        win32clipboard.CloseClipboard()


# ── Graceful window close ───────────────────────────────────────────────────

def graceful_close(window: Any, pid: int, *, timeout: float = 5.0) -> bool:
    """Close a top-level window via WM_CLOSE, terminate the process if needed.

    Sends WM_CLOSE first so VCL forms run their ``OnDestroy`` / ``OnClose``
    handlers (writing autosave files, releasing exclusive handles). Only
    falls back to SIGTERM if the process refuses to exit within ``timeout``.

    Returns True if the process exited cleanly, False if it had to be killed.
    """
    try:
        window.close()  # pywinauto wraps WM_CLOSE
    except Exception as e:
        log.debug("WindowSpecification.close() raised %r — sending raw WM_CLOSE", e)
        try:
            import win32con
            import win32gui

            hwnd = window.element_info.handle
            if hwnd:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception as inner:
            log.warning("Raw WM_CLOSE also failed for pid=%s: %r", pid, inner)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OSError):
            return True
        time.sleep(0.1)

    log.warning("Process pid=%s did not exit within %.1fs — sending SIGTERM",
                pid, timeout)
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return True
    return False
