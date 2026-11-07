import contextlib
import io
import logging
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from gpu_gate.config import validate
from gpu_gate.gate import (BELOW_THRESHOLD, GAME_BUSY, NO_GAME, NO_SAMPLES, Gate, decide,
                           uid_match_guard, uid_min)
from gpu_gate.__main__ import run_loop, safe_tick
from gpu_gate.ollama import OllamaClient
from gpu_gate.server import GateHTTPServer

from .fakes import AS_OLLAMA, PDEV, FakeClock, StubOllama, Tree, fdinfo

STATUS_KEYS = {"available", "reason", "avg30", "samples", "game", "gpu_busy", "ollama_share", "ts",
               "uid_match", "uid_match_reason"}


class DecideTest(unittest.TestCase):
    def test_no_game_is_available_even_when_busy(self):
        self.assertEqual(decide(99.0, None, 50), (True, NO_GAME))
        self.assertEqual(decide(None, None, 50), (True, NO_GAME))

    def test_game_below_threshold_is_available(self):
        self.assertEqual(decide(49.9, "x.exe", 50), (True, BELOW_THRESHOLD))

    def test_game_at_or_above_threshold_is_unavailable(self):
        self.assertEqual(decide(50.0, "x.exe", 50), (False, GAME_BUSY))
        self.assertEqual(decide(80.0, "x.exe", 50), (False, GAME_BUSY))

    def test_game_without_samples_fails_toward_the_game(self):
        self.assertEqual(decide(None, "x.exe", 50), (False, NO_SAMPLES))


class GateTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.t = Tree()
        self.t.card("card1", busy=0)
        self.wall = FakeClock(1_800_000_000.0)
        self.mono = FakeClock(50.0)
        self.unloads = 0
        cfg = validate({"sysfs_root": self.t.sys, "proc_root": self.t.proc,
                        "window": 60, "interval": 5})
        self.gate = Gate(cfg, self.wall, self.mono, unloader=self._unload, **AS_OLLAMA)

    def tearDown(self):
        logging.disable(logging.NOTSET)
        self.t.cleanup()

    def _unload(self):
        self.unloads += 1

    def tick(self, busy):
        self.mono.t += 5
        self.wall.t += 5
        self.t.set_busy(busy)
        return self.gate.tick()

    def start_game(self):
        self.t.proc_(3000, "Spider-Man.exe", argv=["S:\\Spider-Man.exe"])

    def test_status_shape(self):
        st = self.tick(30)
        self.assertEqual(set(st), STATUS_KEYS)
        self.assertEqual(st, {"available": True, "reason": NO_GAME, "avg30": 30.0, "samples": 1,
                              "game": None, "gpu_busy": 30, "ollama_share": 0.0,
                              "ts": 1_800_000_005.0, "uid_match": True,
                              "uid_match_reason": None})
        self.assertEqual(self.gate.status(), st)
        self.assertIsNot(self.gate.status(), st)  # a copy

    def test_unload_on_transition_only(self):
        self.start_game()
        st = self.tick(20)
        self.assertEqual((st["available"], st["reason"], st["game"]),
                         (True, BELOW_THRESHOLD, "Spider-Man.exe"))
        self.assertEqual(self.unloads, 0)
        st = self.tick(100)  # avg30 = 60
        self.assertEqual((st["available"], st["reason"], st["avg30"]), (False, GAME_BUSY, 60.0))
        self.assertEqual(self.unloads, 1)
        for _ in range(5):
            self.assertFalse(self.tick(100)["available"])
        self.assertEqual(self.unloads, 1)  # not repeated while unavailable
        self.t.kill(3000)
        st = self.tick(100)
        self.assertEqual((st["available"], st["reason"]), (True, NO_GAME))
        self.start_game()
        self.assertFalse(self.tick(100)["available"])
        self.assertEqual(self.unloads, 2)  # a new transition unloads again

    def test_first_tick_unavailable_unloads(self):
        self.start_game()
        self.assertFalse(self.tick(90)["available"])
        self.assertEqual(self.unloads, 1)

    def test_game_util_subtracts_ollama_and_window_expires(self):
        self.start_game()
        self.t.proc_(10, "ollama", "/usr/local/bin/ollama", ["ollama"], fds={5: fdinfo(1)})
        self.tick(100)  # first meter sample: share 0 -> game_util 100
        self.t.set_fd(10, 5, fdinfo(1, gfx=4_000_000_000))  # 80 % of 5 s
        st = self.tick(100)  # game_util 20 -> avg30 60
        self.assertEqual((st["ollama_share"], st["avg30"], st["samples"]), (80.0, 60.0, 2))
        self.t.set_fd(10, 5, fdinfo(1, gfx=9_000_000_000))  # 100 % of 5 s -> clamp game_util at 0
        st = self.tick(50)
        self.assertEqual((st["ollama_share"], st["avg30"]), (100.0, 40.0))
        for _ in range(12):  # 60 s window: all of the above expires
            st = self.tick(10)
        self.assertEqual((st["samples"], st["avg30"]), (12, 10.0))
        self.assertTrue(st["available"])

    def test_missing_card(self):
        t2 = Tree()
        try:
            cfg = validate({"sysfs_root": t2.sys, "proc_root": t2.proc})
            gate = Gate(cfg, self.wall, self.mono, unloader=self._unload, **AS_OLLAMA)
            st = gate.tick()
            self.assertEqual((st["gpu_busy"], st["avg30"], st["samples"], st["available"]),
                             (None, None, 0, True))
        finally:
            t2.cleanup()

    def test_card_override(self):
        other = self.t.card("card7", busy=None, pdev="0000:99:00.0")
        self.t.set_busy(77, "0000:99:00.0")
        cfg = validate({"sysfs_root": self.t.sys, "proc_root": self.t.proc, "card": other})
        gate = Gate(cfg, self.wall, self.mono, **AS_OLLAMA)
        self.assertEqual(gate.tick()["gpu_busy"], 77)
        self.assertEqual(gate.meter.pdev, "0000:99:00.0")
        self.assertEqual(self.gate.tick()["gpu_busy"], 0)
        self.assertEqual(self.gate.meter.pdev, PDEV)

    def test_healthy(self):
        self.assertFalse(self.gate.healthy())
        self.tick(0)
        self.assertTrue(self.gate.healthy())
        self.mono.t += 16
        self.assertFalse(self.gate.healthy())


    def test_suspend_clock_jump_ages_samples_out(self):
        # mono is CLOCK_BOOTTIME in production: a suspend shows up as a jump
        self.start_game()
        for _ in range(6):
            self.tick(100)
        self.assertEqual((self.gate.status()["samples"], self.gate.status()["available"]), (6, False))
        self.mono.t += 3600  # an hour asleep
        self.assertFalse(self.gate.healthy())  # the pre-suspend answer is stale
        st = self.tick(20)
        self.assertEqual((st["samples"], st["avg30"], st["available"], st["reason"]),
                         (1, 20.0, True, BELOW_THRESHOLD))
        self.assertTrue(self.gate.healthy())

    def test_failed_ticks_go_stale(self):
        self.tick(30)
        self.assertTrue(self.gate.healthy())
        with mock.patch("gpu_gate.gate.procfs.scan", side_effect=OSError("boom")):
            for i in range(4):
                self.mono.t += 5
                self.assertFalse(safe_tick(self.gate))
                if i == 2:  # 15 s = exactly 3 intervals: still fresh
                    self.assertTrue(self.gate.healthy())
        self.assertEqual(self.gate.status()["gpu_busy"], 30)  # last good answer kept...
        self.assertFalse(self.gate.healthy())                  # ...but it is stale (20 s > 15 s)
        self.tick(40)
        self.assertTrue(self.gate.healthy())


