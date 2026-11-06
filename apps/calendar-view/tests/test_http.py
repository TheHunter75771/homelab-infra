"""The HTTP surface (calendar_view/server.py): the security headers on every response, the exact route
table (404 for everything else, path traversal and its encoded forms included), 405 for every method but
GET and HEAD, and error bodies that never echo the request. Standard library only (a stub service)."""

from __future__ import annotations

import http.client
import json
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from calendar_view import server as srv

from .helpers import APP

CSP = "default-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
CANARY = "canary-config-must-never-be-served"


class StubService:
    def __init__(self) -> None:
        self.ok = True

    def agenda(self):
        return 200, {"generated": "2026-11-06T10:30:00-05:00", "stale": False, "stale_seconds": 3, "today": {},
                     "days": [], "title": "<script>alert(1)</script>"}

    def healthy(self) -> bool:
        return self.ok


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # The real page files, with a config.json planted beside them and one level up: neither is reachable.
        cls.tmp = Path(tempfile.mkdtemp(prefix="calendar-view-http-"))
        web = cls.tmp / "web"
        shutil.copytree(APP / "web", web)
        (web / "config.json").write_text(json.dumps({"password": CANARY}))
        (cls.tmp / "config.json").write_text(json.dumps({"password": CANARY}))
        cls.service = StubService()
        cls.server = srv.ViewServer(("127.0.0.1", 0), cls.service, static=srv.load_static(web))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, args=(0.05,), daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def request(self, method: str, path: str, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=False, skip_accept_encoding=True)
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            conn.endheaders()
            r = conn.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            conn.close()

    def raw(self, data: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(data)
            chunks = []
            while chunk := s.recv(65536):
                chunks.append(chunk)
        return b"".join(chunks)

    def assert_security_headers(self, headers: dict) -> None:
        self.assertEqual(headers.get("content-security-policy"), CSP)
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(headers.get("referrer-policy"), "no-referrer")


class Routes(Base):
    def test_the_page_and_its_files(self) -> None:
        for path, ctype, name in (("/", "text/html; charset=utf-8", "index.html"),
                                  ("/static/app.css", "text/css; charset=utf-8", "app.css"),
                                  ("/static/app.js", "text/javascript; charset=utf-8", "app.js")):
            with self.subTest(path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["content-type"], ctype)
                self.assertEqual(body, (APP / "web" / name).read_bytes())
                self.assert_security_headers(headers)
                self.assertEqual(headers["cache-control"], "no-cache")

    def test_the_api(self) -> None:
        status, headers, body = self.request("GET", "/api/agenda")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assert_security_headers(headers)
        self.assertEqual(json.loads(body)["stale_seconds"], 3)
        # A query string doesn't change the route.
        self.assertEqual(self.request("GET", "/api/agenda?t=1")[0], 200)

    def test_healthz(self) -> None:
        status, headers, body = self.request("GET", "/healthz")
        self.assertEqual((status, body, headers["cache-control"]), (200, b"ok\n", "no-store"))
        self.assert_security_headers(headers)
        self.service.ok = False
        try:
            status, headers, body = self.request("GET", "/healthz")
            self.assertEqual((status, body), (503, b"stale\n"))
            self.assert_security_headers(headers)
        finally:
            self.service.ok = True

    def test_head(self) -> None:
        for path in ("/", "/api/agenda", "/healthz", "/nope"):
            with self.subTest(path):
                get_status, _, get_body = self.request("GET", path)
                status, headers, body = self.request("HEAD", path)
                self.assertEqual((status, body), (get_status, b""))
                self.assertEqual(headers["content-length"], str(len(get_body)))
                self.assert_security_headers(headers)

    def test_everything_else_is_404(self) -> None:
        paths = [
            "/nope", "/index.html", "/static", "/static/", "/static/app.css/", "/static//app.css", "//static/app.css",
            "/STATIC/app.css", "/static/./app.css", "/static/app.css%00", "/static/app.js.map", "/api", "/api/",
            "/api/agenda/", "/healthz/", "/config.json", "/web/index.html", "/calendar_view/server.py",
            "/static/../config.json", "/static/../../config.json", "/static/%2e%2e/config.json",
            "/static/%2E%2E/config.json", "/static/..%2fconfig.json", "/static/%2e%2e%2fconfig.json",
            "/static/%252e%252e/config.json", "/static/..%5cconfig.json", "/static/..\\config.json",
            "/static/config.json", "/%2e%2e/config.json", "/static/%c0%ae%c0%ae/config.json", "/./", "/?",
            "http://127.0.0.1/static/app.js", "*",
        ]
        for path in paths:
            with self.subTest(path):
                status, headers, body = self.request("GET", path)
                if path == "/?":  # "/" with an empty query string is the page
                    self.assertEqual(status, 200)
                    continue
                self.assertEqual(status, 404)
                self.assertEqual(body, b"404 Not Found\n")
                self.assertNotIn(CANARY.encode(), body)
                self.assert_security_headers(headers)
                self.assertEqual(headers["cache-control"], "no-store")

    def test_errors_never_echo_the_request(self) -> None:
        status, _, body = self.request("GET", "/<script>alert(1)</script>")
        self.assertEqual((status, body), (404, b"404 Not Found\n"))


class Methods(Base):
    def test_only_get_and_head(self) -> None:
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "PROPFIND", "REPORT", "MKCALENDAR",
                       "PROPPATCH", "FOO"):
            for path in ("/", "/api/agenda", "/nope"):
                with self.subTest(method=method, path=path):
                    status, headers, body = self.request(method, path)
                    self.assertEqual(status, 405)
                    self.assertEqual(headers["allow"], "GET, HEAD")
                    self.assertEqual(body, b"405 Method Not Allowed\n")
                    self.assert_security_headers(headers)

    def test_malformed_requests_get_the_headers_too(self) -> None:
        for request in (b"GARBAGE\r\n\r\n", b"GET / HTTP/9.9\r\n\r\n", b"GET /\r\n"):
            with self.subTest(request):
                reply = self.raw(request)
                head = reply.split(b"\r\n\r\n", 1)[0].decode("latin-1").lower()
                self.assertTrue(head.startswith(("http/1.0 4", "http/1.0 5")), head)
                self.assertIn("content-security-policy: " + CSP.lower(), head)
                self.assertIn("x-content-type-options: nosniff", head)
                self.assertIn("referrer-policy: no-referrer", head)


class Static(unittest.TestCase):
    def test_the_route_table_is_exactly_the_page(self) -> None:
        self.assertEqual(set(srv.STATIC), {"/", "/static/app.css", "/static/app.js"})
        self.assertEqual(sorted(p.name for p in (APP / "web").iterdir()), ["app.css", "app.js", "index.html"])


if __name__ == "__main__":
    unittest.main()
