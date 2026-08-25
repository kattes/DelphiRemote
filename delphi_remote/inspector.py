"""Inspector — extracts the IDE's current state as a JSON-friendly snapshot.

Phase 3 MVP: derives state from the main-window title and the editor-pane
hierarchy. Does not (yet) decode build-config dropdowns, project-manager
contents, or editor cursor position — those need deeper UIA probing.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from delphi_remote.ide_client import BridgeError, DelphiIDE

log = logging.getLogger(__name__)

# Matches "[wird ausgeführt]", "[Erzeugt]", "[Anhalten]", etc.
_MARKER_RE = re.compile(r"\[([^\[\]]+)\]")
# IDE_VERSION example: "Delphi 12 Community Edition"
_IDE_VERSION_RE = re.compile(r"Delphi\s+\d+(\.\d+)?\s+[A-Za-z]+\s*Edition", re.IGNORECASE)

EDIT_WINDOW_CLASS = "TEditWindow"


@dataclass
class IDEState:
    process_id: int
    main_window_title: str
    ide_version: str | None
    active_project: str | None
    active_unit: str | None
    build_state_markers: list[str] = field(default_factory=list)
    is_built: bool = False
    is_running: bool = False
    editor_open_file: str | None = None
    pending_dialogs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


def parse_title(title: str) -> tuple[str | None, str | None, str | None, list[str]]:
    """Parse the IDE's main-window title.

    Returns (project_name, ide_version, active_unit, marker_list).
    Marker list contains the full bracketed forms (e.g. "[Erzeugt]").
    Any field may be None if the title doesn't match the expected pattern.
    """
    if not title:
        return None, None, None, []

    markers_inner = _MARKER_RE.findall(title)
    markers = [f"[{m}]" for m in markers_inner]
    title_clean = _MARKER_RE.sub("", title).strip()

    parts = [p.strip() for p in title_clean.split(" - ")]
    if len(parts) < 2:
        return None, None, None, markers

    project = parts[0] or None
    ide_version: str | None = None
    unit: str | None = None

    # The IDE version segment matches a known shape; prefer that for robustness.
    for idx, segment in enumerate(parts[1:], start=1):
        if _IDE_VERSION_RE.search(segment):
            ide_version = segment
            # Anything after the IDE version segment is the active unit.
            if idx + 1 < len(parts):
                unit = parts[idx + 1] or None
            break
    else:
        # Fallback: assume "<project> - <ide> - <unit>"
        if len(parts) >= 2:
            ide_version = parts[1] or None
        if len(parts) >= 3:
            unit = parts[2] or None

    return project, ide_version, unit, markers


class Inspector:
    def __init__(self, ide: DelphiIDE) -> None:
        self.ide = ide

    def inspect(self) -> IDEState:
        process_id = self.ide.process_id
        title = self.ide.read_main_window_title()
        project, ide_version, unit, markers = parse_title(title)

        editor_file = self._find_editor_open_file()
        pending = self._find_pending_dialogs()

        return IDEState(
            process_id=process_id,
            main_window_title=title,
            ide_version=ide_version,
            active_project=project,
            active_unit=unit,
            build_state_markers=markers,
            is_built="[Erzeugt]" in markers,
            is_running="[wird ausgeführt]" in markers,
            editor_open_file=editor_file,
            pending_dialogs=pending,
        )

    def _find_editor_open_file(self) -> str | None:
        elem = self.ide.find_first(class_name=EDIT_WINDOW_CLASS)
        if elem is None:
            return None
        try:
            name = elem.name
            return name if name else None
        except Exception:
            return None

    def _find_pending_dialogs(self) -> list[dict[str, Any]]:
        """Report dialogs that are pending, wherever they sit.

        Two searches, because Delphi puts its dialogs in two different places.
        The UIA pass below walks down from the main window and finds the ones
        parented to it. Modal dialogs, though, are top-level windows that the
        main window merely owns - the UIA pass never sees them, and reporting
        "pending_dialogs: []" while such a dialog blocks everything is worse
        than reporting nothing: it sends the caller looking in the wrong place.
        That is exactly what happened when TReadErrorDlg blocked build after
        build during the COMBO port.
        """
        if not self.ide.is_attached():
            return []

        results: list[dict[str, Any]] = []
        try:
            from delphi_remote.toplevel import find_blocking
            for _, cls, title in find_blocking(self.ide.process_id):
                results.append({
                    "class_name": cls,
                    "name": title,
                    "rectangle": None,
                    "scope": "top-level (blockiert das Hauptfenster)",
                })
        except Exception as e:                              # noqa: BLE001
            log.debug("Top-level dialog scan failed: %r", e)

        candidates = ("TProgressForm", "TMessageForm", "TConfirmDialog")
        for cls in candidates:
            try:
                elem = self.ide.find_first(class_name=cls)
            except BridgeError:
                continue
            if elem is None:
                continue
            results.append({
                "class_name": cls,
                "name": _safe_attr(elem, "name"),
                "rectangle": _safe_rect(elem),
            })
        return results


def _safe_attr(elem: Any, attr: str) -> str:
    try:
        return getattr(elem, attr) or ""
    except Exception:
        return ""


def _safe_rect(elem: Any) -> list[int] | None:
    try:
        r = elem.rectangle
        return [r.left, r.top, r.right, r.bottom]
    except Exception:
        return None
