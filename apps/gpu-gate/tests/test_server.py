import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request

from gpu_gate.server import GateHTTPServer

from .test_gate import STATUS_KEYS

STATUS = {"available": True, "reason": "no_game", "avg30": 12.5, "samples": 3,
          "game": None, "gpu_busy": 10, "ollama_share": 0.0, "ts": 1.5,
          "uid_match": True, "uid_match_reason": None}


class FakeGate:
    def __init__(self):
        self.st = None
        self.ok = False

    def status(self):
        return self.st

    def healthy(self):
        return self.ok


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class ServerTest(unittest.TestCase):
    max_handlers = 16
    conn_timeout = 5.0
    request_deadline = 10.0

    def setUp(self):
        self.gate = FakeGate()
        self.srv = GateHTTPServer("127.0.0.1", 0, self.gate, max_handlers=self.max_handlers,
                                  conn_timeout=self.conn_timeout,
                                  request_deadline=self.request_deadline)
        self.port = self.srv.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.socks = []

    def tearDown(self):
        for s in self.socks:
            s.close()
        self.srv.shutdown()
        self.srv.server_close()

    def get(self, path, method="GET"):
        req = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status, r.headers, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                body = e.read()
            return e.code, e.headers, json.loads(body) if body.startswith(b"{") else None

    def idle(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        self.socks.append(s)
        return s


class ApiTest(ServerTest):
    def test_status(self):
        self.assertEqual(self.get("/status")[::2], (503, {"error": "no sample yet"}))
        self.gate.st, self.gate.ok = dict(STATUS), True
        code, headers, body = self.get("/status?x=1")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(set(body), STATUS_KEYS)
        self.assertEqual(body, STATUS)

    def test_stale_status_is_503(self):
        self.gate.st, self.gate.ok = dict(STATUS), False  # last good sample > 3 intervals old
        self.assertEqual(self.get("/status")[::2], (503, {"error": "stale", "ts": 1.5}))

    def test_healthz(self):
        self.assertEqual(self.get("/healthz")[::2], (503, {"ok": False}))
        self.gate.ok = True
        self.assertEqual(self.get("/healthz")[::2], (200, {"ok": True}))

    def test_unknown_path_and_method(self):
        self.assertEqual(self.get("/api/ps")[0], 404)
        self.assertEqual(self.get("/status", method="POST")[0], 501)


class IdleTimeoutTest(ServerTest):
    conn_timeout = 0.3

    def test_idle_connection_is_closed_and_frees_its_thread(self):
        s = self.idle()
        self.assertTrue(wait_for(lambda: self.srv.active == 1))
        t0 = time.monotonic()
        self.assertEqual(s.recv(1024), b"")  # server hung up on the silent client
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertTrue(wait_for(lambda: self.srv.active == 0))


class HandlerCapTest(ServerTest):
    max_handlers = 2
    conn_timeout = 10.0

    def test_connections_beyond_the_cap_are_dropped_then_recover(self):
        self.gate.st, self.gate.ok = dict(STATUS), True
        self.idle()
        self.idle()
        self.assertTrue(wait_for(lambda: self.srv.active == 2))
        extra = self.idle()
        t0 = time.monotonic()
        try:
            data = extra.recv(1024)
        except ConnectionResetError:
            data = b""
        self.assertEqual(data, b"")  # closed at once, not queued behind the idle ones
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(self.srv.active, 2)
        for s in self.socks:
            s.close()
        self.socks = []
        self.assertTrue(wait_for(lambda: self.srv.active == 0))
        self.assertEqual(self.get("/status")[0], 200)



class SlowlorisTest(ServerTest):
    """The Reviewer's scenario, scaled down: every slot held by clients that send one header
    byte per read timeout's worth of time. Each read succeeds, so only the overall deadline
    can free the slots; a normal /status must get through soon after it."""

    max_handlers = 4
    conn_timeout = 0.5
    request_deadline = 1.0

    def test_trickling_clients_cannot_starve_status(self):
        self.gate.st, self.gate.ok = dict(STATUS), True
        stop = threading.Event()
        cut = []

        def trickle():
            s = self.idle()
            for b in b"GET /status HTTP/1.1\r\nX-Slow: " + b"a" * 1000:
                if stop.wait(0.2):  # < conn_timeout: the per-read timeout never fires
                    return
                try:
                    s.sendall(bytes([b]))
                except OSError:
                    cut.append(time.monotonic() - t0)
                    return

        t0 = time.monotonic()
        threads = [threading.Thread(target=trickle) for _ in range(self.max_handlers)]
        for t in threads:
            t.start()
        try:
            self.assertTrue(wait_for(lambda: self.srv.active == self.max_handlers))
            got = None
            while time.monotonic() - t0 < 4.0 and got != 200:
                try:
                    got = self.get("/status")[0]
                except OSError:  # dropped on accept while the slots are full
                    time.sleep(0.05)
            took = time.monotonic() - t0
            self.assertEqual(got, 200)
            self.assertLess(took, 3.0)  # deadline 1 s + slack; without it this never succeeds
            self.assertTrue(wait_for(lambda: len(cut) == self.max_handlers))
            self.assertTrue(all(0.9 < c < 2.5 for c in cut), cut)  # cut at the deadline
        finally:
            stop.set()
            for t in threads:
                t.join(2)


if __name__ == "__main__":
    unittest.main()
