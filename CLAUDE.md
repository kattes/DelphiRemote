# Delphi IDE Remote Bridge

Python-basierte Brücke zur Fernsteuerung der Delphi IDE (RAD Studio Community/Free Edition) und der daraus kompilierten VCL-Anwendungen. Ziel: Claude Code soll Builds auslösen, Compile-Output strukturiert auslesen und perspektivisch UI-Tests gegen native VCL-Applikationen ausführen können — ohne dass eine kostenpflichtige Lizenz mit Command-Line-Compiler erforderlich ist.

## Motivation und Hintergrund

Die **Delphi Community Edition** enthält keinen Command-Line-Compiler (`dcc32.exe` ist deaktiviert/nicht enthalten). Damit fallen alle herkömmlichen MCP-Server-Lösungen aus, die auf `dcc32` oder `MSBuild` aufbauen (z.B. `delphi-build-mcp-server` von Basti-Fantasti). Auch der LSP-MCP-Server von SkybuckFlying ist keine Option, da er Delphi 13 mit gültiger Lizenz voraussetzt.

Lösungsansatz: Statt die Toolchain unter der IDE zu nutzen, **steuern wir die laufende IDE selbst** als Compile-Backend. Die VCL-basierte `bds.exe` ist eine reguläre Windows-Anwendung; ihre Fenster, Menüs und Ausgabe-Panels sind über Standard-Windows-APIs ansprechbar.

## Ziele (Goals)

1. **Build-Loop**: Claude Code kann ein Delphi-Projekt durch einen einzigen Bash-Aufruf kompilieren lassen und erhält strukturiertes Error/Warning-Output zurück (JSON).
2. **Robuste Dialog-Behandlung**: Spontan auftauchende Modal-Dialoge der IDE werden automatisch erkannt und nach konfigurierbaren Regeln behandelt — ohne dass der Build hängenbleibt.
3. **IDE-Introspektion**: Claude Code kann den aktuellen Zustand der IDE abfragen (offene Projekte, aktive Datei, Build-Konfiguration).
4. **UI-Test-Framework**: Aufbauend auf der Bridge wird eine schlanke Test-DSL bereitgestellt, mit der VCL-Anwendungen (kompiliert aus der CE) automatisiert getestet werden können.

## Nicht-Ziele (Non-Goals)

- **Kein CI-Ersatz**: Die Bridge braucht eine sichtbar laufende IDE und einen interaktiven Desktop. Headless-Server-Builds sind nicht das Ziel.
- **Keine Code-Generierung im IDE-Editor**: Code-Edits laufen weiterhin über das Filesystem, nicht über simulierte Tastatureingaben in den Editor.
- **Kein Refactoring-Engine-Ersatz**: Go-to-Definition, Find References etc. bleiben außen vor — dafür wäre LSP nötig.
- **Kein UniGUI-UI-Test**: UniGUI rendert im Browser; dafür wäre Playwright/Selenium das richtige Werkzeug, nicht `pywinauto`. Server-seitige Logik kann aber über HTTP-Calls getestet werden.

## Architektur

```
┌─────────────────────────────────────────────────────────────┐
│  Claude Code (Bash)                                          │
│  $ python -m delphi_remote.cli build MyProject.dproj         │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  CLI Entry Point (cli.py)                                    │
│  Argument-Parsing, JSON-Output, Exit-Codes                   │
└──────────────────────────┬──────────────────────────────────┘
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
┌──────────────────────────┐  ┌──────────────────────────┐
│  Build Orchestrator      │  │  Dialog Watchdog         │
│  (build.py)              │  │  (watchdog.py)           │
│                          │  │                          │
│  - IDE in Vordergrund    │  │  - SetWinEventHook       │
│  - Compile triggern      │  │  - Whitelist/Rules       │
│  - Auf Fertigstellung    │  │  - Auto-Klick / Abort    │
│    warten                │  │  - Logging               │
│  - Messages-Panel lesen  │  │                          │
└──────────────┬───────────┘  └─────────────┬────────────┘
               │                            │
               └────────────┬───────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  IDE Client (ide_client.py)                                  │
│  - Window-Discovery (bds.exe)                                │
│  - pywinauto UIA Backend                                     │
│  - Menü-/Panel-/Dialog-Operationen                           │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
                ┌──────────────────────┐
                │  Delphi IDE (bds.exe)│
                │  + kompilierte Apps  │
                └──────────────────────┘
```

## Projektlayout

