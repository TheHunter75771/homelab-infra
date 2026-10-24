"""Decision-logic tests for bootgate: a fake host with a fake clock, real state files in a
temporary BOOTGATE_ROOT. No root, no network, no real devices.

Run from the repo root:  python3 -m unittest discover -s roles/bootgate/files/tests -v
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("bootgate", HERE.parent / "bootgate.py")
bootgate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootgate)

UUID = "0f0e0d0c-1111-4222-8333-944445555666"  # synthetic test value, not a real disk


class FakeHost:
    """Models the drive, the mount, USB and systemd with a fake monotonic clock."""

    def __init__(
        self,
        cfg,
        *,
        booting=True,
        device_at=0.0,
        mount_ok=True,
        sentinel=True,
        write_results=None,
        port="2-2",
        udev_fixes=False,
        rebind_fixes=False,
        mounted=None,
        notify_rcs=None,
        dev_majmin="8:1",
        mount_majmin="8:1",
        docker_ok=True,
        compose_up_ok=True,
    ):
        self.cfg = cfg
        self.t = 0.0
        self._booting = booting
        self.device_at = device_at  # None = never appears on its own
        self.mount_ok = mount_ok
        self.sentinel = sentinel
        self.write_results = list(write_results or [])  # consumed in order; default pass
        self.port = port
        self.udev_fixes = udev_fixes
        self.rebind_fixes = rebind_fixes
        self.mounted = mounted  # UUID string at the mount point, "" = stale, None = nothing
        self.notify_rcs = list(notify_rcs or [])
        self.dev_majmin = dev_majmin  # what /dev/disk/by-uuid/<uuid> points at now
        self.mount_majmin = mount_majmin  # what the mount at the mount point was made from
        self.docker_ok = docker_ok
        self.compose_up_ok = compose_up_ok
        self.calls: list[tuple] = []
        self.notes: list[tuple[str, str]] = []

    # time
    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(seconds, 0.0)

    def boot_id(self):
        return "boot-under-test"

    def booting(self):
        return self._booting

    # drive
    def device_present(self):
        return self.device_at is not None and self.t >= self.device_at

    def device_majmin(self):
        return self.dev_majmin if self.device_present() else None

    def mount_info(self):
        if self.mounted is None:
            return None
        return {"UUID": self.mounted, "SOURCE": "/dev/sdz1", "MAJ:MIN": self.mount_majmin}

    def start_mount(self):
        self.calls.append(("start_mount",))
        if self.device_present() and self.mount_ok:
            self.mounted = UUID
            self.mount_majmin = self.dev_majmin
            return True, ""
        return False, "mount: wrong fs type, bad superblock"

    def stop_mount(self):
        self.calls.append(("stop_mount",))
        self.mounted = None
        return "stopped"

    def probe_drive(self):
        self.calls.append(("probe",))
        if not self.sentinel:
            return False, "sentinel .homelab-media-sentinel is missing"
        if self.write_results:
            ok = self.write_results.pop(0)
            return ok, "ok" if ok else "write test failed: Input/output error"
        return True, "ok"

    def udev_retrigger(self):
        self.calls.append(("udev",))
        if self.udev_fixes:
            self.device_at = self.t
        return "udevadm trigger rc=0; settle rc=0"

    def usb_port_live(self):
        return self.port if self.device_present() else None

    def usb_rebind(self, port):
        self.calls.append(("usb_rebind", port))
        if self.rebind_fixes:
            self.device_at = self.t + 7
        return True, f"usb {port}: unbind ok; bind ok"

    def fsck_readonly(self):
        self.calls.append(("fsck_n",))
        return 4, "e2fsck: filesystem has errors (not fixed: -n)"

    def power_cycle(self):
        self.calls.append(("power_cycle", self.cfg.powercycle_method))
        raise bootgate.PowerCycleRequested

    # docker / notify
    def start_docker(self):
        self.calls.append(("start_docker",))
        return (True, "") if self.docker_ok else (False, "Job for docker.service failed")

    def compose_available(self):
        return True

    def compose(self, action):
        self.calls.append(("compose", action))
        if action == "up" and not self.compose_up_ok:
            return False, "Error response from daemon: driver failed programming external connectivity"
        return True, ""

    def notify(self, title, body):
        self.notes.append((title, body))
        return self.notify_rcs.pop(0) if self.notify_rcs else 0

    def clear_rerun_trigger(self):
        self.calls.append(("clear_rerun",))
        return False

    def count(self, name):
        return sum(1 for c in self.calls if c[0] == name)


class GateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.env = {
            "BOOTGATE_ROOT": self.root,
            "BOOTGATE_UUID": UUID,
            "BOOTGATE_WAIT_SECONDS": "720",
            "BOOTGATE_RETRY_WAIT_SECONDS": "30",
            "BOOTGATE_POLL_SECONDS": "2",
            "BOOTGATE_WATCH_CONFIRM_SECONDS": "5",
            "BOOTGATE_ALERT_RETRY_SECONDS": "30",
        }
        self.cfg = bootgate.Config(self.env)
        self.log = bootgate.Log(stream=open(os.devnull, "w"))  # noqa: SIM115
        self.store = bootgate.Store(self.cfg, self.log)
        self.state = pathlib.Path(self.cfg.state_dir)

    def tearDown(self):
        self.log.stream.close()
        self.tmp.cleanup()

    def gate(self, **kw):
        host = FakeHost(self.cfg, **kw)
        return bootgate.Gate(self.cfg, host, self.store, self.log), host

    def history(self):
        path = self.state / "history.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def events(self):
        return [e["event"] for e in self.history()]

    def read(self, name):
        path = self.state / name
        return path.read_text().strip() if path.exists() else None


class BootGate(GateTestCase):
    def test_pass_drive_ready(self):
        gate, host = self.gate()
        self.assertEqual(gate.check_boot(), 0)
        self.assertEqual(self.read("status"), "ok")
        self.assertEqual(self.read("attempts"), "0")
        self.assertEqual(self.read("usb-port"), "2-2")
        self.assertEqual(self.events(), ["pass"])
        self.assertEqual(self.history()[0]["wait_seconds"], 0.0)
        self.assertEqual(host.count("udev") + host.count("usb_rebind") + host.count("power_cycle"), 0)

    def test_late_arrival_within_wait(self):
        gate, host = self.gate(device_at=559.0)  # the worst cold boot observed
        self.assertEqual(gate.check_boot(), 0)
        self.assertEqual(self.events(), ["pass"])
        entry = self.history()[0]
        self.assertGreaterEqual(entry["wait_seconds"], 559.0)
        self.assertLess(entry["wait_seconds"], 562.0)
        self.assertIn("late arrival", entry["detail"])
        self.assertEqual(host.count("udev") + host.count("power_cycle"), 0)

    def test_missing_runs_ladder_then_power_cycles_exactly_once(self):
        self.store.set_usb_port("2-2")  # verified on an earlier boot
        gate, host = self.gate(device_at=None)
        with self.assertRaises(bootgate.PowerCycleRequested):
            gate.check_boot()
        self.assertEqual(host.count("power_cycle"), 1)
        self.assertEqual(host.calls[-1], ("power_cycle", "reboot"))
        self.assertEqual(self.read("attempts"), "1")
        self.assertEqual(self.events(), ["repair", "repair", "repair", "repair", "powercycle"])
        self.assertIn(("usb_rebind", "2-2"), host.calls)
        self.assertEqual(host.count("udev"), 1)
        self.assertEqual(host.count("fsck_n"), 0, "fsck needs the device")
        self.assertGreaterEqual(self.history()[-1]["wait_seconds"], 720.0)

        # The next boot: still missing. attempts=1, so it fails closed and does NOT reboot.
        gate2, host2 = self.gate(device_at=None)
        self.assertEqual(gate2.check_boot(), 1)
        self.assertEqual(host2.count("power_cycle"), 0)
        self.assertEqual(self.read("status"), "failed")
        self.assertEqual(self.read("attempts"), "1")
        self.assertEqual(self.events()[-1], "fail")
        self.assertIn("attempts=1", self.history()[-1]["detail"])

    def test_attempts_already_spent_fails_without_reboot(self):
        self.store.set_attempts(1)
        gate, host = self.gate(device_at=None)
        self.assertEqual(gate.check_boot(), 1)
        self.assertEqual(host.count("power_cycle"), 0)
        self.assertEqual(self.read("status"), "failed")
        self.assertNotIn("powercycle", self.events())

    def test_rtcwake_method_is_used_when_configured(self):
        self.env["BOOTGATE_POWERCYCLE_METHOD"] = "rtcwake"
        self.cfg = bootgate.Config(self.env)
        self.store = bootgate.Store(self.cfg, self.log)
        gate, host = self.gate(device_at=None)
        with self.assertRaises(bootgate.PowerCycleRequested):
            gate.check_boot()
        self.assertEqual(host.calls[-1], ("power_cycle", "rtcwake"))

    def test_outside_boot_never_power_cycles_and_waits_short(self):
        gate, host = self.gate(device_at=None, booting=False)
        self.assertEqual(gate.check_boot(), 1)
        self.assertEqual(host.count("power_cycle"), 0)
        self.assertEqual(self.read("attempts"), None, "attempts untouched")
        self.assertIn("not a boot-time run", self.history()[-1]["detail"])
        self.assertLess(self.history()[0]["wait_seconds"], 31.0)

    def test_unrecordable_attempts_blocks_power_cycle(self):
        gate, host = self.gate(device_at=None)
        self.store.set_attempts = lambda value: None  # disk full / read-only /var
        self.assertEqual(gate.check_boot(), 1)
        self.assertEqual(host.count("power_cycle"), 0)
        self.assertIn("boot-loop guard", self.history()[-1]["detail"])

    def test_garbage_attempts_counts_as_spent(self):
        self.state.mkdir(parents=True)
        (self.state / "attempts").write_text("??\n")
        gate, host = self.gate(device_at=None)
        self.assertEqual(gate.check_boot(), 1)
        self.assertEqual(host.count("power_cycle"), 0)

    def test_udev_retrigger_repairs_without_rebind(self):
        gate, host = self.gate(device_at=None, udev_fixes=True)
        self.assertEqual(gate.check_boot(), 0)
        self.assertEqual(self.events(), ["repair", "pass"])
        self.assertIn("udev re-trigger", self.history()[-1]["detail"])
        self.assertEqual(host.count("usb_rebind"), 0)
        self.assertEqual(self.read("attempts"), "0")

    def test_usb_rebind_repairs_using_recorded_port(self):
        self.store.set_usb_port("2-2")
        gate, host = self.gate(device_at=None, rebind_fixes=True)
        self.assertEqual(gate.check_boot(), 0)
        self.assertEqual(self.events(), ["repair", "repair", "repair", "pass"])
        self.assertIn(("usb_rebind", "2-2"), host.calls)
        self.assertEqual(host.count("power_cycle"), 0)
        self.assertEqual(self.read("status"), "ok")

    def test_no_recorded_port_skips_rebind(self):
        gate, host = self.gate(device_at=None, booting=False)
        gate.check_boot()
        self.assertEqual(host.count("usb_rebind"), 0)
        self.assertTrue(any("re-bind skipped" in e["detail"] for e in self.history()))

    def test_mount_failure_runs_read_only_fsck(self):
        gate, host = self.gate(mount_ok=False, booting=False)
        self.assertEqual(gate.check_boot(), 1)
        self.assertEqual(host.count("fsck_n"), 1)
        self.assertIn("run e2fsck by hand", self.history()[-1]["detail"])

    def test_missing_sentinel_fails_closed(self):
        gate, _ = self.gate(sentinel=False, booting=False)
        self.assertEqual(gate.check_boot(), 1)
        self.assertEqual(self.read("status"), "failed")
        self.assertIn("sentinel", self.history()[-1]["detail"])

    def test_wrong_filesystem_at_mount_point_is_replaced(self):
        gate, host = self.gate(mounted="")  # stale mount, device gone underneath it
        self.assertEqual(gate.check_boot(), 0)
        self.assertLess(host.calls.index(("stop_mount",)), host.calls.index(("start_mount",)))

    def test_history_matches_contract_1(self):
        gate, _ = self.gate(device_at=None, booting=False)
        gate.check_boot()
        gate2, _ = self.gate(device_at=3.0)
        gate2.check_boot()
        for entry in self.history():
            self.assertEqual(set(entry), {"ts", "boot_id", "event", "detail", "wait_seconds"})
            datetime.fromisoformat(entry["ts"])
            self.assertIsInstance(entry["boot_id"], str)
            self.assertIn(entry["event"], ("pass", "repair", "powercycle", "fail", "retry-pass", "media-lost"))
            self.assertIsInstance(entry["detail"], str)
            self.assertIsInstance(entry["wait_seconds"], float)
        self.assertIn(self.read("status"), ("ok", "waiting", "failed", "media-lost"))


class RetryAndWatch(GateTestCase):
    def test_retry_passes_after_failure(self):
        self.store.set_status("failed")
        self.store.set_attempts(1)
        gate, host = self.gate()
        self.assertEqual(gate.check_retry(), 0)
        self.assertEqual(self.read("status"), "ok")
        self.assertEqual(self.read("attempts"), "0")
        self.assertEqual(self.events(), ["retry-pass"])
        self.assertEqual(host.count("start_docker"), 1)
        self.assertEqual(host.count("compose"), 0)
        self.assertEqual(host.notes[0][0], "Media drive back")

    def test_retry_never_reboots(self):
        self.store.set_status("failed")
        self.store.set_attempts(0)
        gate, host = self.gate(device_at=None)
        self.assertEqual(gate.check_retry(), 0)
        self.assertEqual(host.count("power_cycle") + host.count("usb_rebind"), 0)
        self.assertEqual(self.read("status"), "failed")
        self.assertEqual(self.events(), [], "a failing retry does not spam history")
        self.assertLessEqual(host.t, 31.0)

    def test_retry_ignores_healthy_gate(self):
        self.store.set_status("ok")
        gate, host = self.gate()
        self.assertEqual(gate.check_retry(), 0)
        self.assertEqual(host.count("start_docker") + host.count("start_mount"), 0)

    def test_retry_skips_while_gate_holds_lock(self):
        self.store.set_status("failed")
        fd = os.open(self.state / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            gate, host = self.gate()
            self.assertEqual(gate.check_retry(), 0)
            self.assertEqual(host.count("start_mount") + host.count("start_docker"), 0)
            self.assertEqual(host.count("clear_rerun"), 1, "trigger is consumed even when busy")
        finally:
            os.close(fd)

    def test_retry_recovers_stale_waiting(self):
        self.store.set_status("waiting")  # a gate run that was killed mid-way
        gate, _ = self.gate()
        self.assertEqual(gate.check_retry(), 0)
        self.assertEqual(self.read("status"), "ok")

    def test_watch_detects_drop_out(self):
        self.store.set_status("ok")
        gate, host = self.gate(mounted=UUID, write_results=[False, False])
        self.assertEqual(gate.watch(), 0)
        self.assertEqual(self.read("status"), "media-lost")
        self.assertEqual(self.events(), ["media-lost"])
        self.assertIn(("compose", "stop"), host.calls)
        self.assertTrue((self.state / "media-stopped").exists())
        self.assertEqual(host.notes[0][0], "Media drive lost")
        self.assertIn("media apps stopped", host.notes[0][1])

    def test_watch_catches_a_stale_mount_whose_uuid_is_still_reported(self):
        # The drive dropped off and came back as a new sdX; the dead mount (8:1) is still on
        # /media and findmnt still reports the UUID from cache. Verify must not trust it.
        self.store.set_status("ok")
        gate, host = self.gate(mounted=UUID, mount_majmin="8:1", dev_majmin="8:17")
        self.assertEqual(gate.watch(), 0)
        self.assertEqual(self.read("status"), "media-lost")
        self.assertIn("stale mount", self.history()[-1]["detail"])
        self.assertEqual(host.count("probe"), 0, "no I/O against a stale mount")

    def test_retry_replaces_a_stale_mount_whose_uuid_is_still_reported(self):
        self.store.set_status("media-lost")
        gate, host = self.gate(mounted=UUID, mount_majmin="8:1", dev_majmin="8:17")
        self.assertEqual(gate.check_retry(), 0)
        self.assertEqual(self.read("status"), "ok")
        self.assertLess(host.calls.index(("stop_mount",)), host.calls.index(("start_mount",)))
        self.assertEqual(host.mount_majmin, "8:17")

    def test_watch_ignores_a_transient_failure(self):
        self.store.set_status("ok")
        gate, host = self.gate(mounted=UUID, write_results=[False, True])
        self.assertEqual(gate.watch(), 0)
        self.assertEqual(self.read("status"), "ok")
        self.assertEqual(host.count("compose"), 0)

    def test_watch_only_acts_when_ok(self):
        for status in ("failed", "waiting", "media-lost"):
            self.store.set_status(status)
            gate, host = self.gate(mounted=None)
            self.assertEqual(gate.watch(), 0)
            self.assertEqual(host.calls, [])
            self.assertEqual(self.read("status"), status)

    def test_media_lost_then_retry_restores_stack(self):
        self.store.set_status("ok")
        gate, _ = self.gate(mounted=UUID, write_results=[False, False])
        gate.watch()
        gate2, host2 = self.gate(mounted="")  # the old mount went stale; drive is back
        self.assertEqual(gate2.check_retry(), 0)
        self.assertEqual(self.read("status"), "ok")
        self.assertEqual(self.events(), ["media-lost", "retry-pass"])
        self.assertIn(("compose", "up"), host2.calls)
        self.assertFalse((self.state / "media-stopped").exists())
        self.assertIn("media apps restarted", host2.notes[0][1])

    def test_boot_pass_after_media_lost_restarts_stack_on_next_retry(self):
        # Drive dropped, watcher stopped the stack, host rebooted and the gate passed at boot.
        self.store.set_status("ok")
        self.store.set_media_stopped(True)
        gate, host = self.gate(mounted=UUID)
        self.assertEqual(gate.check_retry(), 0)
        self.assertIn(("compose", "up"), host.calls)
        self.assertFalse((self.state / "media-stopped").exists())
        self.assertEqual(host.count("start_mount"), 0)

    def test_alert_retries_until_delivered(self):
        self.store.set_status("failed")
        self.store.history("fail", "media device did not appear within 720s", 720.0, "b")
        gate, host = self.gate(notify_rcs=[1, 1, 0])
        self.assertEqual(gate.alert(), 0)
        self.assertEqual(len(host.notes), 3)
        self.assertEqual(host.notes[0][0], "Media drive failed")
        self.assertIn("did not appear", host.notes[0][1])

    def test_alert_stops_when_nobody_is_subscribed(self):
        gate, host = self.gate(notify_rcs=[2])
        self.assertEqual(gate.alert(), 2)
        self.assertEqual(len(host.notes), 1)


# Mirror of the dashboard's recovery rule (apps/dashboard/server/src/collectors/alerts.js,
# isRecovery): a title resolves its incident only if it ENDS with a recovery phrase and holds
# no failure qualifier anywhere. RecoveryTitles also checks the real one when node is present.
RECOVERY_END_RE = re.compile(r"\b(?:back|recovered|online|resolved|healthy again|ok again|up again)[\s.!]*$", re.I)
QUALIFIER_RE = re.compile(
    r"\b(?:not|no|never|unable|cannot|fail(?:s|ed|ing|ure)?|errors?|still|but|however|except|partial(?:ly)?|"
    r"degraded|without|pending)\b|\w+n['\u2019]t\b",
    re.I,
)


def is_recovery(title):
    return bool(RECOVERY_END_RE.search(title.strip())) and not QUALIFIER_RE.search(title)


class RecoveryTitles(GateTestCase):
    """Reviewer major 1: "Media drive back" only when Docker AND the media apps are back."""

    def media_lost_retry(self, **kw):
        self.store.set_status("media-lost")
        self.store.set_media_stopped(True)
        gate, host = self.gate(**kw)
        return gate.check_retry(), host

    def test_docker_and_media_apps_back_is_the_only_plain_recovery(self):
        rc, host = self.media_lost_retry()
        self.assertEqual(rc, 0)
        self.assertEqual(host.notes[-1][0], "Media drive back")
        self.assertIn(("compose", "up"), host.calls)
        self.assertFalse(self.store.media_stopped())

    def test_media_apps_failing_to_restart_keeps_the_incident_open(self):
        rc, host = self.media_lost_retry(compose_up_ok=False)
        self.assertEqual(rc, 1)
        title, body = host.notes[-1]
        self.assertEqual(title, "Media drive back, media apps did not restart")
        self.assertIn("media apps restart FAILED", body)
        self.assertTrue(self.store.media_stopped(), "kept so the next retry restarts them")
        self.assertEqual(self.read("status"), "ok", "the drive itself is verified")

    def test_restore_only_run_with_failing_media_apps_is_qualified_too(self):
        self.store.set_status("ok")
        self.store.set_media_stopped(True)
        gate, host = self.gate(mounted=UUID, compose_up_ok=False)
        self.assertEqual(gate.check_retry(), 1)
        self.assertEqual(host.notes[-1][0], "Media drive back, media apps did not restart")

    def test_docker_not_starting_wins_and_skips_the_media_restart(self):
        rc, host = self.media_lost_retry(docker_ok=False)
        self.assertEqual(rc, 1)
        self.assertEqual(host.notes[-1][0], "Media drive back, Docker did not start")
        self.assertNotIn(("compose", "up"), host.calls)

    def test_failed_status_retry_needs_no_media_restart(self):
        self.store.set_status("failed")
        gate, host = self.gate()
        self.assertEqual(gate.check_retry(), 0)
        self.assertEqual(host.notes[-1][0], "Media drive back")
        self.assertNotIn(("compose", "up"), host.calls)

    def test_titles_against_the_dashboard_rule(self):
        self.assertTrue(is_recovery(bootgate.TITLE_BACK))
        self.assertIsNone(QUALIFIER_RE.search(bootgate.TITLE_BACK), "the plain title must hold no qualifier")
        for title in (bootgate.TITLE_BACK_NO_DOCKER, bootgate.TITLE_BACK_NO_MEDIA):
            self.assertFalse(is_recovery(title), title)
            self.assertIsNotNone(QUALIFIER_RE.search(title), title)
        for title in (bootgate.TITLE_FAILED, bootgate.TITLE_LOST):
            self.assertFalse(is_recovery(title), title)

    def test_titles_against_the_real_dashboard_code(self):
        alerts_js = HERE.parents[3] / "apps/dashboard/server/src/collectors/alerts.js"
        node = shutil.which("node")
        if not node or not alerts_js.exists():
            self.skipTest("node or the dashboard source is not available")
        titles = [
            bootgate.TITLE_BACK,
            bootgate.TITLE_BACK_NO_DOCKER,
            bootgate.TITLE_BACK_NO_MEDIA,
            bootgate.TITLE_LOST,
            bootgate.TITLE_FAILED,
        ]
        script = (
            f"import {{ isRecovery }} from {json.dumps(alerts_js.as_uri())};"
            f"console.log(JSON.stringify({json.dumps(titles)}.map((t) => isRecovery(t))));"
        )
        out = subprocess.run(
            [node, "--input-type=module", "-e", script], check=False, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [True, False, False, False, False])


class Helpers(unittest.TestCase):
    def test_port_connector(self):
        self.assertEqual(bootgate.usb_port_connector("2-2"), ("2-0:1.0", "usb2-port2"))
        self.assertEqual(bootgate.usb_port_connector("2-1.4"), ("2-1:1.0", "2-1-port4"))
        self.assertEqual(bootgate.usb_port_connector("1-3.2.1"), ("1-3.2:1.0", "1-3.2-port1"))

    def test_config_refuses_bad_values(self):
        cfg = bootgate.Config({"BOOTGATE_ROOT": "/nonexistent", "BOOTGATE_POWERCYCLE_METHOD": "kexec"})
        problems = " ".join(cfg.problems())
        self.assertIn("UUID", problems)
        self.assertIn("POWERCYCLE_METHOD", problems)

    def test_config_file_then_env_override(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(f"{root}/etc/default")
            pathlib.Path(f"{root}/etc/default/bootgate").write_text(
                f'# comment\nBOOTGATE_UUID="{UUID}"\nBOOTGATE_WAIT_SECONDS=600\n'
            )
            cfg = bootgate.Config({"BOOTGATE_ROOT": root, "BOOTGATE_WAIT_SECONDS": "5"})
            self.assertEqual(cfg.uuid, UUID)
            self.assertEqual(cfg.wait_seconds, 5.0)
            self.assertEqual(cfg.problems(), [])


if __name__ == "__main__":
    unittest.main()
