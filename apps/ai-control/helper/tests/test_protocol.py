"""Connection-level rules: the SO_PEERCRED check, the op allowlist, request validation, size and time limits,
the config file.

Run from the repo root:  python3 -m unittest discover -s apps/ai-control/helper/tests -v
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import tempfile
import unittest
from unittest import mock

from harness import ME, Env, helper_mod

CONTRACT_OPS = {"containers", "logs", "sudo-sessions", "sudo-replay", "sudo-commands", "localai-activity", "timers"}


class PeerCheck(unittest.TestCase):
    def tearDown(self) -> None:
        self.env.close()

    def test_real_peer_uid_is_read_from_the_kernel(self) -> None:
        self.env = Env()
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with a, b:
            self.assertEqual(helper_mod.peer_credentials(a), (os.getpid(), os.getuid(), os.getgid()))

    def test_wrong_uid_is_refused_before_the_request_is_read(self) -> None:
        # The configured user is someone else ("nobody"); this process connects with its own uid.
        self.env = Env(user="nobody")
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        request = b'{"op": "sudo-replay", "user": "claude", "id": "00/00/01", "output": true}\n'
        with server, client:
            client.sendall(request)
            outcome = self.env.helper.serve(server)
            # The request is still sitting unread in the server's receive buffer.
            server.setblocking(False)
            self.assertEqual(server.recv(65536), request)
            reply = json.loads(client.recv(65536).decode().splitlines()[0])
        self.assertEqual(outcome, "forbidden")
        self.assertEqual(reply, {"ok": False, "error": {"code": "forbidden",
                                                        "message": "only the aictl user may use this socket"}})
        self.assertEqual(self.env.calls, [], "a refused peer must not cause any command")
        self.assertIn(f"rejected peer uid={os.getuid()}", self.env.log)
        self.assertNotIn("sudo-replay", self.env.log)

    def test_root_is_refused_too(self) -> None:
        self.env = Env()
        with mock.patch.object(helper_mod, "peer_credentials", return_value=(1, 0, 0)):
            outcome, reply = self.env.call({"op": "timers"})
        self.assertEqual((outcome, reply["error"]["code"]), ("forbidden", "forbidden"))
        self.assertEqual(self.env.calls, [])

    def test_a_missing_aictl_user_refuses_everyone(self) -> None:
        self.env = Env(user="no-such-user-x1")
        outcome, reply = self.env.call({"op": "timers"})
        self.assertEqual(outcome, "forbidden")
        self.assertEqual(self.env.calls, [])
        self.assertIn("does not exist", self.env.log)

    def test_the_uid_is_looked_up_by_name_on_every_connection(self) -> None:
        self.env = Env()
        with mock.patch.object(self.env.helper.sys, "uid_of", return_value=os.getuid() + 7) as uid_of:
            outcome, _ = self.env.call({"op": "timers"})
        uid_of.assert_called_with(ME)
        self.assertEqual(outcome, "forbidden")

    def test_peer_credentials_failure_is_a_silent_refusal(self) -> None:
        self.env = Env()
        with mock.patch.object(helper_mod, "peer_credentials", side_effect=OSError(22, "Invalid argument")):
            outcome, reply = self.env.call({"op": "timers"})
        self.assertEqual((outcome, reply), ("forbidden", None))
        self.assertEqual(self.env.calls, [])

    def test_the_allowed_peer_is_served_and_logged_as_op_plus_args(self) -> None:
        self.env = Env()
        self.env.ok({"op": "logs", "source": "docker", "lines": 5})
        self.assertIn(f"request peer_uid={os.getuid()} peer_pid={os.getpid()} op=logs source=docker lines=5", self.env.log)
        self.assertIn("done op=logs source=docker lines=5 result=ok", self.env.log)


class Allowlist(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()

    def tearDown(self) -> None:
        self.env.close()

    def test_the_allowlist_is_exactly_the_contract(self) -> None:
        self.assertEqual(set(helper_mod.OPS), CONTRACT_OPS)
        self.assertEqual(set(self.env.helper.handlers), CONTRACT_OPS)

    def test_anything_else_is_unknown(self) -> None:
        for op in ("exec", "restart", "ps", "run", "shell", "write", "overlay", "CONTAINERS", "logs ", "", "sudo"):
            with self.subTest(op=op):
                self.env.err({"op": op}, "unknown-op")
        for req in ({}, {"op": None}, {"op": ["timers"]}, {"op": 1}):
            with self.subTest(req=req):
                self.env.err(req, "unknown-op")
        self.assertEqual(self.env.calls, [])

    def test_unknown_or_misplaced_fields_are_refused(self) -> None:
        for req in ({"op": "timers", "all": True}, {"op": "containers", "name": "ollama-cpu"},
                    {"op": "logs", "source": "docker", "unit": "sshd"}, {"op": "sudo-replay", "user": "claude",
                                                                         "id": "000001", "path": "/etc/shadow"},
                    {"op": "localai-activity", "uid": 0}, {"op": "sudo-commands", "user": "claude", "grep": "."}):
            with self.subTest(req=req):
                self.env.err(req, "bad-request")
        self.assertEqual(self.env.calls, [])

    def test_required_fields(self) -> None:
        for req in ({"op": "logs"}, {"op": "sudo-sessions"}, {"op": "sudo-replay", "user": "claude"},
                    {"op": "sudo-replay", "id": "000001"}, {"op": "sudo-commands"}):
            with self.subTest(req=req):
                self.env.err(req, "bad-request")

    def test_types_are_strict(self) -> None:
        for req in ({"op": "logs", "source": 1}, {"op": "logs", "source": "docker", "lines": "10"},
                    {"op": "logs", "source": "docker", "lines": True}, {"op": "logs", "source": "docker", "lines": 1.5},
                    {"op": "sudo-replay", "user": "claude", "id": "000001", "output": 1},
                    {"op": "sudo-replay", "user": "claude", "id": "000001", "output": "true"},
                    {"op": "sudo-commands", "user": ["claude"]}, {"op": "localai-activity", "since": True},
                    {"op": "localai-activity", "since": 1.5}, {"op": "localai-activity", "limit": None}):
            with self.subTest(req=req):
                self.env.err(req, "bad-request")
        self.assertEqual(self.env.calls, [])

    def test_bounds(self) -> None:
        for req in ({"op": "logs", "source": "docker", "lines": 0}, {"op": "logs", "source": "docker", "lines": 1001},
                    {"op": "sudo-sessions", "user": "claude", "limit": 201},
                    {"op": "sudo-sessions", "user": "claude", "limit": 0},
                    {"op": "sudo-replay", "user": "claude", "id": "000001", "max_bytes": 262145},
                    {"op": "sudo-replay", "user": "claude", "id": "000001", "max_bytes": 0},
                    {"op": "sudo-commands", "user": "claude", "limit": 501},
                    {"op": "localai-activity", "limit": 10**9}):
            with self.subTest(req=req):
                self.env.err(req, "bad-request")
        self.assertEqual(self.env.calls, [])

    def test_since_is_integer_epoch_seconds(self) -> None:
        now = int(self.env.helper.sys.time())
        for since in (now - 5, now - 3600, now + 3600):
            with self.subTest(since=since):
                self.env.ok({"op": "localai-activity", "since": since})
                self.assertIn(f"--since=@{since}", self.env.calls_of("journalctl")[-1])
        for ancient in (0, 1, -10**15, now - 500 * 86400):  # older than any retained log: clamped to 400 days
            with self.subTest(since=ancient):
                self.env.ok({"op": "localai-activity", "since": ancient})
                got = [a for a in self.env.calls_of("journalctl")[-1] if a.startswith("--since=@")][0]
                self.assertLessEqual(abs(int(got[9:]) - (now - 400 * 86400)), 2)
        # Text of any kind is refused, digits included (Lead, 2026-10-26: integers only).
        for since in (str(now), "10m", "2026-10-26T10:00:00Z", "yesterday", "--help", "1; rm", "", "@0",
                      now + 90000, 10**15, 1.5, True, None, [now]):
            with self.subTest(since=since):
                self.env.err({"op": "localai-activity", "since": since}, "bad-request")


class Limits(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()

    def tearDown(self) -> None:
        self.env.close()

    def test_oversize_request_is_refused(self) -> None:
        big = json.dumps({"op": "timers", "pad": "x" * 5000}).encode() + b"\n"
        outcome, reply = self.env.call(None, raw=big)
        self.assertEqual(outcome, "bad-request")
        self.assertIn("larger than 4096", reply["error"]["message"])
        self.assertEqual(self.env.calls, [])
        self.assertNotIn("xxxx", self.env.log)

    def test_a_request_at_the_limit_is_read(self) -> None:
        body = json.dumps({"op": "timers"})
        raw = (body[:-1] + " " * (4095 - len(body)) + "}\n").encode()
        self.assertEqual(len(raw), 4096)
        outcome, _ = self.env.call(None, raw=raw)
        self.assertEqual(outcome, "ok")

    def test_not_json_or_not_an_object(self) -> None:
        for raw in (b"not json\n", b"[\"timers\"]\n", b"\n", b"", b"\xff\xfe\n", b"{\"op\": \"timers\"\n"):
            with self.subTest(raw=raw):
                outcome, reply = self.env.call(None, raw=raw)
                self.assertIn(outcome, ("bad-request", "unknown-op"))
                self.assertFalse(reply["ok"])

    def test_only_the_first_line_is_the_request(self) -> None:
        outcome, _ = self.env.call(None, raw=b'{"op": "timers"}\n{"op": "containers"}\n')
        self.assertEqual(outcome, "ok")
        self.assertNotIn(["inspect"], [c[:1] for c in self.env.calls_of("docker")])

    def test_a_silent_client_times_out(self) -> None:
        self.env.close()
        self.env = Env(request_timeout=0.2)
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with server, client:
            outcome = self.env.helper.serve(server)
            reply = json.loads(client.recv(4096).decode().splitlines()[0])
        self.assertEqual((outcome, reply["error"]["code"]), ("bad-request", "bad-request"))

    def test_the_log_never_carries_raw_requests(self) -> None:
        self.env.call(None, raw=b'{"op": "exec", "cmd": "cat /etc/shadow"}\n')
        self.env.call(None, raw=b'{"op": "sudo-replay", "user": "claude", "id": "../../etc/shadow"}\n')
        self.assertNotIn("shadow", self.env.log)
        self.assertIn("rejected request", self.env.log)

    def test_an_internal_error_is_contained(self) -> None:
        with mock.patch.object(self.env.helper, "op_timers", side_effect=KeyError("boom")):
            self.env.helper.handlers["timers"] = self.env.helper.op_timers
            outcome, reply = self.env.call({"op": "timers"})
        self.assertEqual(outcome, "internal")
        self.assertEqual(reply["error"], {"code": "internal", "message": "internal error"})
        self.assertNotIn("boom", self.env.log)


class ConfigFile(unittest.TestCase):
    def test_validation(self) -> None:
        good = {"user": "aictl"}
        self.assertEqual(helper_mod.Config.from_mapping(good).containers, ("ollama-cpu", "gpu-broker", "open-webui"))
        for bad in ({"typo": 1}, {"user": "root"}, {"user": "Bad User"}, {"containers": ["../x"]},
                    {"containers": "ollama-cpu"}, {"log_units": {"docker": []}},
                    {"log_units": {"docker": ["--output=cat"]}}, {"log_units": {"x y": ["a.service"]}},
                    {"log_units": {"ollama-cpu": ["ollama.service"]}},  # overlaps a container source
                    {"sudo_users": ["../root"]}, {"docker": "docker"}, {"iolog_root": "/var/log/../x"},
                    {"command_timeout": 11}, {"command_timeout": 0}, {"request_timeout": True},
                    {"timer_patterns": ["-x"]}, {"sudo_exes": ["sudo"]}, {"sudo_exes": []}, {"sudo_exes": ["/usr/../bin/sh"]}, {"docker_unit": "docker"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                helper_mod.Config.from_mapping({**good, **bad})

    def test_the_example_config_is_valid(self) -> None:
        example = pathlib.Path(helper_mod.__file__).parent / "helper.example.json"
        cfg = helper_mod.Config.from_mapping(json.loads(example.read_text()))
        self.assertEqual(set(cfg.sudo_users), {"claude", "localai"})
        self.assertEqual(cfg, helper_mod.Config(), "the example spells out the defaults")

    def test_the_file_must_be_owned_by_the_helper_user_and_not_writable_by_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "helper.json"
            path.write_text(json.dumps({"user": "aictl"}))
            os.chmod(path, 0o644)
            self.assertEqual(helper_mod.load_config(str(path), os.getuid()).user, "aictl")
            with self.assertRaises(ValueError):
                helper_mod.load_config(str(path), os.getuid() + 1)  # e.g. not root-owned in production
            for mode in (0o664, 0o646):
                os.chmod(path, mode)
                with self.subTest(mode=oct(mode)), self.assertRaises(ValueError):
                    helper_mod.load_config(str(path), os.getuid())
            os.chmod(path, 0o644)
            link = pathlib.Path(tmp) / "link.json"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                helper_mod.load_config(str(link), os.getuid())


if __name__ == "__main__":
    unittest.main()
