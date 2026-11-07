"""The HTTP layer (CONTRACTS §12.25.2, row F): stdlib ``http.server``, one request per
connection, JSON in and out.

Order of checks for every request, before any body is read:

1. **Peer:** the socket's peer address must be in ``allowed_peers`` (open-webui's pinned /32).
   Otherwise 403. Headers such as X-Forwarded-For are never consulted.
2. **Bearer secret:** exactly one ``Authorization: Bearer <token>`` header, compared in constant
   time. Otherwise 401.
3. **Route and method:** fixed patterns; the path is never decoded or used as a file path.
   No query strings. Otherwise 400/404/405.
4. **Body** (POST only): ``Content-Type: application/json``, a single ``Content-Length`` of at
   most 64 KiB (no chunked bodies), strict UTF-8, strict JSON (no duplicate keys, no NaN or
   Infinity, an object at the top). Otherwise 411/413/415/400.

Errors are ``{"error": "<short reason>"}`` with no stack trace; the detail of a 5xx goes to the
log. The log never holds a header, a body, a raw path or an unknown method word, so the token
cannot reach it even if a client puts it somewhere odd (and ``RedactingFormatter`` scrubs it as
a second line of defence).
"""
from __future__ import annotations

import json
import logging
import re
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from forge import ForgeError, is_request_id
from library import LibraryError

from .auth import BearerAuth, PeerCheck
from .service import ApiError, Service
from .text import message as clean_message

log = logging.getLogger("forge_api")

MAX_BODY = 64 * 1024
MAX_LINE = 8192
MAX_CONCURRENT = 16
MAX_REJECTING = 16          # refused peers being told 403 at once (a separate pool; never a slot)
REJECT_WAIT = 1.0           # seconds a refused peer gets to send its request line before the 403
SOCKET_TIMEOUT = 10.0       # each read/write on a connection
REQUEST_DEADLINE = 30.0     # a whole connection, start to finish
_IP_FREEBIND = getattr(socket, "IP_FREEBIND", 15)        # Linux: bind the bridge gateway even
_IPV6_FREEBIND = getattr(socket, "IPV6_FREEBIND", 78)    # before docker has created the bridge
_KNOWN_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"})

_RID = r"(?P<rid>[0-9a-z_-]{1,80})"
ROUTES: list[tuple[re.Pattern[str], str, dict[str, str]]] = [
    (re.compile(r"/requests"), "/requests", {"GET": "list", "POST": "create"}),
    (re.compile(rf"/requests/{_RID}"), "/requests/{rid}", {"GET": "detail"}),
    (re.compile(rf"/requests/{_RID}/tick-searches"), "/requests/{rid}/tick-searches", {"POST": "tick-searches"}),
    (re.compile(rf"/requests/{_RID}/tick-sources"), "/requests/{rid}/tick-sources", {"POST": "tick-sources"}),
    (re.compile(rf"/items/{_RID}/(?P<n>[1-9][0-9]{{0,3}})"), "/items/{rid}/{n}", {"GET": "item"}),
]

REASONS = {400: "bad request", 401: "unauthorized", 403: "forbidden", 404: "not found",
           405: "method not allowed", 408: "request timeout", 411: "length required",
           413: "body too large", 414: "request line too long", 415: "unsupported media type",
           431: "request headers too large", 500: "internal error", 501: "not implemented",
           503: "unavailable"}


class RedactingFormatter(logging.Formatter):
    """Replaces the token anywhere in a formatted record (message and traceback)."""

    def __init__(self, fmt: str, secrets: list[str]) -> None:
        super().__init__(fmt)
        self._secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        out = super().format(record)
        for s in self._secrets:
            out = out.replace(s, "[REDACTED]")
        return out


