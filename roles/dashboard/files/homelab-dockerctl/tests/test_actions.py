"""update (the step stream), start/stop/restart, the gate hold and the per-container lock."""

from __future__ import annotations

import fcntl
import os
import time
import unittest

from fakes import DOCKER, IMAGES, Env, cid, read_lines


def steps(lines: list[dict]) -> list[str]:
    return [line["step"] for line in lines if line.get("type") == "step"]


class Update(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()

    def tearDown(self) -> None:
        self.env.close()

    def test_the_step_stream(self) -> None:
        outcome, lines = self.env.call({"op": "update", "name": "sonarr"})
        self.assertEqual(outcome, "ok")
        self.assertEqual(lines[0], {"ok": True, "op": "update", "stream": True, "name": "sonarr",
                                    "pinned": IMAGES["sonarr"]})
        self.assertEqual((lines[1]["step"], lines[1]["image"]), ("pulling", IMAGES["sonarr"]))
        self.assertRegex(lines[1]["at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(steps(lines), ["pulling", "recreating", "waiting-health", "done"])
        self.assertEqual(lines[-1], {"type": "end", "ok": True, "reason": "done"})
        done = next(line for line in lines if line.get("step") == "done")
        self.assertEqual(done["image"], IMAGES["sonarr"])
        self.assertEqual(done["health"], "healthy")
        outputs = [line["text"] for line in lines if line.get("type") == "output"]
        self.assertIn("sonarr Pulled", [t.strip() for t in outputs], "ANSI colour codes are stripped")
        self.assertTrue(all("\x1b" not in t for t in outputs))
        # Steps arrive in order and the output sits between them.
        kinds = [line.get("step") or line["type"] for line in lines[1:]]
        self.assertLess(kinds.index("pulling"), kinds.index("output"))
        self.assertLess(kinds.index("output"), kinds.index("recreating"))

    def test_exact_compose_commands_from_the_deployed_file(self) -> None:
        self.env.call({"op": "update", "name": "sonarr"})
        compose = [c for c in self.env.sys.docker_calls() if c[1] == "compose"]
        f = str(self.env.stacks / "media/compose.yaml")
        base = [DOCKER, "compose", "--ansi", "never", "--progress", "plain", "-f", f]
        self.assertEqual(compose, [
            [*base, "pull", "sonarr"],
            [*base, "up", "--detach", "--no-deps", "--no-build", "--pull", "never", "sonarr"],
        ])

    def test_pull_failure_stops_before_the_recreate(self) -> None:
        self.env.sys.pull_rc = 1
        self.env.sys.pull_output = b"Error response from daemon: manifest unknown\n"
        outcome, lines = self.env.call({"op": "update", "name": "sonarr"})
        self.assertEqual(outcome, "failed")
        self.assertEqual(steps(lines), ["pulling", "failed"])
        failed = next(line for line in lines if line.get("step") == "failed")
        self.assertEqual((failed["failed_step"], failed["code"], failed["message"]), ("pulling", "pull-failed", "exited 1"))
        self.assertEqual(lines[-1], {"type": "end", "ok": False, "reason": "failed", "message": "pulling: exited 1"})
        self.assertFalse(any("up" in c for c in self.env.sys.docker_calls()))

    def test_recreate_failure(self) -> None:
        self.env.sys.up_rc = 1
        outcome, lines = self.env.call({"op": "update", "name": "sonarr"})
        self.assertEqual(outcome, "failed")
        self.assertEqual(steps(lines), ["pulling", "recreating", "failed"])

    def test_unhealthy_after_recreate(self) -> None:
        self.env.sys.health_after_up = ["starting", "starting", "unhealthy"]
        outcome, lines = self.env.call({"op": "update", "name": "sonarr"})
        self.assertEqual(outcome, "failed")
        failed = next(line for line in lines if line.get("step") == "failed")
        self.assertEqual((failed["failed_step"], failed["code"]), ("waiting-health", "health-failed"))
        self.assertEqual(failed["message"], "health check failing")

    def test_health_timeout(self) -> None:
        self.env.sys.health_after_up = ["starting"]
        outcome, lines = self.env.call({"op": "update", "name": "sonarr"})
        self.assertEqual(outcome, "failed")
        failed = next(line for line in lines if line.get("step") == "failed")
        self.assertEqual(failed["message"], "not healthy after 60 s")
        self.assertTrue(self.env.sys.sleeps and all(s == 2 for s in self.env.sys.sleeps))

    def test_exited_after_recreate(self) -> None:
        self.env.sys.state_after_up = "exited"
        outcome, lines = self.env.call({"op": "update", "name": "sonarr"})
        self.assertEqual(outcome, "failed")
        self.assertIn("container exited", next(line for line in lines if line.get("step") == "failed")["message"])

    def test_no_healthcheck_means_running_and_stable(self) -> None:
        self.env.sys.health_after_up = [None]
        outcome, lines = self.env.call({"op": "update", "name": "cloudflared"})
        self.assertEqual(outcome, "ok")
        self.assertEqual(steps(lines), ["pulling", "recreating", "waiting-health", "done"])
        self.assertGreaterEqual(sum(self.env.sys.sleeps), 5)

    def test_local_images_are_never_updated_here(self) -> None:
        outcome, lines = self.env.call({"op": "update", "name": "wol-relay"})
        self.assertEqual(outcome, "local-image")
        self.assertEqual(len(lines), 1)
        self.assertFalse(any(c[1] == "compose" for c in self.env.sys.docker_calls()))

    def test_unlabelled_or_unknown_containers_are_refused(self) -> None:
        for name in ("stranger", "nosuch", cid(1)[:12]):
            with self.subTest(name=name):
                outcome, _ = self.env.call({"op": "update", "name": name})
                self.assertEqual(outcome, "not-allowed")
        self.assertFalse(any(c[1] == "compose" for c in self.env.sys.docker_calls()))

    def test_media_apps_stay_held_while_the_gate_is_not_ok(self) -> None:
        for status in ("failed", "media-lost", "waiting"):
            with self.subTest(status=status):
                self.env.set_gate(status)
                outcome, lines = self.env.call({"op": "update", "name": "sonarr"})
                self.assertEqual(outcome, "held-by-gate")
                self.assertIn(status, lines[0]["error"]["message"])
        self.env.status.unlink()
        self.assertEqual(self.env.call({"op": "update", "name": "sonarr"})[0], "held-by-gate")
        self.assertFalse(any(c[1] == "compose" for c in self.env.sys.docker_calls()))
        # Stacks outside the gate's hold are not affected.
        self.env.set_gate("media-lost")
        self.env.sys.health_after_up = [None]
        self.assertEqual(self.env.call({"op": "update", "name": "cloudflared"})[0], "ok")

    def test_a_second_action_on_the_same_container_is_busy(self) -> None:
        locks = self.env.cache / "locks"
        locks.mkdir(parents=True)
        fd = os.open(locks / "container-sonarr.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for op in ("update", "restart", "stop", "start"):
                with self.subTest(op=op):
                    outcome, _ = self.env.call({"op": op, "name": "sonarr"})
                    self.assertEqual(outcome, "busy")
            self.assertEqual(self.env.call({"op": "restart", "name": "jellyfin"})[0], "ok", "other containers are free")
        finally:
            os.close(fd)
        self.assertEqual(self.env.call({"op": "restart", "name": "sonarr"})[0], "ok")

    def test_the_update_finishes_after_the_client_disconnects(self) -> None:
        def slow_pull(argv: list[str]) -> None:
            if "pull" in argv:
                time.sleep(0.2)  # the client is gone by now

        self.env.sys.on_spawn = slow_pull
        client, thread, result = self.env.call_threaded({"op": "update", "name": "sonarr"})
        header = read_lines(client, 1)[0]
        self.assertTrue(header["stream"])
        client.close()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["outcome"], "ok")
        self.assertTrue(any("up" in c for c in self.env.sys.docker_calls()), "the recreate still ran")
        self.assertIn("result=ok client-gone", self.env.log)


class StartStopRestart(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()

    def tearDown(self) -> None:
        self.env.close()

    def test_actions_use_the_full_container_id(self) -> None:
        for op, state in (("stop", "exited"), ("start", "running"), ("restart", "running")):
            with self.subTest(op=op):
                outcome, lines = self.env.call({"op": op, "name": "sonarr"})
                self.assertEqual(outcome, "ok")
                self.assertEqual(lines, [{"ok": True, "op": op, "data": {"name": "sonarr", "state": state}}])
                self.assertIn([DOCKER, op, cid(1)], self.env.sys.docker_calls())

    def test_failures_are_reported_without_a_wall_of_text(self) -> None:
        self.env.sys.action_rc = 1
        outcome, lines = self.env.call({"op": "restart", "name": "sonarr"})
        self.assertEqual(outcome, "failed")
        self.assertEqual(lines[0]["error"]["message"], "docker restart exited 1: Error response from daemon: boom")

    def test_stop_is_allowed_while_the_gate_holds_but_start_is_not(self) -> None:
        self.env.set_gate("media-lost")
        self.assertEqual(self.env.call({"op": "stop", "name": "sonarr"})[0], "ok")
        self.assertEqual(self.env.call({"op": "start", "name": "sonarr"})[0], "held-by-gate")
        self.assertEqual(self.env.call({"op": "restart", "name": "jellyfin"})[0], "held-by-gate")
        self.assertEqual(self.env.call({"op": "restart", "name": "cloudflared"})[0], "ok")

    def test_unlabelled_containers_are_untouchable(self) -> None:
        for op in ("start", "stop", "restart"):
            with self.subTest(op=op):
                self.assertEqual(self.env.call({"op": op, "name": "stranger"})[0], "not-allowed")
        self.assertFalse([c for c in self.env.sys.docker_calls() if c[1] in ("start", "stop", "restart")])

    def test_requests_are_logged(self) -> None:
        self.env.call({"op": "restart", "name": "sonarr"})
        self.assertIn("op=restart name=sonarr", self.env.log)
        self.assertIn("done op=restart name=sonarr result=ok", self.env.log)


if __name__ == "__main__":
    unittest.main()
