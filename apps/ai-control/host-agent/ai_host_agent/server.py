"""HTTP API (CONTRACTS.md 12.15.3). GET only, bearer auth on every path, one request per
connection, at most `max_concurrent` requests at once (shared by every bind)."""

import json
import logging
import re
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl

from . import sources
from .redact import Redactor
from .runner import ExecError

log = logging.getLogger("ai_host_agent.http")

MAX_HEAD = 8192          # request line + headers, bytes, including the CRLFs
REJECT_WAIT = 1.0        # seconds a busy-rejected client gets to send its request
_IP_FREEBIND = getattr(socket, "IP_FREEBIND", 15)  # Linux
_METHOD = re.compile(r"[A-Z]{1,16}")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")

# path -> (allowed query keys, handler(agent, query) -> body). Every path needs the token.
ROUTES = {
    "/healthz": ((), lambda agent, q: {"ok": True}),
    "/v1/services": ((), lambda agent, q: sources.services(agent.cfg, agent.redactor)),
    "/v1/logs": (("unit", "lines"), lambda agent, q: sources.logs(agent.cfg, agent.redactor, q)),
    "/v1/ollama": ((), lambda agent, q: sources.ollama(agent.cfg, agent.redactor)),
    "/v1/sudo": (("user", "since", "limit"),
                 lambda agent, q: sources.sudo(agent.cfg, agent.redactor, q)),
    "/v1/disk": ((), lambda agent, q: sources.disk(agent.cfg, agent.statfs)),
}


class Agent:
    """What the handlers share: config, the token check, the redactor, statfs, the limits."""

    def __init__(self, cfg, token, auth):
        self.cfg = cfg
        self.auth = auth
        self.redactor = Redactor([token])  # the token is redacted from anything returned
        self.statfs = sources.StatFS(cfg["disk_timeout"])
        n = cfg["max_concurrent"]
        self.slots = threading.BoundedSemaphore(n)    # requests being served
        self.rejects = threading.BoundedSemaphore(n)  # clients being told 503
        self._lock = threading.Lock()
        self.active = 0

    def count(self, n):
        with self._lock:
            self.active += n


class HeadError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _head_end(data):
    """Index just past the blank line ending the head (CRLF, or bare LF), or -1."""
    ends = [i + n for i, n in ((data.find(b"\r\n\r\n"), 4), (data.find(b"\n\n"), 2))
            if i >= 0]
    return min(ends) if ends else -1


def read_head(sock, timeout):
    """Read the request line + headers (up to the blank line) within `timeout` seconds overall,
    so a client trickling bytes can't stretch it. None if the client sent nothing at all."""
    deadline = time.monotonic() + timeout
    data = b""
    while True:
        end = _head_end(data)
        if end >= 0:
            if end > MAX_HEAD:
                break
            return data[:end]
        if len(data) > MAX_HEAD:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HeadError(408, "request timeout")
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            raise HeadError(408, "request timeout") from None
        if not chunk:
            if data:
                raise HeadError(400, "bad request")
            return None
        data += chunk
    first = data.find(b"\n")
    if first < 0 or first + 1 > MAX_HEAD:
        raise HeadError(414, "request line too long")
    raise HeadError(431, "request headers too large")


def parse_head(head):
    """-> (method, target, [(lower-case name, value)]). Raises HeadError(400)."""
    lines = head.decode("latin-1").split("\n")
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in lines]
    parts = lines[0].split(" ")
    if len(parts) != 3 or not _METHOD.fullmatch(parts[0]) or \
            parts[2] not in ("HTTP/1.0", "HTTP/1.1"):
        raise HeadError(400, "bad request line")
    headers = []
    for ln in lines[1:]:
        if not ln:
            break
        name, sep, value = ln.partition(":")
        if not sep or not _HEADER_NAME.fullmatch(name):  # also refuses obs-fold continuations
            raise HeadError(400, "bad header")
        headers.append((name.lower(), value.strip(" \t")))
    return parts[0], parts[1], headers


def linger_close(sock):
    """Half-close, then read (and drop) what the client still sends for a moment, so closing
    with unread request bytes doesn't reset the connection before it reads the response."""
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


