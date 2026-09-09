"""Loopback-only static frontend server for the daily-checkin web UI.

Serves the no-build frontend in ``web/`` on 127.0.0.1:8766. It has no API, no
credentials, no runner, and no filesystem write access — it only hands back the
whitelisted static assets (index.html, app.js, style.css) with the same
security headers the old combined server applied (CSP, no-store, nosniff).

Use with the separate API backend in ``checkin_core.web``::

    .venv/bin/python -m checkin_core.web --host 127.0.0.1 --port 8765
    .venv/bin/python scripts/serve-frontend.py --host 127.0.0.1 --port 8766

The frontend JS calls the API at ``API_BASE`` (default http://127.0.0.1:8765).
"""
from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "web"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "check.example.com"}
# Only these exact top-level basenames are ever served; anything else is 404.
ALLOWED_FILES = {"index.html", "app.js", "style.css", "favicon.svg", "favicon.ico"}
CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "favicon.svg": "image/svg+xml",
    "favicon.ico": "image/svg+xml",
}
# The frontend is served from this origin (8766) and calls the separate API
# backend on a different loopback port (8765 by default) or via public API sub-domain.
# With an opaque `connect-src 'self'` the browser would block cross-origin API fetch,
# so the CSP explicitly allows both the default local API and the check-api domain.
DEFAULT_API_BASE = "http://127.0.0.1:8765"
DEFAULT_LOCAL_DOMAIN_API_BASE = "http://check.example.com:8765 http://check-api.example.com:8765"
DEFAULT_PUBLIC_API_BASE = "https://check-api.example.com"
# Include the API origin in connect-src so the split frontend can reach it.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    f"connect-src 'self' {DEFAULT_API_BASE} {DEFAULT_LOCAL_DOMAIN_API_BASE} {DEFAULT_PUBLIC_API_BASE}; base-uri 'none'; frame-ancestors 'none'"
)


class FrontendHandler(BaseHTTPRequestHandler):
    server_version = "DailyCheckin-Frontend/1"

    def log_message(self, format: str, *args) -> None:
        print(f"frontend {self.address_string()} {format % args}", flush=True)

    @property
    def directory(self) -> Path:
        return Path(getattr(self.server, "frontend_dir", DEFAULT_DIR))

    def allowed_host(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
        return host in ALLOWED_HOSTS

    def _send_file(self, path: Path) -> None:
        raw = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", CONTENT_TYPES[path.name])
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _serve(self) -> None:
        if not self.allowed_host():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        path = unquote(urlparse(self.path).path)
        name = path.lstrip("/")
        # Treat "/" as the app entry so visiting the bare origin works.
        if not name:
            name = "index.html"
        # Treat "/favicon.ico" as alias to favicon.svg if ico not present as separate file
        if name == "favicon.ico" and not (self.directory / "favicon.ico").is_file():
            name = "favicon.svg"
        # Reject any traversal / nested path; only exact top-level files.
        if "/" in name or name not in ALLOWED_FILES:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        base = self.directory.resolve()
        file_path = self.directory / name
        if not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        # Defense-in-depth: the resolved file must stay inside the served dir.
        try:
            file_path.resolve().relative_to(base)
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send_file(file_path)

    def do_GET(self):
        self._serve()

    def do_HEAD(self):
        self._serve()


def serve(host: str = "127.0.0.1", port: int = 8766, directory: str | Path | None = None):
    if host not in ALLOWED_HOSTS:
        raise ValueError("daily-checkin frontend may bind only to loopback")
    frontend_dir = (Path(directory).resolve() if directory else DEFAULT_DIR)
    if not frontend_dir.is_dir():
        raise FileNotFoundError(f"frontend directory not found: {frontend_dir}")
    server = ThreadingHTTPServer((host, port), FrontendHandler)
    server.frontend_dir = frontend_dir
    print(f"Daily Check-in UI: http://{host}:{server.server_port} (dir {frontend_dir})", flush=True)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Loopback-only static frontend for daily-checkin")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--directory", default=None, help="Directory to serve (default: repo web/)")
    args = parser.parse_args()
    serve(args.host, args.port, args.directory)