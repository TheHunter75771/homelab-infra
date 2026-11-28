"""reboot and set-pin (docs/CONTRACTS.md §11.7.1): the uptime and lock checks, the systemd-run argv
(never really run: FakeSystem answers it), the pin validation, the compose rewrite, the pin record and
the rollback."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import time
import unittest

from fakes import DOCKER, IMAGES, SYSTEMD_RUN, Env, dockerctl, read_lines

OLD = IMAGES["sonarr"]
REPO = "lscr.io/linuxserver/sonarr"
NEW = f"{REPO}:4.0.20.3014-ls7@sha256:" + "c" * 64
NEWER = f"{REPO}:4.0.21.3100-ls9@sha256:" + "d" * 64
SYSTEMCTL = "/usr/bin/systemctl"
REBOOT_UNITS = ["homelab-dashboard-reboot.timer", "homelab-dashboard-reboot.service"]
REBOOT_ARGV = [SYSTEMD_RUN, "--unit=homelab-dashboard-reboot", "--collect",
               "--description=Reboot requested from the dashboard",
               "--on-active=10s", "--timer-property=AccuracySec=1s", "/usr/bin/systemctl", "reboot"]


def steps(lines: list[dict]) -> list[str]:
    return [line["step"] for line in lines if line.get("type") == "step"]


def step(lines: list[dict], name: str) -> dict:
    return next(line for line in lines if line.get("step") == name)


class Locked:
    """Hold <cache>/locks/<name>.lock the way another helper instance would."""

    def __init__(self, env: Env, name: str) -> None:
        self.path = env.cache / "locks" / f"{name}.lock"

    def __enter__(self) -> Locked:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, *_exc) -> None:
        os.close(self.fd)


class Reboot(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()

    def tearDown(self) -> None:
        self.env.close()

    def systemd_runs(self) -> list[list[str]]:
        return [c for c in self.env.sys.calls if c[0] == SYSTEMD_RUN]

    def test_schedules_the_reboot_and_replies_first(self) -> None:
        outcome, lines = self.env.call({"op": "reboot"})
        self.assertEqual(outcome, "ok")
        self.assertEqual(lines, [{"ok": True, "op": "reboot", "data": {"scheduled_in": 10}}])
        self.assertEqual(self.systemd_runs(), [REBOOT_ARGV])
        self.assertIn("<4>reboot scheduled in 10 s", self.env.log, "logged at warning level")
        self.assertFalse(self.env.sys.docker_calls(), "a reboot never touches Docker")

    def reboot_systemctl(self) -> list[list[str]]:
        return [c for c in self.env.sys.calls if c[0] == SYSTEMCTL and c[1] in ("is-active", "reset-failed")
                and c[2].startswith("homelab-dashboard-reboot.")]

    def test_the_transient_unit_is_collected_even_when_it_fails(self) -> None:
        self.assertEqual(self.env.call({"op": "reboot"})[0], "ok")
        self.assertIn("--collect", self.systemd_runs()[0])
        # Checked, then any leftover reset (its rc ignored), then scheduled: in that order.
        order = [c for c in self.env.sys.calls if c[0] == SYSTEMD_RUN or c in self.reboot_systemctl()]
        self.assertEqual(order, [[SYSTEMCTL, "is-active", *REBOOT_UNITS], [SYSTEMCTL, "reset-failed", *REBOOT_UNITS],
                                 REBOOT_ARGV])

    def test_a_stale_failed_unit_is_reset_then_scheduled(self) -> None:
        for states in ({"homelab-dashboard-reboot.service": "failed"},
                       {"homelab-dashboard-reboot.timer": "failed", "homelab-dashboard-reboot.service": "failed"},
                       {"homelab-dashboard-reboot.timer": "inactive"}):
            with self.subTest(states=states):
                self.env.sys.calls.clear()
                self.env.sys.reboot_unit_states = dict(states)
                self.env.sys.reset_failed_rc = 1  # e.g. "not loaded": ignored
                outcome, lines = self.env.call({"op": "reboot"})
                self.assertEqual(outcome, "ok")
                self.assertEqual(lines[0]["data"], {"scheduled_in": 10})
                self.assertIn([SYSTEMCTL, "reset-failed", *REBOOT_UNITS], self.env.sys.calls)
                self.assertEqual(self.systemd_runs(), [REBOOT_ARGV])

    def test_an_active_timer_is_already_scheduled(self) -> None:
        for unit, state in (("homelab-dashboard-reboot.timer", "active"),
                            ("homelab-dashboard-reboot.service", "activating")):
            with self.subTest(unit=unit, state=state):
                self.env.sys.calls.clear()
                self.env.sys.reboot_unit_states = {unit: state}
                outcome, lines = self.env.call({"op": "reboot"})
                self.assertEqual(outcome, "busy")
                self.assertEqual(lines[0]["error"]["message"], "a reboot is already scheduled")
                self.assertFalse(self.systemd_runs())
                self.assertNotIn([SYSTEMCTL, "reset-failed", *REBOOT_UNITS], self.env.sys.calls,
                                 "a pending reboot is never reset")

    def test_the_delay_comes_from_the_config(self) -> None:
        env = Env(reboot_delay=30)
        try:
            outcome, lines = env.call({"op": "reboot"})
            self.assertEqual(outcome, "ok")
            self.assertEqual(lines[0]["data"], {"scheduled_in": 30})
            run = [c for c in env.sys.calls if c[0] == SYSTEMD_RUN]
            self.assertIn("--on-active=30s", run[0])
        finally:
            env.close()

    def test_too_soon_after_boot(self) -> None:
        self.env.uptime.write_text("120.40 300.00\n")
        outcome, lines = self.env.call({"op": "reboot"})
        self.assertEqual(outcome, "too-soon")
        self.assertEqual(lines[0]["error"]["code"], "too-soon")
        self.assertIn("8 min", lines[0]["error"]["message"])
        self.assertFalse(self.systemd_runs())
        self.env.uptime.write_text("600.0 1.0\n")
        self.assertEqual(self.env.call({"op": "reboot"})[0], "ok", "exactly the minimum is enough")

    def test_unreadable_uptime_fails_closed(self) -> None:
        for content in ("", "not-a-number 1\n"):
            with self.subTest(content=content):
                self.env.uptime.write_text(content)
                self.assertEqual(self.env.call({"op": "reboot"})[0], "failed")
        self.env.uptime.unlink()
        self.assertEqual(self.env.call({"op": "reboot"})[0], "failed")
        self.assertFalse(self.systemd_runs())

    def test_busy_while_any_action_holds_a_lock(self) -> None:
        for name in ("container-sonarr", "media-pins", "disk-split"):
            with self.subTest(lock=name), Locked(self.env, name):
                outcome, lines = self.env.call({"op": "reboot"})
                self.assertEqual(outcome, "busy")
                self.assertIn(name, lines[0]["error"]["message"])
        self.assertFalse(self.systemd_runs())
        # The lock files are still there but nobody holds them: not busy, and the check released them.
        self.assertEqual(self.env.call({"op": "reboot"})[0], "ok")
        with Locked(self.env, "container-sonarr"):
            pass

    def test_no_lock_directory_is_not_busy(self) -> None:
        self.assertFalse((self.env.cache / "locks").exists())
        self.assertEqual(self.env.call({"op": "reboot"})[0], "ok")

    def test_a_symlinked_lock_is_never_followed(self) -> None:
        locks = self.env.cache / "locks"
        locks.mkdir(parents=True)
        target = self.env.root / "elsewhere"
        target.write_text("")
        os.symlink(target, locks / "container-evil.lock")
        self.assertEqual(self.env.call({"op": "reboot"})[0], "busy")
        self.assertFalse(self.systemd_runs())

    def test_a_reboot_already_scheduled_is_busy(self) -> None:
        for err in ("Failed to start transient timer unit: Unit homelab-dashboard-reboot.timer already exists.\n",
                    "Failed to start transient timer unit: Unit homelab-dashboard-reboot.timer was already loaded "
                    "or has a fragment file.\n"):
            with self.subTest(err=err):
                self.env.sys.systemd_run_rc = 1
                self.env.sys.systemd_run_err = err
                outcome, lines = self.env.call({"op": "reboot"})
                self.assertEqual(outcome, "busy")
                self.assertEqual(lines[0]["error"]["message"], "a reboot is already scheduled")

    def test_other_systemd_run_failures(self) -> None:
        self.env.sys.systemd_run_rc = 1
        self.env.sys.systemd_run_err = "Failed to connect to bus: No such file or directory\nmore\n"
        outcome, lines = self.env.call({"op": "reboot"})
        self.assertEqual(outcome, "failed")
        self.assertEqual(lines[0]["error"]["message"], "systemd-run exited 1: Failed to connect to bus: No such file or directory")
        self.assertNotIn("reboot scheduled", self.env.log)

    def test_takes_no_fields(self) -> None:
        for req in ({"op": "reboot", "name": "sonarr"}, {"op": "reboot", "delay": 0}):
            with self.subTest(req=req):
                self.assertEqual(self.env.call(req)[0], "bad-request")
        self.assertFalse(self.systemd_runs())

    def test_config_validation(self) -> None:
        for bad in ({"reboot_delay": 0}, {"reboot_delay": 1.5}, {"reboot_delay": True},
                    {"reboot_min_uptime": -1}, {"systemd_run": "systemd-run"}, {"proc_uptime": "proc/uptime"},
                    {"state_dir": "state"}, {"pin_stacks": "media"}, {"pin_stacks": ["Media!"]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                dockerctl.Config.from_mapping({"allowed_uid": 10101, **bad})
        cfg = dockerctl.Config.from_mapping({"allowed_uid": 10101})
        self.assertEqual((cfg.reboot_min_uptime, cfg.reboot_delay, cfg.systemd_run, cfg.proc_uptime),
                         (600, 10, "/usr/bin/systemd-run", "/proc/uptime"))
        self.assertEqual((cfg.pin_stacks, cfg.state_dir), (("media",), "/var/lib/homelab-dockerctl"))


class SetPinBase(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()
        self.compose = self.env.stacks / "media" / "compose.yaml"
        self.original = self.compose.read_bytes()
        self.pins = self.env.state / "pins.json"

    def tearDown(self) -> None:
        self.env.close()

    def set_pin(self, pin: str = NEW, name: str = "sonarr", env: Env | None = None):
        return (env or self.env).call({"op": "set-pin", "name": name, "pin": pin})

    def assert_nothing_changed(self) -> None:
        self.assertEqual(self.compose.read_bytes(), self.original)
        self.assertFalse(self.pins.exists())
        self.assertFalse([c for c in self.env.sys.docker_calls() if c[1] == "compose"])
        self.assert_no_temp_files()

    def assert_no_temp_files(self) -> None:
        leftovers = [p.name for d in (self.compose.parent, self.env.state) if d.exists()
                     for p in d.iterdir() if ".tmp-" in p.name]
        self.assertEqual(leftovers, [])


class SetPinValidation(SetPinBase):
    def test_a_different_repository_is_refused(self) -> None:
        digest = "@sha256:" + "c" * 64
        for pin in ("lscr.io/linuxserver/radarr:6.2.0" + digest,
                    f"{REPO}x:4.0.20" + digest,
                    "docker.io/linuxserver/sonarr:4.0.20" + digest,
                    "linuxserver/sonarr:4.0.20" + digest,
                    f"evil.example/{REPO}:4.0.20" + digest):
            with self.subTest(pin=pin):
                outcome, lines = self.set_pin(pin)
                self.assertEqual(outcome, "bad-request")
                self.assertEqual(len(lines), 1)
        self.assert_nothing_changed()

    def test_a_bad_digest_or_tag_is_refused(self) -> None:
        for pin in (f"{REPO}:4.0.20@sha256:" + "C" * 64,
                    f"{REPO}:4.0.20@sha256:" + "c" * 63,
                    f"{REPO}:4.0.20@sha256:" + "c" * 65,
                    f"{REPO}:4.0.20@sha512:" + "c" * 128,
                    f"{REPO}:4.0.20",
                    f"{REPO}@sha256:" + "c" * 64,
                    f"{REPO}:.hidden@sha256:" + "c" * 64,
                    f"{REPO}:-dash@sha256:" + "c" * 64,
                    f"{REPO}:" + "a" * 129 + "@sha256:" + "c" * 64,
                    f"{REPO}:4.0 20@sha256:" + "c" * 64,
                    f"{REPO}:4.0.20@sha256:" + "c" * 64 + "\n",
                    ""):
            with self.subTest(pin=pin):
                self.assertEqual(self.set_pin(pin)[0], "bad-request")
        self.assertEqual(self.set_pin(f"{REPO}:" + "a" * 128 + "@sha256:" + "c" * 64)[0], "ok",
                         "a 128-character tag is the maximum")

    def test_too_long(self) -> None:
        pin = "registry.example/" + "a/" * 250 + "sonarr:1@sha256:" + "c" * 64
        self.assertGreater(len(pin), 512)
        outcome, lines = self.set_pin(pin)
        self.assertEqual(outcome, "bad-request")
        self.assertIn("512", lines[0]["error"]["message"])
        self.assert_nothing_changed()

    def test_the_same_pin(self) -> None:
        outcome, lines = self.set_pin(OLD)
        self.assertEqual(outcome, "already-pinned")
        self.assertEqual(lines[0]["error"]["code"], "already-pinned")
        self.assert_nothing_changed()

    def test_only_apps_in_pin_stacks(self) -> None:
        pin = "cloudflare/cloudflared:2026.6.0@sha256:" + "c" * 64
        outcome, lines = self.set_pin(pin, "cloudflared")
        self.assertEqual(outcome, "not-allowed")
        self.assertIn("edge", lines[0]["error"]["message"])
        self.assertFalse(self.env.sys.docker_calls())

    def test_local_builds_are_refused(self) -> None:
        env = Env(pin_stacks=["media", "utils"])
        try:
            outcome, _ = self.set_pin("homelab/wol-relay:2026.08@sha256:" + "c" * 64, "wol-relay", env)
            self.assertEqual(outcome, "local-image")
            self.assertFalse([c for c in env.sys.docker_calls() if c[1] in ("pull", "compose")])
        finally:
            env.close()

    def test_unknown_or_unwatched_containers(self) -> None:
        for name in ("stranger", "nosuch"):
            with self.subTest(name=name):
                self.assertEqual(self.set_pin(NEW, name)[0], "not-allowed")
        self.assert_nothing_changed()

    def test_missing_or_wrong_fields(self) -> None:
        for req in ({"op": "set-pin", "name": "sonarr"}, {"op": "set-pin", "pin": NEW},
                    {"op": "set-pin", "name": "sonarr", "pin": 5},
                    {"op": "set-pin", "name": "sonarr", "pin": NEW, "force": True},
                    {"op": "set-pin", "name": "../sonarr", "pin": NEW}):
            with self.subTest(req=req):
                self.assertEqual(self.env.call(req)[0], "bad-request")
        self.assert_nothing_changed()

    def test_the_gate_holds_media_apps(self) -> None:
        for status in ("failed", "media-lost"):
            with self.subTest(status=status):
                self.env.set_gate(status)
                outcome, lines = self.set_pin()
                self.assertEqual(outcome, "held-by-gate")
                self.assertIn(status, lines[0]["error"]["message"])
        self.assert_nothing_changed()
        self.assertFalse([c for c in self.env.sys.docker_calls() if c[1] == "pull"])

    def test_docker_inactive(self) -> None:
        self.env.sys.docker_state = "inactive"
        self.assertEqual(self.set_pin()[0], "docker-inactive")
        self.assertFalse(self.env.sys.docker_calls())
        self.assert_nothing_changed()

    def test_busy_while_another_pin_or_action_runs(self) -> None:
        for name in ("media-pins", "container-sonarr"):
            with self.subTest(lock=name), Locked(self.env, name):
                self.assertEqual(self.set_pin()[0], "busy")
        self.assert_nothing_changed()

    def test_the_pin_must_be_on_exactly_one_image_line(self) -> None:
        cases = {
            "twice": self.original + f'\nx-dup:\n  image: "{OLD}"\n'.encode(),
            "never (an anchored value)": self.original.replace(f'image: "{OLD}"'.encode(),
                                                               f'image: &sonarr_image "{OLD}"'.encode()),
        }
        for what, data in cases.items():
            with self.subTest(what=what):
                self.compose.write_bytes(data)
                outcome, lines = self.set_pin()
                self.assertEqual(outcome, "failed")
                self.assertEqual(lines, [{"ok": False, "error": {"code": "failed",
                                                                 "message": "compose file doesn't match its pin"}}])
                self.assertEqual(self.compose.read_bytes(), data, "nothing changed")
        self.assertFalse([c for c in self.env.sys.docker_calls() if c[1] in ("pull", "compose")])

    def test_an_unreadable_pin_record_changes_nothing(self) -> None:
        self.env.state.mkdir()
        self.pins.write_text("{not json")
        outcome, lines = self.set_pin()
        self.assertEqual(outcome, "failed")
        self.assertEqual(len(lines), 1)
        self.assertEqual(self.compose.read_bytes(), self.original)
        self.assertEqual(self.pins.read_text(), "{not json")

    def test_a_writable_compose_file_is_never_rewritten(self) -> None:
        os.chmod(self.compose, 0o666)
        self.assertEqual(self.set_pin()[0], "not-allowed", "an untrusted compose file declares nothing")
        self.assertEqual(self.compose.read_bytes(), self.original)


class SetPinRun(SetPinBase):
    def test_the_happy_path(self) -> None:
        outcome, lines = self.set_pin()
        self.assertEqual(outcome, "ok")
        self.assertEqual(lines[0], {"ok": True, "op": "set-pin", "stream": True, "name": "sonarr",
                                    "from": OLD, "to": NEW})
        self.assertEqual(steps(lines), ["pulling", "pinning", "recreating", "waiting-health", "done"])
        self.assertEqual(step(lines, "pulling")["image"], NEW)
        self.assertRegex(step(lines, "pinning")["at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(step(lines, "done")["health"], "healthy")
        self.assertEqual(lines[-1], {"type": "end", "ok": True, "reason": "done"})
        outputs = [line["text"] for line in lines if line.get("type") == "output"]
        self.assertIn("Status: Downloaded newer image", outputs)
        # Exactly: pull by digest, then the same compose up as update (never a compose pull).
        f = str(self.compose)
        base = [DOCKER, "compose", "--ansi", "never", "--progress", "plain", "-f", f]
        work = [c for c in self.env.sys.docker_calls() if c[1] in ("pull", "compose")]
        self.assertEqual(work, [[DOCKER, "pull", NEW],
                                [*base, "up", "--detach", "--no-deps", "--no-build", "--pull", "never", "sonarr"]])
        self.assertIn(f"pin={NEW}", self.env.log)
        self.assertIn("result=ok", self.env.log)

    def test_the_compose_file_changes_on_one_line_only(self) -> None:
        mode = stat.S_IMODE(os.stat(self.compose).st_mode)
        self.set_pin()
        after = self.compose.read_bytes()
        self.assertEqual(after, self.original.replace(f'    image: "{OLD}"\n'.encode(), f'    image: "{NEW}"\n'.encode()))
        self.assertNotEqual(after, self.original)
        self.assertEqual(stat.S_IMODE(os.stat(self.compose).st_mode), mode)
        self.assertEqual(os.stat(self.compose).st_uid, self.env.cfg.trusted_owner_uid)
        self.assertTrue(self.env.helper._trusted(self.compose))
        self.assert_no_temp_files()
        # ps now shows the new pin; the other apps keep theirs.
        _, lines = self.env.call({"op": "ps"})
        declared = {d["name"]: d["pinned"] for d in lines[0]["data"]["declared"]}
        self.assertEqual(declared["sonarr"], NEW)
        self.assertTrue(declared["radarr"].startswith("lscr.io/linuxserver/radarr:6.1.1"))

    def test_plain_and_single_quoted_lines_keep_their_style(self) -> None:
        for before, after in ((f"image: {OLD}  # pinned", f"image: {NEW}  # pinned"),
                              (f"image: '{OLD}'", f"image: '{NEW}'")):
            with self.subTest(before=before):
                env = Env()
                try:
                    compose = env.stacks / "media" / "compose.yaml"
                    data = compose.read_bytes().replace(f'image: "{OLD}"'.encode(), before.encode())
                    compose.write_bytes(data)
                    self.assertEqual(self.set_pin(env=env)[0], "ok")
                    self.assertEqual(compose.read_bytes(), data.replace(before.encode(), after.encode()))
                finally:
                    env.close()

    def test_the_pin_record(self) -> None:
        self.set_pin()
        record = json.loads(self.pins.read_text())
        self.assertEqual(record, {"version": 1, "pins": {"media/sonarr": {
            "image": NEW, "previous": OLD, "at": "2026-10-24T13:53:20Z"}}})
        self.assertEqual(stat.S_IMODE(os.stat(self.pins).st_mode), 0o644)

    def test_previous_keeps_the_repo_pin_across_updates(self) -> None:
        self.assertEqual(self.set_pin(NEW)[0], "ok")
        self.assertEqual(self.set_pin(NEWER)[0], "ok")
        entry = json.loads(self.pins.read_text())["pins"]["media/sonarr"]
        self.assertEqual((entry["image"], entry["previous"]), (NEWER, OLD))
        self.assertIn(f'image: "{NEWER}"'.encode(), self.compose.read_bytes())

    def test_other_entries_are_kept_and_a_stale_entry_is_not_trusted(self) -> None:
        radarr = {"image": "lscr.io/linuxserver/radarr:6.2@sha256:" + "e" * 64, "previous": "x", "at": "t"}
        stale = {"image": f"{REPO}:9.9@sha256:" + "f" * 64, "previous": f"{REPO}:0.1@sha256:" + "0" * 64, "at": "t"}
        self.env.state.mkdir()
        self.pins.write_text(json.dumps({"version": 1, "pins": {"media/radarr": radarr, "media/sonarr": stale}}))
        self.assertEqual(self.set_pin()[0], "ok")
        pins = json.loads(self.pins.read_text())["pins"]
        self.assertEqual(pins["media/radarr"], radarr)
        # The record didn't describe what the compose file had, so the compose pin is the one to remember.
        self.assertEqual(pins["media/sonarr"]["previous"], OLD)

    def test_a_pull_failure_changes_nothing(self) -> None:
        self.env.sys.image_pull_rc = 1
        self.env.sys.image_pull_output = b"Error response from daemon: manifest unknown\n"
        outcome, lines = self.set_pin()
        self.assertEqual(outcome, "failed")
        self.assertEqual(steps(lines), ["pulling", "failed"])
        failed = step(lines, "failed")
        self.assertEqual((failed["failed_step"], failed["code"], failed["message"]),
                         ("pulling", "pull-failed", "exited 1"))
        self.assertNotIn("rolled_back", failed)
        self.assertEqual(lines[-1], {"type": "end", "ok": False, "reason": "failed", "message": "pulling: exited 1"})
        self.assert_nothing_changed()

    def test_a_health_failure_rolls_back_exactly(self) -> None:
        self.env.state.mkdir()
        prior = b'{"version": 1,\n "pins": {"media/radarr": {"image": "x", "previous": "y", "at": "z"}}}\n'
        self.pins.write_bytes(prior)
        os.chmod(self.pins, 0o644)
        self.env.sys.health_after_ups = [["starting", "unhealthy"], ["healthy"]]
        outcome, lines = self.set_pin()
        self.assertEqual(outcome, "failed")
        self.assertEqual(steps(lines), ["pulling", "pinning", "recreating", "waiting-health", "rolling-back", "failed"])
        failed = step(lines, "failed")
        self.assertEqual((failed["failed_step"], failed["code"], failed["rolled_back"]),
                         ("waiting-health", "health-failed", True))
        self.assertEqual(failed["message"], "health check failing; rolled back to 4.0.17.2952-ls312")
        self.assertEqual(lines[-1]["message"], "waiting-health: health check failing; rolled back to 4.0.17.2952-ls312")
        self.assertEqual(self.compose.read_bytes(), self.original)
        self.assertEqual(self.pins.read_bytes(), prior, "the record is back to its exact prior bytes")
        ups = [c for c in self.env.sys.docker_calls() if c[1] == "compose" and "up" in c]
        self.assertEqual(len(ups), 2, "the rollback recreates the app on the old pin")
        self.assert_no_temp_files()

    def test_a_rollback_removes_a_record_that_did_not_exist(self) -> None:
        self.env.sys.health_after_ups = [["unhealthy"], ["healthy"]]
        self.assertEqual(self.set_pin()[0], "failed")
        self.assertFalse(self.pins.exists())
        self.assertEqual(self.compose.read_bytes(), self.original)

    def test_a_recreate_failure_rolls_back(self) -> None:
        self.env.sys.up_rcs = [1, 0]
        outcome, lines = self.set_pin()
        self.assertEqual(outcome, "failed")
        self.assertEqual(steps(lines), ["pulling", "pinning", "recreating", "rolling-back", "failed"])
        failed = step(lines, "failed")
        self.assertEqual((failed["failed_step"], failed["code"], failed["rolled_back"]),
                         ("recreating", "recreate-failed", True))
        self.assertEqual(self.compose.read_bytes(), self.original)
        self.assertFalse(self.pins.exists())

    def watch_writes(self, fail_compose_once: bool = False) -> list[str]:
        """Record every atomic write's file name (and optionally fail the first compose write)."""
        written: list[str] = []
        real = self.env.helper._atomic_write
        failed = []

        def spy(path, data, mode, gid):
            written.append(path.name)
            if fail_compose_once and path.name == "compose.yaml" and not failed:
                failed.append(True)
                raise OSError(28, "No space left on device")
            return real(path, data, mode, gid)

        self.env.helper._atomic_write = spy
        return written

    def test_the_record_is_written_before_the_compose_file(self) -> None:
        written = self.watch_writes()
        self.assertEqual(self.set_pin()[0], "ok")
        self.assertEqual(written, ["pins.json", "compose.yaml"])

    def test_the_rollback_restores_the_compose_file_then_the_record(self) -> None:
        self.env.state.mkdir()
        prior = b'{"version": 1, "pins": {}}\n'
        self.pins.write_bytes(prior)
        written = self.watch_writes()
        self.env.sys.health_after_ups = [["unhealthy"], ["healthy"]]
        self.assertEqual(self.set_pin()[0], "failed")
        self.assertEqual(written, ["pins.json", "compose.yaml", "compose.yaml", "pins.json"])
        self.assertEqual(self.compose.read_bytes(), self.original)
        self.assertEqual(self.pins.read_bytes(), prior)

    def test_a_compose_write_failure_after_the_record_rolls_the_record_back(self) -> None:
        written = self.watch_writes(fail_compose_once=True)
        outcome, lines = self.set_pin()
        self.assertEqual(outcome, "failed")
        self.assertEqual(steps(lines), ["pulling", "pinning", "rolling-back", "failed"])
        failed = step(lines, "failed")
        self.assertEqual((failed["failed_step"], failed["rolled_back"]), ("pinning", True))
        self.assertEqual(written[:2], ["pins.json", "compose.yaml"])
        self.assertEqual(self.compose.read_bytes(), self.original)
        self.assertFalse(self.pins.exists(), "the record that got ahead is removed again")
        self.assert_no_temp_files()

    def test_a_failure_while_pinning_rolls_back(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores the read-only directory")
        self.env.state.mkdir()
        os.chmod(self.env.state, 0o555)  # the pin record (written first) can't be written
        try:
            outcome, lines = self.set_pin()
        finally:
            os.chmod(self.env.state, 0o755)
        self.assertEqual(outcome, "failed")
        self.assertEqual(steps(lines), ["pulling", "pinning", "rolling-back", "failed"])
        failed = step(lines, "failed")
        self.assertEqual((failed["failed_step"], failed["code"], failed["rolled_back"]), ("pinning", "pin-failed", True))
        self.assertEqual(self.compose.read_bytes(), self.original)

    def test_a_failed_rollback_says_so(self) -> None:
        self.env.sys.up_rcs = [0, 1]
        self.env.sys.health_after_ups = [["unhealthy"]]
        outcome, lines = self.set_pin()
        self.assertEqual(outcome, "failed")
        failed = step(lines, "failed")
        self.assertIs(failed["rolled_back"], False)
        self.assertEqual(failed["message"], "health check failing; rollback also failed: recreating: exited 1")
        # The files are still back to what they were: only the recreate on the old pin failed.
        self.assertEqual(self.compose.read_bytes(), self.original)
        self.assertFalse(self.pins.exists())
        self.assertEqual(lines[-1]["type"], "end")
        self.assertFalse(lines[-1]["ok"])

    def test_an_unhealthy_rollback_says_so(self) -> None:
        self.env.sys.health_after_ups = [["unhealthy"], ["unhealthy"]]
        _, lines = self.set_pin()
        failed = step(lines, "failed")
        self.assertIs(failed["rolled_back"], False)
        self.assertIn("rollback also failed: waiting-health: health check failing", failed["message"])

    def test_the_client_going_away_never_stops_it(self) -> None:
        def slow_pull(argv: list[str]) -> None:
            if argv[1] == "pull":
                time.sleep(0.2)  # the client is gone by now

        self.env.sys.on_spawn = slow_pull
        client, thread, result = self.env.call_threaded({"op": "set-pin", "name": "sonarr", "pin": NEW})
        header = read_lines(client, 1)[0]
        self.assertEqual((header["stream"], header["to"]), (True, NEW))
        client.close()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["outcome"], "ok")
        self.assertIn(f'image: "{NEW}"'.encode(), self.compose.read_bytes())
        self.assertEqual(json.loads(self.pins.read_text())["pins"]["media/sonarr"]["image"], NEW)
        self.assertIn("result=ok client-gone", self.env.log)

    def test_the_locks_are_held_for_the_whole_run(self) -> None:
        seen: dict[str, bool] = {}

        def probe(argv: list[str]) -> None:
            if argv[1] == "compose" and "up" in argv:
                seen["busy"] = self.env.helper._held_lock() is not None
                for name in ("media-pins", "container-sonarr"):
                    fd = os.open(self.env.cache / "locks" / f"{name}.lock", os.O_RDONLY)
                    try:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    finally:
                        os.close(fd)

        self.env.sys.on_spawn = probe
        self.assertEqual(self.set_pin()[0], "ok")
        self.assertTrue(seen["busy"], "a reboot during a set-pin is refused")
        self.assertIsNone(self.env.helper._held_lock(), "both released at the end")


if __name__ == "__main__":
    unittest.main()