def _log_line(method, path, code):
    # Method, known path (never the query, never a header) and status. An unknown path is "-":
    # a client could put anything in it, the token included.
    log.info("%s %s %d", method if _METHOD.fullmatch(method or "") else "-",
             path if path in ROUTES else "-", code)


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-host-agent"
    sys_version = ""

    def setup(self):
        self.timeout = self.server.agent.cfg["conn_timeout"]  # per socket read/write
        super().setup()

    def handle(self):
        self.close_connection = True
        self.command, self.request_version, self.requestline = "-", "HTTP/1.0", ""
        self.route = "-"
        try:
            head = read_head(self.connection, self.server.agent.cfg["head_timeout"])
            if head is None:
                return  # connected and closed without a request
            method, target, headers = parse_head(head)
        except HeadError as e:
            self._reply(e.code, {"error": str(e)})
            return
        self.command = method
        self.requestline = "%s - HTTP/1.0" % method
        path, _, query = target.partition("?")
        self.route = path
        if method != "GET":
            self._reply(405, {"error": "method not allowed"}, [("Allow", "GET")])
            return
        if not path.startswith("/") or "#" in target:
            self._reply(400, {"error": "bad request target"})
            return
        agent = self.server.agent
        if not agent.auth.check([v for n, v in headers if n == "authorization"]):
            self._reply(401, {"error": "unauthorized"}, [("WWW-Authenticate", "Bearer")])
            return
        route = ROUTES.get(path)
        if route is None:
            self._reply(404, {"error": "not found"})
            return
        allowed, fn = route
        try:
            params = self._query(query, allowed)
            code, body = 200, agent.redactor.scrub(fn(agent, params))
        except sources.BadRequest as e:
            code, body = 400, {"error": str(e)}
        except ExecError as e:
            code, body = 502, {"error": str(e)}
        except Exception:  # noqa: BLE001 - one bad request must not matter
            log.exception("%s failed", path)
            code, body = 500, {"error": "internal error"}
        self._reply(code, body)

    @staticmethod
    def _query(query, allowed):
        try:
            pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=bool(query),
                              errors="strict", max_num_fields=8)
        except (ValueError, UnicodeDecodeError):
            raise sources.BadRequest("bad query string") from None
        params = {}
        for k, v in pairs:
            if k not in allowed:
                raise sources.BadRequest("unknown parameter (allowed: %s)"
                                         % (", ".join(allowed) or "none"))
            if k in params:
                raise sources.BadRequest("repeated parameter")
            params[k] = v
        return params

    def _reply(self, code, body, extra=()):
        data = b"" if self.command == "HEAD" else (json.dumps(body) + "\n").encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
        finally:
            _log_line(self.command, self.route, code)

    def finish(self):
        try:
            super().finish()
        finally:
            linger_close(self.connection)

    def log_request(self, code="-", size="-"):
        pass  # _reply logs method, path and status itself

    def log_message(self, fmt, *args):
        pass  # nothing from http.server's own logging (it would print the request line)


class AgentHTTPServer(ThreadingHTTPServer):
    """Accept -> a slot from the agent's shared semaphore -> a handler thread. With every slot
    taken, the client gets 503 from a small reject pool of the same size; with that full too,
    the connection is closed on accept. Thread count stays <= 2 x max_concurrent. Each served
    connection also has an overall `request_deadline`, after which its socket is shut down."""

    daemon_threads = True

    def __init__(self, host, port, agent):
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.agent = agent
        super().__init__((host, port), Handler)

    def process_request(self, request, client_address):
        agent = self.agent
        if not agent.slots.acquire(blocking=False):
            if agent.rejects.acquire(blocking=False):
                try:
                    threading.Thread(target=self._reject, args=(request,), daemon=True).start()
                except BaseException:
                    agent.rejects.release()
                    self.shutdown_request(request)
                    raise
            else:
                log.warning("busy: closing a connection on accept")
                self.shutdown_request(request)
            return
        agent.count(1)
        timer = threading.Timer(agent.cfg["request_deadline"], self._expire, (request,))
        timer.daemon = True
        try:
            timer.start()
            threading.Thread(target=self._serve_one, args=(request, client_address, timer),
                             daemon=True).start()
        except BaseException:
            timer.cancel()
            self._release(request)
            raise

    def _serve_one(self, request, client_address, timer):
        try:
            self.finish_request(request, client_address)
        except Exception:  # noqa: BLE001 - one bad connection must not matter
            self.handle_error(request, client_address)
        finally:
            timer.cancel()
            self._release(request)

    def _release(self, request):
        self.shutdown_request(request)
        self.agent.count(-1)
        self.agent.slots.release()

    def _reject(self, request):
        method, path = "-", "-"
        try:
            try:
                head = read_head(request, REJECT_WAIT)
                if head:
                    method, target, _ = parse_head(head)
                    path = target.partition("?")[0]
            except (HeadError, OSError):
                pass
            body = b'{"error": "busy"}\n'
            request.settimeout(1.0)
            request.sendall(b"HTTP/1.0 503 Service Unavailable\r\n"
                            b"Content-Type: application/json\r\n"
                            b"Content-Length: %d\r\n"
                            b"Cache-Control: no-store\r\nRetry-After: 1\r\nConnection: close\r\n"
                            b"\r\n%s" % (len(body), body))
            linger_close(request)
        except OSError:
            pass
        finally:
            _log_line(method, path, 503)
            self.shutdown_request(request)
            self.agent.rejects.release()

    @staticmethod
    def _expire(request):
        log.debug("deadline: closing a connection")
        try:
            request.shutdown(socket.SHUT_RDWR)  # wakes a blocked read or write
        except OSError:
            pass

    def handle_error(self, request, client_address):
        log.debug("connection failed", exc_info=True)

    def server_bind(self):
        # IP_FREEBIND: bind the tailnet address even if tailscaled hasn't assigned it yet (boot).
        try:
            self.socket.setsockopt(socket.SOL_IP, _IP_FREEBIND, 1)
        except OSError:
            pass
        socketserver.TCPServer.server_bind(self)  # skip HTTPServer's reverse-DNS getfqdn()
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port
