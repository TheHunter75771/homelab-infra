import json
import logging
import socket
import threading
import time
import unittest

from ai_host_agent.server import MAX_HEAD, ROUTES

from .fakes import TOKEN, Env, FakeOllama, Running, wait_for


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


class ServerTest(unittest.TestCase):
    cfg_over = {}

    def setUp(self):
        self.env = Env()
        self.ollama = FakeOllama()
        self.cfg = self.env.cfg(ollama_url="http://127.0.0.1:%d" % self.ollama.port,
                                **self.cfg_over)
        self.run_ = Running(self.cfg)
        self.logs = LogCapture()
        self.logger = logging.getLogger("ai_host_agent")
        self.logger.addHandler(self.logs)
        self.old_level = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.socks = []

    def tearDown(self):
        for s in self.socks:
            s.close()
        self.logger.removeHandler(self.logs)
        self.logger.setLevel(self.old_level)
        self.run_.close()
        self.ollama.close()
        self.env.cleanup()

    def idle(self):
        s = socket.create_connection(("127.0.0.1", self.run_.port), timeout=5)
        self.socks.append(s)
        return s

    def status_of(self, raw):
        return int(raw.split(b" ", 2)[1]) if raw.startswith(b"HTTP/") else None


class AuthTest(ServerTest):
    def test_every_path_needs_the_token(self):
        for path in list(ROUTES) + ["/nope", "/v1/logs?unit=ollama"]:
            for token in (None, "wrong-token", TOKEN[:-1], ""):
                with self.subTest(path=path, token=token):
                    code, headers, body = self.run_.get(path, token=token)
                    self.assertEqual(code, 401)
                    self.assertEqual(headers["WWW-Authenticate"], "Bearer")
                    self.assertEqual(json.loads(body), {"error": "unauthorized"})
        self.assertEqual(self.env.calls(), [])  # nothing ran for an unauthenticated client

    def test_right_token(self):
        code, headers, body = self.run_.get("/healthz")
        self.assertEqual((code, json.loads(body)), (200, {"ok": True}))
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.run_.get("/nope")[0], 404)  # 404 only after auth

    def test_other_schemes_and_duplicate_headers(self):
        for auth in ("Basic " + TOKEN, TOKEN, "Bearer  " + TOKEN + " x"):
            raw = self.run_.raw(b"GET /healthz HTTP/1.1\r\nAuthorization: %s\r\n\r\n"
                                % auth.encode())
            self.assertEqual(self.status_of(raw), 401, auth)
        two = ("GET /healthz HTTP/1.1\r\nAuthorization: Bearer %s\r\n"
               "Authorization: Bearer %s\r\n\r\n" % (TOKEN, TOKEN)).encode()
        self.assertEqual(self.status_of(self.run_.raw(two)), 401)


class MethodTest(ServerTest):
    def test_only_get(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "FOO"):
            for token in (TOKEN, None):
                with self.subTest(method=method, token=token):
                    code, headers, body = self.run_.get("/v1/services", method=method,
                                                        token=token, data=b"{}")
                    self.assertEqual(code, 405)
                    self.assertEqual(headers["Allow"], "GET")
                    self.assertEqual(json.loads(body), {"error": "method not allowed"})
        raw = self.run_.raw(b"HEAD /healthz HTTP/1.1\r\nAuthorization: Bearer %s\r\n\r\n"
                            % TOKEN.encode())
        self.assertEqual(self.status_of(raw), 405)
        self.assertEqual(self.env.calls(), [])

    def test_malformed_requests_are_400(self):
        auth = b"Authorization: Bearer " + TOKEN.encode() + b"\r\n"
        for bad in (b"GET /healthz\r\n\r\n", b"GET /healthz HTTP/2.0\r\n\r\n",
                    b"get /healthz HTTP/1.1\r\n\r\n", b"GET  /healthz HTTP/1.1\r\n\r\n",
                    b"GET /healthz HTTP/1.1\r\nBad Header\r\n\r\n",
                    b"GET /healthz HTTP/1.1\r\n" + auth + b" folded\r\n\r\n",
                    b"GET http://x/healthz HTTP/1.1\r\n" + auth + b"\r\n",
                    b"GET /healthz#x HTTP/1.1\r\n" + auth + b"\r\n"):
            with self.subTest(bad=bad[:40]):
                self.assertEqual(self.status_of(self.run_.raw(bad)), 400)


class HeaderLimitTest(ServerTest):
    def head(self, size, line_pad=0):
        """A GET /healthz head of exactly `size` bytes (padding in an X-Pad header)."""
        start = b"GET /healthz" + b"?" * 0 + b" HTTP/1.1\r\nAuthorization: Bearer " + \
            TOKEN.encode() + b"\r\nX-Pad: "
        if line_pad:
            start = b"GET /healthz?" + b"a" * line_pad + b" HTTP/1.1\r\n"
            return start + b"\r\n"
        return start + b"a" * (size - len(start) - 4) + b"\r\n\r\n"

    def test_exactly_8k_is_served(self):
        h = self.head(MAX_HEAD)
        self.assertEqual(len(h), MAX_HEAD)
        self.assertEqual(self.status_of(self.run_.raw(h)), 200)

    def test_one_byte_over_is_431(self):
        h = self.head(MAX_HEAD + 1)
        self.assertEqual(len(h), MAX_HEAD + 1)
        out = self.run_.raw(h)
        self.assertEqual(self.status_of(out), 431)

    def test_huge_headers_431_without_reading_them_all(self):
        s = self.idle()
        s.sendall(b"GET /healthz HTTP/1.1\r\n" + b"X-A: " + b"a" * 20000)
        out = s.recv(65536)
        self.assertEqual(self.status_of(out), 431)

    def test_long_request_line_is_414(self):
        out = self.run_.raw(self.head(0, line_pad=MAX_HEAD + 100))
        self.assertEqual(self.status_of(out), 414)

    def test_never_ending_line_is_414(self):
        s = self.idle()
        s.sendall(b"GET /" + b"a" * (MAX_HEAD + 10))
        self.assertEqual(self.status_of(s.recv(65536)), 414)