```
delphi_remote/
├── __init__.py
├── cli.py                  # Bash entry point
├── build.py                # Build orchestrator
├── watchdog.py             # Dialog watchdog (Win32 hooks)
├── ide_client.py           # Window/IDE handling
├── inspector.py            # IDE state introspection
├── testing/
│   ├── __init__.py
│   ├── dsl.py              # Test DSL (delphi_app, window, button, ...)
│   ├── runner.py           # Test discovery and execution
│   └── visual.py           # Screenshot diffing (Pillow + imagehash)
├── rules/
│   ├── default_rules.yaml  # Standard dialog whitelist
│   └── README.md           # How to extend rules
├── logs/                   # Auto-generated, gitignored
└── tests/
    ├── unit/               # pytest unit tests for the bridge itself
    └── integration/        # End-to-end against a sample Delphi project
```

## Komponenten im Detail

### 1. CLI (`cli.py`)

Subkommandos:
- `build <dproj>` — kompiliert ein Projekt, gibt JSON mit Errors/Warnings/Hints
- `inspect` — gibt aktuellen IDE-Zustand als JSON
- `run <dproj>` — startet die kompilierte EXE und attached die Test-Bridge
- `test <suite>` — führt eine Test-Suite gegen die laufende App aus
- `watchdog --daemon` — startet den Watchdog standalone für manuelles Debugging

Output ist **immer JSON auf stdout**, Logs gehen auf stderr und in `logs/`. Exit-Code 0 = Erfolg, 1 = Build-Fehler, 2 = Bridge-Fehler (IDE nicht erreichbar etc.).

### 2. Build Orchestrator (`build.py`)

Ablauf:
1. IDE-Prozess (`bds.exe`) finden, ggf. starten und auf Ready warten
2. Watchdog im Hintergrund-Thread aktivieren
3. Projekt öffnen, falls noch nicht offen (über IDE-Menü `File → Open Project`)
4. Build-Konfiguration setzen (Debug/Release, Win32/Win64) — über `Project → Configuration Manager`
5. Compile triggern (Menü `Project → Compile` oder `Ctrl+F9`)
6. Auf Build-Ende warten (Polling auf Statusbar-Text oder Messages-Panel)
7. Messages-Panel auslesen, parsen, strukturieren
8. Watchdog stoppen, JSON ausgeben

**Messages-Panel-Parsing**: Jede Zeile wird gegen Regex gematcht:
- `[dcc32 Hint] Unit.pas(123): H2077 Value assigned to 'X' never used`
- `[dcc32 Warning] Unit.pas(45): W1014 Method 'Foo' hides virtual method...`
- `[dcc32 Error] Unit.pas(89): E2003 Undeclared identifier: 'Bar'`

Output-Schema:
```json
{
  "status": "ok|errors|aborted",
  "duration_seconds": 12.4,
  "diagnostics": [
    {"severity": "error", "file": "Unit.pas", "line": 89,
     "code": "E2003", "message": "Undeclared identifier: 'Bar'"}
  ],
  "stats": {"errors": 1, "warnings": 0, "hints": 3}
}
```

### 3. Dialog Watchdog (`watchdog.py`)

**Bevorzugte Implementierung**: `SetWinEventHook` mit `EVENT_SYSTEM_DIALOGSTART` über `pywin32`. Kein Polling-Overhead, OS-Push.

**Fallback**: 200-ms-Polling über `pywinauto.findwindows.find_elements`.

**Regelwerk** (`rules/default_rules.yaml`):
```yaml
rules:
  - name: "File changed externally"
    match:
      window_class: "TMessageForm"
      content_contains: ["modified", "geändert"]
    action: click_button
    button: ["Yes", "Ja", "Reload", "Neu laden"]

  - name: "Project out of date"
    match:
      window_class: "TMessageForm"
      content_contains: ["out of date"]
    action: click_button
    button: ["Yes", "Ja"]

  - name: "CE License Reminder"
    match:
      title_contains: ["Community Edition"]
    action: click_button
    button: ["OK", "Continue"]
    increment_counter: ce_reminders

default_action: abort_and_screenshot
```

**Whitelist-Prinzip**: Nur explizit aufgeführte Dialoge werden bestätigt. Alles andere → Build abbrechen, Screenshot speichern, Dialog-Klasse + Inhalt loggen, im JSON-Output als `unhandled_dialog` zurückgeben. Verhindert das versehentliche Wegklicken von "Save changes to ProductionUnit.pas?".

**Identifikation**: Kombination aus Window-Klasse, Parent-Prozess (`bds.exe`) und Static-Control-Inhalt. Niemals nur Title-Match (zu fragil bei Lokalisierung).

**Logging**: Jede Aktion mit Timestamp, Klasse, Title, Inhalt, gewählter Aktion in `logs/watchdog-YYYY-MM-DD.log`.

### 4. IDE Client (`ide_client.py`)

Wrapper um `pywinauto` mit UIA-Backend. Stellt sprechende Methoden bereit:

