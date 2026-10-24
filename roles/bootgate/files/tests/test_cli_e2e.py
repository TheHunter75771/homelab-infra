"""End-to-end tests: run the real bootgate CLI (real Host class) against a fake root.

Stub executables for systemctl, findmnt, udevadm, fsck, umount, rtcwake, docker and
homelab-notify sit first on PATH. They share a JSON 'world' file and log every call, so the
tests check the exact commands bootgate runs: that fsck only ever gets -n, that a reboot is
requested at most once, that --dry-run changes nothing, that sysfs re-bind writes land in the
right files. No root needed.

Run from the repo root:  python3 -m unittest discover -s roles/bootgate/files/tests -v
"""

from __future__ import annotations

import fcntl
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "bootgate.py"
UUID = "0f0e0d0c-1111-4222-8333-944445555666"  # synthetic test value, not a real disk

STUB = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys
    root = os.environ["BOOTGATE_ROOT"]
    name = os.path.basename(sys.argv[0])
    args = sys.argv[1:]
    with open(os.path.join(root, "calls.jsonl"), "a") as fh:
        fh.write(json.dumps([name] + args) + "\\n")
    wpath = os.path.join(root, "world.json")
    world = json.load(open(wpath))
    dev = os.path.join(root, "dev/disk/by-uuid", world["uuid"])
    def save():
        json.dump(world, open(wpath, "w"))
    rc = 0
    if name == "systemctl":
        verb = args[0]
        units = [a for a in args[1:] if not a.startswith("-")]
        if verb == "is-system-running":
            print(world["system_state"])
        elif verb == "start" and units == ["media.mount"]:
            if os.path.exists(dev) and world.get("mount_ok", True):
                world["mounted"] = True
                save()
            else:
                print("Job for media.mount failed.", file=sys.stderr)
                rc = 1
        elif verb == "stop" and units == ["media.mount"]:
            world["mounted"] = False
            save()
    elif name == "findmnt":
        if world.get("mounted"):
            print('UUID="%s" SOURCE="/dev/sdz1" MAJ:MIN="8:1"' % world["uuid"])
        else:
            rc = 1
    elif name == "umount":
        world["mounted"] = False
        save()
    elif name == "fsck":
        print("fsck from util-linux (stub)")
        rc = world.get("fsck_rc", 0)
    sys.exit(rc)
    """
)


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.bin = self.root / "stubbin"
        self.bin.mkdir()
        stub = STUB.format(python=sys.executable)
        for name in ("systemctl", "findmnt", "udevadm", "fsck", "umount", "rtcwake", "docker", "homelab-notify"):
            path = self.bin / name
            path.write_text(stub)
            path.chmod(0o755)
        self.world = {"uuid": UUID, "system_state": "starting", "mounted": False}
        self.save_world()
        # the drive's mount point with its sentinel
        (self.root / "media").mkdir()
        (self.root / "media/.homelab-media-sentinel").write_text("homelab media drive\n")
        # sysfs: block device -> USB port 2-2 on root hub usb2
        dev_sys = (
            self.root / "sys/devices/pci0000:00/0000:00:14.0/usb2/2-2/2-2:1.0/host1/target1:0:0/1:0:0:0/block/sdz/sdz1"
        )
        dev_sys.mkdir(parents=True)
        (self.root / "sys/class/block").mkdir(parents=True)
        (self.root / "sys/class/block/sdz1").symlink_to(dev_sys)
        (self.root / "sys/bus/usb/devices/2-2").mkdir(parents=True)
        (self.root / "sys/bus/usb/drivers/usb").mkdir(parents=True)
        for f in ("bind", "unbind"):
            (self.root / "sys/bus/usb/drivers/usb" / f).write_text("")
        (self.root / "proc/sys/kernel/random").mkdir(parents=True)
        (self.root / "proc/sys/kernel/random/boot_id").write_text("e2e-boot\n")
        (self.root / "srv/homelab/stacks/media").mkdir(parents=True)
        (self.root / "srv/homelab/stacks/media/compose.yaml").write_text("services: {}\n")
        (self.root / "run/bootgate").mkdir(parents=True)
        (self.root / "dev").mkdir()
        (self.root / "dev/sdz1").write_text("")
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "BOOTGATE_ROOT": str(self.root),
            "BOOTGATE_UUID": UUID,
            "BOOTGATE_NOTIFY": str(self.bin / "homelab-notify"),
            "BOOTGATE_WAIT_SECONDS": "0.3",
            "BOOTGATE_RETRY_WAIT_SECONDS": "0.2",
            "BOOTGATE_UDEV_WAIT_SECONDS": "0.1",
            "BOOTGATE_REBIND_WAIT_SECONDS": "0.1",
            "BOOTGATE_POLL_SECONDS": "0.05",
            "BOOTGATE_USB_PAUSE_SECONDS": "0",
            "BOOTGATE_WATCH_CONFIRM_SECONDS": "0",
            "BOOTGATE_POWERCYCLE_GRACE_SECONDS": "0",
            "BOOTGATE_ALERT_RETRY_SECONDS": "0",
        }

    def tearDown(self):
        self.tmp.cleanup()

    # helpers
    def save_world(self):
        (self.root / "world.json").write_text(json.dumps(self.world))

    def plug(self):
        link = self.root / "dev/disk/by-uuid" / UUID
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.is_symlink():
            link.symlink_to("../../sdz1")

    def unplug(self):
        link = self.root / "dev/disk/by-uuid" / UUID
        if link.is_symlink():
            link.unlink()

    def run_gate(self, *args, **env):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return proc.returncode, proc.stdout + proc.stderr

    def calls(self, name=None):
        path = self.root / "calls.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        return [r for r in rows if name is None or r[0] == name]

    def state(self, name):
        path = self.root / "var/lib/bootgate" / name
        return path.read_text().strip() if path.exists() else None

    def events(self):
        path = self.root / "var/lib/bootgate/history.jsonl"
        return [json.loads(x)["event"] for x in path.read_text().splitlines()] if path.exists() else []


class BootCli(CliTestCase):
    def test_boot_pass_mounts_verifies_and_records_port(self):
        self.plug()
        rc, out = self.run_gate("check", "--boot")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.state("status"), "ok")
        self.assertEqual(self.state("attempts"), "0")
        self.assertEqual(self.state("usb-port"), "2-2")
        self.assertEqual(self.events(), ["pass"])
        self.assertIn(["systemctl", "start", "--no-ask-password", "media.mount"], self.calls())
        self.assertFalse((self.root / "media/.bootgate-write-test").exists(), "write test cleans up")

    def test_boot_missing_reboots_once_then_fails_closed(self):
        (self.root / "var/lib/bootgate").mkdir(parents=True)
        (self.root / "var/lib/bootgate/usb-port").write_text("2-2\n")
        rc, out = self.run_gate("check", "--boot")
        # The stub 'reboot' returns and the host stays up, so the gate fails closed.
        self.assertEqual(rc, 1, out)
        reboots = [c for c in self.calls("systemctl") if c[1] == "reboot"]
        self.assertEqual(len(reboots), 1)
        self.assertEqual(self.state("attempts"), "1")
        self.assertEqual(self.events()[-2:], ["powercycle", "fail"])
        self.assertEqual((self.root / "sys/bus/usb/drivers/usb/unbind").read_text(), "2-2")
        self.assertEqual((self.root / "sys/bus/usb/drivers/usb/bind").read_text(), "2-2")
        self.assertIn(["udevadm", "trigger", "--action=add", "--subsystem-match=block"], self.calls())

        rc, out = self.run_gate("check", "--boot")  # the next boot, drive still missing
        self.assertEqual(rc, 1, out)
        reboots = [c for c in self.calls("systemctl") if c[1] == "reboot"]
        self.assertEqual(len(reboots), 1, "never a second power-cycle")
        self.assertEqual(self.state("status"), "failed")

    def test_rtcwake_command(self):
        rc, _ = self.run_gate("check", "--boot", BOOTGATE_POWERCYCLE_METHOD="rtcwake")
        self.assertEqual(rc, 1)
        self.assertEqual(self.calls("rtcwake"), [["rtcwake", "-m", "off", "-s", "60"]])

    def test_running_system_never_reboots(self):
        self.world["system_state"] = "degraded"
        self.save_world()
        rc, out = self.run_gate("check", "--boot")
        self.assertEqual(rc, 1, out)
        self.assertFalse([c for c in self.calls("systemctl") if c[1] == "reboot"])
        self.assertIsNone(self.state("attempts"))

    def test_fsck_is_only_ever_read_only(self):
        self.plug()
        self.world.update(mount_ok=False, fsck_rc=4, system_state="running")
        self.save_world()
        rc, out = self.run_gate("check", "--boot")
        self.assertEqual(rc, 1, out)
        fscks = self.calls("fsck")
        self.assertEqual(fscks, [["fsck", "-n", str(self.root / "dev/disk/by-uuid" / UUID)]])
        self.assertIn("run e2fsck by hand", out)

    def test_dry_run_changes_nothing(self):
        (self.root / "var/lib/bootgate").mkdir(parents=True)
        (self.root / "var/lib/bootgate/usb-port").write_text("2-2\n")
        rc, out = self.run_gate("--dry-run", "check", "--boot")
        self.assertEqual(rc, 1, out)
        self.assertEqual(sorted(os.listdir(self.root / "var/lib/bootgate")), ["usb-port"])
        mutating = [c for c in self.calls() if c[0] != "findmnt" and c[:2] != ["systemctl", "is-system-running"]]
        self.assertEqual(mutating, [], "dry-run ran a mutating command")
        self.assertEqual((self.root / "sys/bus/usb/drivers/usb/unbind").read_text(), "")
        self.assertIn("DRY-RUN would run: systemctl reboot", out)
        self.assertIn("DRY-RUN would write attempts='1'", out)

    def test_missing_config_fails_closed(self):
        rc, out = self.run_gate("check", "--boot", BOOTGATE_UUID="")
        self.assertEqual(rc, 1)
        self.assertIn("BOOTGATE_UUID", out)


class RetryWatchCli(CliTestCase):
    def test_rerun_trigger_is_consumed_and_retry_recovers(self):
        (self.root / "var/lib/bootgate").mkdir(parents=True)
        (self.root / "var/lib/bootgate/status").write_text("failed\n")
        (self.root / "var/lib/bootgate/attempts").write_text("1\n")
        (self.root / "run/bootgate/rerun").write_text("")
        self.plug()
        rc, out = self.run_gate("check", "--retry")
        self.assertEqual(rc, 0, out)
        self.assertFalse((self.root / "run/bootgate/rerun").exists())
        self.assertEqual(self.state("status"), "ok")
        self.assertEqual(self.state("attempts"), "0")
        self.assertIn(["systemctl", "start", "--no-ask-password", "docker.service"], self.calls())
        notes = self.calls("homelab-notify")
        self.assertEqual(notes[0][1:5], ["--tag", "bootgate", "--title", "Media drive back"])
        self.assertFalse([c for c in self.calls("systemctl") if c[1] == "reboot"])

    def test_watch_stops_media_stack_then_retry_brings_it_back(self):
        self.plug()
        self.assertEqual(self.run_gate("check", "--boot")[0], 0)
        self.unplug()  # the drive drops off the bus; the kernel mount goes stale
        self.world["mounted"] = True
        self.save_world()
        (self.root / "media/.homelab-media-sentinel").unlink()  # what a dead mount looks like here
        rc, out = self.run_gate("watch")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.state("status"), "media-lost")
        compose = str(self.root / "srv/homelab/stacks/media/compose.yaml")
        self.assertIn(["docker", "compose", "-f", "/srv/homelab/stacks/media/compose.yaml", "stop"], self.calls())
        self.assertTrue(pathlib.Path(compose).exists())
        title = self.calls("homelab-notify")[-1][4]
        self.assertEqual(title, "Media drive lost")

        (self.root / "media/.homelab-media-sentinel").write_text("homelab media drive\n")
        self.plug()
        rc, out = self.run_gate("check", "--retry")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.state("status"), "ok")
        self.assertIn(["docker", "compose", "-f", "/srv/homelab/stacks/media/compose.yaml", "up", "-d"], self.calls())
        self.assertEqual(self.events(), ["pass", "media-lost", "retry-pass"])

    def test_status_command(self):
        self.plug()
        self.run_gate("check", "--boot")
        rc, out = self.run_gate("status")
        self.assertEqual(rc, 0)
        self.assertIn("status:   ok", out)
        self.assertIn("usb-port: 2-2", out)


def load_bootgate():
    spec = importlib.util.spec_from_file_location("bootgate_inproc", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HungPopen:
    """A child stuck in uninterruptible I/O: never exits, ignores SIGKILL. wait() without a
    timeout would block forever, so here it fails the test instead of hanging the suite."""

    instances: list[HungPopen] = []

    def __init__(self, argv, **_kw):
        self.argv = argv
        self.pid = 4_000_000 + len(HungPopen.instances)
        self.killed = False
        self.returncode = None
        HungPopen.instances.append(self)

    def wait(self, timeout=None):
        if timeout is None:
            raise AssertionError("unbounded wait() on a child stuck in D-state")
        raise subprocess.TimeoutExpired(self.argv, timeout)

    def kill(self):
        self.killed = True  # SIGKILL is queued, but a D-state process does not die

    def poll(self):
        return None


class HungChildren(CliTestCase):
    """B1: a hung USB device must only ever hang a child, never the gate."""

    def setUp(self):
        super().setUp()
        HungPopen.instances = []
        self.bg = load_bootgate()
        self.real_popen = subprocess.Popen
        self.env.update(BOOTGATE_WRITE_TEST_TIMEOUT="0.2")
        self.environ = mock.patch.dict(os.environ, self.env)
        self.environ.start()
        self.out = io.StringIO()
        self.log = self.bg.Log(stream=self.out)

    def tearDown(self):
        self.environ.stop()
        super().tearDown()

    def popen_hanging(self, match):
        real = self.real_popen

        def factory(argv, **kw):
            if any(match in str(a) for a in argv):
                return HungPopen(argv, **kw)
            return real(argv, **kw)

        return mock.patch.object(self.bg.subprocess, "Popen", side_effect=factory)

    def test_run_abandons_a_child_that_ignores_sigkill(self):
        host = self.bg.Host(self.bg.Config(), self.log)
        with self.popen_hanging("hung-disk-tool"):
            started = time.monotonic()
            rc, out = host._run(["hung-disk-tool", "--read"], timeout=0.1)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(rc, 124)
        self.assertIn("could not be killed", out)
        self.assertTrue(HungPopen.instances[0].killed)
        self.assertEqual(host.last_abandoned_pid, HungPopen.instances[0].pid)

    def test_run_kills_a_merely_slow_child(self):
        host = self.bg.Host(self.bg.Config(), self.log)
        started = time.monotonic()
        rc, out = host._run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.3)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual((rc, host.last_abandoned_pid), (124, None))
        self.assertIn("killed", out)

    def test_run_captures_large_output_without_pipes(self):
        host = self.bg.Host(self.bg.Config(), self.log)
        rc, out = host._run([sys.executable, "-c", "print('x' * 1_000_000)"], timeout=30)
        self.assertEqual((rc, len(out)), (0, 1_000_000))

    def test_watch_with_a_hung_drive_reports_media_lost_and_releases_the_lock(self):
        self.plug()
        self.assertEqual(self.run_gate("check", "--boot")[0], 0)  # gate passed earlier
        # From now on every drive probe hangs in D-state (sentinel stat / write never return).
        (self.root / "proc/4000000").mkdir(parents=True)
        (self.root / "proc/4000000/cmdline").write_bytes(b"/usr/bin/python3\0bootgate\0_probe\0/media\0")
        with self.popen_hanging("_probe"):
            started = time.monotonic()
            rc = self.bg.main(["watch"], log=self.log)
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertEqual(rc, 0, self.out.getvalue())
        self.assertEqual(self.state("status"), "media-lost")
        last = json.loads((self.root / "var/lib/bootgate/history.jsonl").read_text().splitlines()[-1])
        self.assertEqual(last["event"], "media-lost")
        self.assertIn("still stuck", last["detail"])  # the 5 s re-check refused to stack a second probe
        self.assertIn("drive probe hung", self.out.getvalue())  # the first check
        self.assertEqual(len(HungPopen.instances), 1, "the re-check must not start a second stuck probe")
        self.assertIn("still stuck", self.out.getvalue())
        self.assertEqual(self.state("../../../run/bootgate/stuck-probe"), "4000000")
        self.assertIn(["docker", "compose", "-f", "/srv/homelab/stacks/media/compose.yaml", "stop"], self.calls())
        self.assertEqual(self.calls("homelab-notify")[-1][4], "Media drive lost")
        fd = os.open(self.root / "var/lib/bootgate/.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if watch() still held it
        finally:
            os.close(fd)

    def test_sentinel_is_checked_in_the_child_not_in_the_gate(self):
        self.plug()
        self.world["mounted"] = True
        self.save_world()
        host = self.bg.Host(self.bg.Config(), self.log)
        real_stat = os.stat
        sentinel = str(self.root / "media/.homelab-media-sentinel")

        def guarded_stat(path, *a, **kw):
            if str(path) == sentinel:
                raise AssertionError("the gate process touched the drive's filesystem")
            return real_stat(path, *a, **kw)

        with mock.patch.object(self.bg.os, "stat", side_effect=guarded_stat):
            self.assertEqual(host.probe_drive(), (True, "ok"))
        (self.root / "media/.homelab-media-sentinel").unlink()
        self.assertEqual(host.probe_drive(), (False, "sentinel .homelab-media-sentinel is missing"))

    def test_stuck_marker_is_cleared_once_the_probe_dies(self):
        (self.root / "run/bootgate/stuck-probe").write_text("4000123\n")  # no such /proc entry
        host = self.bg.Host(self.bg.Config(), self.log)
        self.assertIsNone(host._stuck_probe())
        self.assertFalse((self.root / "run/bootgate/stuck-probe").exists())


if __name__ == "__main__":
    unittest.main()