class StaleStatusEndToEndTest(unittest.TestCase):
    """Real Gate + real server: /status and /healthz both 503 when every tick throws."""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.t = Tree()
        self.t.card("card1", busy=10)
        self.mono = FakeClock(0.0)
        self.gate = Gate(validate({"sysfs_root": self.t.sys, "proc_root": self.t.proc}),
                         FakeClock(), self.mono, **AS_OLLAMA)
        self.srv = GateHTTPServer("127.0.0.1", 0, self.gate)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def tearDown(self):
        logging.disable(logging.NOTSET)
        self.srv.shutdown()
        self.srv.server_close()
        self.t.cleanup()

    def code(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=3) as r:
                return r.status
        except urllib.error.HTTPError as e:
            e.close()
            return e.code

    def test_every_tick_throws_from_the_start(self):
        with mock.patch.object(self.gate, "tick", side_effect=RuntimeError("boom")):
            stop = threading.Event()
            t = threading.Thread(target=run_loop, args=(self.gate, 0.01, stop))
            t.start()
            self.assertTrue(safe_tick(self.gate) is False)
            time.sleep(0.1)
            stop.set()
            t.join(2)
            self.assertFalse(t.is_alive())
            calls = self.gate.tick.call_count
        self.assertGreater(calls, 3)  # the loop kept going through the exceptions
        self.assertEqual((self.code("/status"), self.code("/healthz")), (503, 503))

    def test_good_then_every_tick_throws(self):
        self.gate.tick()
        self.assertEqual((self.code("/status"), self.code("/healthz")), (200, 200))
        with mock.patch("gpu_gate.gate.read_busy", side_effect=OSError("gpu gone")):
            for _ in range(4):
                self.mono.t += 5
                safe_tick(self.gate)
        self.assertEqual((self.code("/status"), self.code("/healthz")), (503, 503))


