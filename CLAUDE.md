# Delphi IDE Remote Bridge

Python-basierte Brücke zur Fernsteuerung der Delphi IDE (RAD Studio Community/Free Edition) und der daraus kompilierten VCL-Anwendungen. Ziel: Claude Code soll Builds auslösen, Compile-Output strukturiert auslesen und perspektivisch UI-Tests gegen native VCL-Applikationen ausführen können — ohne dass eine kostenpflichtige Lizenz mit Command-Line-Compiler erforderlich ist.

## Status (v0.3.0)

Phasen 1-5 sind erledigt; die Bridge ist seit dem CoverGenerator-Pilotprojekt produktiv im Einsatz und kann ein neues Delphi-Projekt von Build bis UI-Smoke-Test komplett über Claude Code treiben.

- **Build-Loop** (`delphi-remote build <dproj>`): JSON-Output mit Diagnostics, Pre-Build-EXE-Kill, Watchdog-Dialog-Handling.
- **Watchdog**: "Neu laden?", "Compilieren", "Erzeugen" werden auto-bestätigt; Projekt-lokale Regeln via `.delphi_remote.yaml`.
- **Inspector** (`delphi-remote inspect`): aktuelles IDE-State als JSON.
- **Resident Watcher** (`delphi-remote watch`): Daemon-Modus, ~1.5s Latenz weniger pro Call.
- **Test-DSL** (`delphi_remote.testing`): `delphi_app()`-Context-Manager, File-Dialog-Helper, StateGuard, Visual-Regression.

Offen / nice-to-have (Phase 6): Lokaler MCP-Server-Wrapper, Messages-Panel-Cache.

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
├── cli.py                  # Bash entry point (click subcommands)
├── build.py                # Build orchestrator + Pre-Build-EXE-Kill
├── watchdog.py             # Dialog watchdog (rule-driven, polling-based)
├── watcher.py              # Resident TCP daemon for low-latency builds
├── ide_client.py           # Window/IDE handling, attach, foreground hygiene
├── inspector.py            # IDE state introspection (title parse + pending dialogs)
├── default_rules.yaml      # Default dialog whitelist (bundled as package data)
└── testing/
    ├── __init__.py         # Re-exports the public DSL surface
    ├── dsl.py              # delphi_app() context manager, DelphiApp wrapper
    ├── file_dialog.py      # OS file-dialog detection + paste_path helper
    ├── helpers.py          # DPI awareness, clipboard retries, graceful_close
    ├── state_guard.py      # %APPDATA% state-file backup/restore
    └── visual.py           # Visual regression (imagehash + pixel diff)
