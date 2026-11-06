"""HTTP: the page, its two static files, /api/agenda and /healthz. Read-only; no auth (it listens on
loopback only, and tailscale serve publishes it to the tailnet).

- Routing is an exact match of the request target as sent (the query string dropped) against a fixed
  table.
  Nothing is ever looked up on disk per request: the three page files are read once at start. So
  /static/../config.json, its percent-encoded forms, //static/app.js, /static/app.js/ and the like are
  all 404, and there is no directory listing.
- GET and HEAD only; any other method is 405 with `Allow: GET, HEAD`.
- Every response, errors included, carries the CONTRACTS §14.2 security headers. Error bodies are a
  fixed status line and never echo the request.
"""

from __future__ import annotations

import json
import logging
import socket
import socketserver
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("calendar_view.http")

CSP = "default-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
SECURITY_HEADERS = (
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)
# Request path -> (file in the web directory, Content-Type). The whole static surface.
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
ALLOWED_METHODS = "GET, HEAD"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def load_static(web_dir: Path = WEB_DIR) -> dict[str, tuple[bytes, str]]:
    return {route: ((web_dir / name).read_bytes(), ctype) for route, (name, ctype) in STATIC.items()}


class Handler(BaseHTTPRequestHandler):
    server_version = "calendar-view"
    sys_version = ""
    timeout = 10  # seconds per socket operation (StreamRequestHandler applies it)

    # --- plumbing ---------------------------------------------------------------------------------------
    def end_headers(self) -> None:
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    def _respond(self, status: int, body: bytes, content_type: str, cache: str, extra=()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None) -> None:
        """Also used by http.server itself (400, 414, 431, 501): a fixed body with the security headers."""
        self.close_connection = True
        status = HTTPStatus(code)
        extra = (("Allow", ALLOWED_METHODS),) if code == HTTPStatus.METHOD_NOT_ALLOWED else ()
        self._respond(code, f"{code} {status.phrase}\n".encode(), "text/plain; charset=utf-8", "no-store", extra)

    def parse_request(self) -> bool:
        ok = super().parse_request()
        # The request target exactly as sent: http.server rewrites a leading "//" to "/" in self.path.
        words = self.requestline.split()
        self.target = words[1] if len(words) >= 2 else ""
        if ok and self.request_version == "HTTP/0.9":
            # An HTTP/0.9 answer has no status line and no headers at all: refuse it with a 1.0 one instead.
            self.request_version = "HTTP/1.0"
            self.send_error(HTTPStatus.BAD_REQUEST)
            return False
        return ok

    def __getattr__(self, name: str):
        # http.server dispatches to do_<METHOD> and answers 501 when there is none: every other method is 405.
        if name.startswith("do_"):
            return self._not_allowed
        raise AttributeError(name)

    def _not_allowed(self) -> None:
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    def log_message(self, format, *args) -> None:
        log.debug("%s %s", self.client_address[0], format % args)

    # --- routes -----------------------------------------------------------------------------------------
    def do_GET(self) -> None:
        path = self.target.split("?", 1)[0]
        if path in self.server.static:
            body, content_type = self.server.static[path]
            self._respond(200, body, content_type, "no-cache")
        elif path == "/api/agenda":
            status, payload = self.server.service.agenda()
            body = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            self._respond(status, body, "application/json; charset=utf-8", "no-store")
        elif path == "/healthz":
            ok = self.server.service.healthy()
            self._respond(200 if ok else 503, b"ok\n" if ok else b"stale\n", "text/plain; charset=utf-8", "no-store")
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:
        self.do_GET()


class ViewServer(ThreadingHTTPServer):
    """At most `max_handlers` connections at once; more are closed on accept."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], service, static: dict | None = None, max_handlers: int = 32):
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.service = service
        self.static = load_static() if static is None else static
        self._slots = threading.BoundedSemaphore(max_handlers)
        super().__init__(address, Handler)

    def server_bind(self) -> None:
        # HTTPServer.server_bind does a reverse DNS lookup (getfqdn) of the listen address: not needed.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            log.debug("busy: dropping a connection")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
