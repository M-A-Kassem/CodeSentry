"""VS Code diagnostics bridge: a local endpoint for the companion extension.

The companion extension (``vscode-extension/`` in this repository) reads
diagnostics through VS Code's official ``vscode.languages`` API -- the same
data shown in the Problems panel -- and POSTs a full snapshot here whenever
they change, plus a periodic heartbeat.

Security of the local interface:

- Bound to ``127.0.0.1`` only; never reachable from the network.
- Every request needs a random bearer token, regenerated on every start and
  published (with the port) in a discovery file under the user's own
  ``%LOCALAPPDATA%`` folder, which other Windows users cannot read.
- Requests with an ``Origin`` header (i.e. from a web page in a browser)
  and requests whose ``Host`` is not the loopback address are rejected, so
  a malicious website cannot talk to the bridge (DNS rebinding / CSRF).
- Bodies are size-limited and strictly validated; the bridge only ever
  *receives* diagnostics. It never sends source code anywhere, and the
  extension sends diagnostic metadata only, never file contents.
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

from ... import config as config_module
from ..diagnostic import Diagnostic
from .base import STATE_ACTIVE, STATE_ERROR, STATE_IDLE, Provider

_logger = logging.getLogger("screenshot_logger")

PROTOCOL_VERSION = 1
DISCOVERY_FILE = config_module.USER_STATE_DIR / "vscode-bridge.json"
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_DIAGNOSTICS = 5000
MAX_MESSAGE_CHARS = 2000
STALE_AFTER_SECONDS = 45.0  # extension heartbeats every 15 s
FORGET_AFTER_SECONDS = 300.0


class PayloadError(ValueError):
    pass


def _opt_str(value: Any, limit: int = 1024) -> Optional[str]:
    if value is None or value == "":
        return None
    return str(value)[:limit]


def _opt_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_snapshot(payload: Any) -> tuple[str, dict[str, Any], list[Diagnostic]]:
    """Validate a ``/v1/diagnostics`` body.

    Returns ``(client_id, client_info, diagnostics)``. Raises
    :class:`PayloadError` for anything malformed. Individual bad entries are
    skipped rather than rejecting the whole snapshot.
    """
    if not isinstance(payload, dict):
        raise PayloadError("body must be a JSON object")
    client = payload.get("client")
    if not isinstance(client, dict) or not client.get("id"):
        raise PayloadError("client.id is required")
    client_id = str(client["id"])[:200]
    raw_diagnostics = payload.get("diagnostics", [])
    if not isinstance(raw_diagnostics, list):
        raise PayloadError("diagnostics must be a list")

    app_key = _opt_str(client.get("app"), 50) or "vscode"
    diagnostics: list[Diagnostic] = []
    for entry in raw_diagnostics[:MAX_DIAGNOSTICS]:
        if not isinstance(entry, dict) or not entry.get("message"):
            continue
        diagnostics.append(
            Diagnostic(
                severity=str(entry.get("severity", "error")),
                message=str(entry["message"])[:MAX_MESSAGE_CHARS],
                file=_opt_str(entry.get("file")),
                line=_opt_int(entry.get("line")),
                column=_opt_int(entry.get("column")),
                source=_opt_str(entry.get("source"), 100),
                code=_opt_str(entry.get("code"), 100),
                language=_opt_str(entry.get("language"), 50),
                application=app_key,
            )
        )
    info = {
        "app_name": _opt_str(client.get("appName"), 100) or "VS Code",
        "workspace": _opt_str(client.get("workspace"), 300),
        "focused": bool(client.get("focused")),
        "active_file": _opt_str(payload.get("activeFile")),
    }
    return client_id, info, diagnostics


class _ExclusiveServer(ThreadingHTTPServer):
    """Refuses to share its port.

    The stdlib server sets SO_REUSEADDR, which on Windows lets *another*
    process bind the same port and intercept the extension's requests
    (token included). SO_EXCLUSIVEADDRUSE forbids that.
    """

    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


class _Handler(BaseHTTPRequestHandler):
    server_version = "ErrorLoggerBridge/1"
    bridge: "VSCodeBridgeProvider"  # set on the subclass created per server

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        _logger.debug("VS Code bridge: " + format, *args)

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        if self.command == "POST" and not getattr(self, "_body_consumed", False):
            # Replying without reading the request body makes Windows reset
            # the connection, so the client would never see this status.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if 0 < length <= MAX_BODY_BYTES:
                self.rfile.read(length)
            else:
                self.close_connection = True
            self._body_consumed = True
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._reply(403, {"error": "bad host"})
            return False
        if self.headers.get("Origin"):
            self._reply(403, {"error": "browser requests are not accepted"})
            return False
        supplied = self.headers.get("Authorization") or ""
        expected = f"Bearer {self.bridge.token}"
        if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            self._reply(401, {"error": "bad token"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if not self._authorized():
            return
        if self.path == "/v1/health":
            self._reply(200, {"ok": True, "protocol": PROTOCOL_VERSION})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if not self._authorized():
            return
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self._reply(415, {"error": "expected application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY_BYTES:
            self._reply(413, {"error": "body missing or too large"})
            return
        try:
            raw = self.rfile.read(length)
            self._body_consumed = True
            payload = json.loads(raw.decode("utf-8"))
            if self.path == "/v1/diagnostics":
                count = self.bridge.receive_snapshot(payload)
                self._reply(200, {"ok": True, "accepted": count})
            elif self.path == "/v1/goodbye":
                self.bridge.receive_goodbye(payload)
                self._reply(200, {"ok": True})
            else:
                self._reply(404, {"error": "not found"})
        except (UnicodeDecodeError, json.JSONDecodeError, PayloadError) as exc:
            self._reply(400, {"error": str(exc)})
        except Exception:
            _logger.warning("VS Code bridge request failed.", exc_info=True)
            self._reply(500, {"error": "internal error"})


class VSCodeBridgeProvider(Provider):
    name = "vscode"
    display_name = "VS Code (extension)"

    def __init__(
        self,
        port: int = 0,
        discovery_file: Optional[Path] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self.requested_port = port
        self.discovery_file = discovery_file or DISCOVERY_FILE
        self._clock = clock
        self.token = ""
        self.port = 0
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._clients: dict[str, dict[str, Any]] = {}
        self._clients_lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------

    def start(self, tracker) -> None:
        super().start(tracker)
        self.token = secrets.token_urlsafe(32)
        handler = type("BridgeHandler", (_Handler,), {"bridge": self})
        try:
            self._server = _ExclusiveServer(("127.0.0.1", self.requested_port), handler)
        except OSError as exc:
            self.set_status(STATE_ERROR, f"could not listen on port {self.requested_port}: {exc}")
            raise
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="vscode-bridge", daemon=True)
        self._thread.start()
        self._write_discovery_file()
        self.set_status(STATE_IDLE, f"listening on 127.0.0.1:{self.port}; waiting for the extension")
        _logger.info("VS Code bridge listening on 127.0.0.1:%s", self.port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        try:
            self.discovery_file.unlink(missing_ok=True)
        except OSError:
            _logger.debug("Could not remove bridge discovery file.", exc_info=True)
        with self._clients_lock:
            self._clients.clear()
        super().stop()

    def _write_discovery_file(self) -> None:
        self.discovery_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.discovery_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"protocol": PROTOCOL_VERSION, "port": self.port, "token": self.token}),
            encoding="utf-8",
        )
        tmp.replace(self.discovery_file)

    # -- requests (called on server threads) ------------------------------

    def receive_snapshot(self, payload: Any) -> int:
        client_id, info, diagnostics = parse_snapshot(payload)
        with self._clients_lock:
            self._clients[client_id] = {**info, "last_seen": self._clock()}
        if self.tracker is not None:
            self.tracker.update(self.name, client_id, diagnostics)
        self._refresh_status()
        return len(diagnostics)

    def receive_goodbye(self, payload: Any) -> None:
        if not isinstance(payload, dict) or not isinstance(payload.get("client"), dict):
            raise PayloadError("client is required")
        client_id = str(payload["client"].get("id", ""))[:200]
        with self._clients_lock:
            self._clients.pop(client_id, None)
        if self.tracker is not None:
            self.tracker.clear_scope(self.name, client_id)
        self._refresh_status()

    # -- housekeeping (controller thread) -----------------------------------

    def on_context(self, foreground, running) -> None:
        self.expire_clients()

    def expire_clients(self) -> None:
        """Mark silent windows stale, and forget ones gone for good."""
        now = self._clock()
        with self._clients_lock:
            clients = list(self._clients.items())
        for client_id, info in clients:
            age = now - info["last_seen"]
            if age > FORGET_AFTER_SECONDS:
                with self._clients_lock:
                    self._clients.pop(client_id, None)
                if self.tracker is not None:
                    self.tracker.clear_scope(self.name, client_id)
            elif age > STALE_AFTER_SECONDS and self.tracker is not None:
                self.tracker.mark_stale(self.name, client_id)
        self._refresh_status()

    def active_paths(self) -> list[str]:
        clients = sorted(self.connected_clients().values(), key=lambda c: (c.get("focused", False), c["last_seen"]), reverse=True)
        return [c["active_file"] for c in clients if c.get("active_file")]

    def connected_clients(self) -> dict[str, dict[str, Any]]:
        with self._clients_lock:
            return {key: dict(value) for key, value in self._clients.items()}

    def _refresh_status(self) -> None:
        if self._server is None:
            return
        now = self._clock()
        live = [c for c in self.connected_clients().values() if now - c["last_seen"] <= STALE_AFTER_SECONDS]
        if live:
            names = ", ".join(sorted({c.get("workspace") or c["app_name"] for c in live}))
            self.set_status(STATE_ACTIVE, f"{len(live)} window(s) connected: {names}")
        else:
            self.set_status(STATE_IDLE, f"listening on 127.0.0.1:{self.port}; waiting for the extension")