class App:
    """What the handlers share."""

    def __init__(self, cfg: dict[str, Any], token: str, service: Service | None = None, *,
                 max_body: int = MAX_BODY, socket_timeout: float = SOCKET_TIMEOUT,
                 request_deadline: float = REQUEST_DEADLINE, max_concurrent: int = MAX_CONCURRENT,
                 max_rejecting: int = MAX_REJECTING, reject_wait: float = REJECT_WAIT) -> None:
        self.cfg = cfg
        self.service = service or Service(cfg)
        self.peers = PeerCheck(cfg["allowed_peers"])
        self.auth = BearerAuth(token)
        self.max_body = max_body
        self.socket_timeout = socket_timeout
        self.request_deadline = request_deadline
        self.slots = threading.BoundedSemaphore(max_concurrent)
        self.rejects = threading.BoundedSemaphore(max_rejecting)
        self.reject_wait = reject_wait


def strict_json(data: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in items:
            if k in out:
                raise ValueError("duplicate key")
            out[k] = v
        return out

    def constant(name: str) -> Any:
        raise ValueError(f"{name} is not JSON")

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ApiError(400, "the body is not UTF-8") from None
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except RecursionError:
        raise ApiError(400, "malformed JSON: nested too deeply") from None
    except ValueError as exc:
        reason = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise ApiError(400, f"malformed JSON: {clean_message(reason)}") from None
    if not isinstance(value, dict):
        raise ApiError(400, "the body must be a JSON object")
    return value


def _media_type_ok(values: list[str]) -> bool:
    if len(values) != 1:
        return False
    parts = [p.strip().lower() for p in values[0].split(";")]
    if parts[0] != "application/json":
        return False
    return all(p in ("charset=utf-8", 'charset="utf-8"') for p in parts[1:] if p)


def linger_close(sock: socket.socket) -> None:
    """Half-close, then drop what the client still sends for a moment, so closing with unread
    request bytes does not reset the connection before the client reads the response."""
    try:
        sock.shutdown(socket.SHUT_WR)
        sock.settimeout(0.2)
        end = time.monotonic() + 0.5
        total = 0
        while total < 65536 and time.monotonic() < end:
            chunk = sock.recv(4096)
            if not chunk:
                break
            total += len(chunk)
    except OSError:
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "forge-api"
    sys_version = ""
    server: ForgeApiServer

    def setup(self) -> None:
        self.timeout = self.server.app.socket_timeout
        super().setup()

    def handle(self) -> None:
        self.close_connection = True
        self.handle_one_request()

    def handle_one_request(self) -> None:
        self.command, self.request_version, self.requestline = "-", "HTTP/1.0", ""
        self.route_label, self.rid_label, self._started = "-", None, time.monotonic()
        self._replied = False
        try:
            raw = self.rfile.readline(MAX_LINE + 1)
        except OSError:
            return
        if not raw:
            return
        if len(raw) > MAX_LINE:
            self._reply(414, {"error": REASONS[414]})
            return
        self.raw_requestline = raw
        try:
            if not self.parse_request():   # its refusals come back through send_error below
                return
        except OSError:
            return
        try:
            self._dispatch()
        except OSError:
            log.debug("connection failed", exc_info=True)

    # --------------------------------------------------------------- dispatch

    def _dispatch(self) -> None:
        app = self.server.app
        if not app.peers.allows(self.client_address[0]):
            self._reply(403, {"error": REASONS[403]})
            return
        if not app.auth.check(self.headers.get_all("Authorization") or []):
            self._reply(401, {"error": REASONS[401]}, [("WWW-Authenticate", "Bearer")])
            return
        target = self.path
        if not target.startswith("/") or "?" in target or "#" in target:
            self._reply(400, {"error": "bad request target (no query strings)"})
            return
        for pattern, label, methods in ROUTES:
            m = pattern.fullmatch(target)
            if m:
                break
        else:
            self._reply(404, {"error": REASONS[404]})
            return
        self.route_label = label
        params = m.groupdict()
        if is_request_id(params.get("rid") or ""):
            self.rid_label = params["rid"]
        action = methods.get(self.command)
        if action is None:
            self._reply(405, {"error": REASONS[405]}, [("Allow", ", ".join(sorted(methods)))])
            return
        try:
            body = self._read_body() if self.command == "POST" else None
        except ApiError as exc:
            self._reply(exc.status, {"error": exc.message})
            return
        except TimeoutError:
            self._reply(408, {"error": REASONS[408]})
            return
        except OSError:
            log.debug("connection failed while reading the body", exc_info=True)
            return
        try:
            code, result = self._run(app.service, action, params, body)
        except ApiError as exc:
            out = {"error": exc.message, **exc.extra}
            self._reply(exc.status, out)
            return
        except (ForgeError, LibraryError, OSError) as exc:
            log.warning("%s %s: the request's files could not be read: %s", self.command, label, exc)
            self._reply(409, {"error": "the request's files could not be read"})
            return
        except Exception:
            log.exception("%s %s failed", self.command, label)
            self._reply(500, {"error": REASONS[500]})
            return
        self._reply(code, result)

    @staticmethod
    def _run(service: Service, action: str, params: dict[str, str], body: Any) -> tuple[int, Any]:
        if action == "list":
            return 200, service.list_requests()
        if action == "create":
            return 201, service.create_request(body)
        if action == "detail":
            return 200, service.get_request(params["rid"])
        if action == "tick-searches":
            return 200, service.tick("searches", params["rid"], body)
        if action == "tick-sources":
            return 200, service.tick("sources", params["rid"], body)
        if action == "item":
            return 200, service.get_item(params["rid"], int(params["n"]))
        raise AssertionError(action)

    def _read_body(self) -> Any:
        if self.headers.get_all("Transfer-Encoding"):
            raise ApiError(411, "chunked bodies are not accepted; send Content-Length")
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1:
            raise ApiError(411, "exactly one Content-Length is required")
        text = lengths[0].strip()
        if not (text.isascii() and text.isdigit()) or len(text) > 12:
            raise ApiError(400, "bad Content-Length")
        n = int(text)
        if n > self.server.app.max_body:
            raise ApiError(413, f"the body is larger than {self.server.app.max_body} bytes")
        if not _media_type_ok(self.headers.get_all("Content-Type") or []):
            raise ApiError(415, "Content-Type must be application/json")
        data = self.rfile.read(n)
        if len(data) != n:
            raise ApiError(400, "the body is shorter than Content-Length")
        return strict_json(data)

    # --------------------------------------------------------------- replies

    def handle_expect_100(self) -> bool:
        return True  # no "100 Continue" before the peer and token checks; the client just sends

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        # http.server's own refusals (bad request line, header limits, bad version): JSON, and
        # never the request line it would otherwise echo.
        self._reply(code, {"error": REASONS.get(code, "bad request")})

    def _reply(self, code: int, body: Any, extra: list[tuple[str, str]] | tuple = ()) -> None:
        if self._replied:
            return
        self._replied = True
        self.close_connection = True
        data = (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self.send_response_only(code)
            self.send_header("Date", self.date_time_string())
            self.send_header("Server", self.server_version)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
        except OSError:
            log.debug("could not send the reply", exc_info=True)
        finally:
            self._log(code)

    def _log(self, code: int) -> None:
        method = self.command if self.command in _KNOWN_METHODS else "-"
        route = self.route_label + (f" rid={self.rid_label}" if self.rid_label else "")
        ms = int((time.monotonic() - self._started) * 1000)
        log.info("%s %s %s %d %dms", self.client_address[0], method, route, code, ms)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            linger_close(self.connection)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        pass  # _reply logs a safe line itself

    def log_message(self, format: str, *args: Any) -> None:
        pass  # http.server's own logging would print the raw request line


class ForgeApiServer(ThreadingHTTPServer):
    """Accept -> **peer check** -> a slot (at most ``max_concurrent`` connections) -> a handler
    thread. The peer is checked on accept, before any slot is taken (Reviewer Q, Q-F1): a refused
    peer is told 403 by a small separate pool that gives it at most ``reject_wait`` seconds to send
    its request line, or, with that pool full, is closed at once. So refused peers can never use
    up the service's slots. With every slot taken, an allowed connection is closed at once. Each
    served connection has an overall deadline after which its socket is shut down, so a client
    trickling bytes cannot hold a thread.

    ``sock``: an already bound, listening socket (systemd socket activation, see
    ``activation.py``); otherwise the server binds ``(host, port)`` itself."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, app: App, host: str, port: int, *, sock: socket.socket | None = None) -> None:
        self.app = app
        if sock is None:
            self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
            super().__init__((host, port), Handler)
            return
        self.address_family = sock.family
        super().__init__((host, port), Handler, bind_and_activate=False)
        self.socket.close()
        self.socket = sock
        self.server_address = sock.getsockname()
        self.server_name, self.server_port = host, self.server_address[1]

    def server_bind(self) -> None:
        if self.address_family == socket.AF_INET6:
            opt = (socket.IPPROTO_IPV6, _IPV6_FREEBIND)
        else:
            opt = (socket.SOL_IP, _IP_FREEBIND)
        try:
            self.socket.setsockopt(*opt, 1)
        except OSError:
            pass
        socketserver.TCPServer.server_bind(self)  # skip HTTPServer's reverse-DNS getfqdn()
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port

    def process_request(self, request: Any, client_address: Any) -> None:
        peer = client_address[0] if client_address else None
        if not self.app.peers.allows(peer):
            self._refuse_peer(request, peer)
            return
        if not self.app.slots.acquire(blocking=False):
            log.warning("busy: closing a connection from %s on accept", peer)
            self.shutdown_request(request)
            return
        timer = threading.Timer(self.app.request_deadline, self._expire, (request,))
        timer.daemon = True
        try:
            timer.start()
            threading.Thread(target=self._serve_one, args=(request, client_address, timer), daemon=True).start()
        except BaseException:
            timer.cancel()
            self.shutdown_request(request)
            self.app.slots.release()
            raise

    def _refuse_peer(self, request: socket.socket, peer: Any) -> None:
        if not self.app.rejects.acquire(blocking=False):
            log.warning("refused peer %s: closed on accept (reject pool full)", peer)
            self.shutdown_request(request)
            return
        try:
            threading.Thread(target=self._reject, args=(request, peer), daemon=True).start()
        except BaseException:
            self.app.rejects.release()
            self.shutdown_request(request)
            raise

    def _reject(self, request: socket.socket, peer: Any) -> None:
        """403 to a refused peer: read (and drop) at most its request head for ``reject_wait``
        seconds in total, so closing does not reset the connection before the client reads the
        answer; nothing it sent is parsed or logged."""
        try:
            deadline = time.monotonic() + self.app.reject_wait
            seen = b""
            while b"\r\n\r\n" not in seen and b"\n\n" not in seen and len(seen) < MAX_LINE:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                request.settimeout(left)
                try:
                    chunk = request.recv(4096)
                except (TimeoutError, OSError):
                    break
                if not chunk:
                    break
                seen = (seen + chunk)[-MAX_LINE:]
            body = b'{"error": "forbidden"}\n'
            request.settimeout(1.0)
            request.sendall(b"HTTP/1.0 403 Forbidden\r\nServer: forge-api\r\n"
                            b"Content-Type: application/json; charset=utf-8\r\n"
                            b"Content-Length: %d\r\nCache-Control: no-store\r\n"
                            b"X-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n%s" % (len(body), body))
            linger_close(request)
        except OSError:
            pass
        finally:
            log.info("%s - - 403 (peer refused on accept)", peer)
            self.shutdown_request(request)
            self.app.rejects.release()

    def _serve_one(self, request: Any, client_address: Any, timer: threading.Timer) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:
            log.debug("connection failed", exc_info=True)
        finally:
            timer.cancel()
            self.shutdown_request(request)
            self.app.slots.release()

    @staticmethod
    def _expire(request: socket.socket) -> None:
        log.debug("deadline: closing a connection")
        try:
            request.shutdown(socket.SHUT_RDWR)  # wakes a blocked read or write
        except OSError:
            pass

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.debug("connection from %s failed", client_address[0] if client_address else "-")
