"""Unit tests for the alert floor's decision logic (no Docker, no network, no root).

Run from the repo root:  python3 -m unittest discover -s roles/notifier/files/tests -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("homelab_alertfloor", HERE.parent / "homelab_alertfloor.py")
af = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(af)


def ctr(name="jellyfin", status="running", health="healthy", restarts=0, cid="id1", exit_code=0):  # noqa: PLR0917
    return {
        "name": name,
        "id": cid,
        "status": status,
        "health": health,
        "restart_count": restarts,
        "exit_code": exit_code,
    }


def ev(name, action, t):
    return {"Type": "container", "Action": action, "Actor": {"Attributes": {"name": name}}, "timeNano": t}


class Recorder:
    def __init__(self, rcs=None):
        self.rcs = list(rcs or [])
        self.pushes: list[tuple[str, str]] = []

    def __call__(self, tag, title, body):
        self.pushes.append((tag, title))
        return self.rcs.pop(0) if self.rcs else 0


class Harness(unittest.TestCase):
    """Drives af.run() through a scripted sequence of observations."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.gate = root / "status"
        self.gate.write_text("ok\n")
        self.env = mock.patch.dict(
            os.environ,
            {
                "HOMELAB_ALERTFLOOR_STATE": str(root / "af/state.json"),
                "HOMELAB_ALERTFLOOR_BOOTGATE_STATUS": str(self.gate),
            },
        )
        self.env.start()
        self.out = mock.patch("builtins.print")
        self.out.start()
        self.notify = Recorder()
        self.t = 1_000_000
        self.windows: list[tuple[int, int]] = []

    def tearDown(self):
        self.out.stop()
        self.env.stop()
        self.tmp.cleanup()

    def check(self, containers, events=()):
        def observe(since, until):
            self.windows.append((since, until))
            if containers is None:
                return None, [], "Cannot connect to the Docker daemon"
            return containers, list(events), ""

        self.t += 120
        af.run(now=self.t, observe_fn=observe, notify=self.notify)
        return [title for _, title in self.notify.pushes]


class Unhealthy(Harness):
    def test_two_consecutive_unhealthy_checks_push_once_then_recover_once(self):
        self.assertEqual(self.check([ctr(health="unhealthy")]), [])
        self.assertEqual(self.check([ctr(health="unhealthy")]), ["jellyfin unhealthy"])
        self.assertEqual(self.check([ctr(health="unhealthy")]), ["jellyfin unhealthy"], "pushed twice")
        self.assertEqual(self.check([ctr(health="healthy")]), ["jellyfin unhealthy", "jellyfin recovered"])
        self.assertEqual(self.check([ctr(health="healthy")]), ["jellyfin unhealthy", "jellyfin recovered"])

    def test_single_unhealthy_blip_is_ignored(self):
        self.check([ctr(health="unhealthy")])
        self.check([ctr(health="healthy")])
        self.check([ctr(health="unhealthy")])
        self.assertEqual(self.notify.pushes, [])

    def test_failed_push_is_retried_next_run(self):
        self.notify.rcs = [1, 0]
        self.check([ctr(health="unhealthy")])
        self.check([ctr(health="unhealthy")])  # push fails (rc 1)
        self.check([ctr(health="unhealthy")])  # retried, delivered
        self.check([ctr(health="unhealthy")])
        self.assertEqual([t for _, t in self.notify.pushes], ["jellyfin unhealthy", "jellyfin unhealthy"])


