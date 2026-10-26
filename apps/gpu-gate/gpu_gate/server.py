"""HTTP API: GET /status, GET /healthz. Read-only, no auth (exposure is limited by the firewall)."""

import json
import logging
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("gpu_gate.http")

_IP_FREEBIND = getattr(socket, "IP_FREEBIND", 15)  # Linux


class Handler(BaseHTTPRequestHandler):
    server_version = "gpu-gate"
    sys_version = ""

    def setup(self):
        self.timeout = self.server.conn_timeout  # StreamRequestHandler applies it to the socket
        super().setup()

    def _json(self, code, body):
        data = (json.dumps(body) + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 (http.server naming)
        path = self.path.split("?", 1)[0]
        gate = self.server.gate
        if path == "/status":
            st = gate.status()
            if st is None:
                self._json(503, {"error": "no sample yet"})
            elif not gate.healthy():  # same rule as /healthz: never serve a stale answer
                self._json(503, {"error": "stale", "ts": st["ts"]})
            else:
                self._json(200, st)
        elif path == "/healthz":
            ok = gate.healthy()
            self._json(200 if ok else 503, {"ok": ok})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):  # the broker polls every few seconds: keep it quiet
        log.debug("%s %s", self.address_string(), fmt % args)


class GateHTTPServer(ThreadingHTTPServer):
    """At most `max_handlers` connections are served at once; more are closed on accept.
    Each connection gets `conn_timeout` seconds per socket operation and `request_deadline`
    seconds overall, from accept through response (CONTRACTS.md 12.9): when the deadline
    passes the socket is shut down, so a client trickling one byte per read can't hold a slot."""

    daemon_threads = True

    def __init__(self, host, port, gate, max_handlers=16, conn_timeout=5.0, request_deadline=10.0):
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.gate = gate
        self.conn_timeout = conn_timeout
        self.request_deadline = request_deadline
        self._slots = threading.BoundedSemaphore(max_handlers)
        self._active_lock = threading.Lock()
        self.active = 0
        super().__init__((host, port), Handler)

    def _count(self, n):
        with self._active_lock:
            self.active += n

    def process_request(self, request, client_address):
        """Called right after accept: take a slot, arm the deadline, serve in a thread."""
        if not self._slots.acquire(blocking=False):
            log.debug("busy: dropping connection from %s", client_address[0])
            self.shutdown_request(request)
            return
        self._count(1)
        timer = threading.Timer(self.request_deadline, self._expire, (request, client_address))
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
        self._count(-1)
        self._slots.release()

    @staticmethod
    def _expire(request, client_address):
        log.debug("deadline: closing connection from %s", client_address[0])
        try:
            request.shutdown(socket.SHUT_RDWR)  # wakes the handler's blocked read with EOF
        except OSError:
            pass  # already closed

    def handle_error(self, request, client_address):
        log.debug("connection from %s failed", client_address[0], exc_info=True)

    def server_bind(self):
        # IP_FREEBIND: bind the tailnet address even if tailscaled hasn't assigned it yet (boot).
        try:
            self.socket.setsockopt(socket.SOL_IP, _IP_FREEBIND, 1)
        except OSError:
            pass
        socketserver.TCPServer.server_bind(self)  # skip HTTPServer's reverse-DNS getfqdn()
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port
