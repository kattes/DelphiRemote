"""Resident watcher daemon — keeps the UIA connection warm between requests.

Phase 6 deliverable. Usage:

    Terminal A:  delphi-remote watch
    Terminal B:  delphi-remote build path/to/Project.dproj   # routes via daemon

Protocol: JSON object per request, single-line, terminated by '\\n'. Server
writes a single JSON-line response, then closes the connection. Single-threaded
(the IDE itself is single-threaded; serializing requests matches that reality).
"""
from __future__ import annotations

import json
import logging
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import Any

from delphi_remote.ide_client import BridgeError, DelphiIDE

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17556

# How long a client should wait when probing whether a watcher exists.
PROBE_TIMEOUT_SECONDS = 0.2


# ── Server ──────────────────────────────────────────────────────────────────

class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        peer = self.client_address
        try:
            raw = self.rfile.readline()
        except Exception as e:
            self._send({"status": "protocol_error", "error": f"could not read: {e!r}"})
            return

        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            self._send({"status": "protocol_error", "error": "empty request"})
            return

        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            self._send({"status": "protocol_error", "error": f"invalid JSON: {e}"})
            return

        command = request.get("command")
        args = request.get("args") or {}
        log.info("watcher: %s -> %s args=%s", peer, command, args)
        try:
            response = self.server.dispatch(command, args)  # type: ignore[attr-defined]
        except Exception as e:
            log.exception("Handler error for command %r", command)
            response = {"status": "bridge_error", "error": repr(e)}
        self._send(response)

    def _send(self, obj: dict[str, Any]) -> None:
        try:
            payload = json.dumps(obj, ensure_ascii=False) + "\n"
            self.wfile.write(payload.encode("utf-8"))
        except Exception as e:
            log.warning("Could not write response: %r", e)


class WatcherServer(socketserver.TCPServer):
    allow_reuse_address = True

    def __init__(self, host: str, port: int, rules_path: Path) -> None:
        super().__init__((host, port), _Handler)
        self.rules_path = rules_path
        self.started_at = time.time()
        self.requests_handled = 0
        self.ide: DelphiIDE | None = None
        self._lock = threading.Lock()

    # Single dispatch lock — only one IDE operation at a time.
    def dispatch(self, command: str | None, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.requests_handled += 1
            if command == "ping":
                return {
                    "status": "ok", "pong": True,
                    "uptime_seconds": round(time.time() - self.started_at, 2),
                    "requests_handled": self.requests_handled,
                }
            if command == "shutdown":
                threading.Thread(target=self._shutdown_async, daemon=True).start()
                return {"status": "ok", "shutting_down": True}
            if command == "build":
                return self._do_build(args)
            if command == "inspect":
                return self._do_inspect()
            return {"status": "protocol_error",
                    "error": f"unknown command: {command!r}"}

    def _shutdown_async(self) -> None:
        time.sleep(0.1)
        self.shutdown()

    def _ensure_attached(self) -> DelphiIDE:
        """Return a known-good attached IDE; reattach if the connection went stale.

        Raises BridgeError if no IDE is running. Use _get_or_create_ide(strict=False)
        when the caller can launch the IDE itself (build path with auto_start).
        """
        if self.ide is not None and self.ide.is_attached():
            try:
                self.ide.read_main_window_title()
                return self.ide
            except BridgeError:
                log.warning("Cached IDE handle is stale; reattaching")
        ide = DelphiIDE()
        ide.attach()
        self.ide = ide
        return ide

    def _get_or_create_ide(self, *, strict: bool) -> DelphiIDE:
        """Return a DelphiIDE for the build path.

        - If strict=True, behaves like _ensure_attached (raises if no IDE).
        - If strict=False, returns a cached attached IDE if available, else a
          fresh unattached DelphiIDE that the caller (Builder.ensure_project_loaded)
          will attach or launch as needed.
        """
        if strict:
            return self._ensure_attached()
        if self.ide is not None and self.ide.is_attached():
            try:
                self.ide.read_main_window_title()
                return self.ide
            except BridgeError:
                log.warning("Cached IDE handle is stale; will recreate")
        return DelphiIDE()

    def _do_build(self, args: dict[str, Any]) -> dict[str, Any]:
        from delphi_remote.build import Builder
        from delphi_remote.watchdog import DialogWatchdog, load_rules

        dproj_arg = args.get("dproj")
        if not dproj_arg:
            return {"status": "protocol_error", "error": "missing 'dproj' arg"}
        dproj = Path(dproj_arg)
        if not dproj.is_file():
            return {"status": "bridge_error",
                    "error": f"dproj does not exist: {dproj}"}

        timeout = float(args.get("timeout", 180.0))
        build_config = args.get("config")
        target_platform = args.get("platform")
        auto_start = bool(args.get("auto_start", True))

        try:
            ide = self._get_or_create_ide(strict=not auto_start)
            rules = load_rules(self.rules_path)
            watchdog = DialogWatchdog(ide, rules)
            result = Builder(ide, watchdog=watchdog).build(
                dproj, timeout=timeout,
                build_config=build_config, target_platform=target_platform,
                auto_start=auto_start,
            )
            self.ide = ide  # cache the (now-attached) handle
        except BridgeError as e:
            return {"status": "bridge_error", "error": str(e)}

        payload = result.to_dict()
        payload.update({
            "dproj": str(dproj),
            "requested_config": build_config,
            "requested_platform": target_platform,
            "served_by": "watcher",
        })
        return payload

    def _do_inspect(self) -> dict[str, Any]:
        from delphi_remote.inspector import Inspector
        try:
            ide = self._ensure_attached()
            state = Inspector(ide).inspect()
        except BridgeError as e:
            return {"status": "bridge_error", "error": str(e)}
        return {"status": "ok", "served_by": "watcher", **state.to_dict()}


def serve(host: str, port: int, rules_path: Path) -> None:
    log.info("Starting watcher on %s:%d (rules=%s)", host, port, rules_path)
    with WatcherServer(host, port, rules_path) as server:
        server.serve_forever()


# ── Client ──────────────────────────────────────────────────────────────────

def send_request(
    command: str,
    args: dict[str, Any] | None = None,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    connect_timeout: float = PROBE_TIMEOUT_SECONDS,
    read_timeout: float = 300.0,
) -> dict[str, Any] | None:
    """Send a request to the watcher.

    Returns the parsed JSON response on success, or None if no watcher is
    listening (so callers can fall through to direct execution).
    """
    try:
        sock = socket.create_connection((host, port), timeout=connect_timeout)
    except (ConnectionRefusedError, TimeoutError, OSError):
        return None

    try:
        sock.settimeout(read_timeout)
        request = {"command": command, "args": args or {}}
        sock.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))

        chunks: list[bytes] = []
        while True:
            try:
                chunk = sock.recv(4096)
            except (ConnectionResetError, OSError):
                break
            if not chunk:
                break
            chunks.append(chunk)
        body = b"".join(chunks).decode("utf-8", errors="replace").strip()
        if not body:
            return {"status": "bridge_error",
                    "error": "watcher closed connection with no response"}
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            return {"status": "bridge_error",
                    "error": f"watcher returned malformed JSON: {e}",
                    "raw": body[:500]}
    finally:
        try:
            sock.close()
        except Exception:
            pass


def is_watcher_running(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    """Quick probe — true if a watcher answers ping within PROBE_TIMEOUT_SECONDS."""
    response = send_request("ping", host=host, port=port,
                            connect_timeout=PROBE_TIMEOUT_SECONDS,
                            read_timeout=PROBE_TIMEOUT_SECONDS)
    return bool(response and response.get("pong"))
