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
              target_platform: str | None = None) -> BuildResult:
        from pywinauto import mouse
        from pywinauto.keyboard import send_keys

        if build_config or target_platform:
            log.warning("--config/--platform are accepted but not yet applied to the IDE; "
                        "active selection in IDE is used (Phase 1 limitation).")

        start = time.monotonic()
        project_name = dproj.stem

        title = self.ide.read_main_window_title()
        if not title.startswith(f"{project_name} - "):
            raise BridgeError(
                f"Active IDE project does not match {dproj.name!r} "
                f"(window title: {title!r}). Open the project in the IDE first."
            )

        meldungen = self.ide.find_first(
            name=MELDUNGEN_PANEL_NAME, class_name=MELDUNGEN_PANEL_CLASS,
        )
        if meldungen is None:
            raise BridgeError(
                "Meldungen panel not found. Open it via Ansicht → Meldungen and retry."
            )
        rect = meldungen.rectangle

        try:
            saved_clip = _read_clipboard_text()
        except Exception as e:
            log.warning("Clipboard snapshot failed: %r", e)
            saved_clip = ""

        sentinel = f"__delphi_remote_sentinel_{int(time.time() * 1000)}__"
        _set_clipboard_text(sentinel)

        try:
            self.ide.focus_main_window()
            time.sleep(0.2)
            log.info("Triggering compile (Strg+F9)")
            send_keys("^{F9}")

            final_title = self._wait_for_build_complete(timeout=timeout)
            log.info("Build settled, title=%r", final_title)

            # Watchdog pass: dismiss known post-build dialogs (e.g. TProgressForm).
            # Two passes with a delay catch dialogs that appear slightly late.
            if self.watchdog is not None:
                first_pass = self.watchdog.scan_and_dismiss()
                if first_pass:
                    time.sleep(0.4)
                    self.watchdog.scan_and_dismiss()
                else:
                    time.sleep(0.3)
                    self.watchdog.scan_and_dismiss()

            cx = (rect.left + rect.right) // 2
            cy = (rect.top + rect.bottom) // 2 - 15  # avoid the TTabSet at the bottom edge
            log.info("Focusing Meldungen at (%d, %d)", cx, cy)
            mouse.click(button="left", coords=(cx, cy))
            time.sleep(0.2)
            send_keys("^a")
            time.sleep(0.15)
            send_keys("^c")
            time.sleep(0.3)

            output_text = self._read_until_non_sentinel(sentinel, timeout=3.0)
            if output_text == sentinel:
                log.warning("Clipboard still holds sentinel — Strg+C did not produce data")
            elif output_text and "[dcc32" not in output_text and "Compilieren" not in output_text:
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

    def _wait_for_build_complete(self, *, timeout: float, stable_for: float = 1.5) -> str:
        deadline = time.monotonic() + timeout
        last_title: str | None = None
        last_change = time.monotonic()
        while time.monotonic() < deadline:
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