```

## Komponenten im Detail

### 1. CLI (`cli.py`)

Subkommandos:
- `build <dproj>` — kompiliert ein Projekt, gibt JSON mit Errors/Warnings/Hints
  - Flags: `--config`, `--platform`, `--timeout`, `--rules`, `--no-watchdog`, `--no-watcher`, `--no-auto-start`, `--no-kill-exe`
  - ⚠️ `--config` und `--platform` werden **entgegengenommen, aber nicht angewandt**
    (Phase-1-Stand; die CLI meldet das selbst als Warning). Es gilt die im IDE
    aktive Konfiguration. Siehe Caveat 8.
- `inspect` — gibt aktuellen IDE-Zustand als JSON
- `watch` — startet den resident Watcher-Daemon (TCP 17556)
- `watcher-status` / `watcher-stop` — Watcher-Lebenszyklus
- `inspect-windows` — Window-Tree-Dump für Selektor-Discovery
- (UI-Tests laufen als reguläre Python-Skripte, die `delphi_remote.testing` importieren — kein eigenes `test`-Subkommando, das wäre unnötiger Wrapper)

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

#### Praxisnotizen zur Test-DSL

- `delphi_app()` leitet `title_re` vom EXE-Namen ab. Setzt das Formular seinen
  Titel in `FormCreate` um (z. B. `"DocFilter - F07 AS BUILD table extraction"`),
  schlägt das Warten fehl — `title_re=r"^DocFilter"` explizit mitgeben.
- Läuft evtl. noch eine andere Instanz derselben Anwendung (auch eine vom
  Anwender gestartete), bindet `delphi_app()` womöglich deren Fenster. Wenn das
  stören kann: Prozess selbst starten und das Fenster über `process_id()`
  auf die eigene PID filtern.
- **UIA liefert bei VCL-`TEdit` keinen Text** (`window_text()` und `texts()`
  sind leer, obwohl das Feld gefüllt ist). Zum Setzen `set_edit_text()`
  benutzen; `^a{DEL}` + `type_keys` leert ein vorbelegtes Feld nicht zuverlässig
  und hängt den neuen Wert an den alten an. Zum Verifizieren
  `capture_window()` statt Textabfrage.
- Eine gebaute Anwendung immer **mehr als einmal** durchlaufen lassen und über
  `window.close()` beenden, nicht über `kill()` — sonst bleiben Deadlocks nach
  dem ersten Lauf und blockierte Shutdowns unentdeckt.

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

### Phase 1 — Foundation (MVP Build-Loop) ✅
- [x] `ide_client.py`: Attach an `bds.exe`, Menü-Navigation, Messages-Panel auslesen
- [x] `build.py`: Compile-Trigger, Output-Parsing, JSON-Schema
- [x] `cli.py`: `build`-Subkommando funktional
- [x] **Acceptance**: `delphi-remote build sample.dproj` produziert valides JSON, Exit-Code matched Build-Status

### Phase 2 — Robustheit (Watchdog) ✅
- [x] `watchdog.py` mit Rule-basiertem Scan (Implementation: Polling im Build-Loop, kein SetWinEventHook nötig)
- [x] `default_rules.yaml` mit den realen Top-Dialogen der Delphi CE: "Neu laden?", "Compilieren", "Erzeugen"
- [x] Pre-Build-EXE-Kill (laufende EXE schließen, damit der Linker schreiben kann) — `build.terminate_previous_exe()`
- [x] **Acceptance**: erfüllt im CoverGenerator-Piloten — wiederholte Builds mit extern modifizierten Files laufen ohne manuellen Eingriff durch

### Phase 3 — Introspektion ✅
- [x] `inspector.py`: aktive Datei, Build-State-Markers, pending Dialogs
- [x] `cli.py inspect`-Subkommando
- [x] **Acceptance**: erfüllt — Inspector deckt Title-Parse und Pending-Dialog-Scan ab; Cursor-Position bleibt offen (UIA-Limitation, kein realer Bedarf bisher)

### Phase 4 — Test DSL ✅
- [x] `testing/dsl.py`: `delphi_app()` Context-Manager, `fill_edit_in_group`, `click_button`, `wait_for_file_dialog`, `set_window_rect`, `read_status_bar`
- [x] `testing/file_dialog.py`: `FileDialog`-Wrapper, `paste_path` (Alt+N → ^a{DEL} → ^v → {ENTER}), `wait_for_modal` / `wait_for_no_modal`
- [x] `testing/helpers.py`: `enable_dpi_awareness`, `set_clipboard_text` (mit Retry), `graceful_close` (WM_CLOSE → SIGTERM-Fallback)
- [x] `testing/state_guard.py`: `StateGuard(path)` Context-Manager und `guard_module()` für Script-Top-Level
- [x] **Acceptance**: erfüllt durch den CoverGenerator-Piloten — `feature_test.py`, `aspect_test.py`, `spacing_test.py`, `smoke_test.py` haben das Pattern dort hand-rolled exerziert; ist jetzt upstream konsolidiert
- [ ] Optional: `testing/runner.py` pytest-Plugin — bislang nicht nötig, Standalone-Scripts reichen

### Phase 5 — Visual Regression ✅
- [x] `testing/visual.py`: `matches_baseline(actual, baseline_name)` mit perzeptuellem Hash + Pixel-Diff-Ratio
- [x] `compare_images()` + `VisualDiff`-Dataclass für detaillierte Asserts
- [x] `capture_window()` via `mss` für Fenster-Screenshots
- [x] Baseline-Update via Env-Var `DELPHI_REMOTE_UPDATE_BASELINES=1`
- [ ] Optional: Property-Based "Smoke-Tests" via Hypothesis

### Phase 6 — Komfort
- [x] Resident Watcher (`delphi-remote watch`) — Daemon-Modus
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
8. **`--config` / `--platform` sind wirkungslos**: Der Build nimmt die im IDE
   aktive Konfiguration, nicht die übergebene. Workaround bis
   `set_build_config()` verdrahtet ist: im `.dproj`
   `<Config Condition="'$(Config)'==''">Release</Config>` setzen und neu bauen —
   das IDE bemerkt die externe Änderung, der Watchdog quittiert den
   Reload-Prompt automatisch. Kontrolle über den Ablageort der EXE
   (`Win64\Release` vs. `Win64\Debug`), nicht über den Rückgabewert.
9. **Deutsches Meldungen-Panel: erledigt, Caveat war ueberholt.** Frueher hiess
   es hier, der Clipboard-Inhalt werde verworfen und `diagnostics` bleibe leer.
   Das stimmt nicht mehr: `_SEVERITY_MAP` in `build.py` kennt die deutschen
   Schweregrade (Fehler, Warnung, Hinweis, Fataler Fehler), `BUILD_DONE_MARKER`
   ist `"[Erzeugt]"`. Am 25.08.2026 gegen Delphi 12 CE (deutsch) verifiziert:
   ein Build mit absichtlich eingebautem Fehler liefert alle vier Schweregrade
   korrekt mit Datei, Zeile, Code und deutschem Meldungstext.
   Die Zeile `"Clipboard content does not look like Meldungen output"` ist nur
   eine Log-Warnung und verwirft nichts.
   Zykluszeit: rund 14 Sekunden pro Build.

10. **JSON-Ausgabe war cp1252 statt UTF-8** (behoben am 25.08.2026). Die CLI
   serialisiert mit `ensure_ascii=False`; auf einer deutschen Konsole landeten
   Umlaute damit als cp1252-Bytes auf stdout. JSON ist per Definition UTF-8,
   also brach `json.loads(rohbytes.decode("utf-8"))` mit `UnicodeDecodeError`
   auf `0xFC` ab. Unter Windows fiel es lange nicht auf, weil Pythons `open()`
   ohne Angabe die Locale-Kodierung nimmt und damit zufaellig das Richtige tat.
   `_force_utf8_streams()` in `cli.py` stellt stdout und stderr jetzt beim
   Start auf UTF-8 um. Wer die Ausgabe einliest, sollte trotzdem explizit
   `encoding="utf-8"` angeben statt sich auf das Locale zu verlassen.

11. **Projektwechsel haengt Projekte NICHT mehr an eine Gruppe an**
   (behoben am 25.08.2026). `os.startfile` auf eine `.dproj` entspricht einem
   Doppelklick im Explorer; eine laufende IDE beantwortet das, indem sie das
   Projekt der bestehenden **Projektgruppe hinzufuegt**. Zwei Projekte in einer
   Gruppe heisst: bei jedem Wechsel fragt die IDE modal nach dem Speichern der
   Gruppe. Der Dialog blockiert die Bridge und aeussert sich als
   `Could not read Meldungen panel after multiple attempts` - ein Symptom, das
   nicht im entferntesten auf die Ursache zeigt.
   `_load_project_replacing()` in `ide_client.py` benutzt jetzt
   *Datei -> Projekt oeffnen* (Strg+F11) samt Dateidialog. Das ist die
   Operation, die das aktive Projekt **ersetzt**. Die Dateizuordnung dient nur
   noch als Rueckfallebene, wenn der Dialog ausbleibt, und protokolliert das.

12. **Fortschrittsfenster "Erzeugen" blieb nach dem Bau stehen** (behoben am
   02.10.2026). Ist in der IDE "Nach erfolgreicher Compilierung automatisch
   schliessen" nicht angehakt, wartet das `TProgressForm` nach dem Bau auf OK.
   Es ist ein Top-Level-Fenster neben dem Hauptfenster; die Watchdog-Regel
   "Post-build progress dialog" greift nur, solange ihre Schleife laeuft. Geht
   das Fenster erst danach auf, blieb es stehen und blockierte die IDE. Beim
   naechsten Bau desselben Projekts raeumte `_clear_blocking_dialogs` es weg -
   beim Bau eines *anderen* Projekts kam der Projektwechsel (Strg+F11) aber
   vorher und scheiterte mit `did not become active within 60s`.
   Jetzt: `toplevel.KNOWN` kennt `TProgressForm` (nur "OK" - waehrend eines
   laufenden Baus heisst der Knopf "Abbrechen" und wird nie geklickt),
   `ensure_project_loaded` raeumt blockierende Fenster vor dem Projektwechsel
   ab, und `build()` schliesst das Fortschrittsfenster am Ende jedes Baus
   (`_close_progress_window`). Verifiziert mit abwechselnden Bauten zweier
   Projekte (DAS Designer und sein Testprojekt), danach `pending_dialogs: []`.

13. **"Neu laden?", CE-Lizenzhinweis und die Projektgruppe** (behoben am
   04.10.2026). Drei Ursachen fuer `did not become active within 60s`,
   `Could not read Meldungen panel` und `ElementNotEnabled`:
   - `_drain_pre_build_modals` kehrte ohne residenten Watchdog sofort zurueck
     und suchte "Neu laden?" (Titel "Informationen", Knoepfe Ja/Nein/Alle
     Ja/Alle Nein) gar nicht. Jetzt raeumt es immer ab (`clear_blocking`
     beantwortet sie mit "Alle Ja": neu laden) und wartet bis zu 1 s auf die
     Abfrage, die erst nach dem Aktivieren der IDE kommt.
   - Der Lizenzhinweis der Community Edition (`TCENotificationDialog`, ohne
     Fenstertitel) erscheint gelegentlich, auch ueber einem laufenden Bau. Die
     Watchdog-Regel "CE License Reminder" (Titel enthaelt "Community
     Edition") traf ihn nie. Jetzt in `toplevel.KNOWN` (nur "OK").
   - Beim Projektwechsel scheiterte Strg+F11 an einem gesperrten Hauptfenster
     (`ElementNotEnabled`) oder wurde verschluckt, und der Rueckfall ueber die
     Dateizuordnung haengte das Projekt als zweites einer "ProjectGroup1" an.
     Danach fragte die IDE vor jedem Bau "ProjectGroup1 speichern unter"
     (Windows-Dateidialog `#32770`) und blieb blockiert.
     Jetzt (Regel des Anwenders): **vor jedem Projektwechsel Datei -> Alle
     schliessen** (`_close_all`). Das Hauptmenue (TActionMainMenuBar) ist weder
     per UIA noch MSAA noch WM_COMMAND erreichbar, nur per Tastatur: Alt+D,
     300 ms warten, dann "h" ("Alle sc_h_liessen"), als echte Tastenereignisse
     (keybd_event) - pywinauto `type_keys("%dh")` in einem Zug liess das Menue
     offen. Speichern-Fragen: "Nein". Danach Strg+F11; verschluckt, ein zweiter
     Versuch nach Esc; sonst ein klarer Fehler. Den Rueckfall ueber die
     Dateizuordnung gibt es bei laufender IDE nicht mehr.
     `_answer_pending_prompts` beantwortet offene Abfragen vor und nach dem
     Aktivieren und waehrend `_wait_for_project`.
   - Bau-Ende: Solange das Fortschrittsfenster "Abbrechen" anbietet
     (`toplevel.build_running`), gilt der Bau als laufend, auch bei ruhiger
     Titelleiste - der CE-Lizenzhinweis kommt einige Sekunden nach dem Start
     und haelt den Bau an.
   Verifiziert: zwei Durchgaenge mit je fuenf Bauten (Bau, offene Unit
   geaendert und gleiches Projekt gebaut, zurueckgesetzt, geaendert und
   Projektwechsel, zurueck) - alle `status: ok`, keine Projektgruppe; dann mit
   "Alle schliessen" fuenf Bauten mit Projektwechseln, alle `status: ok`.
   Haengt die IDE doch einmal an "ProjectGroup1 speichern unter": Dialog
   abbrechen, Datei -> Alle schliessen (Speichern der Gruppe: Nein), Projekt
   neu oeffnen.

## Pilot-Empfehlung

Erstes echtes Projekt für Phase 4: **Camera-Range-Tool** oder ein abgegrenzter Teilbereich des **Combo Map Viewers**. Beide sind klein genug zum schnellen Iterieren, aber real genug um echten Wert zu liefern. SRT ist als Pilot zu groß — die Login-Flows und UniGUI-Server-Komponenten verlangen einen anderen Test-Ansatz (HTTP-basiert) und würden die Bridge in der Frühphase überfordern.

## Erweiterung um MCP (optional, Phase 6)

Sobald die Bridge stabil läuft, kann sie als lokaler MCP-Server gewrapped werden, sodass Claude Code sie nicht über Bash, sondern als nativen Tool-Call ansprechen kann. Empfohlene Tools:
- `delphi_build(dproj_path, config?, platform?)` → Diagnostics
- `delphi_inspect()` → IDE-State
- `delphi_run_tests(suite_path)` → Test-Results
- `delphi_screenshot(window_name?)` → Image (für Vision-API-Analyse durch Claude)

Das ist die Endausbau-Stufe und darf bis dahin ignoriert werden — der Bash-CLI-Pfad reicht für 95% der Workflows.
