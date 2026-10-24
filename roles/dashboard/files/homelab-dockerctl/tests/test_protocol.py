"""Connection-level rules: SO_PEERCRED rejection, the operation allowlist, request validation.

Run from the repo root:  python3 -m unittest discover -s roles/dashboard/files/homelab-dockerctl/tests -v
"""

from __future__ import annotations

import json
import os
import socket
import unittest
from unittest import mock

from fakes import DOCKER, Env, dockerctl

CONTRACT_OPS = {"ps", "logs", "start", "stop", "restart", "update", "docker-state", "last-pull", "disk-split", "boots"}


class PeerCredentials(unittest.TestCase):
    def tearDown(self) -> None:
        self.env.close()

    def test_real_peer_uid_is_read_from_the_kernel(self) -> None:
        self.env = Env()
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with a, b:
            pid, uid, gid = dockerctl.peer_credentials(a)
        self.assertEqual((pid, uid, gid), (os.getpid(), os.getuid(), os.getgid()))

    def test_other_uid_is_rejected_before_anything_runs(self) -> None:
        self.env = Env(allowed_uid=os.getuid() + 1)
        outcome, lines = self.env.call({"op": "ps"})
        self.assertEqual(outcome, "forbidden")
        self.assertEqual(lines, [{"ok": False, "error": {"code": "forbidden",
                                                         "message": "only the dashboard user may use this socket"}}])
        self.assertEqual(self.env.sys.calls, [], "a rejected peer must not cause any command")
        self.assertIn(f"rejected peer uid={os.getuid()}", self.env.log)

    def test_root_is_rejected_too(self) -> None:
        self.env = Env()
        with mock.patch.object(dockerctl, "peer_credentials", return_value=(1, 0, 0)):
            outcome, lines = self.env.call({"op": "docker-state"})
        self.assertEqual(outcome, "forbidden")
        self.assertFalse(lines[0]["ok"])
        self.assertEqual(self.env.sys.calls, [])

    def test_the_rejected_request_is_never_read(self) -> None:
        self.env = Env(allowed_uid=os.getuid() + 1)
        outcome, _ = self.env.call(None, raw=b'{"op": "stop", "name": "sonarr"}\n')
        self.assertEqual(outcome, "forbidden")
        self.assertNotIn("sonarr", self.env.log)

    def test_peer_credentials_failure_is_a_rejection(self) -> None:
        self.env = Env()
        with mock.patch.object(dockerctl, "peer_credentials", side_effect=OSError(22, "Invalid argument")):
            outcome, lines = self.env.call({"op": "ps"})
        self.assertEqual(outcome, "forbidden")
        self.assertEqual(lines, [])
        self.assertEqual(self.env.sys.calls, [])

    def test_allowed_peer_is_served_and_logged(self) -> None:
        self.env = Env()
        outcome, lines = self.env.call({"op": "docker-state"})
        self.assertEqual(outcome, "ok")
        self.assertEqual(lines, [{"ok": True, "op": "docker-state", "data": {"state": "active", "unit_state": "active"}}])
        self.assertIn(f"request peer_uid={os.getuid()} peer_pid={os.getpid()} op=docker-state", self.env.log)
        self.assertIn("done op=docker-state result=ok", self.env.log)


