"""Concurrent requests, the forge lock shared with other processes, and the connection limits."""
import os
import socket
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from .helpers import APPROVER, TOKEN, Env, Served

PROPOSAL = {"title": "Parallel request", "why": "testing", "queries": [{"query": "q one", "why": "w"}]}


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.srv = Served(self, self.env)

    def test_parallel_creates_get_distinct_ids(self):
        with ThreadPoolExecutor(12) as pool:
            results = list(pool.map(lambda _: self.srv.call("POST", "/requests", PROPOSAL), range(12)))
        self.assertEqual([r[0] for r in results], [201] * 12, [r[1] for r in results])
        rids = {r[1]["rid"] for r in results}
        self.assertEqual(len(rids), 12)
        for rid in rids:
            self.assertIn("- [ ] q one", self.env.read(rid, "searches.md"))

    def test_parallel_ticks_tick_each_box_once(self):
        rid = self.env.new_request()
        queries = ["apa 7 citation rules", "apa 7 reference list format", "apa 7 in-text citations"]
        body = {"queries": queries, "user": APPROVER}
        with ThreadPoolExecutor(10) as pool:
            results = list(pool.map(lambda _: self.srv.call("POST", f"/requests/{rid}/tick-searches", body), range(10)))
        self.assertEqual([r[0] for r in results], [200] * 10)
        for q in queries:
            outcomes = [next(x["result"] for x in r[1]["results"] if x["query"] == q) for r in results]
            self.assertEqual(outcomes.count("ticked"), 1, (q, outcomes))
            self.assertEqual(outcomes.count("already"), 9)
        self.assertEqual(self.env.read(rid, "searches.md").count("\n- [x] "), 3)
        self.assertEqual(self.env.read(rid, "state.md").count("approve-chat"), 1)

    def test_mixed_reads_and_writes(self):
        rid = self.env.with_sources([f"https://example.com/{i}" for i in range(5)])

        def work(i):
            if i % 3 == 0:
                return self.srv.call("POST", f"/requests/{rid}/tick-sources",
                                     {"urls": [f"https://example.com/{i % 5}"], "user": APPROVER})[0]
            if i % 3 == 1:
                return self.srv.call("GET", f"/requests/{rid}")[0]
            return self.srv.call("GET", "/requests")[0]
        with ThreadPoolExecutor(12) as pool:
            codes = list(pool.map(work, range(30)))
        self.assertEqual(set(codes), {200})

    def test_waits_for_another_process_holding_the_forge_lock(self):
        rid = self.env.new_request()
        lock = self.env.root / "35-forge" / ".forge-lock"
        lock.write_text("pid 1 (the research gate)\n")
        threading.Timer(0.3, lock.unlink).start()
        code, out, _ = self.srv.call("POST", f"/requests/{rid}/tick-searches",
                                     {"queries": ["apa 7 citation rules"], "user": APPROVER})
        self.assertEqual(code, 200, out)
        self.assertEqual(out["counts"]["ticked"], 1)

    def test_lock_held_too_long_is_503(self):
        env = Env(self, lock_tries=3, lock_sleep=0.01)
        srv = Served(self, env)
        rid = env.new_request()
        lock = env.root / "35-forge" / ".forge-lock"
        lock.write_text("stuck\n")
        self.addCleanup(lambda: lock.exists() and os.unlink(lock))
        code, out, _ = srv.call("POST", f"/requests/{rid}/tick-searches",
                                {"queries": ["apa 7 citation rules"], "user": APPROVER})
        self.assertEqual((code, out), (503, {"error": "the forge is busy; try again in a moment"}))
        self.assertEqual(srv.call("POST", "/requests", PROPOSAL)[0], 503)
        self.assertNotIn("\n- [x] ", env.read(rid, "searches.md"))


class LimitTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)

    def test_busy_connections_are_closed(self):
        srv = Served(self, self.env, max_concurrent=1, request_deadline=5)
        held = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        self.addCleanup(held.close)
        held.sendall(b"GET /requests HTTP/1.1\r\n")   # holds the only slot
        time.sleep(0.2)
        with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as second:
            second.sendall(f"GET /requests HTTP/1.0\r\nAuthorization: Bearer {TOKEN}\r\n\r\n".encode())
            try:
                data = second.recv(100)
            except ConnectionResetError:
                data = b""
            self.assertEqual(data, b"")
        held.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if srv.call("GET", "/requests")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.05)
        else:
            self.fail("the slot was never released")

    def test_body_that_never_arrives_is_408(self):
        srv = Served(self, self.env, socket_timeout=0.3, request_deadline=5)
        with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as s:
            s.sendall(f"POST /requests HTTP/1.0\r\nAuthorization: Bearer {TOKEN}\r\n"
                      "Content-Type: application/json\r\nContent-Length: 10\r\n\r\n{}".encode())
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        self.assertIn(b" 408 ", data.split(b"\r\n")[0])

    def test_trickling_client_is_cut_off(self):
        srv = Served(self, self.env, request_deadline=0.5, socket_timeout=5)
        with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as s:
            s.sendall(b"GET /requests HTTP/1.1\r\nX: ")
            t0 = time.monotonic()
            for _ in range(20):
                try:
                    s.sendall(b"a")
                except OSError:
                    break
                time.sleep(0.1)
                s.settimeout(0.01)
                try:
                    if s.recv(1) == b"":
                        break
                except TimeoutError:
                    pass
                except OSError:
                    break
            self.assertLess(time.monotonic() - t0, 1.8)


if __name__ == "__main__":
    unittest.main()
