"""Build orchestrator — drives the Delphi IDE to compile a project.

Strategy:
  1. Verify the right project is open (compare title-bar prefix to dproj stem).
  2. Plant a sentinel value in the clipboard.
  3. Focus the IDE main window, send Strg+F9 (Compile).
  4. Poll the title bar; the IDE appends "[Erzeugt]" once a build settles.
  5. Click into the Meldungen panel, send Strg+A then Strg+C to copy all rows
     (TVirtualStringTree-based controls don't expose their data via UIA).
  6. Read clipboard, parse `[dcc32 ...]` lines into structured diagnostics.
  7. Restore the original clipboard contents.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from delphi_remote.ide_client import BridgeError, DelphiIDE
from delphi_remote.watchdog import DialogEvent, DialogWatchdog

log = logging.getLogger(__name__)

BUILD_DONE_MARKER = "[Erzeugt]"
MELDUNGEN_PANEL_CLASS = "TMessageViewForm"
MELDUNGEN_PANEL_NAME = "Meldungen"

# Severity translation: the IDE prints localized severity tags; we map both
# German (CE default for the user) and English back to a stable normalized form.
# Keys are lowercased — see parse_diagnostics() for case-insensitive lookup.
_SEVERITY_MAP: dict[str, str] = {
    "fehler": "error",
    "error": "error",
    "fataler fehler": "fatal_error",
    "schwerwiegender fehler": "fatal_error",
    "fatal error": "fatal_error",
    "warnung": "warning",
    "warning": "warning",
    "hinweis": "hint",
    "hint": "hint",
}

# Match: [dcc32 <severity>] <file>(<line>): <code> <message>
DIAGNOSTIC_RE = re.compile(
    r"^\[dcc32\s+(?P<sev>[^\]]+?)\]\s+"
    r"(?P<file>[^()]+?)\((?P<line>\d+)\):\s+"
    r"(?P<code>[A-Z]\d+)\s+"
    r"(?P<message>.*)$"
)


@dataclass
class Diagnostic:
    severity: str
    file: str
    line: int
    code: str
    message: str


@dataclass
class BuildResult:
    status: str
    duration_seconds: float
    diagnostics: list[Diagnostic] = field(default_factory=list)
    final_title: str = ""
    raw_output_chars: int = 0
    stats: dict[str, int] = field(default_factory=lambda: {
        "errors": 0, "warnings": 0, "hints": 0, "fatal_errors": 0, "other": 0,
    })
    dialogs_dismissed: list[DialogEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "duration_seconds": round(self.duration_seconds, 2),
            "final_title": self.final_title,
            "raw_output_chars": self.raw_output_chars,
            "diagnostics": [asdict(d) for d in self.diagnostics],
            "stats": self.stats,
            "dialogs_dismissed": [e.to_dict() for e in self.dialogs_dismissed],
        }


def parse_diagnostics(text: str) -> list[Diagnostic]:
    """Extract structured diagnostics from raw Meldungen-panel text."""
    out: list[Diagnostic] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith("[dcc32"):
            continue
        match = DIAGNOSTIC_RE.match(stripped)
        if match is None:
            log.debug("Unmatched dcc32 line: %r", stripped)
            continue
        severity_label = match.group("sev").strip()
        normalized = _SEVERITY_MAP.get(severity_label.lower(), severity_label.lower())
        out.append(Diagnostic(
            severity=normalized,
            file=match.group("file").strip(),
            line=int(match.group("line")),
            code=match.group("code"),
            message=match.group("message").strip(),
        ))
    return out


def _count_severities(diags: list[Diagnostic]) -> dict[str, int]:
    counts = {"errors": 0, "warnings": 0, "hints": 0, "fatal_errors": 0, "other": 0}
    for d in diags:
        if d.severity == "error":
            counts["errors"] += 1
        elif d.severity == "warning":
            counts["warnings"] += 1
        elif d.severity == "hint":
            counts["hints"] += 1
        elif d.severity == "fatal_error":
            counts["fatal_errors"] += 1
        else:
            counts["other"] += 1
    return counts


# ── Foreground window save/restore ──────────────────────────────────────────

def _save_foreground_hwnd() -> int | None:
    """Capture the user's currently-foreground window so we can restore it later."""
    try:
        import win32gui
        hwnd = win32gui.GetForegroundWindow()
        return int(hwnd) if hwnd else None
    except Exception as e:
        log.debug("Could not read foreground hwnd: %r", e)
        return None


