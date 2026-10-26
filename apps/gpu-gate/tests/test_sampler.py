import os
import unittest

from gpu_gate import procfs
from gpu_gate.sampler import OllamaMeter, card_pdev, find_card, parse_fdinfo, read_busy

from .fakes import OLLAMA_UID, OTHER_UID, PDEV, PLAIN_FDINFO, FakeClock, Tree, fdinfo

OLLAMA_EXE = "/usr/local/bin/ollama"


class CardTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()

    def tearDown(self):
        self.t.cleanup()

    def test_first_card_with_busy_file_skipping_connectors(self):
        self.t.card("card0", busy=None, pdev="0000:00:02.0")  # iGPU without gpu_busy_percent
        # connector fixture deliberately points at a device that HAS the file: name rule must skip it
        self.t.connector("card1-DP-1", pdev="0000:03:00.0")
        self.t.card("card10", busy=7, pdev="0000:04:00.0")
        self.t.card("card2", busy=33)
        self.t.set_busy(99, "0000:03:00.0")
        dev = find_card(self.t.sys)
        self.assertEqual(dev, os.path.join(self.t.sys, "class", "drm", "card2", "device"))
        self.assertEqual(read_busy(dev), 33)
        self.assertEqual(card_pdev(dev), PDEV)

    def test_no_card(self):
        self.assertIsNone(find_card(self.t.sys))
        self.assertIsNone(find_card(os.path.join(self.t.root, "nope")))

    def test_read_busy_bad_values(self):
        dev = self.t.card("card1", busy="garbage")
        self.assertIsNone(read_busy(dev))
        self.t.set_busy(250)
        self.assertEqual(read_busy(dev), 100)
        os.remove(os.path.join(self.t.sys, "devices", PDEV, "gpu_busy_percent"))
        self.assertIsNone(read_busy(dev))

    def test_pdev_unknown_for_non_pci_dir(self):
        d = os.path.join(self.t.root, "device")
        os.mkdir(d)
        self.assertIsNone(card_pdev(d))


class MeterTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.clock = FakeClock(100.0)
        self.m = OllamaMeter(["ollama"], ["gfx"], PDEV, self.clock)

    def tearDown(self):
        self.t.cleanup()

    def sample(self, dt=5.0):
        self.clock.t += dt
        return self.m.sample(procfs.scan(self.t.proc))

    def test_parse_fdinfo_real_format(self):
        info = parse_fdinfo(fdinfo(29, gfx=2645479908, compute=6476230))
        self.assertEqual(info["drm-client-id"], "29")
        self.assertEqual(info["drm-engine-gfx"], "2645479908 ns")
        self.assertEqual(info["drm-memory-gtt"], "8208 KiB")

    def test_first_sample_is_zero_then_delta_over_interval(self):
        self.t.proc_(10, "ollama", OLLAMA_EXE, ["ollama", "runner"], fds={5: fdinfo(1, gfx=10**12)})
        self.assertEqual(self.sample(), 0.0)
        self.t.set_fd(10, 5, fdinfo(1, gfx=10**12 + 2_500_000_000))  # 2.5 s busy in 5 s
        self.assertAlmostEqual(self.sample(), 50.0)

    def test_client_id_dedupe_across_fds_and_processes(self):
        # the same client in two fds of one process and in a forked child: count once
        self.t.proc_(10, "ollama", OLLAMA_EXE, ["ollama", "serve"],
                     fds={5: fdinfo(1, gfx=0), 6: fdinfo(1, gfx=0)})
        self.t.proc_(11, "ollama", OLLAMA_EXE, ["ollama", "runner"], fds={7: fdinfo(1, gfx=0)})
        self.sample()
        for pid, fd in ((10, 5), (10, 6), (11, 7)):
            self.t.set_fd(pid, fd, fdinfo(1, gfx=1_000_000_000))
        self.assertAlmostEqual(self.sample(), 20.0)

    def test_distinct_clients_add_up_and_clamp(self):
        self.t.proc_(10, "ollama", OLLAMA_EXE, ["ollama"], fds={5: fdinfo(1), 6: fdinfo(2)})
        self.sample()
        self.t.set_fd(10, 5, fdinfo(1, gfx=1_000_000_000))
        self.t.set_fd(10, 6, fdinfo(2, gfx=1_500_000_000))
        self.assertAlmostEqual(self.sample(), 50.0)
        self.t.set_fd(10, 5, fdinfo(1, gfx=9_000_000_000))
        self.t.set_fd(10, 6, fdinfo(2, gfx=9_000_000_000))
        self.assertEqual(self.sample(), 100.0)

    def test_exclusions(self):
        # a game's DRM client, a non-DRM fd, another GPU's client and a compute-only counter
        self.t.proc_(10, "ollama", OLLAMA_EXE, ["ollama"], fds={
            3: PLAIN_FDINFO, 5: fdinfo(1), 6: fdinfo(9, pdev="0000:00:02.0")})
        self.t.proc_(20, "Spider-Man.exe", "/x/wine64-preloader", ["S:\\Spider-Man.exe"],
                     fds={141: fdinfo(16042)})
        self.sample()
        self.t.set_fd(10, 5, fdinfo(1, compute=4_000_000_000))  # this meter counts gfx only
        self.t.set_fd(10, 6, fdinfo(9, gfx=4_000_000_000, pdev="0000:00:02.0"))
        self.t.set_fd(20, 141, fdinfo(16042, gfx=4_000_000_000))
        self.assertEqual(self.sample(), 0.0)

    def test_default_engines_count_gfx_and_compute(self):
        from gpu_gate.config import DEFAULTS
        self.assertEqual(DEFAULTS["ollama_engines"], ["gfx", "compute"])
        self.m = OllamaMeter(DEFAULTS["ollama_match"], DEFAULTS["ollama_engines"], PDEV, self.clock)
        self.t.proc_(10, "ollama", argv=["ollama", "runner"], fds={5: fdinfo(1)})
        self.sample()
        self.t.set_fd(10, 5, fdinfo(1, gfx=500_000_000, compute=2_000_000_000))
        self.assertAlmostEqual(self.sample(), 50.0)

    def test_engines_configurable(self):
        self.m = OllamaMeter(["ollama"], ["gfx", "compute"], PDEV, self.clock)
        self.t.proc_(10, "ollama", OLLAMA_EXE, ["ollama"], fds={5: fdinfo(1)})
        self.sample()
        self.t.set_fd(10, 5, fdinfo(1, gfx=1_000_000_000, compute=1_000_000_000))
        self.assertAlmostEqual(self.sample(), 40.0)

    def test_matches_argv0_when_comm_differs(self):
        self.t.proc_(10, "llama-runner", argv=["/usr/local/bin/ollama", "runner"],
                     fds={5: fdinfo(1)})
        self.sample()
        self.t.set_fd(10, 5, fdinfo(1, gfx=500_000_000))
        self.assertAlmostEqual(self.sample(), 10.0)

    def test_new_client_counter_reset_vanished_and_unreadable(self):
        self.t.proc_(10, "ollama", OLLAMA_EXE, ["ollama"], fds={5: fdinfo(1, gfx=5_000_000_000)})
        self.sample()
        # client 1 restarted (counter went down), client 2 is new (lifetime counter): both 0
        self.t.set_fd(10, 5, fdinfo(1, gfx=1000))
        self.t.set_fd(10, 6, fdinfo(2, gfx=10**13))
        self.assertEqual(self.sample(), 0.0)
        os.mkdir(os.path.join(self.t.proc, "10", "fdinfo", "7"))  # unreadable fdinfo entry
        self.t.proc_(12, "ollama", OLLAMA_EXE, ["ollama"])
        os.remove(os.path.join(self.t.proc, "12", "comm"))  # vanished mid-scan
        self.assertEqual(self.sample(), 0.0)
        self.t.kill(10)
        self.assertEqual(self.sample(), 0.0)

    def test_zero_elapsed_is_zero(self):
        self.t.proc_(10, "ollama", OLLAMA_EXE, ["ollama"], fds={5: fdinfo(1)})
        self.sample()
        self.t.set_fd(10, 5, fdinfo(1, gfx=10**9))
        self.assertEqual(self.sample(dt=0), 0.0)



