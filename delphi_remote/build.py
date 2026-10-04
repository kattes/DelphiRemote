"""Build orchestrator — drives the Delphi IDE to compile a project.

Strategy:
  1. Verify the right project is open (compare title-bar prefix to dproj stem).
  2. Terminate any previously-built EXE that's still running (linker can't
     overwrite a locked file — without this the build silently aborts).
  3. Plant a sentinel value in the clipboard.
  4. Focus the IDE main window, send Umschalt+F9 (Build).
  5. Poll the title bar; the IDE appends "[Erzeugt]" once a build settles.
  6. Click into the Meldungen panel, send Strg+A then Strg+C to copy all rows
     (TVirtualStringTree-based controls don't expose their data via UIA).
  7. Read clipboard, parse `[dcc32 ...]` lines into structured diagnostics.
  8. Restore the original clipboard contents.
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
    exe_terminated: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "duration_seconds": round(self.duration_seconds, 2),
            "final_title": self.final_title,
            "raw_output_chars": self.raw_output_chars,
            "diagnostics": [asdict(d) for d in self.diagnostics],
            "stats": self.stats,
            "dialogs_dismissed": [e.to_dict() for e in self.dialogs_dismissed],
            "exe_terminated": self.exe_terminated,
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


# ── Pre-build EXE termination ───────────────────────────────────────────────

# The Delphi linker writes directly to the output EXE. If that EXE is still
# running (because the previous test/run hasn't been closed) the linker fails
# silently — the build ends as "aborted" with empty diagnostics, which looks
# like a phantom failure. Terminate the previous instance before triggering
# the new build.

# Hard guard: never close these process names even if a dproj somehow shares
# the stem. bds.exe must never be killed (it would lose the IDE session); the
# others are common system EXEs that share a name with a project would be a
# very unlucky collision.
_NEVER_KILL_EXE_NAMES = frozenset({
    "bds.exe", "explorer.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
    "python.exe", "pythonw.exe", "code.exe", "claude.exe",
})


def _enumerate_windows_for_exe(exe_basename_lower: str) -> list[tuple[int, int]]:
    """Find top-level windows belonging to processes named `exe_basename_lower`.

    Returns a list of (hwnd, pid) tuples. Empty list if nothing matches.
    Best-effort: skips processes we can't query (elevated, exited).
    """
    import win32api
    import win32con
    import win32gui
    import win32process

    matches: list[tuple[int, int]] = []

    def visit(hwnd: int, _: object) -> bool:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if not pid:
                return True
            handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_LIMITED_INFORMATION | win32con.PROCESS_TERMINATE,
                False, pid,
            )
            try:
                exe_path = win32process.GetModuleFileNameEx(handle, 0)
            finally:
                win32api.CloseHandle(handle)
            basename = exe_path.rsplit("\\", 1)[-1].lower()
            if basename == exe_basename_lower:
                matches.append((hwnd, pid))
        except Exception as e:
            log.debug("EnumWindows: skip hwnd=%s: %r", hwnd, e)
        return True

    win32gui.EnumWindows(visit, None)
    return matches


def terminate_previous_exe(dproj: Path, *, wait_seconds: float = 3.0) -> dict[str, Any]:
    """Close any running instance of the EXE this dproj would produce.

    Order of operations per match:
      1. SendMessage WM_CLOSE (graceful — triggers FormDestroy / autosave).
      2. Wait up to `wait_seconds` for the process to exit.
      3. If still alive, TerminateProcess as a hard fallback.

    Returns a dict suitable for inclusion in the build JSON output:
      {"attempted": int, "graceful": int, "forced": int, "exe": "<basename>"}
    """
    exe_basename = f"{dproj.stem}.exe"
    exe_basename_lower = exe_basename.lower()

    if exe_basename_lower in _NEVER_KILL_EXE_NAMES:
        log.warning("Refusing to terminate %r — name is on the never-kill list",
                    exe_basename)
        return {"attempted": 0, "graceful": 0, "forced": 0,
                "exe": exe_basename, "skipped_reason": "never_kill_list"}

    import win32api
    import win32con
    import win32gui
    import win32process

    matches = _enumerate_windows_for_exe(exe_basename_lower)
    if not matches:
        return {"attempted": 0, "graceful": 0, "forced": 0, "exe": exe_basename}

    # Dedupe by pid — a single process can own several top-level windows.
    pids_to_hwnd: dict[int, int] = {}
    for hwnd, pid in matches:
        pids_to_hwnd.setdefault(pid, hwnd)

    log.info("Found %d running instance(s) of %s — terminating",
             len(pids_to_hwnd), exe_basename)

    graceful = 0
    forced = 0
    for pid, hwnd in pids_to_hwnd.items():
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception as e:
            log.warning("WM_CLOSE PostMessage failed for pid=%s: %r", pid, e)

        deadline = time.monotonic() + wait_seconds
        exited = False
        while time.monotonic() < deadline:
            try:
                handle = win32api.OpenProcess(
                    win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
                )
            except Exception:
                exited = True
                break
            try:
                code = win32process.GetExitCodeProcess(handle)
                if code != win32con.STILL_ACTIVE:
                    exited = True
                    break
            except Exception:
                exited = True
                break
            finally:
                try:
                    win32api.CloseHandle(handle)
                except Exception:
                    pass
            time.sleep(0.1)

        if exited:
            graceful += 1
            continue

        # Hard fallback — process refused to close in time.
        try:
            handle = win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, pid)
        except Exception as e:
            log.warning("Could not open pid=%s for forced terminate: %r", pid, e)
            continue
        try:
            win32api.TerminateProcess(handle, 1)
            forced += 1
        except Exception as e:
            log.warning("TerminateProcess on pid=%s failed: %r", pid, e)
        finally:
            try:
                win32api.CloseHandle(handle)
            except Exception:
                pass

    return {
        "attempted": len(pids_to_hwnd),
        "graceful": graceful,
        "forced": forced,
        "exe": exe_basename,
    }


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
              auto_start: bool = True,
              kill_running_exe: bool = True) -> BuildResult:
        if build_config or target_platform:
            log.warning("--config/--platform are accepted but not yet applied to the IDE; "
                        "active selection in IDE is used (Phase 1 limitation).")

        start = time.monotonic()
        project_name = dproj.stem

        if auto_start:
            self.ide.ensure_project_loaded(dproj)

        # Vorbedingung, bevor irgendetwas angefasst wird: ohne das
        # Meldungen-Fenster gibt es keine Diagnostik, und ein Bau, dessen
        # Ergebnis niemand lesen kann, ist umsonst. Frueher stand die
        # Pruefung erst kurz vor Umschalt+F9 und ohne Abbruch - die Bridge
        # baute dann trotzdem und meldete minutenlang nichts. Hier kostet
        # sie den Bruchteil einer Sekunde und laesst die IDE in Ruhe.
        self._ensure_meldungen_visible()

        # Close any still-running instance of the EXE the build is about to
        # overwrite. The Delphi linker writes the file in place and fails
        # silently when it's locked — the resulting build looks "aborted"
        # with empty diagnostics, which is hard to debug. Doing this here
        # avoids that whole class of phantom failures.
        if kill_running_exe:
            try:
                exe_termination = terminate_previous_exe(dproj)
            except Exception as e:
                log.warning("Pre-build EXE termination failed: %r", e)
                exe_termination = {"attempted": 0, "graceful": 0, "forced": 0,
                                   "exe": f"{project_name}.exe",
                                   "error": repr(e)}
        else:
            exe_termination = {"attempted": 0, "graceful": 0, "forced": 0,
                               "exe": f"{project_name}.exe",
                               "skipped_reason": "disabled_by_caller"}

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
            # Clear anything that blocks the main window before touching it.
            # The watchdog cannot do this: it only runs once the build is under
            # way, and it searches below the main window, where modal dialogs
            # are not. A dialog left standing here makes every keystroke fail
            # with ElementNotEnabled - a message that says nothing about the
            # cause. During a port these dialogs are the norm, not the
            # exception: every form still referring to an uninstalled
            # component produces one when the designer opens it, and the IDE
            # opens exactly the unit a failed build reported an error in.
            self._clear_blocking_dialogs()

            # Force the IDE to re-check its open units against disk. Delphi
            # only runs that check when its main window *regains* activation.
            # The bridge already keeps the IDE in the foreground, so on a
            # second build in a row there is no activation edge, no
            # "Informationen — Alle Ja" prompt, and the IDE happily compiles
            # its stale editor buffer. Since the IDE opens the offending unit
            # after every failed build, that is exactly the file the caller
            # just edited — the loop silently builds the previous source.
            #
            # Minimizing and restoring creates the missing activation edge.
            # It has to happen here, inside the build, because the watchdog
            # that answers the reload prompt is only armed for this window.
            self._nudge_activation()

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
            #
            # Mehrfach fragen: der Bau baut die Oberflaeche der IDE um, und
            # unmittelbar danach liefert die UIA-Abfrage schon einmal nichts,
            # obwohl das Fenster da ist. Vor dem Bau ist es nachweislich
            # gefunden worden - sonst waere schon dort abgebrochen worden -,
            # also ist ein einzelner Fehlversuch hier kein Beleg fuer sein
            # Fehlen. Ohne die Wiederholung geht die Diagnostik eines
            # gelungenen Baus verloren.
            meldungen = None
            for _ in range(5):
                meldungen = self.ide.find_first(
                    name=MELDUNGEN_PANEL_NAME, class_name=MELDUNGEN_PANEL_CLASS,
                )
                if meldungen is not None:
                    break
                time.sleep(0.5)
            if meldungen is None:
                raise BridgeError(
                    "Meldungen panel vanished during the build. The build "
                    "itself may well have succeeded - check the EXE timestamp. "
                    "If this repeats, leave the IDE in the foreground during "
                    "the build."
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
                exe_terminated=exe_termination,
            )
        finally:
            try:
                _set_clipboard_text(saved_clip)
            except Exception as e:
                log.warning("Could not restore clipboard: %r", e)
            # Leave the IDE usable: the progress window stays open after a
            # build when its "close automatically" box is unticked. It is a
            # top-level window, so the watchdog (which searches below the main
            # window) never sees it; answer it here, by its OK only.
            self._close_progress_window()
            # Put the IDE back the way we found it: re-minimize before
            # restoring the user's foreground window so the IDE doesn't
            # flash visible at the very end.
            if was_minimized:
                try:
                    self.ide.minimize_main_window()
                except Exception as e:
                    log.warning("Could not re-minimize IDE: %r", e)
            _restore_foreground_hwnd(original_foreground)

    def _clear_blocking_dialogs(self) -> int:
        """Answer modal dialogs that own the main window. Returns how many."""
        try:
            from delphi_remote.toplevel import clear_blocking
            answered = clear_blocking(self.ide.process_id)
        except Exception as e:                              # noqa: BLE001
            log.warning("Could not scan for blocking dialogs: %r", e)
            return 0
        for entry in answered:
            log.info("Pre-build: cleared %s %r with %r",
                     entry["class_name"], entry["title"], entry["button"])
            if self.watchdog is not None:
                self.watchdog.events.append(DialogEvent(
                    rule_name="Pre-build clearing",
                    class_name=entry["class_name"],
                    name=entry["title"],
                    button_clicked=entry["button"],
                    timestamp=time.time(),
                ))
        return len(answered)

    def _close_progress_window(self) -> None:
        """Answer the build progress window ("Erzeugen") with OK, if it is open."""
        try:
            from delphi_remote.toplevel import clear_blocking
            answered = clear_blocking(self.ide.process_id, settle_seconds=0.2,
                                      classes=("TProgressForm",))
        except Exception as e:                              # noqa: BLE001
            log.warning("Could not close the progress window: %r", e)
            return
        for entry in answered:
            log.info("After the build: closed %r with %r", entry["title"], entry["button"])

    def _nudge_activation(self, settle_seconds: float = 0.8) -> None:
        """Minimize and restore the IDE so it re-checks open units against disk.

        Delphi compares timestamps of open editor buffers only when its main
        window regains activation. Without an activation edge it keeps
        compiling the buffer it loaded earlier, so an edit made from outside
        the IDE never reaches the compiler — no error, no prompt, just the
        previous source built again.

        Answering the prompt this provokes is part of the job. Raising it is
        the whole point of the nudge, and it opens *after* every sweep the
        caller ran beforehand - as a top-level modal that disables the main
        window, so the next keystroke would die with ElementNotEnabled and a
        message naming nothing that caused it.

        Any failure here is deliberately non-fatal: a build against a possibly
        stale buffer is still more useful than no build at all, and the caller
        gets the diagnostics either way.
        """
        try:
            self.ide.minimize_main_window()
            time.sleep(settle_seconds)
            self.ide.force_to_foreground(settle_ms=int(settle_seconds * 1000))
            time.sleep(settle_seconds)
        except Exception as e:                              # noqa: BLE001
            log.warning("Could not nudge IDE activation: %r", e)
        self._dismiss_pending_dialogs()

    def _ensure_meldungen_visible(self, *, attempts: int = 3) -> None:
        """Make sure the Meldungen panel is there — or give up right away.

        Alt+Umschalt+M toggles the panel, so it is only sent when the panel
        is missing; sending it blindly would hide one the user wants.

        The important part is what happens when the keystroke does not help.
        This used to log a warning and return, and the build was triggered
        anyway. That drives the IDE through a full build whose result cannot
        be read afterwards — minutes of UI automation on a window the user
        cannot touch, ending in "Meldungen panel not found even after Build".
        From the outside that is indistinguishable from a hung IDE, and it
        has hung one.

        The panel is a precondition, not an afterthought: without it there
        are no diagnostics, so a build is pointless. Checking costs a
        fraction of a second, so failing here is both faster and honest.
        """
        for versuch in range(attempts):
            if self.ide.find_first(name=MELDUNGEN_PANEL_NAME,
                                   class_name=MELDUNGEN_PANEL_CLASS) is not None:
                return
            if versuch == 0:
                log.info("Meldungen panel not visible — sending Alt+Umschalt+M")
                try:
                    self.ide.main_window.type_keys("%+m", set_foreground=True)
                except Exception as e:
                    log.warning("Could not send Alt+Umschalt+M: %r", e)
                    break
            time.sleep(0.4)

        raise BridgeError(
            "Meldungen panel not available, so a build could not be read back. "
            "Nothing was triggered in the IDE. "
            "One-time IDE setup needed: open the panel manually via "
            "Ansicht → Werkzeugfenster → Meldungen (or Ansicht → Meldungen). "
            "The IDE will remember it for future sessions."
        )

    def _dismiss_pending_dialogs(self) -> int:
        """Clear whatever dialogs are open; safe to call any time.

        Two sweeps, because the two kinds of dialog sit in different places.
        The watchdog walks the UIA tree below the main window, which is where
        the progress dialogs of a running build appear. `clear_blocking`
        searches the process's top-level windows through Win32 and is the only
        one that sees a modal *owning* the main window - notably the
        "Neu laden?" prompt Delphi raises when a unit it holds open changed on
        disk. That one disables the main window, so leaving it standing turns
        every following step into an ElementNotEnabled.
        """
        total = self._clear_blocking_dialogs()
        if self.watchdog is None:
            return total
        try:
            return total + self.watchdog.scan_and_dismiss()
        except Exception as e:
            log.warning("Watchdog scan failed: %r", e)
            return total

    def _drain_pre_build_modals(self, *, passes: int = 4, first_wait: float = 1.0) -> int:
        """Clear the modals that appeared from `force_to_foreground` (typically
        "Neu laden?" per modified file) before triggering the build. Stops as
        soon as a pass dismisses nothing. Returns the total number of dialogs
        dismissed.

        Also without a watchdog: `_dismiss_pending_dialogs` answers the
        top-level prompts through `clear_blocking` on its own. (It used to
        return here at once, and a reload prompt left the build reading a
        Meldungen panel that never came.) The prompt comes a moment after the
        activation, so the first pass waits up to `first_wait` seconds for it.
        """
        total = 0
        deadline = time.monotonic() + first_wait
        while True:
            n = self._dismiss_pending_dialogs()
            if n or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        total += n
        for _ in range(max(1, passes) - 1):
            if n == 0:
                break
            time.sleep(0.2)
            n = self._dismiss_pending_dialogs()
            total += n
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
            elif self._build_still_running():
                # the progress window still offers "Abbrechen": compiling, or
                # waiting for a dialog over it (the CE licence reminder comes
                # a few seconds into a build). A quiet title bar is no end.
                last_change = time.monotonic()
            else:
                idle = time.monotonic() - last_change
                if BUILD_DONE_MARKER in current and idle >= stable_for:
                    return current
                if idle >= stable_for * 4:
                    return current
            time.sleep(0.25)
        raise BridgeError(f"Build did not complete within {timeout:.0f}s")

    def _build_still_running(self) -> bool:
        try:
            from delphi_remote.toplevel import build_running
            return build_running(self.ide.process_id)
        except Exception as e:                              # noqa: BLE001
            log.debug("Could not check the progress window: %r", e)
            return False

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