def _restore_foreground_hwnd(hwnd: int | None) -> None:
    """Best-effort restore of the user's prior foreground window after a build.

    Windows can refuse SetForegroundWindow due to focus-stealing prevention;
    if so we silently skip and let the IDE keep focus.
    """
    if not hwnd:
        return
    try:
        import win32gui
        if not win32gui.IsWindow(hwnd):
            return
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:
        log.debug("Could not restore foreground hwnd %s: %r", hwnd, e)


# ── Clipboard helpers ───────────────────────────────────────────────────────

def _open_clipboard_with_retry(retries: int = 10, delay: float = 0.05) -> None:
    import win32clipboard
    last_err: Exception | None = None
    for _ in range(retries):
        try:
            win32clipboard.OpenClipboard()
            return
        except Exception as e:
            last_err = e
            time.sleep(delay)
    raise BridgeError(f"Could not open clipboard after {retries} attempts: {last_err!r}")


def _read_clipboard_text() -> str:
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


def _set_clipboard_text(text: str) -> None:
    import win32clipboard
    _open_clipboard_with_retry()
    try:
        win32clipboard.EmptyClipboard()
        if text:
            win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


# ── Builder ─────────────────────────────────────────────────────────────────

class Builder:
    def __init__(self, ide: DelphiIDE, watchdog: DialogWatchdog | None = None) -> None:
        self.ide = ide
        self.watchdog = watchdog

    def build(self, dproj: Path, *, timeout: float = 180.0,
              build_config: str | None = None,
              target_platform: str | None = None,
              auto_start: bool = True) -> BuildResult:
        if build_config or target_platform:
            log.warning("--config/--platform are accepted but not yet applied to the IDE; "
                        "active selection in IDE is used (Phase 1 limitation).")

        start = time.monotonic()
        project_name = dproj.stem

        if auto_start:
            self.ide.ensure_project_loaded(dproj)

        # Defensive pre-flight: bring the IDE genuinely to the foreground via
        # Win32 (not just pywinauto's softer set_foreground). This makes
        # Delphi run its external-file-changed check NOW, so any "Neu laden?"
        # prompt for files edited between builds opens before we send Shift+F9
        # rather than mid-build. Without this, type_keys("+{F9}") hits a
        # disabled main window with ElementNotEnabled, or the prompt fires
        # after the build started and the Linker aborts.
        was_minimized = self.ide.force_to_foreground(settle_ms=300)
        self._drain_pre_build_modals(passes=4)

        title = self.ide.read_main_window_title()
        if not title.startswith(f"{project_name} - "):
            raise BridgeError(
                f"Active IDE project does not match {dproj.name!r} "
                f"(window title: {title!r}). Open the project in the IDE first, "
                f"or omit --no-auto-start to let the bridge launch it."
            )

        original_foreground = _save_foreground_hwnd()

        try:
            saved_clip = _read_clipboard_text()
        except Exception as e:
            log.warning("Clipboard snapshot failed: %r", e)
            saved_clip = ""

        sentinel = f"__delphi_remote_sentinel_{int(time.time() * 1000)}__"
        _set_clipboard_text(sentinel)

        try:
            # Make sure the Meldungen panel is visible before we trigger the
            # build. Without it we can't capture diagnostics. Toggling via
            # Alt+Umschalt+M is a no-op when the panel is already there, so
            # we check first and only send the shortcut when the panel is
            # missing — otherwise we'd hide a panel the user wanted open.
            self._ensure_meldungen_visible()

            # Use Build (Umschalt+F9), not Compile (Strg+F9). Compile is a
            # no-op when the .exe is newer than all sources — title flips to
            # [Erzeugt] but no Meldungen output is produced. Build always
            # recompiles, which guarantees we get diagnostics.
            log.info("Triggering build (Umschalt+F9)")
            self.ide.main_window.type_keys("+{F9}", set_foreground=True)

            # Wait for the build to settle, scanning the watchdog periodically
            # so dialogs that appear mid-build (license popup, etc.) get
            # dismissed without blocking us. The in-loop watchdog runs every
            # second, so a redundant post-loop scan would just add latency —
            # if a late dialog blocks the Meldungen copy, the retry path
            # below will scan again.
            final_title = self._wait_for_build_complete_with_watchdog(timeout=timeout)
            log.info("Build settled, title=%r", final_title)

            # Locate Meldungen *after* the compile. On a cold-boot IDE the
            # panel is auto-hidden; the IDE shows it once compile output exists.
            meldungen = self.ide.find_first(
                name=MELDUNGEN_PANEL_NAME, class_name=MELDUNGEN_PANEL_CLASS,
            )
            if meldungen is None:
                raise BridgeError(
                    "Meldungen panel not found even after Build. "
                    "One-time IDE setup needed: open the panel manually via "
                    "Ansicht → Werkzeugfenster → Meldungen (or Ansicht → Meldungen). "
                    "The IDE will remember it for future sessions."
                )

            output_text = self._extract_meldungen_with_retry(
                meldungen=meldungen, sentinel=sentinel, max_attempts=3,
            )

            if output_text == sentinel or not output_text:
                raise BridgeError(
                    "Could not read Meldungen panel after multiple attempts. "
                    "If this happens repeatedly, leave the IDE foreground during "
                    "the build, or check for a modal dialog the watchdog rules "
                    "don't yet cover."
                )

            if "[dcc32" not in output_text and "Compilieren" not in output_text:
                log.warning("Clipboard content does not look like Meldungen output "
                            "(%d chars, first 80=%r)", len(output_text), output_text[:80])

            diagnostics = parse_diagnostics(output_text)
            stats = _count_severities(diagnostics)

            if stats["errors"] or stats["fatal_errors"]:
                status = "errors"
            elif BUILD_DONE_MARKER in final_title:
                status = "ok"
            else:
                status = "aborted"

            return BuildResult(
                status=status,
                duration_seconds=time.monotonic() - start,
                diagnostics=diagnostics,
                final_title=final_title,
                raw_output_chars=len(output_text),
                stats=stats,
                dialogs_dismissed=list(self.watchdog.events) if self.watchdog else [],
            )
        finally:
            try:
                _set_clipboard_text(saved_clip)
            except Exception as e:
                log.warning("Could not restore clipboard: %r", e)
            # Put the IDE back the way we found it: re-minimize before
            # restoring the user's foreground window so the IDE doesn't
            # flash visible at the very end.
            if was_minimized:
                try:
                    self.ide.minimize_main_window()
                except Exception as e:
                    log.warning("Could not re-minimize IDE: %r", e)
            _restore_foreground_hwnd(original_foreground)

    def _ensure_meldungen_visible(self) -> None:
        """Open the Meldungen panel via Alt+Umschalt+M if it isn't already
        in the IDE's UIA tree. Idempotent: skips the keystroke when the
        panel is present, since Alt+Umschalt+M is a toggle.
        """
        existing = self.ide.find_first(
            name=MELDUNGEN_PANEL_NAME, class_name=MELDUNGEN_PANEL_CLASS,
        )
        if existing is not None:
            return
        log.info("Meldungen panel not visible — sending Alt+Umschalt+M")
        try:
            self.ide.main_window.type_keys("%+m", set_foreground=True)
        except Exception as e:
            log.warning("Could not send Alt+Umschalt+M to open Meldungen: %r", e)
            return
        time.sleep(0.4)

    def _dismiss_pending_dialogs(self) -> int:
        """Run a single watchdog pass; safe to call any time. Returns count dismissed."""
        if self.watchdog is None:
            return 0
        try:
            return self.watchdog.scan_and_dismiss()
        except Exception as e:
            log.warning("Watchdog scan failed: %r", e)
            return 0

    def _drain_pre_build_modals(self, *, passes: int = 4) -> int:
        """Loop the watchdog before triggering the build to clear modals
        that appeared from `force_to_foreground` (typically "Neu laden?"
        per modified file). Stops as soon as a pass dismisses nothing.
        Returns the total number of dialogs dismissed.
        """
        if self.watchdog is None:
            return 0
        total = 0
        for _ in range(max(1, passes)):
            n = self._dismiss_pending_dialogs()
            if n == 0:
                break
            total += n
            time.sleep(0.2)
        if total:
            log.info("Pre-build watchdog dismissed %d dialog(s)", total)
        return total

    def _wait_for_build_complete_with_watchdog(self, *, timeout: float,
                                               stable_for: float = 0.6) -> str:
        """Poll the title bar until the build settles; scan watchdog rules
        every poll so dialogs appearing mid-build don't block us.
        """
        deadline = time.monotonic() + timeout
        last_title: str | None = None
        last_change = time.monotonic()
        last_watchdog = 0.0
        while time.monotonic() < deadline:
            # Periodic watchdog scan — dialogs may appear at any point during
            # build (e.g. CE license reminder, "out of date" prompts).
            if time.monotonic() - last_watchdog > 1.0:
                self._dismiss_pending_dialogs()
                last_watchdog = time.monotonic()
            try:
                current = self.ide.read_main_window_title()
            except BridgeError:
                current = last_title or ""
            if current != last_title:
                last_title = current
                last_change = time.monotonic()
            else:
                idle = time.monotonic() - last_change
                if BUILD_DONE_MARKER in current and idle >= stable_for:
                    return current
                if idle >= stable_for * 4:
                    return current
            time.sleep(0.25)
        raise BridgeError(f"Build did not complete within {timeout:.0f}s")

    def _extract_meldungen_with_retry(self, *, meldungen: Any, sentinel: str,
                                      max_attempts: int = 3) -> str:
        """Extract Meldungen content via clipboard, retrying if a copy fails.

        Each attempt:
          1. Re-resolve the Meldungen panel (it may have moved/scrolled).
          2. Try UIA-based focus + type_keys; fall back to coord-click.
          3. Read clipboard until non-sentinel or short timeout.

        Returns the captured text, or the sentinel/empty if all attempts fail.
        """
        from pywinauto import mouse
        from pywinauto.keyboard import send_keys

        text = ""
        target = meldungen
        for attempt in range(max_attempts):
            if attempt > 0:
                log.info("Clipboard still empty — retrying focus + copy "
                         "(attempt %d/%d)", attempt + 1, max_attempts)
                # A late-appearing dialog could be blocking us; sweep once.
                # Also re-resolve the panel — it may have moved or its active
                # tab may have changed since the first attempt.
                self._dismiss_pending_dialogs()
                target = self.ide.find_first(
                    name=MELDUNGEN_PANEL_NAME, class_name=MELDUNGEN_PANEL_CLASS,
                ) or meldungen
                _set_clipboard_text(sentinel)

            if not self._copy_meldungen_via_uia(target):
                self.ide.focus_main_window()
                time.sleep(0.2)
                try:
                    rect = target.rectangle
                except Exception as e:
                    log.warning("Could not read Meldungen rect on attempt %d: %r",
                                attempt + 1, e)
                    continue
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2 - 15
                log.info("Falling back to coord click on Meldungen at (%d, %d)",
                         cx, cy)
                mouse.click(button="left", coords=(cx, cy))
                time.sleep(0.2)
                send_keys("^a^c", pause=0.05)
                time.sleep(0.3)

            text = self._read_until_non_sentinel(sentinel, timeout=2.0)
            if text and text != sentinel:
                return text

        return text  # may still be sentinel/empty — caller decides

    def _copy_meldungen_via_uia(self, meldungen: Any) -> bool:
        """Focus the Meldungen tree and run Strg+A / Strg+C through pywinauto.

        Uses the wrapper's bound `type_keys(set_foreground=True)` — pywinauto
        handles the AttachThreadInput dance so this works even if the IDE
        wasn't the foreground window when the build started.

        Returns True if Strg+A and Strg+C were both dispatched. Caller is
        expected to fall back to coord-based clicking if this returns False.
        """
        from pywinauto.controls.uiawrapper import UIAWrapper

        try:
            children = list(meldungen.children())
        except Exception as e:
            log.debug("Could not enumerate Meldungen children: %r", e)
            return False

        # The panel hosts two TBetterHintWindowVirtualDrawTree controls (one
        # per tab); only the active one accepts focus. Try each in turn.
        candidates: list[Any] = []
        for child in children:
            try:
                cls = child.class_name or ""
            except Exception:
                continue
            if "VirtualDrawTree" in cls or "VirtualStringTree" in cls:
                candidates.append(child)
        if not candidates:
            log.debug("No tree control found inside Meldungen — will coord-click")
            return False

        for tree in candidates:
            cls = getattr(tree, "class_name", "?") or "?"
            try:
                wrapper = UIAWrapper(tree)
                wrapper.set_focus()
                log.info("Focused Meldungen tree (%s); sending Strg+A / Strg+C", cls)
                # Single combined call — keeps Strg+A and Strg+C atomic so
                # a focus loss between them can't split the sequence.
                wrapper.type_keys("^a^c", set_foreground=True, pause=0.05)
                time.sleep(0.4)
                return True
            except Exception as e:
                log.debug("type_keys on %s failed: %r", cls, e)
                continue
        return False

    @staticmethod
    def _read_until_non_sentinel(sentinel: str, *, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        text = ""
        while time.monotonic() < deadline:
            try:
                text = _read_clipboard_text()
            except BridgeError:
                text = ""
            if text and text != sentinel:
                return text
            time.sleep(0.1)
        return text