class UidGuardTest(unittest.TestCase):
    """Lead decision: uid matching is refused for root and for login users (uid >= UID_MIN),
    with one stderr warning and uid_match false + a reason in /status; the names still apply."""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.t = Tree()
        self.t.card("card1", busy=90)
        self.defs = os.path.join(self.t.root, "login.defs")
        with open(self.defs, "w") as f:
            f.write("# UID_MIN 5\nMAIL_DIR /var/spool/mail\nUID_MIN\t\t\t 1000  # first login\n"
                    "UID_MAX 60000\nSYS_UID_MIN 201\n")
        # an `ollama`-named server and an unnamed worker, both on the uid under test
        self.cfg = validate({"sysfs_root": self.t.sys, "proc_root": self.t.proc,
                             "ollama_match": ["ollama"]})
        self.clock = FakeClock(100.0)

    def tearDown(self):
        logging.disable(logging.NOTSET)
        self.t.cleanup()

    def run_as(self, uid, ticks=3):
        self.t.proc_(900, "ollama", argv=["/usr/local/bin/ollama", "serve"], uid=uid,
                     fds={5: fdinfo(1)})
        self.t.proc_(901, "llama-server", argv=["/x/llama-server"], uid=uid, fds={6: fdinfo(2)})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            gate = Gate(self.cfg, self.clock, self.clock, uid=uid, login_defs=self.defs)
            gate.tick()
            for i in range(1, ticks):  # each process does 1 s of compute per 5 s tick
                self.t.set_fd(900, 5, fdinfo(1, compute=i * 1_000_000_000))
                self.t.set_fd(901, 6, fdinfo(2, compute=i * 1_000_000_000))
                self.clock.t += 5
                st = gate.tick()
        return gate, st, err.getvalue()

    def test_uid_min_parsing(self):
        self.assertEqual(uid_min(self.defs), 1000)
        self.assertEqual(uid_min("/nonexistent/login.defs"), 1000)
        with open(self.defs, "w") as f:
            f.write("UID_MIN 500\n")
        self.assertEqual(uid_min(self.defs), 500)
        with open(self.defs, "w") as f:
            f.write("UID_MIN lots\n")
        self.assertEqual(uid_min(self.defs), 1000)
        with open(self.defs, "w") as f:  # as in shadow, the last definition wins
            f.write("UID_MIN 500\nUID_MAX 60000\nUID_MIN\t2000 # override\n")
        self.assertEqual(uid_min(self.defs), 2000)
        with open(self.defs, "w") as f:  # a bad last value falls back to the default
            f.write("UID_MIN 500\nUID_MIN x\n")
        self.assertEqual(uid_min(self.defs), 1000)

    def test_root_turns_uid_matching_off(self):
        gate, st, err = self.run_as(0)
        self.assertEqual((st["uid_match"], gate.meter.match_uid), (False, None))
        self.assertIn("uid 0 (root)", st["uid_match_reason"])
        self.assertEqual(err.count("WARNING"), 1)  # once per run, not per tick
        self.assertIn("uid 0", err)
        self.assertEqual(st["ollama_share"], 20.0)  # only the named `ollama` (1 s / 5 s)

    def test_login_user_turns_uid_matching_off(self):
        gate, st, err = self.run_as(1000)
        self.assertFalse(st["uid_match"])
        self.assertIn("uid 1000 >= UID_MIN 1000", st["uid_match_reason"])
        self.assertEqual(err.count("WARNING"), 1)
        self.assertIn("uid 1000", err)
        self.assertEqual(st["ollama_share"], 20.0)

    def test_system_uid_keeps_uid_matching(self):
        gate, st, err = self.run_as(980)
        self.assertEqual((st["uid_match"], st["uid_match_reason"]), (True, None))
        self.assertEqual(gate.meter.match_uid, 980)
        self.assertEqual(err, "")
        self.assertEqual(st["ollama_share"], 40.0)  # the named server + the unnamed worker

    def test_uid_min_from_login_defs_is_used(self):
        with open(self.defs, "w") as f:
            f.write("UID_MIN 2000\n")
        self.assertTrue(self.run_as(1500)[1]["uid_match"])
        with open(self.defs, "w") as f:
            f.write("UID_MIN 900\n")
        self.assertFalse(self.run_as(980)[1]["uid_match"])

    def test_guard_function(self):
        self.assertEqual(uid_match_guard(980, self.defs), (True, None))
        self.assertFalse(uid_match_guard(0, self.defs)[0])
        self.assertFalse(uid_match_guard(1000, self.defs)[0])
        self.assertFalse(uid_match_guard(65534, self.defs)[0])  # nobody is >= UID_MIN too


class GateUnloadsOllamaTest(unittest.TestCase):
    """End to end against the stub: a transition makes exactly the contract's HTTP calls."""

    def test_transition_hits_stub(self):
        logging.disable(logging.CRITICAL)
        t, stub = Tree(), StubOllama(["qwen3:14b"])
        try:
            t.card("card1", busy=95)
            t.proc_(3000, "gamescope", "/usr/bin/gamescope", ["gamescope"])
            client = OllamaClient(stub.url, 2)
            cfg = validate({"sysfs_root": t.sys, "proc_root": t.proc})
            gate = Gate(cfg, FakeClock(), FakeClock(), unloader=client.unload_all, **AS_OLLAMA)
            gate.tick()
            gate.tick()
            self.assertEqual(stub.requests, [
                ("GET", "/api/ps", None),
                ("POST", "/api/generate", {"model": "qwen3:14b", "keep_alive": 0}),
            ])
        finally:
            logging.disable(logging.NOTSET)
            stub.close()
            t.cleanup()


if __name__ == "__main__":
    unittest.main()