```python
class DelphiIDE:
    def attach(self) -> None: ...
    def open_project(self, dproj_path: Path) -> None: ...
    def set_build_config(self, config: str, platform: str) -> None: ...
    def trigger_compile(self) -> None: ...
    def trigger_build(self) -> None: ...
    def read_messages_panel(self) -> list[str]: ...
    def is_busy(self) -> bool: ...
    def wait_until_idle(self, timeout: float = 60.0) -> None: ...
    def screenshot(self, path: Path) -> None: ...
```

Menü-Operationen über `app.menu_select("Project->Compile")`, Panel-Zugriffe über UIA-TextPattern wo verfügbar.

### 5. Test DSL (`testing/dsl.py`)

Schlanke, lesbare API für VCL-UI-Tests. Nutzt aus, dass VCL-Komponenten ihre `Name`-Property als Window-Identifier exponieren — das DFM ist quasi die Test-API.

```python
from delphi_remote.testing import delphi_app

def test_login_flow():
    with delphi_app("MyApp.exe") as app:
        login = app.window("frmLogin")
        login.edit("edUser").type("katte")
        login.edit("edPass").type("test123")
        login.button("btnLogin").click()

        main = app.wait_for_window("frmMain", timeout=5)
        assert main.label("lblStatus").text == "Connected"
```

API-Bausteine:
- `delphi_app(exe_path, *, args=None, test_mode=True)` — Context Manager, startet App, attached, killt am Ende
- `app.window(name)` — VCL-Form per `Name`-Property
- `app.wait_for_window(name, timeout)` — wartet auf erscheinendes Form
- `window.button(name) / .edit(name) / .label(name) / .listview(name)` — typed accessors
- `.click() / .type(text) / .text / .items / .select(item)` — Aktionen und Properties
- `app.screenshot()` / `app.dump_tree()` — Debug-Helfer
- `app.assert_no_dialog()` — am Test-Ende kein hängender Dialog

### 6. Visual Regression (`testing/visual.py`)

`Pillow` + `imagehash` für pixel-perfekte und perzeptuelle Vergleiche.

```python
def test_combo_map_render():
    with delphi_app("ComboMapViewer.exe") as app:
        app.window("frmMain").button("btnLoadKMZ").click()
        app.open_file_dialog().select("samples/test.kmz")
        app.wait_until_idle()
        assert app.window("frmMain").matches_baseline("combo_map_test_kmz.png", tolerance=0.02)
```

Baselines liegen in `tests/visual/baselines/`, neue Aufnahmen via `pytest --update-baselines`.

## Phasenplan

### Phase 1 — Foundation (MVP Build-Loop)
- [ ] `ide_client.py`: Attach an `bds.exe`, Menü-Navigation, Messages-Panel auslesen
- [ ] `build.py`: Compile-Trigger, Output-Parsing, JSON-Schema
- [ ] `cli.py`: `build`-Subkommando funktional
- [ ] **Acceptance**: `python -m delphi_remote.cli build sample.dproj` produziert valides JSON, Exit-Code matched Build-Status

### Phase 2 — Robustheit (Watchdog)
- [ ] `watchdog.py` mit `SetWinEventHook`
- [ ] `default_rules.yaml` mit den Top-10-Dialogen der Delphi CE
- [ ] Logging in `logs/`
- [ ] **Acceptance**: Bei laufendem Watchdog läuft `build` 100x in Folge ohne manuellen Eingriff durch, auch wenn parallel Dateien extern verändert werden

### Phase 3 — Introspektion
- [ ] `inspector.py`: Liste offener Projekte, aktive Datei, Build-Config, Cursor-Position
- [ ] `cli.py inspect`-Subkommando
- [ ] **Acceptance**: JSON-Output deckt 90% der "wo bin ich gerade?"-Fragen ab

### Phase 4 — Test DSL (Pilot)
- [ ] `testing/dsl.py` Grundbausteine: `delphi_app`, `window`, Button/Edit/Label
- [ ] `testing/runner.py`: pytest-Integration
- [ ] Pilot-Suite gegen ein kleines Tool (Vorschlag: Camera-Range-Tool oder Teilbereich Combo Map Viewer — kein SRT, zu groß als Einstieg)
- [ ] **Acceptance**: 5 grüne Tests gegen das Pilot-Projekt, einer absichtlich rot zur Validierung des Failure-Pfads

### Phase 5 — Visual Regression + Erweiterung
- [ ] `testing/visual.py`
- [ ] Test-Suite-Erweiterung um Visual Checks
- [ ] Property-Based "Smoke-Tests" via Hypothesis (zufällige Eingabesequenzen → App darf nicht crashen)