class Allowlist(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()

    def tearDown(self) -> None:
        self.env.close()

    def test_the_allowlist_is_exactly_the_contract(self) -> None:
        self.assertEqual(set(dockerctl.OPS), CONTRACT_OPS)
        self.assertEqual(set(self.env.helper.handlers), CONTRACT_OPS)

    def test_anything_else_is_unknown(self) -> None:
        for op in ("exec", "build", "rm", "pull", "inspect", "images", "volume", "run", "PS", "ps ", "", "compose"):
            with self.subTest(op=op):
                outcome, lines = self.env.call({"op": op, "name": "sonarr"})
                self.assertEqual(outcome, "unknown-op")
                self.assertEqual(lines[0]["error"]["code"], "unknown-op")
        self.assertEqual(self.env.sys.calls, [])

    def test_op_must_be_a_string(self) -> None:
        for req in ({}, {"op": None}, {"op": ["ps"]}, {"op": 1}, {"name": "sonarr"}):
            with self.subTest(req=req):
                outcome, _ = self.env.call(req)
                self.assertEqual(outcome, "unknown-op")

    def test_every_allowed_op_dispatches(self) -> None:
        self.env.sys.boots_output = "[]"
        self.env.sys.du_output = ""
        for op, extra in [("ps", {}), ("docker-state", {}), ("last-pull", {}), ("boots", {}),
                          ("logs", {"name": "sonarr"}), ("stop", {"name": "sonarr"}), ("start", {"name": "sonarr"}),
                          ("restart", {"name": "sonarr"}), ("update", {"name": "sonarr"})]:
            with self.subTest(op=op):
                outcome, lines = self.env.call({"op": op, **extra})
                self.assertEqual(outcome, "ok", lines)
                self.assertTrue(lines[0]["ok"])
                self.assertEqual(lines[0]["op"], op)
                if op in ("logs", "update"):
                    self.assertTrue(lines[0]["stream"])
                    self.assertEqual((lines[-1]["type"], lines[-1]["ok"]), ("end", True), lines)
                else:
                    self.assertEqual(len(lines), 1)
                    self.assertEqual(set(lines[0]), {"ok", "op", "data"})


class Validation(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()

    def tearDown(self) -> None:
        self.env.close()

    def assert_bad(self, request, code: str = "bad-request") -> None:
        outcome, lines = self.env.call(request)
        self.assertEqual(outcome, code, lines)
        self.assertEqual(len(lines), 1)
        self.assertFalse(lines[0]["ok"])
        self.assertEqual(lines[0]["error"]["code"], code)

    def test_names_are_strict(self) -> None:
        for name in ("", "Sonarr", "../sonarr", "a b", "-x", "x" * 64, "sonarr;rm -rf /", "sonarr\n", "són",
                     "/sonarr", "sonarr/..", ".hidden", "$(id)", "*"):
            with self.subTest(name=name):
                self.assert_bad({"op": "restart", "name": name})
        for bad_type in (1, None, ["sonarr"], {"n": 1}, True):
            with self.subTest(name=bad_type):
                self.assert_bad({"op": "stop", "name": bad_type})
        self.assertEqual(self.env.sys.docker_calls(), [])

    def test_name_is_required(self) -> None:
        for op in ("logs", "start", "stop", "restart", "update"):
            with self.subTest(op=op):
                self.assert_bad({"op": op})

    def test_unknown_or_misplaced_fields_are_refused(self) -> None:
        self.assert_bad({"op": "ps", "name": "sonarr"})
        self.assert_bad({"op": "restart", "name": "sonarr", "timeout": 1})
        self.assert_bad({"op": "logs", "name": "sonarr", "cmd": "sh"})
        self.assert_bad({"op": "update", "name": "sonarr", "image": "evil:latest"})
        self.assert_bad({"op": "disk-split", "path": "/etc"})
        self.assertEqual(self.env.sys.calls, [])

    def test_tail_bounds_and_type(self) -> None:
        for tail in (-1, 2001, 10**9, "10", 1.5, True, None):
            with self.subTest(tail=tail):
                self.assert_bad({"op": "logs", "name": "sonarr", "tail": tail})
        for tail in (0, 1, 2000):
            with self.subTest(tail=tail):
                outcome, _ = self.env.call({"op": "logs", "name": "sonarr", "tail": tail})
                self.assertEqual(outcome, "ok")

    def test_follow_must_be_a_boolean(self) -> None:
        for follow in (1, 0, "true", None, []):
            with self.subTest(follow=follow):
                self.assert_bad({"op": "logs", "name": "sonarr", "follow": follow})

    def test_since_formats(self) -> None:
        for since in ("10m", "2h", "30s", "1792850000", "2026-10-24", "2026-10-24T10:00:00Z",
                      "2026-10-24T10:00:00.123456789+02:00"):
            with self.subTest(since=since):
                outcome, _ = self.env.call({"op": "logs", "name": "sonarr", "since": since})
                self.assertEqual(outcome, "ok")
        for since in ("yesterday", "10d", "--help", "1; rm", "", "2026-10-24 10:00", "-1h", "10m ", 5):
            with self.subTest(since=since):
                self.assert_bad({"op": "logs", "name": "sonarr", "since": since})

    def test_request_must_be_one_json_object(self) -> None:
        for raw in (b"not json\n", b"[\"ps\"]\n", b"\"ps\"\n", b"\n", b"", b"\xff\xfe\n", b"{\"op\": \"ps\"\n"):
            with self.subTest(raw=raw):
                outcome, lines = self.env.call(None, raw=raw)
                self.assertIn(outcome, ("bad-request", "unknown-op"))
                self.assertFalse(lines[0]["ok"])

    def test_request_size_limit(self) -> None:
        big = json.dumps({"op": "ps", "pad": "x" * 5000}).encode() + b"\n"
        outcome, lines = self.env.call(None, raw=big)
        self.assertEqual(outcome, "bad-request")
        self.assertIn("larger than 4096", lines[0]["error"]["message"])

    def test_only_the_first_line_is_the_request(self) -> None:
        outcome, lines = self.env.call(None, raw=b'{"op": "docker-state"}\n{"op": "stop", "name": "sonarr"}\n')
        self.assertEqual(outcome, "ok")
        self.assertEqual(len(lines), 1)
        self.assertNotIn([DOCKER, "stop"], [c[:2] for c in self.env.sys.calls])

    def test_a_silent_client_times_out(self) -> None:
        self.env.close()
        self.env = Env(request_timeout=0.2)
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with server, client:
            outcome = self.env.helper.serve(server)
            server.shutdown(socket.SHUT_WR)
            reply = json.loads(client.recv(4096).decode().splitlines()[0])
        self.assertEqual(outcome, "bad-request")
        self.assertEqual(reply["error"]["code"], "bad-request")

    def test_the_log_never_carries_raw_requests(self) -> None:
        self.env.call(None, raw=b'{"op": "exec", "cmd": "cat /etc/shadow"}\n')
        self.assertNotIn("shadow", self.env.log)
        self.assertIn("rejected request", self.env.log)


class ConfigFile(unittest.TestCase):
    def test_unknown_keys_and_root_uid_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            dockerctl.Config.from_mapping({"allowed_uid": 10101, "typo_key": 1})
        for uid in (0, -1, "10101", True):
            with self.subTest(uid=uid), self.assertRaises(ValueError):
                dockerctl.Config.from_mapping({"allowed_uid": uid})
        with self.assertRaises(ValueError):
            dockerctl.Config.from_mapping({"allowed_uid": 10101, "media_dirs": ["../etc"]})
        with self.assertRaises(ValueError):
            dockerctl.Config.from_mapping({"allowed_uid": 10101, "docker": "docker"})
        cfg = dockerctl.Config.from_mapping({"allowed_uid": 10101, "media_dirs": ["tv"]})
        self.assertEqual(cfg.media_dirs, ("tv",))


if __name__ == "__main__":
    unittest.main()