class SlowHeadTest(ServerTest):
    cfg_over = {"head_timeout": 0.5, "conn_timeout": 5}

    def test_trickled_head_gets_408_at_the_head_deadline(self):
        s = self.idle()
        t0 = time.monotonic()
        out = b""
        for b in b"GET /healthz HTTP/1.1\r\nX-Slow: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa":
            try:
                s.sendall(bytes([b]))
            except OSError:
                break
            time.sleep(0.05)  # every recv succeeds: only the overall head deadline can fire
            if time.monotonic() - t0 > 3:
                break
        s.settimeout(3)
        try:
            out = s.recv(65536)
        except OSError:
            pass
        self.assertEqual(self.status_of(out), 408)
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertTrue(wait_for(lambda: self.run_.agent.active == 0))


class ConcurrencyTest(ServerTest):
    cfg_over = {"max_concurrent": 2, "head_timeout": 5, "conn_timeout": 5}

    def test_beyond_the_cap_gets_503_then_recovers(self):
        self.idle()
        self.idle()
        self.assertTrue(wait_for(lambda: self.run_.agent.active == 2))
        t0 = time.monotonic()
        code, headers, body = self.run_.get("/healthz")
        self.assertEqual(code, 503)
        self.assertEqual(json.loads(body), {"error": "busy"})
        self.assertEqual(headers["Retry-After"], "1")
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertTrue(wait_for(lambda: "GET /healthz 503" in self.logs.lines))
        for s in self.socks:
            s.close()
        self.socks = []
        self.assertTrue(wait_for(lambda: self.run_.agent.active == 0))
        self.assertEqual(self.run_.get("/healthz")[0], 200)

    def test_busy_requests_hold_slots(self):
        """Two slow journalctl calls fill the cap; a third request is refused, not queued."""
        self.env.fixture("journalctl.log.sleep", "1.0")
        results = []
        threads = [threading.Thread(
            target=lambda: results.append(self.run_.get("/v1/logs?unit=ollama")[0]))
            for _ in range(2)]
        for t in threads:
            t.start()
        self.assertTrue(wait_for(lambda: self.run_.agent.active == 2))
        self.assertEqual(self.run_.get("/healthz")[0], 503)
        for t in threads:
            t.join(5)
        self.assertEqual(results, [200, 200])

    def test_reject_pool_full_closes_on_accept(self):
        self.idle()
        self.idle()
        self.assertTrue(wait_for(lambda: self.run_.agent.active == 2))
        self.idle()  # these two wait in the reject pool (up to REJECT_WAIT)
        self.idle()
        time.sleep(0.2)
        extra = self.idle()
        try:
            data = extra.recv(1024)
        except ConnectionResetError:
            data = b""
        self.assertEqual(data, b"")
        self.assertIn("busy: closing a connection on accept", self.logs.lines)
        self.assertLessEqual(threading.active_count(), 40)


class DeadlineTest(ServerTest):
    cfg_over = {"request_deadline": 1, "head_timeout": 30, "conn_timeout": 30}

    def test_overall_deadline_frees_the_slot(self):
        s = self.idle()
        self.assertTrue(wait_for(lambda: self.run_.agent.active == 1))
        t0 = time.monotonic()
        s.settimeout(5)
        try:
            s.recv(1024)
        except OSError:
            pass
        self.assertLess(time.monotonic() - t0, 3)
        self.assertTrue(wait_for(lambda: self.run_.agent.active == 0))


class LoggingTest(ServerTest):
    def test_logs_method_path_status_only(self):
        self.run_.get("/healthz")
        self.run_.get("/v1/logs?unit=ollama&lines=5", headers={"X-Secret": "hdr-" + TOKEN})
        self.run_.get("/healthz", token="wrong")
        self.run_.get("/" + TOKEN)                       # the token in the path
        self.run_.get("/v1/logs?unit=" + TOKEN)          # and in the query
        self.run_.get("/v1/services", method="POST", data=b"x")
        self.assertEqual(self.logs.lines, [
            "GET /healthz 200", "GET /v1/logs 200", "GET /healthz 401", "GET - 404",
            "GET /v1/logs 400", "POST /v1/services 405"])
        joined = "\n".join(self.logs.lines)
        for needle in (TOKEN, TOKEN[:10], "Bearer", "Authorization", "unit="):
            self.assertNotIn(needle, joined)


if __name__ == "__main__":
    unittest.main()
