import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import unittest

from gpu_gate.__main__ import boottime, build_gate, run_once
from gpu_gate.config import DEFAULTS, ConfigError, load, validate
from gpu_gate.gate import Gate, uid_min

from .fakes import AS_OLLAMA, OLLAMA_UID, FakeClock, Tree, fdinfo

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = load(None)
        self.assertEqual(cfg, DEFAULTS)
        self.assertEqual((cfg["interval"], cfg["window"], cfg["threshold"], cfg["port"]),
                         (5, 1800, 50, 11500))
        self.assertEqual(cfg["ollama_url"], "http://127.0.0.1:11434")

    def test_example_config_is_valid(self):
        cfg = load(os.path.join(APP, "config.example.json"))
        self.assertEqual(cfg["port"], 11500)

    def test_override_and_trailing_slash(self):
        cfg = validate({"threshold": 40, "ollama_url": "http://127.0.0.1:11434/"})
        self.assertEqual((cfg["threshold"], cfg["ollama_url"]), (40, "http://127.0.0.1:11434"))

    def test_rejects(self):
        for bad in ({"treshold": 50}, {"interval": 0}, {"interval": True}, {"port": 70000},
                    {"binds": []}, {"binds": "127.0.0.1"}, {"games": [""]}, {"threshold": 101},
                    {"window": 1, "interval": 5}, {"ollama_url": "file:///etc/passwd"},
                    {"card": ""}, []):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                validate(bad)

    def test_bad_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{nope")
        try:
            with self.assertRaises(ConfigError):
                load(f.name)
        finally:
            os.remove(f.name)


    def test_bounds_and_log_level(self):
        for bad in ({"interval": 0.4}, {"window": 0.4, "interval": 0.5}, {"threshold": 0.5},
                    {"ollama_timeout": 0.1}, {"ollama_timeout": 61}, {"log_level": "LOUD"},
                    {"log_level": "warn"}, {"log_level": "NOTSET"}, {"ollama_engines": []},
                    {"ollama_engines": ["gfx compute"]}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                validate(bad)
        cfg = validate({"interval": 0.5, "window": 0.5, "log_level": "debug",
                        "ollama_engines": ["gfx", "enc_1", "video-enhance"]})
        self.assertEqual((cfg["interval"], cfg["log_level"]), (0.5, "DEBUG"))


class ClockTest(unittest.TestCase):
    def test_gate_uses_clock_boottime(self):
        with contextlib.redirect_stderr(io.StringIO()):  # the uid guard may warn here
            gate = build_gate(validate({}))
        self.assertIs(gate.mono, boottime)
        self.assertAlmostEqual(boottime(), time.clock_gettime(time.CLOCK_BOOTTIME), delta=1.0)
        mono = time.monotonic()
        self.assertGreaterEqual(boottime(), mono)  # boottime also counts suspend


class RunOnceTest(unittest.TestCase):
    """Deterministic: run_once with fake clocks, the worker's compute advancing between samples."""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.t = Tree()
        self.t.card("card1", busy=96)
        self.t.proc_(901, "llama-server", uid=OLLAMA_UID, argv=["/x/llama-server"],
                     fds={12: fdinfo(77, compute=16_800_000_000)})
        self.clock = FakeClock(100.0)
        self.gate = Gate(validate({"sysfs_root": self.t.sys, "proc_root": self.t.proc,
                                   "ollama_match": ["ollama"]}), self.clock, self.clock,
                         **AS_OLLAMA)
        self.sleeps = []

    def tearDown(self):
        logging.disable(logging.NOTSET)
        self.t.cleanup()

    def sleep(self, s):  # the live numbers: +5.8 s of compute in 6 s
        self.sleeps.append(s)
        self.clock.t += s
        self.t.set_fd(901, 12, fdinfo(77, compute=22_600_000_000))

    def test_two_samples_show_ollama_share(self):
        st = run_once(self.gate, 2, 6.0, self.sleep)
        self.assertEqual(self.sleeps, [6.0])
        self.assertEqual((st["samples"], st["gpu_busy"], st["ollama_share"]), (2, 96, 96.7))

    def test_one_sample_cannot(self):
        st = run_once(self.gate, 1, 6.0, self.sleep)
        self.assertEqual((self.sleeps, st["ollama_share"]), ([], 0.0))


class OnceTest(unittest.TestCase):
    """The real entrypoint (`gpu-gate --once`) against a fake tree, under `python3 -I`."""

    def test_once_prints_status(self):
        t = Tree()
        try:
            t.card("card1", busy=64)
            t.proc_(3000, "gamescope", argv=["gamescope"])
            cfg = os.path.join(t.root, "c.json")
            with open(cfg, "w") as f:
                json.dump({"sysfs_root": t.sys, "proc_root": t.proc, "interval": 0.5,
                           "ollama_url": "http://127.0.0.1:9"}, f)
            r = subprocess.run([sys.executable, "-I", os.path.join(APP, "gpu-gate"),
                                "--config", cfg, "--once"],
                               capture_output=True, text=True, timeout=30, check=True)
            st = json.loads(r.stdout)  # stdout is the JSON alone (logs go to stderr): jq-able
            self.assertIn("INFO gpu_gate: card", r.stderr)
            self.assertEqual(st["samples"], 2)  # --once defaults to 2 samples
            self.assertEqual((st["available"], st["reason"], st["game"], st["gpu_busy"]),
                             (False, "game_busy", "gamescope", 64))
            # the test runs as whoever runs it: as root or a login user the guard must trip
            uid = os.getuid()
            tripped = uid == 0 or uid >= uid_min()
            self.assertEqual(st["uid_match"], not tripped)
            if tripped:
                self.assertIn("uid %d" % uid, st["uid_match_reason"])
                self.assertEqual(r.stderr.count("WARNING: uid matching off"), 1)
                self.assertIn("uid %d" % uid, r.stderr)
        finally:
            t.cleanup()

    def test_once_cli_reports_ollama_share(self):
        """The README check, for real: `--once` while the worker's compute counter advances must
        report ollama_share > 0 (one sample always said 0.0). The test runs as a login user, so
        uid matching is off and the default names find `llama-server`."""
        t = Tree()
        try:
            t.card("card1", busy=96)
            t.proc_(901, "llama-server", argv=["/usr/local/lib/ollama/llama-server"],
                    fds={12: fdinfo(77, compute=16_800_000_000)})
            cfg = os.path.join(t.root, "c.json")
            with open(cfg, "w") as f:
                json.dump({"sysfs_root": t.sys, "proc_root": t.proc, "interval": 1.5,
                           "ollama_url": "http://127.0.0.1:9"}, f)
            p = subprocess.Popen([sys.executable, "-I", os.path.join(APP, "gpu-gate"),
                                  "--config", cfg, "--once"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                for line in p.stderr:  # the first sample has started once the card is logged
                    if "card" in line:
                        break
                time.sleep(0.5)  # first sample done; the second is ~1 s away
                t.set_fd(901, 12, fdinfo(77, compute=16_800_000_000 + 1_200_000_000))
                out, _ = p.communicate(timeout=30)
            finally:
                if p.poll() is None:
                    p.kill()
                    p.communicate()
            st = json.loads(out)
            self.assertEqual(p.returncode, 0)
            self.assertGreater(st["ollama_share"], 0)
            self.assertAlmostEqual(st["ollama_share"], 80.0, delta=15)  # 1.2 s over ~1.5 s
        finally:
            t.cleanup()

    def test_samples_flag(self):
        r = subprocess.run([sys.executable, "-m", "gpu_gate", "--once", "--samples", "0"],
                           cwd=APP, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--samples must be >= 1", r.stderr)

    def test_bad_config_exits_2(self):
        r = subprocess.run([sys.executable, "-m", "gpu_gate", "--config", "/nonexistent.json"],
                           cwd=APP, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertIn("config", r.stderr)


if __name__ == "__main__":
    unittest.main()
