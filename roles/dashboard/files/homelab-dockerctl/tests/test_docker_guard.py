"""The docker-inactive guard: while docker.service is not active, no docker command (and so no
socket activation of Docker, which would re-run the boot gate) may ever be issued."""

from __future__ import annotations

import unittest

from fakes import DOCKER, SYSTEMCTL, Env

ALL_REQUESTS = [
    {"op": "ps"},
    {"op": "docker-state"},
    {"op": "logs", "name": "sonarr"},
    {"op": "logs", "name": "sonarr", "follow": True},
    {"op": "start", "name": "sonarr"},
    {"op": "stop", "name": "cloudflared"},
    {"op": "restart", "name": "jellyfin"},
    {"op": "update", "name": "sonarr"},
    {"op": "last-pull"},
    {"op": "boots"},
    {"op": "disk-split"},
]
NEEDS_DOCKER = {"logs", "start", "stop", "restart", "update"}


class DockerInactive(unittest.TestCase):
    def test_no_docker_command_for_any_op_in_any_non_active_state(self) -> None:
        for state in ("inactive", "failed", "activating", "deactivating", "reloading", "unknown", ""):
            env = Env()
            env.sys.docker_state = state
            env.sys.du_output = f"10\t{env.media}/tv\n"
            try:
                for req in ALL_REQUESTS:
                    with self.subTest(state=state, op=req["op"]):
                        outcome, lines = env.call(req)
                        if req["op"] in NEEDS_DOCKER:
                            self.assertEqual(outcome, "docker-inactive")
                            self.assertEqual(lines[0]["error"]["code"], "docker-inactive")
                            self.assertEqual(len(lines), 1, "no stream may start")
                        else:
                            self.assertEqual(outcome, "ok", lines)
                self.assertEqual(env.sys.docker_calls(), [], f"docker was called while docker.service was {state!r}")
                self.assertTrue(env.sys.calls, "the guard itself must have run")
            finally:
                env.close()

    def test_the_guard_asks_systemd_exactly(self) -> None:
        env = Env()
        env.sys.docker_state = "inactive"
        try:
            env.call({"op": "restart", "name": "sonarr"})
            self.assertEqual(env.sys.calls, [[SYSTEMCTL, "is-active", "docker.service"]])
        finally:
            env.close()

    def test_ps_still_lists_the_declared_apps_without_docker(self) -> None:
        env = Env()
        env.sys.docker_state = "failed"
        try:
            outcome, lines = env.call({"op": "ps"})
            self.assertEqual(outcome, "ok")
            data = lines[0]["data"]
            self.assertEqual(data["docker"], "failed")
            self.assertEqual(data["containers"], [])
            names = sorted(d["name"] for d in data["declared"])
            self.assertEqual(names, ["cloudflared", "jellyfin", "jellyseerr", "prowlarr", "radarr", "sabnzbd",
                                     "sonarr", "wol-relay"])
            self.assertEqual(env.sys.docker_calls(), [])
        finally:
            env.close()

    def test_docker_state_maps_to_the_contract_values(self) -> None:
        for raw, want in (("active", "active"), ("failed", "failed"), ("inactive", "inactive"),
                          ("activating", "inactive"), ("deactivating", "inactive"), ("", "inactive")):
            env = Env()
            env.sys.docker_state = raw
            try:
                _, lines = env.call({"op": "docker-state"})
                self.assertEqual(lines[0]["data"]["state"], want, raw)
                self.assertEqual(lines[0]["data"]["unit_state"], raw or "unknown")
            finally:
                env.close()

    def test_the_guard_is_rechecked_before_every_docker_command(self) -> None:
        """Docker stops between the pull and the recreate: the recreate must not be issued."""
        env = Env()

        def stop_docker_after_pull(argv: list[str]) -> None:
            if "pull" in argv:
                env.sys.docker_state = "deactivating"

        env.sys.on_spawn = stop_docker_after_pull
        try:
            outcome, lines = env.call({"op": "update", "name": "sonarr"})
            self.assertEqual(outcome, "failed")
            steps = [line["step"] for line in lines if line.get("type") == "step"]
            self.assertEqual(steps, ["pulling", "recreating", "failed"])
            failed = next(line for line in lines if line.get("step") == "failed")
            self.assertEqual((failed["failed_step"], failed["code"]), ("recreating", "docker-inactive"))
            self.assertIn("docker.service is deactivating", failed["message"])
            self.assertFalse(any("up" in c for c in env.sys.docker_calls()), "no compose up after Docker stopped")
            self.assertEqual(lines[-1], {"type": "end", "ok": False, "reason": "failed",
                                         "message": "recreating: " + failed["message"]})
        finally:
            env.close()

    def test_a_follow_never_starts_when_docker_is_down(self) -> None:
        env = Env()
        env.sys.docker_state = "inactive"
        try:
            outcome, _ = env.call({"op": "logs", "name": "sonarr", "follow": True})
            self.assertEqual(outcome, "docker-inactive")
            self.assertEqual(env.sys.procs, [])
        finally:
            env.close()

    def test_the_docker_binary_is_only_reached_through_the_guard(self) -> None:
        """Every docker invocation in an active run is preceded by a fresh is-active check."""
        env = Env()
        try:
            env.call({"op": "update", "name": "sonarr"})
            previous = None
            for call in env.sys.calls:
                if call[0] == DOCKER:
                    self.assertEqual(previous, [SYSTEMCTL, "is-active", "docker.service"], call)
                previous = call
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()
