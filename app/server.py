"""Local HTTP server for the web calculator.

Run with: python app/server.py --host 127.0.0.1 --port 8080

Serves static assets from this script's directory and a single JSON API
endpoint POST /api/calculate. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from calculator import calculate

MAX_BODY_BYTES = 4096

_ASSET_DIR = os.path.dirname(os.path.abspath(__file__))

_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def _read_asset(filename: str) -> bytes:
    path = os.path.join(_ASSET_DIR, filename)
    with open(path, "rb") as handle:
        return handle.read()


class CalculatorHandler(BaseHTTPRequestHandler):
    server_version = "Calculator/1.0"
    sys_version = ""

    # -- helpers ---------------------------------------------------------

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _path(self) -> str:
        return urlsplit(self.path).path

    # -- HTTP verbs ------------------------------------------------------

    def do_GET(self) -> None:
        path = self._path()
        asset = _ASSETS.get(path)
        if asset is None:
            self._send_error_json(404, "Not found")
            return
        filename, content_type = asset
        try:
            body = _read_asset(filename)
        except OSError:
            self._send_error_json(404, "Not found")
            return
        self._send_bytes(200, body, content_type)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        if self._path() != "/api/calculate":
            self._send_error_json(404, "Not found")
            return

        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else 0
        except (TypeError, ValueError):
            self._send_error_json(400, "Invalid Content-Length")
            return

        if length > MAX_BODY_BYTES:
            self._send_error_json(413, "Request body too large")
            return
        if length < 0:
            self._send_error_json(400, "Invalid Content-Length")
            return

        body = self.rfile.read(length) if length else b""

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(400, "Invalid JSON body")
            return

        if not isinstance(payload, dict):
            self._send_error_json(400, "JSON body must be an object")
            return

        expression = payload.get("expression")
        if not isinstance(expression, str):
            self._send_error_json(400, "Expression must be a string")
            return

        try:
            result = calculate(expression)
        except ValueError as exc:
            self._send_error_json(400, str(exc))
            return

        self._send_json(200, {"result": result})

    # -- quiet, body-free logging ---------------------------------------

    def log_message(self, fmt, *args) -> None:  # noqa: D401 - stdlib signature
        """Log only method and path, never request bodies."""
        message = fmt % args if args else fmt
        parts = message.split()
        method = parts[0] if parts else "-"
        path = parts[1] if len(parts) > 1 else "-"
        super().log_message("%s %s", method, path)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Local web calculator server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), CalculatorHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
