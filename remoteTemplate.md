<!--
  TEMPLATE — copy this file to any new Delphi project as `CLAUDE.md`.
  Replace every `<…>` placeholder with the actual project value.
  Delete this comment block once the file is customized.
-->

# <PROJECT_NAME>

<ONE_LINE_DESCRIPTION — e.g. "Delphi 12 CE VCL application — does X.">

## Tooling: delphi_remote bridge

Build, diagnose, and inspect this project from Claude Code via the
**Delphi IDE Remote Bridge**:

- Source / docs: `C:\Users\Katte\Projects\delphi_remote\` (and on GitHub at
  https://github.com/kattes/DelphiRemote)
- Architecture, phases, dialog rule format, and known caveats live in that
  repo's `CLAUDE.md` — read it once if anything bridge-related is unclear.

### Standard commands

```powershell
# Build this project (auto-launches the IDE if not running)
delphi-remote build <ABSOLUTE_PATH_TO_DPROJ>

# Optional: explicit configuration / platform
delphi-remote build <ABSOLUTE_PATH_TO_DPROJ> --config Debug --platform Win32

# Inspect current IDE state (active project, unit, build markers)
delphi-remote inspect

# Resident watcher daemon (run in a separate terminal for ~1.5s less
# startup overhead per call)
delphi-remote watch
```

Replace `<ABSOLUTE_PATH_TO_DPROJ>` with this project's `.dproj` path —
the bridge is project-agnostic and accepts any absolute dproj path.

### Per-project dialog rules (optional)

If this project triggers IDE dialogs the default watchdog rules don't
cover (e.g. project-specific "out of date" prompts), drop a
`.delphi_remote.yaml` next to the `.dproj`. The bridge walks up from the
dproj path and merges the project-local file over the bundled defaults.
Format and examples are in `default_rules.yaml` inside the bridge repo.

## Project notes

<!--
  Project-specific guidance for Claude Code: coding style for this
  codebase, important units, non-obvious build configurations, notable
  third-party components (WebView2, UniGUI, etc.), known quirks, links
  to issue trackers or design docs. Keep this short and accurate; rot
  is worse than absence.
-->

- Main project file: `<NAME>.dproj`
- Main unit: `<UnitMain>.pas` / `.dfm`
- Build configurations used: <Debug, Release, …>
- Target platform(s): <Win32, Win64, …>
- Notable dependencies / third-party: <…>
- Coding conventions specific to this project: <…>

## Anything bridge-related belongs upstream

If you discover a new IDE dialog the bridge should auto-dismiss, a
shortcut quirk, or a watcher edge case — that fix goes into the bridge
repo (`delphi_remote/`), not into this file. This `CLAUDE.md` is for
**this project**, not for tooling that applies to all Delphi projects.