### Phase 6 — Komfort
- [ ] `--watch`-Mode: Bridge bleibt resident, kürzere Build-Latenz
- [ ] Messages-Panel-Cache (nur Diff seit letztem Build parsen)
- [ ] Optional: Lokaler MCP-Server-Wrapper, sodass Claude Code die Bridge als MCP-Tool sieht statt als Bash-Aufruf

## Technische Spezifikation

**Python-Version**: 3.11+

**Hauptabhängigkeiten**:
- `pywinauto >= 0.6.8` — UIA-Backend für Window-Steuerung
- `pywin32` — Win32-API, `SetWinEventHook`
- `mss` — schnelle Screenshots
- `Pillow`, `imagehash` — Visual Regression
- `pyyaml` — Rules-Konfiguration
- `pytest` — Test-Runner für Phase 4+
- `click` oder `argparse` — CLI

**Plattform**: Windows 10/11 x64. Linux/macOS sind nicht im Scope (Delphi CE läuft eh nur auf Windows).

**Delphi-Version**: Primär RAD Studio 12 Athens CE, Forward-Kompatibilität mit 13 Florence anstreben (Menüstruktur kann sich ändern → Selektoren konfigurierbar machen).

## Coding Conventions (Python)

- **Black** für Formatierung, Line-Length 100
- **Type Hints** überall, `from __future__ import annotations` als Default
- **Logging** über `logging`-Modul, kein `print` außer in `cli.py` für JSON-Output
- **Dataclasses** für strukturierte Objekte (Diagnostics, Dialog-Events, Test-Results)
- **Keine Inline-Variable-Initialisierung in dichten Blöcken** — Lesbarkeit vor Kürze
- **`try/finally` für jeden Resource-Acquire** (Window-Handles, Hooks, Subprozesse)
- Funktionen >40 Zeilen sind ein Code-Smell und werden zerlegt
- **Modulgröße**: max ~300 Zeilen, dann splitten

## Coding Conventions (Delphi-Seite, falls VCL-Helper nötig)

Falls für robustes Auslesen des Messages-Panels ein kleiner VCL-Helper kompiliert werden muss:

- Traditionelle `var`-Blöcke, **keine** Inline-Variable-Deklarationen
- Hungarian-Style Prefixes (`btnFoo`, `edBar`, `lblBaz`)
- Strenges `try/finally` um jeden Resource-Aquire
- Minimal-invasive Fixes statt Rewrites
- Keine Frameworks, plain VCL

## Bekannte Einschränkungen und Caveats

1. **IDE muss sichtbar laufen**: Minimiert ist OK, aber nicht in einer gesperrten Session.
2. **Fokus-Diebstahl**: Während eines Builds nicht in andere Apps wechseln, die modale Dialoge öffnen — kann den Watchdog verwirren.
3. **Lokalisierung**: Deutsche IDE-Lokalisierung wird unterstützt, andere Sprachen brauchen Erweiterung der Rules.
4. **Race Conditions beim Build-Ende-Detect**: Statusbar-Text und Messages-Panel updaten asynchron. Conservative timeout + double-check-Pattern.
5. **CE-License-Dialog**: Erscheint sporadisch, wird durch Rule abgefangen, aber Counter sollte mitlaufen, falls Lizenz wirklich ausläuft.
6. **WebView2-Komponenten** (relevant für Combo Map Viewer): UIA-Tree ist hier eingeschränkt, ggf. Visual Regression statt strukturierter Asserts.
7. **Headless ist explizit kein Ziel**: Wer CI braucht, baut sich einen dedizierten Build-Server mit Auto-Login und gesperrtem User.

## Pilot-Empfehlung

Erstes echtes Projekt für Phase 4: **Camera-Range-Tool** oder ein abgegrenzter Teilbereich des **Combo Map Viewers**. Beide sind klein genug zum schnellen Iterieren, aber real genug um echten Wert zu liefern. SRT ist als Pilot zu groß — die Login-Flows und UniGUI-Server-Komponenten verlangen einen anderen Test-Ansatz (HTTP-basiert) und würden die Bridge in der Frühphase überfordern.

## Erweiterung um MCP (optional, Phase 6)

Sobald die Bridge stabil läuft, kann sie als lokaler MCP-Server gewrapped werden, sodass Claude Code sie nicht über Bash, sondern als nativen Tool-Call ansprechen kann. Empfohlene Tools:
- `delphi_build(dproj_path, config?, platform?)` → Diagnostics
- `delphi_inspect()` → IDE-State
- `delphi_run_tests(suite_path)` → Test-Results
- `delphi_screenshot(window_name?)` → Image (für Vision-API-Analyse durch Claude)

Das ist die Endausbau-Stufe und darf bis dahin ignoriert werden — der Bash-CLI-Pfad reicht für 95% der Workflows.
