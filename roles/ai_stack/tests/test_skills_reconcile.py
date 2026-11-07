"""Unit tests for roles/ai_stack/files/skills_reconcile.py (stdlib only).

  python3 -m unittest discover -s roles/ai_stack/tests -p 'test_*.py' -v
"""

import contextlib
import importlib.util
import io
import os
import shutil
import socket
import subprocess
import time
import unittest
from unittest import mock

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "files", "skills_reconcile.py")
spec = importlib.util.spec_from_file_location("skills_reconcile", SCRIPT)
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)

UNIT = "var-lib-homelab\\x2dai-skills.mount"
ARGS = ["--host", "100.64.0.20", "--where", "/var/lib/homelab-ai/skills"]


class FakeSystemctl:
    def __init__(self, active, fail=()):
        self.active, self.fail, self.calls = active, set(fail), []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "is-active":
            return 0 if self.active else 3
        return 1 if args[0] in self.fail else 0


class ReconcileTest(unittest.TestCase):
    def run_case(self, up, active, fail=(), argv=ARGS):
        probes = []

        def probe(host, port, timeout):
            probes.append((host, port, timeout))
            return up

        ctl = FakeSystemctl(active, fail)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = rec.main(argv, probe=probe, run=ctl)
        return rc, ctl.calls, probes, out.getvalue()

    def test_pc_up_mount_active_does_nothing(self):
        rc, calls, probes, out = self.run_case(up=True, active=True)
        self.assertEqual((rc, calls), (0, [("is-active", "--quiet", UNIT)]))
        self.assertEqual(probes, [("100.64.0.20", 445, 4.0)])
        self.assertIn("-> none", out)

    def test_pc_up_mount_inactive_starts_it(self):
        rc, calls, _, out = self.run_case(up=True, active=False)
        self.assertEqual((rc, calls), (0, [("is-active", "--quiet", UNIT), ("start", "--no-block", UNIT)]))
        self.assertIn("-> start", out)

    def test_pc_down_mount_active_stops_it(self):
        rc, calls, _, out = self.run_case(up=False, active=True)
        self.assertEqual((rc, calls), (0, [("is-active", "--quiet", UNIT), ("stop", "--no-block", UNIT)]))
        self.assertIn("unreachable", out)

    def test_pc_down_mount_inactive_does_nothing(self):
        rc, calls, _, _ = self.run_case(up=False, active=False)
        self.assertEqual((rc, calls), (0, [("is-active", "--quiet", UNIT)]))

    def test_a_failed_systemctl_call_fails_the_pass(self):
        rc, _, _, out = self.run_case(up=True, active=False, fail=("start",))
        self.assertEqual(rc, 1)
        self.assertIn("FAILED", out)

    def test_never_touches_the_mount_path(self):
        touched = []

        def guard(real):
            def wrapper(path, *args, **kwargs):
                if "homelab-ai" in os.fsdecode(path) if isinstance(path, (str, bytes, os.PathLike)) else False:
                    touched.append(path)
                    raise AssertionError(f"filesystem access to {path!r}")
                return real(path, *args, **kwargs)

            return wrapper

        names = ("stat", "lstat", "listdir", "scandir", "access", "open")
        patches = [mock.patch(f"os.{n}", guard(getattr(os, n))) for n in names]
        patches.append(mock.patch("builtins.open", guard(open)))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            for up in (True, False):
                for active in (True, False):
                    self.assertEqual(self.run_case(up, active)[0], 0)
        self.assertEqual(touched, [])

    def test_timeout_is_bounded(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rec.main([*ARGS, "--timeout", "30"], probe=lambda *a: True, run=FakeSystemctl(True))
        _, _, probes, _ = self.run_case(True, True, argv=[*ARGS, "--timeout", "3", "--port", "4450"])
        self.assertEqual(probes, [("100.64.0.20", 4450, 3.0)])


class SeveralMountsTest(unittest.TestCase):
    """One probe of the PC, then the same decision for every --where (skills and writing, §12.21)."""

    SKILLS, WRITING = UNIT, "var-lib-homelab\\x2dai-writing.mount"
    ARGV = (*ARGS, "--where", "/var/lib/homelab-ai/writing")

    def run_case(self, up, active_units, fail=(), raises=None):
        raises = raises or {}
        probes = []

        def probe(*a):
            probes.append(a)
            return up

        calls = []

        def ctl(*args):
            calls.append(args)
            if (args[0], args[-1]) in raises:
                raise raises[(args[0], args[-1])]
            if args[0] == "is-active":
                return 0 if args[2] in active_units else 3
            return 1 if (args[0], args[-1]) in fail else 0

        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = rec.main(list(self.ARGV), probe=probe, run=ctl)
        return rc, calls, probes, out.getvalue().splitlines()

    def test_one_probe_and_one_line_per_mount(self):
        rc, calls, probes, lines = self.run_case(True, {self.SKILLS, self.WRITING})
        self.assertEqual((rc, len(probes), len(lines)), (0, 1, 2))
        self.assertEqual(calls, [("is-active", "--quiet", self.SKILLS), ("is-active", "--quiet", self.WRITING)])

    def test_pc_up_mounts_only_the_inactive_one(self):
        rc, calls, _, _ = self.run_case(True, {self.SKILLS})
        self.assertEqual(rc, 0)
        self.assertEqual([c for c in calls if c[0] != "is-active"], [("start", "--no-block", self.WRITING)])

    def test_pc_down_unmounts_every_active_one(self):
        rc, calls, _, _ = self.run_case(False, {self.SKILLS, self.WRITING})
        self.assertEqual(rc, 0)
        self.assertEqual([c for c in calls if c[0] != "is-active"], [("stop", "--no-block", self.SKILLS), ("stop", "--no-block", self.WRITING)])

    def test_pc_down_leaves_inactive_ones(self):
        rc, calls, _, _ = self.run_case(False, {self.WRITING})
        self.assertEqual(rc, 0)
        self.assertEqual([c for c in calls if c[0] != "is-active"], [("stop", "--no-block", self.WRITING)])

    def test_a_failure_on_one_still_handles_the_other_and_fails_the_pass(self):
        rc, calls, _, lines = self.run_case(False, {self.SKILLS, self.WRITING}, fail={("stop", self.SKILLS)})
        self.assertEqual(rc, 1)
        self.assertIn(("stop", "--no-block", self.WRITING), calls)
        self.assertIn("FAILED", lines[0])
        self.assertNotIn("FAILED", lines[1])

    def test_start_and_stop_never_block(self):
        for up, active in ((True, set()), (False, {self.SKILLS, self.WRITING})):
            _, calls, _, _ = self.run_case(up, active)
            actions = [c for c in calls if c[0] != "is-active"]
            self.assertEqual(len(actions), 2)
            self.assertTrue(all(c[1] == "--no-block" and len(c) == 3 for c in actions), actions)

    def test_a_hanging_start_on_the_first_share_still_handles_the_second(self):
        hang = subprocess.TimeoutExpired(["systemctl", "start", "--no-block", self.SKILLS], 10)
        rc, calls, _, lines = self.run_case(True, set(), raises={("start", self.SKILLS): hang})
        self.assertEqual(rc, 1)
        self.assertIn(("start", "--no-block", self.WRITING), calls)
        self.assertIn("-> start FAILED (TimeoutExpired", lines[0])
        self.assertTrue(lines[1].endswith("-> start"), lines[1])

    def test_a_hanging_is_active_or_missing_systemctl_is_contained(self):
        hang = subprocess.TimeoutExpired(["systemctl"], 10)
        rc, calls, _, lines = self.run_case(False, {self.WRITING}, raises={("is-active", self.SKILLS): hang})
        self.assertEqual(rc, 1)
        self.assertIn("unknown -> none FAILED (TimeoutExpired", lines[0])
        self.assertIn(("stop", "--no-block", self.WRITING), calls)
        rc, _, _, lines = self.run_case(True, set(), raises={("start", self.WRITING): FileNotFoundError("systemctl")})
        self.assertEqual(rc, 1)
        self.assertIn("-> start FAILED (FileNotFoundError", lines[1])

    def test_a_hanging_stop_is_logged_as_stop(self):
        hang = subprocess.TimeoutExpired(["systemctl", "stop", "--no-block", self.SKILLS], 10)
        rc, calls, _, lines = self.run_case(False, {self.SKILLS, self.WRITING}, raises={("stop", self.SKILLS): hang})
        self.assertEqual(rc, 1)
        self.assertIn("-> stop FAILED (TimeoutExpired", lines[0])
        self.assertIn(("stop", "--no-block", self.WRITING), calls)
        self.assertTrue(lines[1].endswith("-> stop"), lines[1])

    def test_real_wrapper_timeout_fails_the_pass(self):
        """main() with the REAL systemctl() wrapper: a subprocess timeout must surface as a failure, never as
        a clean rc (0 would read as "active"/"done", 3 as "inactive")."""

        def fake_run(argv, **kwargs):
            if argv[1] == "start":
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            result = mock.Mock()
            result.returncode = 3  # is-active: inactive
            return result

        with (
            mock.patch.object(rec.subprocess, "run", side_effect=fake_run) as run,
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            rc = rec.main(list(self.ARGV), probe=lambda *a: True)
        lines = out.getvalue().splitlines()
        self.assertEqual(rc, 1)
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIn("inactive -> start FAILED (TimeoutExpired", line)
        self.assertEqual([c.args[0][1] for c in run.call_args_list], ["is-active", "start", "is-active", "start"])
        self.assertTrue(all(c.kwargs["timeout"] == rec.SYSTEMCTL_TIMEOUT for c in run.call_args_list))

    def test_the_real_systemctl_call_is_bounded(self):
        self.assertLessEqual(rec.SYSTEMCTL_TIMEOUT, 15)
        with mock.patch.object(rec.subprocess, "run") as run:
            run.return_value.returncode = 0
            rec.systemctl("start", "--no-block", self.SKILLS)
        self.assertEqual(run.call_args.args[0], ["systemctl", "start", "--no-block", self.SKILLS])
        self.assertEqual(run.call_args.kwargs["timeout"], rec.SYSTEMCTL_TIMEOUT)

    def test_the_same_mount_twice_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rec.main([*ARGS, "--where", "/var/lib/homelab-ai/skills/"], probe=lambda *a: True, run=FakeSystemctl(True))


class ProbeTest(unittest.TestCase):
    def test_real_probe_open_and_closed_port(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            self.assertTrue(rec.reachable("127.0.0.1", port, 2))
        finally:
            srv.close()
        self.assertFalse(rec.reachable("127.0.0.1", port, 2))

    def test_unroutable_host_returns_within_the_timeout(self):
        start = time.monotonic()
        # TEST-NET-1: never answers (or fails fast without a route).
        self.assertFalse(rec.reachable("192.0.2.1", 445, 1.0))
        self.assertLess(time.monotonic() - start, 3)


class UnitNameTest(unittest.TestCase):
    PATHS = ("/var/lib/homelab-ai/skills", "/srv/ai-library", "/a/.hid/x y", "/.x/y", "/mnt/ü-ä", "/a//b/")

    def test_known(self):
        self.assertEqual(rec.mount_unit("/var/lib/homelab-ai/skills"), UNIT)

    def test_refuses_unclean_paths(self):
        for bad in ("relative/path", "/", "/a/../b", "/a/./b"):
            with self.assertRaises(ValueError):
                rec.mount_unit(bad)

    @unittest.skipUnless(shutil.which("systemd-escape"), "systemd-escape not installed")
    def test_matches_systemd_escape(self):
        for path in self.PATHS:
            want = subprocess.run(
                ["systemd-escape", "--path", "--suffix=mount", path], capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(rec.mount_unit(path), want, path)


if __name__ == "__main__":
    unittest.main()
