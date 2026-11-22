"""The command-backed ops, run through the real subprocess path against stub executables that
record their argv: containers, logs, sudo-commands, localai-activity, timers, and the timeout."""

from __future__ import annotations

import json
import os
import time
import unittest

from harness import helper_mod, CID, ME, Env, inspect_obj, journal_line

LEAKS = ("do-not-leak", "PROBE-SECRET", "/srv/x", "abcdef0123456789", "k-123456")


class Containers(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()

    def tearDown(self) -> None:
        self.env.close()

    def test_allowlisted_containers_only_and_only_the_contract_fields(self) -> None:
        data = self.env.ok({"op": "containers"})
        self.assertEqual(data["docker"], "active")
        self.assertEqual([c["name"] for c in data["containers"]], ["ollama-cpu", "gpu-broker", "open-webui"])
        for c in data["containers"]:
            self.assertEqual(set(c), {"name", "state", "health", "started_at", "restart_count", "image"})
        self.assertEqual(data["containers"][0], {"name": "ollama-cpu", "state": "running", "health": "healthy",
                                                 "started_at": 1792994400,
                                                 "restart_count": 0, "image": "example/ollama-cpu:1.0"})
        self.assertEqual((data["containers"][1]["health"], data["containers"][1]["restart_count"]), (None, 2))
        self.assertEqual(data["containers"][2]["state"], "exited")
        blob = json.dumps(data)
        for leak in LEAKS:
            self.assertNotIn(leak, blob)
        # once for the docker field, and again right before the docker command (the guard never caches)
        self.assertEqual(self.env.calls_of("systemctl"), [["is-active", "docker.service"]] * 2)
        self.assertEqual([c[0] for c in self.env.calls], ["systemctl", "systemctl", "docker"])
        self.assertEqual(self.env.calls_of("docker"),
                         [["inspect", "--type", "container", "--", "ollama-cpu", "gpu-broker", "open-webui"]])

    def test_other_containers_in_the_answer_are_ignored_and_missing_ones_reported(self) -> None:
        self.env.set_rule("docker", [{"match": ["inspect"], "rc": 1, "stderr": "Error: No such object: open-webui",
                                      "stdout": json.dumps([inspect_obj("ollama-cpu"), inspect_obj("stranger"),
                                                            {**inspect_obj("gpu-broker"), "Name": "/gpu-broker-x"}])}])
        data = self.env.ok({"op": "containers"})
        self.assertEqual([(c["name"], c["state"]) for c in data["containers"]],
                         [("ollama-cpu", "running"), ("gpu-broker", "missing"), ("open-webui", "missing")])
        self.assertNotIn("stranger", json.dumps(data))

    def test_docker_inactive_never_touches_docker(self) -> None:
        for state in ("inactive", "failed", "activating"):
            with self.subTest(state=state):
                self.env.docker_inactive(state)
                data = self.env.ok({"op": "containers"})
                self.assertEqual(data, {"docker": "failed" if state == "failed" else "inactive", "containers": []})
                err = self.env.err({"op": "logs", "source": "ollama-cpu"}, "docker-inactive")
                self.assertIn("not touched", err["message"])
        self.assertEqual(self.env.calls_of("docker"), [], "docker must never run while docker.service is not active")
        self.assertTrue(self.env.calls_of("systemctl"))

    def test_invalid_inspect_output(self) -> None:
        self.env.set_rule("docker", [{"match": ["inspect"], "stdout": "not json"}])
        self.env.err({"op": "containers"}, "failed")


class Logs(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()

    def tearDown(self) -> None:
        self.env.close()

    def test_unit_sources_map_to_fixed_units(self) -> None:
        self.env.set_rule("journalctl", [{"stdout": "\n".join([
            journal_line(1793000002000000, "second \x1b[1mbold\x1b[0m token=abc123"),
            journal_line(1793000001000000, [104, 105, 0xff]),
            "{partial",
        ]) + "\n"}])
        data = self.env.ok({"op": "logs", "source": "aictl-helper", "lines": 50, "since": int(time.time()) - 3600})
        self.assertEqual(data["kind"], "unit")
        self.assertEqual([line["text"] for line in data["lines"]], ["hi�", "second bold token=[REDACTED]"])
        self.assertEqual(data["lines"][1]["ts"], 1793000002)
        argv = self.env.calls_of("journalctl")[0]
        self.assertEqual(argv[:6], ["--output=json", "--no-pager", "--quiet", "--reverse", "--lines=50",
                                    "--output-fields=MESSAGE,_SYSTEMD_UNIT"])
        self.assertTrue(argv[6].startswith("--since=@"))
        self.assertEqual(argv[7:], ["--unit=aictl-helper@*.service"])
        self.env.ok({"op": "logs", "source": "homelab-ai-stack"})
        self.assertEqual(self.env.calls_of("journalctl")[1][-1], "--unit=homelab-ai-stack*")
        self.assertIn("--lines=200", self.env.calls_of("journalctl")[1])

    def test_container_logs_by_exact_name_then_id(self) -> None:
        data = self.env.ok({"op": "logs", "source": "gpu-broker", "lines": 3, "since": 1793000000})
        self.assertEqual(data["kind"], "container")
        self.assertEqual(self.env.calls_of("docker"), [
            ["inspect", "--type", "container", "--", "gpu-broker"],
            ["logs", "--timestamps", "--tail", "3", "--since", "1793000000", CID["gpu-broker"]],
        ])
        texts = [line["text"] for line in data["lines"]]
        self.assertIn("started", texts)
        self.assertIn("Authorization: Bearer [REDACTED]", texts)
        self.assertIn("warn: api_key=[REDACTED]", texts)  # stderr is read too
        blob = json.dumps(data)
        for leak in LEAKS:
            self.assertNotIn(leak, blob)
        self.assertNotIn("\x1b", blob)
        self.assertEqual([x["ts"] for x in data["lines"]], [1792994400, 1792994401, 1792994402])

    def test_lines_are_redacted_before_they_are_capped(self) -> None:
        token = "ghp_" + "Z" * 36
        line = "x" * 1990 + " " + token  # the 2000-char cap falls inside the token
        self.env.set_rule("journalctl", [{"stdout": journal_line(1793000000000000, line) + "\n" +
                                          journal_line(1793000000000000, "y" * 5000) + "\n"}])
        data = self.env.ok({"op": "logs", "source": "docker"})
        texts = [entry["text"] for entry in data["lines"]]
        self.assertEqual([len(t) for t in texts], [2000, 2000])
        self.assertNotIn("ghp_", texts[1])
        self.assertNotIn("ZZZZ", texts[1])

    def test_unknown_sources_are_refused_without_running_anything(self) -> None:
        for source in ("sshd", "ollama", "../docker", "docker.service", "aictl-helper@1", "Docker", "*", "-u",
                       "homelab-ai-stack-x", "stranger"):
            with self.subTest(source=source):
                self.env.err({"op": "logs", "source": source}, None)
        self.assertEqual(self.env.calls, [])

    def test_a_vanished_container(self) -> None:
        self.env.set_rule("docker", [{"match": ["inspect"], "stdout": "[]", "rc": 1}])
        self.env.err({"op": "logs", "source": "open-webui"}, "not-found")
        self.assertEqual([c[0] for c in self.env.calls_of("docker")], ["inspect"])


SUDO = "/usr/bin/sudo.ws"
CLAUDE_UID = "999"


def sudo_line(ts: int, msg: str, *, exe: str = SUDO, uid: str = CLAUDE_UID, pid: str = "4242", **extra) -> str:
    return journal_line(ts, msg, _EXE=exe, _UID=uid, _PID=pid, _COMM="sudo", **extra)


class SudoCommands(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()

    def tearDown(self) -> None:
        self.env.close()

    def rows(self, lines: list[str], user: str = "claude", **req) -> list[dict]:
        self.env.set_rule("journalctl", [{"stdout": "".join(line + "\n" for line in lines)}])
        return self.env.ok({"op": "sudo-commands", "user": user, **req})["commands"]

    def test_parses_sudo_lines_by_the_user(self) -> None:
        rows = self.rows([
            sudo_line(1793000003000000, "  claude : TTY=pts/1 ; PWD=/home/claude ; USER=root ; TSID=00/00/2A ; "
                                        "COMMAND=/usr/bin/curl -H 'Authorization: Bearer tok-0123456789' x ; y"),
            sudo_line(1793000002000000, "  claude : TTY=pts/1 ; PWD=/srv ; USER=www-data ; COMMAND=/usr/bin/id"),
            sudo_line(1793000001000000, "pam_unix(sudo:session): session opened for user root by claude(uid=0)"),
        ], limit=10, since=int(time.time()) - 86400)
        self.assertEqual(rows, [
            {"ts": 1793000003, "user": "claude", "runas": "root", "cwd": "/home/claude",
             "command": "/usr/bin/curl -H 'Authorization: Bearer [REDACTED]' x ; y", "event": "command",
             "ambiguous": False},
            {"ts": 1793000002, "user": "claude", "runas": "www-data", "cwd": "/srv", "command": "/usr/bin/id",
             "event": "command", "ambiguous": False},
        ])
        argv = self.env.calls_of("journalctl")[0]
        self.assertEqual(argv[-3:], ["_EXE=/usr/bin/sudo.ws", "_EXE=/usr/lib/cargo/bin/sudo", "_UID=999"])
        self.assertIn("--output-fields=MESSAGE,_EXE,_UID,_PID", argv)
        self.assertIn("--grep=^\\s*claude : ", argv)
        self.assertIn("--case-sensitive=true", argv)
        self.assertIn("--lines=10", argv)
        self.assertFalse([a for a in argv if a.startswith("_COMM=")])

    def test_events_are_classified_from_sudo_wording(self) -> None:
        lines = [  # (message, expected event), newest first
            ("claude : command not allowed ; TTY=pts/1 ; PWD=/home/claude ; USER=root ; COMMAND=/usr/bin/su",
             "refused"),
            ("claude : a password is required ; TTY=pts/1 ; PWD=/home/claude ; USER=root ; COMMAND=/usr/bin/id",
             "refused"),
            ("claude : user NOT in sudoers ; TTY=pts/1 ; PWD=/home/claude ; USER=root ; COMMAND=/usr/bin/id",
             "refused"),
            ("claude : 3 incorrect password attempts ; TTY=pts/1 ; PWD=/home/claude ; USER=root ; "
             "COMMAND=/usr/bin/id", "auth_fail"),
            ("claude : 1 incorrect password attempt ; TTY=pts/1 ; PWD=/home/claude ; USER=root ; "
             "COMMAND=/usr/bin/id", "auth_fail"),
            ("claude : authentication failure ; TTY=pts/1 ; PWD=/home/claude ; USER=root ; COMMAND=/usr/bin/id",
             "auth_fail"),
            ("claude : 2 incorrect password attempts ; command not allowed ; TTY=pts/1 ; PWD=/ ; USER=root ; "
             "COMMAND=/usr/bin/su", "auth_fail"),
            # The command's own text can't fake an event.
            ("claude : TTY=pts/1 ; PWD=/home/claude ; USER=root ; COMMAND=/usr/bin/echo command not allowed",
             "command"),
            ("claude : TTY=pts/1 ; PWD=/home/claude ; USER=root ; COMMAND=/usr/bin/logger "
             "3 incorrect password attempts ; authentication failure", "command"),
            ("claude : TTY=pts/1 ; PWD=/home/claude ; USER=root ; TSID=00/00/2B ; COMMAND=/usr/bin/apt update",
             "command"),
        ]
        rows = self.rows([sudo_line(1793000100000000 - i * 1000000, msg, pid=str(100 + i))
                          for i, (msg, _) in enumerate(lines)])
        self.assertEqual([c["event"] for c in rows], [want for _, want in lines])
        for c in rows:
            self.assertEqual(set(c), {"ts", "user", "runas", "cwd", "command", "event", "ambiguous"})
        self.assertEqual(rows[7]["command"], "/usr/bin/echo command not allowed")

    def test_forged_rows_are_excluded(self) -> None:
        # _COMM is self-set and _EXE can be faked from a user namespace: the query ANDs _UID=<claude's uid>,
        # Python re-checks _EXE and _UID, and the message must name claude itself.
        real = "claude : TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/usr/bin/real"
        forged = "claude : TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/usr/bin/forged"
        rows = self.rows([
            sudo_line(1793000006000000, forged, uid="998"),  # another uid (localai) forging claude
            sudo_line(1793000005000000, "localai : TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/usr/bin/forged"),
            sudo_line(1793000004000000, forged, exe="/usr/bin/python3"),  # wrong executable
            journal_line(1793000003000000, forged, _UID=CLAUDE_UID, _COMM="sudo"),  # no _EXE
            sudo_line(1793000002000000, forged, uid="0"),  # root, not claude
            sudo_line(1793000001000000, real, _CAP_EFFECTIVE="1ffffffffff"),
        ])
        self.assertEqual([c["command"] for c in rows], ["/usr/bin/real"])
        self.assertEqual(self.env.calls_of("journalctl")[0][-1], "_UID=999")

    def test_localai_through_sudo_rs(self) -> None:
        rows = self.rows([sudo_line(1793000001000000, "localai : user NOT in sudoers ; TTY=pts/2 ; PWD=/ ; "
                                    "USER=root ; COMMAND=/usr/bin/id", exe="/usr/lib/cargo/bin/sudo", uid="998")],
                         user="localai")
        self.assertEqual([(c["command"], c["event"]) for c in rows], [("/usr/bin/id", "refused")])
        self.assertEqual(self.env.calls_of("journalctl")[0][-1], "_UID=998")

    def test_ambiguous_rows(self) -> None:
        rows = self.rows([
            # a cwd holding a sudo field pattern (a directory named "x ; USER=root")
            sudo_line(1793000006000000, "claude : TTY=pts/1 ; PWD=/tmp/x ; USER=root ; USER=nobody ; "
                                        "COMMAND=/usr/bin/id", pid="1"),
            # a command holding one
            sudo_line(1793000005000000, "claude : TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/usr/bin/echo a ;USER=x",
                      pid="2"),
            # a long line split by sudo: the continuation, and the first part (same _PID)
            sudo_line(1793000004000000, "claude : (command continued) tail-of-a-long-command", pid="3"),
            sudo_line(1793000003000000, "claude : TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/usr/bin/printf head",
                      pid="3"),
            sudo_line(1793000002000000, "claude : TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/usr/bin/plain ; ok",
                      pid="4"),
        ])
        self.assertEqual([(c["command"], c["ambiguous"]) for c in rows], [
            ("/usr/bin/id", True), ("/usr/bin/echo a ;USER=x", True), ("tail-of-a-long-command", True),
            ("/usr/bin/printf head", True), ("/usr/bin/plain ; ok", False)])
        self.assertEqual((rows[2]["cwd"], rows[2]["runas"], rows[2]["event"]), ("", "", "command"))

    def test_the_parser_flags_split_and_injected_lines(self) -> None:
        parse = helper_mod.parse_sudo_message
        self.assertEqual(parse("  claude : (command continued) rest of it"),
                         {"user": "claude", "runas": "", "cwd": "", "command": "rest of it", "event": "command",
                          "continued": True, "ambiguous": True})
        self.assertTrue(parse("claude : TTY=x ; PWD=/a ; USER=root ; USER=x ; COMMAND=/bin/id")["ambiguous"])
        self.assertTrue(parse("claude : TTY=x ; PWD=/a ; USER=root ; COMMAND=/bin/echo ; TSID=1")["ambiguous"])
        self.assertFalse(parse("claude : TTY=x ; PWD=/a ; USER=root ; COMMAND=/bin/echo a;b")["ambiguous"])

    def test_default_since_is_a_day(self) -> None:
        self.env.ok({"op": "sudo-commands", "user": "localai"})
        since = [a for a in self.env.calls_of("journalctl")[0] if a.startswith("--since=@")][0]
        self.assertLess(abs(int(since[9:]) - (time.time() - 86400)), 5)

    def test_no_match_exit_1_is_empty(self) -> None:
        self.env.set_rule("journalctl", [{"rc": 1}])
        self.assertEqual(self.env.ok({"op": "sudo-commands", "user": "claude"})["commands"], [])

    def test_a_missing_audited_user(self) -> None:
        self.env.uids["claude"] = None  # pwd has no such user
        self.env.err({"op": "sudo-commands", "user": "claude"}, "not-found")
        self.assertEqual(self.env.calls_of("journalctl"), [])

    def test_unknown_users_are_refused(self) -> None:
        for user in ("root", "noah", "aictl", "claude ", "Claude", "../claude", "", "*"):
            with self.subTest(user=user):
                self.env.err({"op": "sudo-commands", "user": user}, None)
        self.assertEqual(self.env.calls, [])


class LocalaiActivity(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()

    def tearDown(self) -> None:
        self.env.close()

    def test_entries_by_the_localai_uid(self) -> None:
        self.env.set_rule("journalctl", [{"stdout": "\n".join([
            journal_line(1793000001000000, "pulled model; password=hunter2", _UID=str(os.getuid()),
                         _SYSTEMD_UNIT="user@1.service", _COMM="ollama"),
            journal_line(1793000000000000, "someone else", _UID="0", _COMM="x"),
        ]) + "\n"}])
        data = self.env.ok({"op": "localai-activity", "limit": 5})
        self.assertEqual(data["user"], ME)
        self.assertEqual(data["entries"], [{"ts": 1793000001, "unit": "user@1.service", "comm": "ollama",
                                            "message": "pulled model; password=[REDACTED]"}])
        argv = self.env.calls_of("journalctl")[0]
        self.assertEqual(argv[-1], f"_UID={os.getuid()}")
        self.assertIn("--lines=5", argv)

    def test_a_missing_localai_user(self) -> None:
        self.env.close()
        self.env = Env(localai_user="no-such-user-x2")
        self.env.err({"op": "localai-activity"}, "not-found")
        self.assertEqual(self.env.calls, [])


class Timers(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()

    def tearDown(self) -> None:
        self.env.close()

    def test_filtered_to_the_allowlist_with_last_result(self) -> None:
        data = self.env.ok({"op": "timers"})
        self.assertEqual(data["timers"], [
            {"unit": "ai-models-pull.timer", "next": 1793055600, "last": 1793052054,
             "last_result": "exit-code"},
            {"unit": "homelab-ai-backup.timer", "next": 1793056627, "last": None, "last_result": None},
        ])
        self.assertEqual(self.env.calls_of("systemctl"), [
            ["list-timers", "--all", "--output=json", "--no-pager"],
            ["show", "--property=Id,Result", "--", "ai-models-pull.service", "homelab-ai-backup.service"],
        ])

    def test_failure_and_garbage(self) -> None:
        self.env.set_rule("systemctl", [{"match": ["list-timers"], "rc": 1}])
        self.env.err({"op": "timers"}, "failed")
        self.env.set_rule("systemctl", [{"match": ["list-timers"], "stdout": "{"}])
        self.env.err({"op": "timers"}, "failed")


class Timeouts(unittest.TestCase):
    def test_a_hung_command_is_killed_at_the_timeout(self) -> None:
        env = Env(command_timeout=0.5)
        try:
            env.set_rule("journalctl", [{"sleep": 30, "pidfile": "hung.pid"}])
            started = time.monotonic()
            err = env.err({"op": "localai-activity"}, "timeout")
            self.assertLess(time.monotonic() - started, 4)
            self.assertIn("did not finish within 0.5 s", err["message"])
            pid = int((env.bin / "hung.pid").read_text())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail("the hung stub is still running")
            self.assertIn("result=timeout", env.log)
        finally:
            env.close()

    def test_no_timeout_can_exceed_ten_seconds(self) -> None:
        from harness import helper_mod
        self.assertEqual(helper_mod.MAX_TIMEOUT, 10.0)
        for key in ("command_timeout", "read_timeout", "request_timeout", "send_timeout"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                helper_mod.Config.from_mapping({key: 10.5})
        # and System.run clamps whatever it is handed
        started = time.monotonic()
        with self.assertRaises(helper_mod.OpError) as ctx:
            helper_mod.System().run(["/bin/sleep", "30"], 0.3)
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertLess(time.monotonic() - started, 3)

    def test_output_is_capped(self) -> None:
        env = Env()
        try:
            env.set_rule("systemctl", [{"match": ["list-timers"], "stdout": "x" * 3_000_000}])
            env.err({"op": "timers"}, "failed")  # capped at 2 MiB, then refused as invalid JSON
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()