class Down(Harness):
    def test_crash_without_stop_event_alerts(self):
        self.check([ctr()])
        titles = self.check([ctr(status="exited", exit_code=139)], [ev("jellyfin", "die", 5)])
        self.assertEqual(titles, ["jellyfin is down"])
        self.assertEqual(self.check([ctr()]), ["jellyfin is down", "jellyfin recovered"])

    def test_intentional_stop_is_silent_and_stays_silent(self):
        self.check([ctr()])
        stop = [ev("jellyfin", "kill", 1), ev("jellyfin", "die", 2), ev("jellyfin", "stop", 3)]
        self.assertEqual(self.check([ctr(status="exited")], stop), [])
        self.assertEqual(self.check([ctr(status="exited")]), [], "intent must be remembered across runs")

    def test_restart_after_stop_then_crash_alerts(self):
        self.check([ctr()])
        events = [ev("jellyfin", "stop", 1), ev("jellyfin", "start", 2), ev("jellyfin", "die", 3)]
        self.assertEqual(self.check([ctr(status="exited")], events), ["jellyfin is down"])

    def test_seen_running_clears_old_intent_even_without_a_start_event(self):
        self.check([ctr()])
        self.check([ctr(status="exited")], [ev("jellyfin", "stop", 1)])
        self.check([ctr()])  # started while we were not looking: no 'start' event in our window
        self.assertEqual(self.check([ctr(status="exited")], [ev("jellyfin", "die", 9)]), ["jellyfin is down"])

    def test_dead_container_alerts(self):
        self.assertEqual(self.check([ctr(status="dead")]), ["jellyfin is down"])

    def test_recreated_container_forgets_old_intent(self):
        self.check([ctr()])
        self.check([ctr(status="exited")], [ev("jellyfin", "stop", 1)])
        self.assertEqual(self.check([ctr(status="exited", cid="id2")]), ["jellyfin is down"])

    def test_removed_container_drops_incident_silently(self):
        self.check([ctr(status="exited")])
        self.assertEqual(len(self.notify.pushes), 1)
        self.check([])
        self.assertEqual(len(self.notify.pushes), 1, "no 'recovered' for a container that was removed")


class Restarts(Harness):
    def test_restart_rise_of_three_alerts_and_clears_when_stable(self):
        self.check([ctr(restarts=1)])
        self.assertEqual(self.check([ctr(restarts=3)]), [], "rise of 2 is below the floor")
        self.assertEqual(self.check([ctr(restarts=6, status="restarting")]), ["jellyfin restart loop"])
        self.assertEqual(self.check([ctr(restarts=7)]), ["jellyfin restart loop"], "still rising: no recovery")
        self.assertEqual(self.check([ctr(restarts=7)]), ["jellyfin restart loop", "jellyfin recovered"])

    def test_new_container_resets_baseline(self):
        self.check([ctr(restarts=10)])
        self.assertEqual(self.check([ctr(restarts=0, cid="id2")]), [])


class Daemon(Harness):
    def test_docker_unreachable_twice_alerts_once_and_recovers(self):
        self.check([ctr()])
        self.assertEqual(self.check(None), [])
        self.assertEqual(self.check(None), ["Docker unreachable"])
        self.assertEqual(self.check(None), ["Docker unreachable"])
        self.assertEqual(self.check([ctr()]), ["Docker unreachable", "Docker recovered"])

    def test_open_container_incident_survives_a_daemon_outage(self):
        self.check([ctr(status="exited")])
        self.check(None)
        self.check(None)
        titles = [t for _, t in self.notify.pushes]
        self.assertNotIn("jellyfin recovered", titles)

    def test_events_window_is_contiguous(self):
        self.check([ctr()])
        self.check([ctr()])
        self.assertEqual(self.windows[1][0], self.windows[0][1])


class GateGuard(Harness):
    def test_skipped_unless_gate_ok(self):
        for status in ("failed", "media-lost", "waiting"):
            self.gate.write_text(status + "\n")
            self.check([ctr(status="exited")])
        self.assertEqual(self.notify.pushes, [])
        self.assertEqual(self.windows, [], "docker must not even be queried")

    def test_missing_gate_status_skips(self):
        self.gate.unlink()
        self.check([ctr(status="exited")])
        self.assertEqual(self.windows, [])


class Pure(unittest.TestCase):
    def test_intent_marks_follow_event_order(self):
        marks = af.intent_marks([ev("a", "start", 3), ev("a", "stop", 1), ev("b", "kill", 2), ev("c", "die", 1)])
        self.assertEqual(marks, {"a": False, "b": True})

    def test_reconcile_counts_no_subscribers_as_done(self):
        rec = Recorder(rcs=[2])
        out = af.reconcile({}, {"x:down": ("x is down", "")}, rec)
        self.assertEqual(out, {"x:down": "x is down"})
        self.assertEqual(rec.pushes[0][0], "alertfloor:x:down")

    def test_state_file_is_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s", "state.json")
            af._write_state(path, {"a": 1})
            self.assertEqual(json.loads(pathlib.Path(path).read_text()), {"a": 1})


if __name__ == "__main__":
    unittest.main()