class UidMatchTest(unittest.TestCase):
    """CONTRACTS 12.9: Ollama 0.40.11's GPU worker is `llama-server`, started by `ollama serve`,
    both as uid `ollama`, the uid the gate runs as. Under Vulkan its fdinfo advances
    drm-engine-compute (live: 16.8 s -> 22.6 s over ~6 s at 96 % gpu_busy)."""

    def setUp(self):
        self.t = Tree()
        self.clock = FakeClock(100.0)
        # the live tree: ollama serve (no DRM fd) -> llama-server (the GPU worker)
        self.t.proc_(900, "ollama", argv=["/usr/local/bin/ollama", "serve"], uid=OLLAMA_UID)
        self.t.proc_(901, "llama-server", ppid=900, uid=OLLAMA_UID,
                     argv=["/usr/local/lib/ollama/llama-server", "--model", "/x/blobs/sha256-0"],
                     fds={12: fdinfo(77, compute=16_800_000_000)})

    def tearDown(self):
        self.t.cleanup()

    def meter(self, match=("ollama",), match_uid=OLLAMA_UID, own_pid=None):
        return OllamaMeter(list(match), ["gfx", "compute"], PDEV, self.clock,
                           match_uid=match_uid, own_pid=own_pid)

    def run2(self, m, updates, dt=6.0, start=()):
        """Sample, apply `updates` (pid, fd, fdinfo), advance dt, sample again -> share.
        `start` first resets fds to their starting counters (for a second run)."""
        for pid, fd, text in start:
            self.t.set_fd(pid, fd, text)
        m.sample(procfs.scan(self.t.proc))
        for pid, fd, text in updates:
            self.t.set_fd(pid, fd, text)
        self.clock.t += dt
        return m.sample(procfs.scan(self.t.proc))

    LIVE = [(901, 12, fdinfo(77, compute=22_600_000_000))]  # +5.8 s in 6 s

    def test_same_uid_worker_counted_without_a_name_match(self):
        # the name list is the old default ["ollama"]: only the uid rule can find llama-server
        self.assertAlmostEqual(self.run2(self.meter(), self.LIVE), 5.8 / 6 * 100)

    def test_name_only_misses_the_worker(self):
        # what the live gate did before the fix: ollama_share 0 at 96 % busy
        self.assertEqual(self.run2(self.meter(match_uid=None), self.LIVE), 0.0)

    def test_default_names_include_llama_server_as_fallback(self):
        from gpu_gate.config import DEFAULTS
        self.assertTrue(DEFAULTS["ollama_match_uid"])
        self.assertIn("llama-server", DEFAULTS["ollama_match"])
        m = self.meter(match=DEFAULTS["ollama_match"], match_uid=None)
        self.assertAlmostEqual(self.run2(m, self.LIVE), 5.8 / 6 * 100)

    def test_other_uid_same_name_counts_only_via_the_name_list(self):
        # a desktop user's own llama-server: not Ollama's uid
        self.t.proc_(950, "llama-server", uid=OTHER_UID, argv=["/home/u/bin/llama-server"],
                     fds={4: fdinfo(88, compute=0)})
        upd = [(950, 4, fdinfo(88, compute=3_000_000_000))]  # 50 % of 6 s
        self.assertEqual(self.run2(self.meter(match=("ollama",)), upd), 0.0)
        self.assertAlmostEqual(self.run2(self.meter(match=("llama-server",)), upd,
                                         start=[(950, 4, fdinfo(88, compute=0))]), 50.0)

    def test_gate_own_pid_excluded(self):
        # the gate shares the uid; even if it had a DRM fd it must not count itself
        self.t.proc_(999, "python3", uid=OLLAMA_UID, argv=["/usr/bin/python3", "-I", "gpu-gate"],
                     fds={3: fdinfo(99, gfx=0)})
        upd = [(999, 3, fdinfo(99, gfx=3_000_000_000))]
        self.assertEqual(self.run2(self.meter(own_pid=999), upd), 0.0)
        self.assertAlmostEqual(self.run2(self.meter(own_pid=None), upd,
                                         start=[(999, 3, fdinfo(99, gfx=0))]), 50.0)

    def test_same_uid_other_gpu_stays_excluded(self):
        self.t.set_fd(901, 13, fdinfo(78, compute=0, pdev="0000:00:02.0"))
        upd = [(901, 13, fdinfo(78, compute=6_000_000_000, pdev="0000:00:02.0"))]
        self.assertEqual(self.run2(self.meter(), upd), 0.0)

    def test_real_uid_is_used_not_effective(self):
        # real ollama / effective other: counted; real other / effective ollama: not counted
        self.t.proc_(960, "worker-a", uid=OLLAMA_UID, euid=OTHER_UID, argv=["/x/worker-a"],
                     fds={4: fdinfo(60, compute=0)})
        self.t.proc_(961, "worker-b", uid=OTHER_UID, euid=OLLAMA_UID, argv=["/x/worker-b"],
                     fds={4: fdinfo(61, compute=0)})
        procs = {p.pid: p for p in procfs.scan(self.t.proc)}
        self.assertEqual((procs[960].uid(), procs[961].uid()), (OLLAMA_UID, OTHER_UID))
        upd = [(960, 4, fdinfo(60, compute=3_000_000_000)),
               (961, 4, fdinfo(61, compute=3_000_000_000))]
        self.assertAlmostEqual(self.run2(self.meter(match=("nothing",)), upd), 50.0)  # 960 only

    def test_status_uid_parsing(self):
        procs = {p.pid: p for p in procfs.scan(self.t.proc)}
        self.assertEqual(procs[901].uid(), OLLAMA_UID)
        os.remove(os.path.join(self.t.proc, "900", "status"))
        procs = {p.pid: p for p in procfs.scan(self.t.proc)}
        self.assertIsNone(procs[900].uid())  # vanished/unreadable: not Ollama by uid

    def test_gate_wires_uid_and_own_pid(self):
        from gpu_gate.config import validate
        from gpu_gate.gate import Gate
        self.t.card("card1", busy=96)
        gate = Gate(validate({"sysfs_root": self.t.sys, "proc_root": self.t.proc}),
                    self.clock, self.clock, uid=OLLAMA_UID, login_defs="/nonexistent")
        gate.tick()
        self.assertEqual((gate.meter.match_uid, gate.meter.own_pid), (OLLAMA_UID, os.getpid()))
        off = Gate(validate({"sysfs_root": self.t.sys, "proc_root": self.t.proc,
                             "ollama_match_uid": False}), self.clock, self.clock, uid=OLLAMA_UID)
        st = off.tick()
        self.assertIsNone(off.meter.match_uid)
        self.assertEqual((st["uid_match"], st["uid_match_reason"]),
                         (False, "ollama_match_uid is false in the config"))


class LiveScenarioTest(unittest.TestCase):
    """End to end through Gate, the gate and the fake worker both on the `ollama` (system) uid:
    at 96 % busy, ollama_share must follow the worker."""

    def test_gate_counts_llama_server(self):
        import logging
        from gpu_gate.config import validate
        from gpu_gate.gate import Gate
        logging.disable(logging.CRITICAL)
        t, clock = Tree(), FakeClock(100.0)
        try:
            t.card("card1", busy=96)
            t.proc_(901, "llama-server", uid=OLLAMA_UID,
                    argv=["/usr/local/lib/ollama/llama-server"],
                    fds={12: fdinfo(77, compute=16_800_000_000)})
            gate = Gate(validate({"sysfs_root": t.sys, "proc_root": t.proc,
                                  "ollama_match": ["ollama"]}), clock, clock,
                        uid=OLLAMA_UID, login_defs="/nonexistent")
            gate.tick()
            t.set_fd(901, 12, fdinfo(77, compute=22_600_000_000))
            clock.t += 6
            st = gate.tick()
            self.assertEqual((st["gpu_busy"], st["ollama_share"]), (96, 96.7))
        finally:
            logging.disable(logging.NOTSET)
            t.cleanup()


if __name__ == "__main__":
    unittest.main()
