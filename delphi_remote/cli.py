"""Command-line entry point for the Delphi IDE Remote Bridge.

Output contract: JSON on stdout, logs on stderr. Exit codes:
  0 — success
  1 — build errors (project compiled but had errors/aborts)
  2 — bridge error (IDE not reachable, internal failure, not implemented)
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging on stderr.")
@click.version_option()
def main(verbose: bool) -> None:
    """Remote control bridge for the Delphi RAD Studio IDE."""
    _setup_logging(verbose)


@main.command()
@click.argument(
    "dproj",
    type=click.Path(exists=True, dir_okay=False, path_type=Path, resolve_path=True),
)
@click.option("--config", "build_config", default="Debug", show_default=True,
              help="Build configuration (e.g. Debug, Release). Phase 1: not yet applied.")
@click.option("--platform", "target_platform", default="Win32", show_default=True,
              help="Target platform (Win32, Win64). Phase 1: not yet applied.")
@click.option("--timeout", type=float, default=180.0, show_default=True,
              help="Build timeout in seconds.")
@click.option("--rules", "rules_path",
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Path to a custom watchdog rules YAML. "
                   "Walks up from the dproj for .delphi_remote.yaml if omitted.")
@click.option("--no-watchdog", is_flag=True,
              help="Disable the dialog watchdog entirely.")
def build(dproj: Path, build_config: str, target_platform: str, timeout: float,
          rules_path: Path | None, no_watchdog: bool) -> None:
    """Compile a Delphi project via the running IDE."""
    from delphi_remote.build import Builder
    from delphi_remote.ide_client import BridgeError, DelphiIDE
    from delphi_remote.watchdog import DialogWatchdog, default_rules_path, load_rules

    ide = DelphiIDE()
    try:
        ide.attach()
    except BridgeError as e:
        click.echo(json.dumps({"status": "bridge_error", "error": str(e)}))
        sys.exit(2)

    watchdog: DialogWatchdog | None = None
    if not no_watchdog:
        rules_file = rules_path or _resolve_rules_path(dproj)
        try:
            rules = load_rules(rules_file)
        except Exception as e:
            click.echo(json.dumps({
                "status": "bridge_error",
                "error": f"Could not load watchdog rules from {rules_file}: {e!r}",
            }))
            sys.exit(2)
        watchdog = DialogWatchdog(ide, rules)

    try:
        result = Builder(ide, watchdog=watchdog).build(
            dproj,
            timeout=timeout,
            build_config=build_config,
            target_platform=target_platform,
        )
    except BridgeError as e:
        click.echo(json.dumps({"status": "bridge_error", "error": str(e)}))
        sys.exit(2)

    payload = result.to_dict()
    payload.update({
        "dproj": str(dproj),
        "requested_config": build_config,
        "requested_platform": target_platform,
    })
    click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    sys.exit(0 if result.status == "ok" else 1 if result.status == "errors" else 2)


def _resolve_rules_path(dproj: Path) -> Path:
    """Walk up from the dproj looking for a project-local .delphi_remote.yaml.

    Falls back to the package's bundled default_rules.yaml.
    """
    from delphi_remote.watchdog import default_rules_path

    for parent in [dproj.parent, *dproj.parent.parents]:
        candidate = parent / ".delphi_remote.yaml"
        if candidate.is_file():
            return candidate
    return default_rules_path()


@main.command()
def inspect() -> None:
    """Report the current IDE state as JSON (active project, unit, build state)."""
    from delphi_remote.ide_client import BridgeError, DelphiIDE
    from delphi_remote.inspector import Inspector

    ide = DelphiIDE()
    try:
        ide.attach()
    except BridgeError as e:
        click.echo(json.dumps({"status": "bridge_error", "error": str(e)}))
        sys.exit(2)

    try:
        state = Inspector(ide).inspect()
    except BridgeError as e:
        click.echo(json.dumps({"status": "bridge_error", "error": str(e)}))
        sys.exit(2)

    payload = {"status": "ok", **state.to_dict()}
    click.echo(json.dumps(payload, indent=2, ensure_ascii=False))


@main.command("inspect-windows")
@click.option("--depth", type=int, default=4, show_default=True,
              help="Recursion depth (-1 for unlimited; the IDE tree is huge).")
@click.option("--max-children", type=int, default=50, show_default=True,
              help="Cap children per node (-1 for unlimited).")
@click.option("--root-class", "root_class", default=None,
              help="Scope dump to first element with this class name.")
@click.option("--root-name", "root_name", default=None,
              help="Scope dump to first element with this name (combine with --root-class).")
@click.option("--output", "output_path", type=click.Path(dir_okay=False, path_type=Path),
              help="Write JSON to file instead of stdout.")
def inspect_windows(depth: int, max_children: int, root_class: str | None,
                    root_name: str | None, output_path: Path | None) -> None:
    """Dump the bds.exe window tree as JSON (selector discovery helper)."""
    from delphi_remote.ide_client import BridgeError, DelphiIDE

    ide = DelphiIDE()
    try:
        handle = ide.attach()
    except BridgeError as e:
        click.echo(json.dumps({"status": "bridge_error", "error": str(e)}))
        sys.exit(2)

    try:
        if root_class is not None or root_name is not None:
            tree = ide.dump_subtree(
                class_name=root_class, name=root_name,
                depth=depth, max_children=max_children,
            )
            tree_summary: dict[str, object] = {"scoped_to": {
                "class_name": root_class, "name": root_name,
            }}
        else:
            tree = ide.dump_window_tree(depth=depth, max_children=max_children)
            tree_summary = {"top_level_window_count": tree.get("top_level_window_count", 0)}
    except BridgeError as e:
        click.echo(json.dumps({"status": "bridge_error", "error": str(e)}))
        sys.exit(2)

    payload = {
        "status": "ok",
        "handle": {
            "process_id": handle.process_id,
            "main_window_title": handle.main_window_title,
            "main_window_class": handle.main_window_class,
        },
        "tree": tree,
    }
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    if output_path is not None:
        output_path.write_text(rendered, encoding="utf-8")
        summary = {"status": "ok", "written_to": str(output_path),
                   "process_id": handle.process_id, **tree_summary}
        click.echo(json.dumps(summary))
    else:
        click.echo(rendered)


if __name__ == "__main__":
    main()
