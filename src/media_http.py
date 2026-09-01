"""Minimal dependency-free HTTP boundary for the Media I/O Harness service."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Mapping
from urllib.parse import parse_qs, urlparse

from .media_service import MediaRunConflictError, MediaRunService, MediaRunValidationError


def make_handler(service: MediaRunService, health_check: Callable[[], Mapping] | None = None):
    """Create a request handler bound to one service instance.

    ``health_check`` is the deployment probe (架构文档 12 P6): it returns a
    JSON-serializable mapping when healthy and raises otherwise.
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "PrismloopMediaHarness/0.1"

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/v1/media-runs":
                try:
                    request = self._read_json()
                    run_id = service.submit(request)
                    self._write_json(HTTPStatus.ACCEPTED, {"run_id": run_id})
                except (MediaRunValidationError, ValueError, json.JSONDecodeError) as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if parsed.path.startswith("/v1/media-runs/") and parsed.path.endswith(":cancel"):
                run_id = parsed.path.removeprefix("/v1/media-runs/").removesuffix(":cancel")
                try:
                    payload = self._read_json()
                    result = service.cancel(run_id, str(payload.get("reason", "canceled by caller")))
                    self._write_json(HTTPStatus.OK, result)
                except KeyError as exc:
                    self._write_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                except MediaRunConflictError as exc:
                    self._write_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "route not found"})

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                if health_check is None:
                    self._write_json(HTTPStatus.OK, {"status": "ok", "checks": {}})
                    return
                try:
                    payload = health_check()
                except Exception as exc:  # noqa: BLE001 - probe surfaces all failures
                    self._write_json(HTTPStatus.SERVICE_UNAVAILABLE, {"status": "error", "error": str(exc)})
                    return
                self._write_json(HTTPStatus.OK, payload)
                return
            if parsed.path == "/v1/media-capabilities":
                environment_ref = parse_qs(parsed.query).get("environment_ref", [""])[0]
                self._write_json(HTTPStatus.OK, {"capabilities": service.capabilities_for(environment_ref)})
                return
            if parsed.path.startswith("/v1/media-runs/"):
                run_id = parsed.path.removeprefix("/v1/media-runs/")
                try:
                    self._write_json(HTTPStatus.OK, service.get(run_id))
                except KeyError as exc:
                    self._write_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "route not found"})

        def _read_json(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise MediaRunValidationError("request body is required")
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _write_json(self, status: HTTPStatus, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    return Handler


def create_server(
    service: MediaRunService,
    host: str = "127.0.0.1",
    port: int = 8787,
    health_check: Callable[[], Mapping] | None = None,
) -> ThreadingHTTPServer:
    """Create, but do not start, the local service listener."""
    return ThreadingHTTPServer((host, port), make_handler(service, health_check))
