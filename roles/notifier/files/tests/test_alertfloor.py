"""Unit tests for the alert floor's decision logic (no Docker, no network, no root).

Run from the repo root:  python3 -m unittest discover -s roles/notifier/files/tests -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import textwrap
import time
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


EVENTS_FAILED = object()  # an observation whose `docker events` call failed


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

    def check(self, containers, events=(), confirm=None):
        """One timer run. `confirm` = (containers, events) for the re-check that a new "down"
        triggers; without it the re-check sees exactly what the first look saw."""
        looks = [(containers, events)] + ([confirm] if confirm is not None else [])
        self.looks = 0

        def observe(since):
            c, e = looks[min(self.looks, len(looks) - 1)]
            self.looks += 1
            self.windows.append((since, self.t))
            if c is None:
                return None, None, since, "Cannot connect to the Docker daemon"
            # Fake events are offsets (ns) inside this run's window, 60 s before its snapshot.
            seen = int(self.t * 1e9)
            c = [dict(x, seen_ns=x.get("seen_ns", seen)) for x in c]
            if e is EVENTS_FAILED:
                return c, None, self.t, "events: context deadline exceeded"
            base = int((self.t - 60) * 1e9)
            e = [dict(x, timeNano=base + x["timeNano"]) if x["timeNano"] < 10**12 else x for x in e]
            return c, e, self.t, ""

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


class RacesAndRemovals(Harness):
    """Integration finding: a container stopped or removed during a check must never alert."""

    def test_a_stop_that_raced_the_first_look_is_caught_by_the_confirm_pass(self):
        self.check([ctr()])
        titles = self.check([ctr(status="exited")], [], confirm=([ctr(status="exited")], [ev("jellyfin", "stop", 9)]))
        self.assertEqual(titles, [])
        self.assertEqual(self.looks, 2, "a new 'down' must be confirmed with a second look")
        self.assertEqual(self.check([ctr(status="exited")]), [], "intent is remembered afterwards")

    def test_a_container_removed_mid_run_never_alerts(self):
        self.check([ctr(), ctr("sonarr", cid="id2")])
        titles = self.check(
            [ctr(status="exited"), ctr("sonarr", cid="id2")], [], confirm=([ctr("sonarr", cid="id2")], [])
        )
        self.assertEqual(titles, [])
        self.assertNotIn(
            "jellyfin", json.loads(pathlib.Path(os.environ["HOMELAB_ALERTFLOOR_STATE"]).read_text())["containers"]
        )

    def test_a_real_crash_still_alerts_after_the_confirm(self):
        self.check([ctr()])
        titles = self.check([ctr(status="exited", exit_code=139)], [ev("jellyfin", "die", 5)])
        self.assertEqual(titles, ["jellyfin is down"])
        self.assertEqual(self.looks, 2)

    def test_an_oom_after_an_earlier_kill_still_alerts(self):
        self.check([ctr()])
        events = [ev("jellyfin", "kill", 1), ev("jellyfin", "oom", 2), ev("jellyfin", "die", 3)]
        self.assertEqual(self.check([ctr(status="exited", exit_code=137)], events), ["jellyfin is down"])

    def test_compose_down_events_mean_on_purpose(self):
        self.check([ctr()])
        events = [ev("jellyfin", "kill", 1), ev("jellyfin", "die", 2), ev("jellyfin", "destroy", 3)]
        self.assertEqual(self.check([ctr(status="exited")], events), [])

    def test_a_check_without_event_history_never_alerts_down_and_keeps_its_window(self):
        self.check([ctr()])
        first_window = self.windows[-1]
        self.assertEqual(self.check([ctr(status="exited")], EVENTS_FAILED), [])
        state = json.loads(pathlib.Path(os.environ["HOMELAB_ALERTFLOOR_STATE"]).read_text())
        self.assertEqual(state["until"], first_window[1], "the next run must re-read the same window")
        self.assertEqual(state["containers"]["jellyfin"]["id"], "id1")

    def test_a_failed_confirm_never_alerts(self):
        self.check([ctr()])
        self.assertEqual(self.check([ctr(status="exited")], [], confirm=([ctr(status="exited")], EVENTS_FAILED)), [])

    def test_persistent_event_failures_raise_their_own_incident(self):
        self.check([ctr()])
        for _ in range(2):
            self.assertEqual(self.check([ctr()], EVENTS_FAILED), [])
        self.assertEqual(self.check([ctr()], EVENTS_FAILED), ["Alert floor blind"])
        self.assertEqual(self.check([ctr()], EVENTS_FAILED), ["Alert floor blind"])
        self.assertEqual(self.check([ctr()]), ["Alert floor blind", "Alert floor recovered"])


class ReadInspect(unittest.TestCase):
    """The pure judge of a docker inspect result."""

    ITEMS = [{"Id": "a"}, {"Id": "c"}]

    def test_success(self):
        self.assertEqual(af.read_inspect(["a", "c"], 0, json.dumps(self.ITEMS), ""), (self.ITEMS, set(), ""))

    def test_only_ids_named_missing_are_removed(self):
        for err in ("Error: No such object: b\n", "Error response from daemon: No such container: b\n"):
            items, removed, _ = af.read_inspect(["a", "b", "c"], 1, json.dumps(self.ITEMS), err)
            self.assertEqual((items, removed), (self.ITEMS, {"b"}), err)

    def test_everything_else_is_unreachable(self):
        cases = {
            "empty output": (["a"], 1, "", "Cannot connect to the Docker daemon"),
            "empty output, rc 0": (["a"], 0, "", ""),
            "timeout": (["a"], 124, "", "docker: timed out"),
            "no binary": (["a"], 127, "", "docker: not found"),
            "garbage": (["a"], 0, "{oops", ""),
            "other error": (["a", "c"], 1, json.dumps(self.ITEMS), "Error response from daemon: i/o timeout"),
            "missing plus other error": (["a", "b", "c"], 1, json.dumps(self.ITEMS), "No such object: b\ni/o timeout"),
            "unaccounted id": (["a", "b", "c"], 0, json.dumps(self.ITEMS), ""),
            "non-zero, nothing named": (["a"], 1, "[]", ""),
        }
        for name, args in cases.items():
            items, removed, reason = af.read_inspect(*args)
            self.assertIsNone(items, name)
            self.assertEqual(removed, set(), name)
            self.assertTrue(reason, name)


DOCKER_STUB = textwrap.dedent(
    """\
    #!{python}
    # Fake docker CLI for the alert floor. world.json holds containers (inspect items keyed by id),
    # an event log, and triggers that change the world when a subcommand runs for the Nth time,
    # so a test can make a stop or a removal land between ps, inspect and events.
    import json, os, sys, time
    path = os.environ["ALERTFLOOR_STUB_WORLD"]
    world = json.load(open(path))
    cmd, args = sys.argv[1], sys.argv[2:]
    world["counts"][cmd] = world["counts"].get(cmd, 0) + 1
    for trig in world["triggers"]:
        if trig["on"] == cmd and trig["nth"] == world["counts"][cmd] and not trig.get("done"):
            trig["done"] = True
            for cid in trig.get("remove", []):
                world["containers"].pop(cid, None)
            for cid, status in trig.get("set_status", {{}}).items():
                world["containers"][cid]["State"]["Status"] = status
            ts = time.time_ns()
            if trig.get("at") == "until" and cmd == "events":
                ts = int(float(args[args.index("--until") + 1]) * 1e9)
            for k, (name, action) in enumerate(trig.get("events", [])):
                cid = next((i for i, v in world["containers"].items() if v["Name"].lstrip("/") == name), None)
                actor = {{"Attributes": {{"name": name}}}}
                if cid:
                    actor["ID"] = cid
                world["events"].append({{"Type": "container", "Action": action, "id": cid, "Actor": actor,
                    "timeNano": ts + (0 if trig.get("at") == "until" else k)}})
    json.dump(world, open(path, "w"))
    rc = 0
    if cmd == "ps":
        print("\\n".join(world["containers"]))
    elif cmd == "inspect":
        n = world["counts"]["inspect"]
        slow = world.get("inspect_sleep") or {{}}
        fail = world.get("inspect_fail") or {{}}
        if slow.get("nth") == n:
            time.sleep(slow["seconds"])  # long enough for the alert floor's timeout to fire
        if fail.get("nth") == n:
            sys.stdout.write(fail.get("stdout", ""))
            sys.stderr.write(fail.get("stderr", ""))
            rc = fail.get("rc", 1)
        else:
            found = [world["containers"][i] for i in args if i in world["containers"]]
            print(json.dumps(found))
            for i in args:
                if i not in world["containers"]:
                    print("Error: No such object: " + i, file=sys.stderr)
                    rc = 1
    elif cmd == "events" and world.get("events_fail"):
        print("Error response from daemon: context deadline exceeded", file=sys.stderr)
        rc = 1
    elif cmd == "events":
        since = float(args[args.index("--since") + 1]) * 1e9
        until = float(args[args.index("--until") + 1]) * 1e9
        world["windows"] = world.get("windows", []) + [[since / 1e9, until / 1e9]]
        json.dump(world, open(path, "w"))
        wait = until / 1e9 - time.time()
        if wait > 0:
            time.sleep(wait)  # like docker: --until in the future streams until it has passed
        exclusive = world.get("until_exclusive")
        for e in world["events"]:
            if since <= e["timeNano"] and (e["timeNano"] < until if exclusive else e["timeNano"] <= until):
                print(json.dumps(e))
    sys.exit(rc)
    """
)


def item(name, cid, status="running"):
    return {"Name": "/" + name, "Id": cid, "State": {"Status": status, "ExitCode": 0}, "RestartCount": 0}


class RealObserveWithAStubDocker(Harness):
    """The real observe() and run() against a fake docker CLI: no fake observe_fn."""

    def setUp(self):
        super().setUp()
        root = pathlib.Path(self.tmp.name)
        stub = root / "docker"
        stub.write_text(DOCKER_STUB.format(python=sys.executable))
        stub.chmod(0o755)
        self.world_path = root / "world.json"
        os.environ.update(
            HOMELAB_ALERTFLOOR_DOCKER=str(stub),
            ALERTFLOOR_STUB_WORLD=str(self.world_path),
            HOMELAB_ALERTFLOOR_EVENTS_MARGIN="0",  # strict: only ordering may get the event in
        )

    def world(self, containers, triggers=(), events=(), events_fail=False, **extra):
        self.world_path.write_text(
            json.dumps(
                {
                    "containers": {c["Id"]: c for c in containers},
                    "triggers": list(triggers),
                    "events": list(events),
                    "counts": {},
                    "events_fail": events_fail,
                    **extra,
                }
            )
        )

    def real_run(self):
        af.run(notify=self.notify)
        return [title for _, title in self.notify.pushes]

    def counts(self):
        return json.loads(self.world_path.read_text())["counts"]

    def test_stop_between_run_start_and_inspect_is_in_the_window(self):
        # The deploy race: compose down hits jellyfin after the run started, before inspect.
        self.world([item("jellyfin", "id1")])
        self.real_run()  # baseline: running
        self.world(
            [item("jellyfin", "id1")],
            triggers=[
                {
                    "on": "inspect",
                    "nth": 1,
                    "set_status": {"id1": "exited"},
                    "events": [["jellyfin", "kill"], ["jellyfin", "die"], ["jellyfin", "stop"]],
                }
            ],
        )
        containers, events, until, err = af.observe(time.time() - 60)
        self.assertEqual(containers[0]["status"], "exited")
        self.assertIn("stop", [e["Action"] for e in events], "the events window must close after inspect")
        self.world(
            [item("jellyfin", "id1")],
            triggers=[
                {
                    "on": "inspect",
                    "nth": 1,
                    "set_status": {"id1": "exited"},
                    "events": [["jellyfin", "kill"], ["jellyfin", "die"], ["jellyfin", "stop"]],
                }
            ],
        )
        self.assertEqual(self.real_run(), [])
        self.assertEqual(self.counts().get("ps"), 1, "a stop seen in the first window needs no re-check")

    def test_stop_logged_after_inspect_but_before_the_recheck(self):
        self.world([item("jellyfin", "id1")])
        self.real_run()
        self.world(
            [item("jellyfin", "id1", status="exited")],
            triggers=[{"on": "ps", "nth": 2, "events": [["jellyfin", "stop"]]}],
        )
        self.assertEqual(self.real_run(), [])
        self.assertEqual(self.counts()["ps"], 2, "the confirm pass looked again")

    def test_container_removed_between_ps_and_inspect(self):
        self.world([item("jellyfin", "id1"), item("sonarr", "id2")])
        self.real_run()
        self.world(
            [item("jellyfin", "id1"), item("sonarr", "id2")],
            triggers=[
                {
                    "on": "inspect",
                    "nth": 1,
                    "remove": ["id1"],
                    "events": [["jellyfin", "kill"], ["jellyfin", "die"], ["jellyfin", "destroy"]],
                }
            ],
        )
        self.assertEqual(self.real_run(), [])
        state = json.loads(pathlib.Path(os.environ["HOMELAB_ALERTFLOOR_STATE"]).read_text())
        self.assertEqual(state["docker_fail_streak"], 0, "a removal is not Docker being unreachable")
        self.assertEqual(sorted(state["containers"]), ["sonarr"])

    def test_container_removed_before_the_recheck(self):
        self.world([item("jellyfin", "id1")])
        self.real_run()
        self.world([item("jellyfin", "id1", status="exited")], triggers=[{"on": "ps", "nth": 2, "remove": ["id1"]}])
        self.assertEqual(self.real_run(), [])

    def test_real_crash_alerts(self):
        self.world([item("jellyfin", "id1")])
        self.real_run()
        crash = [
            {
                "Type": "container",
                "Action": "die",
                "Actor": {"Attributes": {"name": "jellyfin"}},
                "timeNano": time.time_ns(),
            }
        ]
        self.world([item("jellyfin", "id1", status="exited")], events=crash)
        self.assertEqual(self.real_run(), ["jellyfin is down"])
        self.assertEqual(self.counts()["ps"], 2, "confirmed before pushing")

    def test_failing_docker_events_never_turns_into_a_down_alert(self):
        self.world([item("jellyfin", "id1")])
        self.real_run()
        until_before = json.loads(pathlib.Path(os.environ["HOMELAB_ALERTFLOOR_STATE"]).read_text())["until"]
        self.world([item("jellyfin", "id1", status="exited")], events_fail=True)
        containers, events, _, err = af.observe(time.time() - 60)
        self.assertEqual((containers[0]["status"], events), ("exited", None))
        self.assertIn("deadline", err)
        self.assertEqual(self.real_run(), [], "no event history means no judgement, not 'nobody stopped it'")
        state = json.loads(pathlib.Path(os.environ["HOMELAB_ALERTFLOOR_STATE"]).read_text())
        self.assertEqual(state["until"], until_before)

    def state(self):
        return json.loads(pathlib.Path(os.environ["HOMELAB_ALERTFLOOR_STATE"]).read_text())

    def test_integrator_sequence_snapshot_older_than_the_stop(self):
        # Live 18:12:07: run 1 inspects jellyseerr RUNNING, then the dashboard helper stops it
        # (kill 07.246, stop 07.392, die 07.397): all inside run 1's window, after its snapshot.
        os.environ["HOMELAB_ALERTFLOOR_EVENTS_MARGIN"] = "0.5"  # room for events after inspect
        try:
            self.world([item("jellyseerr", "id9")])
            self.real_run()  # baseline
            self.world(
                [item("jellyseerr", "id9")],
                triggers=[
                    {
                        "on": "events",
                        "nth": 1,
                        "set_status": {"id9": "exited"},
                        "events": [["jellyseerr", "kill"], ["jellyseerr", "stop"], ["jellyseerr", "die"]],
                    }
                ],
            )
            self.assertEqual(self.real_run(), [], "run 1: snapshot says running")
            run1 = json.loads(self.world_path.read_text())
            stops = [e["timeNano"] / 1e9 for e in run1["events"] if e["Action"] in ("kill", "stop", "die")]
            since1, until1 = run1["windows"][-1]
            self.assertEqual(len(stops), 3)
            self.assertTrue(all(since1 <= t <= until1 for t in stops), "the stop is inside run 1's window")
            self.assertTrue(self.state()["containers"]["jellyseerr"]["intentional"], "intent must survive run 1")
            self.world([item("jellyseerr", "id9", status="exited")])
            self.assertEqual(self.real_run(), [], "run 2: exited with no events, stopped on purpose")
            start = [
                {
                    "Type": "container",
                    "Action": "start",
                    "id": "id9",
                    "Actor": {"ID": "id9", "Attributes": {"name": "jellyseerr"}},
                    "timeNano": time.time_ns(),
                }
            ]
            self.world([item("jellyseerr", "id9")], events=start)
            self.assertEqual(self.real_run(), [], "no 'recovered' for an intentional stop")
        finally:
            del os.environ["HOMELAB_ALERTFLOOR_EVENTS_MARGIN"]
            os.environ["HOMELAB_ALERTFLOOR_EVENTS_MARGIN"] = "0"

    def test_stop_then_start_then_crash_alerts(self):
        self.world([item("jellyfin", "id1")])
        self.real_run()
        t = int(self.state()["until"] * 1e9) + 1_000_000  # just after the previous window: in this run's
        seq = [("kill", 0), ("die", 1), ("stop", 2), ("start", 10_000_000), ("die", 20_000_000)]
        crash = [
            {
                "Type": "container",
                "Action": a,
                "id": "id1",
                "Actor": {"ID": "id1", "Attributes": {"name": "jellyfin"}},
                "timeNano": t + dt,
            }
            for a, dt in seq
        ]
        time.sleep(0.05)  # all of it in the past before the run starts
        self.world([item("jellyfin", "id1", status="exited")], events=crash)
        self.assertEqual(self.real_run(), ["jellyfin is down"])
        since, until = json.loads(self.world_path.read_text())["windows"][0]
        self.assertTrue(all(since <= e["timeNano"] / 1e9 <= until for e in crash), "the events were judged")

    def test_stop_events_exactly_at_the_window_boundary(self):
        for exclusive in (False, True):
            with self.subTest(until_exclusive=exclusive):
                self.notify.pushes.clear()
                pathlib.Path(os.environ["HOMELAB_ALERTFLOOR_STATE"]).unlink(missing_ok=True)
                self.world([item("sonarr", "id2")], until_exclusive=exclusive)
                self.real_run()
                self.world(
                    [item("sonarr", "id2")],
                    until_exclusive=exclusive,
                    triggers=[
                        {
                            "on": "events",
                            "nth": 1,
                            "at": "until",
                            "set_status": {"id2": "exited"},
                            "events": [["sonarr", "kill"], ["sonarr", "die"], ["sonarr", "stop"]],
                        }
                    ],
                )
                self.real_run()  # the stop lands exactly on this run's `until`
                self.world(
                    [item("sonarr", "id2", status="exited")],
                    until_exclusive=exclusive,
                    events=json.loads(self.world_path.read_text())["events"],
                )
                self.assertEqual(self.real_run(), [], "next run starts exactly at the boundary")

    def stopped_on_purpose_then(self, **failure):
        """Reviewer's sequence: sonarr stopped on purpose, a quiet run, then one run whose
        inspect fails as given, then a normal run. Returns (state before the failing run,
        state after it, all pushes)."""
        self.world([item("sonarr", "id2")])
        self.real_run()  # baseline: running
        stop = [
            {"Type": "container", "Action": a, "Actor": {"Attributes": {"name": "sonarr"}}, "timeNano": time.time_ns()}
            for a in ("kill", "die", "stop")
        ]
        self.world([item("sonarr", "id2", status="exited")], events=stop)
        self.assertEqual(self.real_run(), [], "stopped on purpose: silent")
        self.world([item("sonarr", "id2", status="exited")])
        self.real_run()  # quiet run: the stop event is now outside the window
        before = self.state()
        self.world([item("sonarr", "id2", status="exited")], **failure)
        self.real_run()  # inspect fails
        after = self.state()
        self.world([item("sonarr", "id2", status="exited")])
        pushes = self.real_run()  # normal again
        return before, after, pushes

    def assert_untouched(self, before, after):
        self.assertEqual(after["docker_fail_streak"], before.get("docker_fail_streak", 0) + 1)
        for key in ("containers", "until", "announced"):
            self.assertEqual(after.get(key), before.get(key), f"{key} must not change on an inspect failure")
        self.assertTrue(after["containers"]["sonarr"]["intentional"])

    def test_reviewer_sequence_empty_inspect_failure_is_not_every_container_removed(self):
        before, after, pushes = self.stopped_on_purpose_then(
            inspect_fail={
                "nth": 1,
                "rc": 1,
                "stdout": "",
                "stderr": "Cannot connect to the Docker daemon at unix:///run/docker.sock.\n",
            }
        )
        self.assertEqual(pushes, [], "no 'sonarr is down ... nobody stopped it' after the failure")
        self.assert_untouched(before, after)
        self.assertTrue(self.state()["containers"]["sonarr"]["intentional"])
        self.assertEqual(self.state()["docker_fail_streak"], 0)

    def test_inspect_timeout_is_unreachable_not_removed(self):
        os.environ["HOMELAB_ALERTFLOOR_DOCKER_TIMEOUT"] = "0.5"
        try:
            before, after, pushes = self.stopped_on_purpose_then(inspect_sleep={"nth": 1, "seconds": 5})
        finally:
            del os.environ["HOMELAB_ALERTFLOOR_DOCKER_TIMEOUT"]
        self.assertEqual(pushes, [])
        self.assert_untouched(before, after)

    def test_mixed_partial_drops_only_the_named_id(self):
        boxes = [item("jellyfin", "id1"), item("sonarr", "id2"), item("radarr", "id3")]
        self.world(boxes)
        self.real_run()
        self.world(boxes, triggers=[{"on": "inspect", "nth": 1, "remove": ["id2"]}])
        containers, events, _, err = af.observe(time.time() - 60)
        self.assertEqual(sorted(c["name"] for c in containers), ["jellyfin", "radarr"])
        self.world(boxes, triggers=[{"on": "inspect", "nth": 1, "remove": ["id2"]}])
        self.assertEqual(self.real_run(), [])
        state = self.state()
        self.assertEqual(sorted(state["containers"]), ["jellyfin", "radarr"])
        self.assertEqual(state["docker_fail_streak"], 0)

    def test_a_partial_with_any_other_error_is_unreachable(self):
        boxes = [item("jellyfin", "id1"), item("sonarr", "id2")]
        self.world(boxes)
        self.real_run()
        before = self.state()
        self.world(
            boxes,
            inspect_fail={
                "nth": 1,
                "rc": 1,
                "stdout": json.dumps([item("jellyfin", "id1")]),
                "stderr": "Error: No such object: id2\nError response from daemon: i/o timeout\n",
            },
        )
        self.assertEqual(self.real_run(), [])
        after = self.state()
        self.assertEqual(after["containers"], before["containers"], "sonarr must not be dropped on a mixed error")
        self.assertEqual(after["until"], before["until"])
        self.assertEqual(after["docker_fail_streak"], 1)

    def test_event_windows_are_contiguous_and_close_after_inspect(self):
        self.world([item("jellyfin", "id1")])
        before = time.time()
        self.real_run()
        self.real_run()
        windows = json.loads(self.world_path.read_text())["windows"]
        self.assertGreaterEqual(windows[0][1], before)
        self.assertAlmostEqual(windows[1][0], windows[0][1], places=3)


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
    NEW = {"phase": None, "intent": False, "exit_intentional": None}

    def test_lifecycle_is_replayed_in_time_order(self):
        fold = af.fold_lifecycle
        # the live failure: running snapshot BEFORE kill/stop/die -> intentional exit
        m = fold(self.NEW, [(100, "seen-up"), (246, "kill"), (392, "stop"), (397, "die")])
        self.assertEqual((m["phase"], m["exit_intentional"]), ("down", True))
        # stop -> start -> die (no stop): a crash
        m = fold(self.NEW, [(1, "kill"), (2, "die"), (3, "stop"), (4, "start"), (5, "die")])
        self.assertEqual((m["phase"], m["exit_intentional"]), ("down", False))
        # snapshot taken mid-shutdown (after the kill, before the die) changes nothing
        m = fold(self.NEW, [(1, "seen-up"), (2, "kill"), (3, "seen-up"), (4, "die")])
        self.assertTrue(m["exit_intentional"])
        # running again after an intentional exit, start event missed: the next die is a crash
        m = fold(self.NEW, [(1, "kill"), (2, "die"), (5, "seen-up"), (9, "die")])
        self.assertFalse(m["exit_intentional"])
        # an OOM after a kill (e.g. a reload signal) is a crash
        m = fold(self.NEW, [(1, "kill"), (2, "oom"), (3, "die")])
        self.assertFalse(m["exit_intentional"])
        # order of the list does not matter, only the timestamps
        m = fold(self.NEW, [(397, "die"), (100, "seen-up"), (392, "stop"), (246, "kill")])
        self.assertTrue(m["exit_intentional"])
        # a tie between an event and the snapshot goes to the event
        m = fold(self.NEW, [(5, "seen-down"), (5, "kill")])
        self.assertTrue(m["exit_intentional"])

    def test_events_are_keyed_by_container_id(self):
        events = [
            {"Action": "destroy", "Actor": {"ID": "old", "Attributes": {"name": "jellyfin"}}, "timeNano": 5},
            {"Action": "start", "Actor": {"ID": "new", "Attributes": {"name": "jellyfin"}}, "timeNano": 4},
        ]
        by_id, by_name = af.lifecycle_events(events)
        self.assertEqual(by_id, {"old": [(5, "destroy")], "new": [(4, "start")]})
        self.assertEqual(by_name, {})

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
