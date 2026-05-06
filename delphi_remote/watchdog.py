"""Dialog watchdog — detects spurious dialogs and dismisses them per rule list.

Phase 2 MVP: rule-driven, single-pass scan against the live UIA tree. A future
revision can switch to SetWinEventHook for push notifications instead of polling.

Rules live in YAML (see default_rules.yaml). Each rule has a `match` block and
an `action`; the first rule whose match is satisfied wins.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from delphi_remote.ide_client import BridgeError, DelphiIDE

log = logging.getLogger(__name__)


@dataclass
class DialogRule:
    name: str
    match_class: str | None = None
    match_name: str | None = None
    match_name_contains: list[str] = field(default_factory=list)
    parent_class: str | None = None
    action: str = "click_button"
    buttons: list[str] = field(default_factory=list)


@dataclass
class DialogEvent:
    rule_name: str
    class_name: str
    name: str
    button_clicked: str | None
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule_name,
            "class_name": self.class_name,
            "name": self.name,
            "button_clicked": self.button_clicked,
            "timestamp": self.timestamp,
        }


def default_rules_path() -> Path:
    """Return the path to the bundled default_rules.yaml."""
    return Path(str(files("delphi_remote") / "default_rules.yaml"))


def load_rules(yaml_path: Path) -> list[DialogRule]:
    """Parse a YAML rules file. Raises FileNotFoundError if missing."""
    with yaml_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    rules: list[DialogRule] = []
    for entry in data.get("rules", []):
        match = entry.get("match", {}) or {}
        contains = match.get("name_contains") or []
        if isinstance(contains, str):
            contains = [contains]
        buttons = entry.get("button") or []
        if isinstance(buttons, str):
            buttons = [buttons]
        rules.append(DialogRule(
            name=entry.get("name", "<unnamed>"),
            match_class=match.get("window_class"),
            match_name=match.get("name") or match.get("title"),
            match_name_contains=list(contains),
            parent_class=match.get("parent_class"),
            action=entry.get("action", "click_button"),
            buttons=list(buttons),
        ))
    return rules


class DialogWatchdog:
    """Single-pass rule-driven watchdog. Call scan_and_dismiss() periodically."""

    def __init__(self, ide: DelphiIDE, rules: list[DialogRule]) -> None:
        self.ide = ide
        self.rules = rules
        self.events: list[DialogEvent] = []

    def scan_and_dismiss(self) -> int:
        """One pass over the rule list. Returns count of dialogs dismissed."""
        if not self.rules:
            return 0
        dismissed = 0
        for rule in self.rules:
            element = self._find_match(rule)
            if element is None:
                continue
            if self._dismiss(element, rule):
                dismissed += 1
        return dismissed

    def _find_match(self, rule: DialogRule) -> Any | None:
        """Locate the first element matching `rule`. Returns ElementInfo or None."""
        try:
            return self.ide.find_first(
                class_name=rule.match_class, name=rule.match_name,
            )
        except BridgeError:
            return None

    def _dismiss(self, element: Any, rule: DialogRule) -> bool:
        if rule.action == "click_button":
            return self._click_button_in(element, rule)
        if rule.action == "abort":
            log.warning("Watchdog rule %r demands abort on element %r/%r",
                        rule.name, _safe_attr(element, "class_name"),
                        _safe_attr(element, "name"))
            return False
        log.warning("Unknown watchdog action %r in rule %r", rule.action, rule.name)
        return False

    def _click_button_in(self, root: Any, rule: DialogRule) -> bool:
        from pywinauto import mouse

        # Capture identity BEFORE clicking — once the dialog closes its UIA
        # handle goes stale and these reads return empty strings.
        root_class = _safe_attr(root, "class_name")
        root_name = _safe_attr(root, "name")

        for button_name in rule.buttons:
            target = _find_button(root, button_name)
            if target is None:
                continue
            try:
                rect = target.rectangle
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
            except Exception as e:
                log.warning("Could not read rectangle of button %r in rule %r: %r",
                            button_name, rule.name, e)
                continue
            log.info("Watchdog: clicking %r at (%d, %d) for rule %r",
                     button_name, cx, cy, rule.name)
            mouse.click(button="left", coords=(cx, cy))
            time.sleep(0.2)
            self.events.append(DialogEvent(
                rule_name=rule.name,
                class_name=root_class,
                name=root_name,
                button_clicked=button_name,
                timestamp=time.time(),
            ))
            return True
        log.debug("Watchdog: rule %r matched but no listed button was found",
                  rule.name)
        return False


def _safe_attr(elem: Any, attr: str) -> str:
    try:
        return getattr(elem, attr) or ""
    except Exception:
        return ""


def _find_button(root: Any, button_name: str) -> Any | None:
    """DFS for a TButton (or anything Button-shaped) named `button_name`."""
    cn = _safe_attr(root, "class_name")
    nm = _safe_attr(root, "name")
    if nm == button_name and ("Button" in cn or cn == "TButton" or cn == ""):
        # cn == "" allows OS-drawn buttons that don't expose a class name.
        try:
            ctype = str(getattr(root, "control_type", "") or "")
        except Exception:
            ctype = ""
        if cn == "TButton" or "Button" in ctype:
            return root
    try:
        for child in root.children():
            hit = _find_button(child, button_name)
            if hit is not None:
                return hit
    except Exception:
        pass
    return None
