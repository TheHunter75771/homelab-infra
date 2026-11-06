import json
import os
import time
import unittest
from unittest import mock

from ai_host_agent import sources

from .fakes import PS, TAGS, TOKEN, UIDS, sudo_entry, wait_for
from .test_server import ServerTest

LOG_ARGV = ["-u", "ollama.service", "-n", "%d", "-o", "short-iso", "--no-pager"]
SUDO_ARGV = ["_EXE=/usr/bin/sudo", "_UID=962",
             "-o", "json", "--output-fields=MESSAGE,_EXE,_UID", "-r", "-n", "5000", "--no-pager"]


class StubTest(ServerTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(sources, "user_uid", UIDS.get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def argvs(self):
        """[(program basename, argv[1:])] in call order."""
        return [(os.path.basename(c["argv"][0]), c["argv"][1:]) for c in self.env.calls()]


class ServicesTest(StubTest):
    def test_services(self):
        code, body = self.run_.json("/v1/services")
        self.assertEqual(code, 200)
        self.assertEqual(body, {"services": [
            {"id": "ollama", "unit": "ollama.service", "load": "loaded", "active": "active",
             "sub": "running", "since": 1790000000, "restarts": 2,
             "main_pid": 4242, "memory_bytes": 123456},  # memory.current, not MemoryCurrent
            {"id": "gpu-gate", "unit": "gpu-gate.service", "load": "loaded", "active": "inactive",
             "sub": "dead", "since": None, "restarts": 0, "main_pid": None, "memory_bytes": None},
        ]})
        props = ["show", "--timestamp=unix", "--property=" + sources.SHOW_PROPS, "--"]
        self.assertEqual(self.argvs(), [("systemctl", props + ["ollama.service"]),
                                        ("systemctl", props + ["gpu-gate.service"])])

    def test_child_gets_a_fixed_environment(self):
        with mock.patch.dict(os.environ, {"CANARY_SECRET": TOKEN, "LD_PRELOAD": "/x.so"}):
            self.run_.json("/v1/services")
        env = self.env.calls()[0]["env"]
        self.assertNotIn("CANARY_SECRET", env)
        self.assertNotIn("LD_PRELOAD", env)
        self.assertEqual(env["PATH"], self.env.bin)
        self.assertNotIn(TOKEN, json.dumps(env))

    def test_memory_fallback_and_cgroup_path_guard(self):
        self.env.fixture("systemctl.ollama.service.out", self.env_show(
            "ControlGroup=/system.slice/../../../etc\nMemoryCurrent=777\n"))
        code, body = self.run_.json("/v1/services")
        self.assertEqual(body["services"][0]["memory_bytes"], 777)
        self.env.fixture("systemctl.ollama.service.out", self.env_show(
            "ControlGroup=/system.slice/other.service\nMemoryCurrent=18446744073709551615\n"))
        self.assertIsNone(self.run_.json("/v1/services")[1]["services"][0]["memory_bytes"])

    @staticmethod
    def env_show(extra):
        return ("LoadState=loaded\nActiveState=active\nSubState=running\n"
                "ActiveEnterTimestamp=Mon 2026-10-26 10:00:00 EDT\nNRestarts=x\nMainPID=7\n"
                + extra)

    def test_one_unit_failing_marks_only_its_entry(self):
        self.env.fixture("systemctl.gpu-gate.service.rc", "1")
        code, body = self.run_.json("/v1/services")
        self.assertEqual(code, 200)
        self.assertEqual(body["services"][0]["active"], "active")
        self.assertEqual(body["services"][1], {"id": "gpu-gate", "unit": "gpu-gate.service",
                                               "error": "systemctl failed (exit 1)"})

    def test_timeout(self):
        self.run_.close()
        from .fakes import Running
        self.run_ = Running(self.env.cfg(exec_timeout=0.5))
        self.env.fixture("systemctl.ollama.service.sleep", "5")
        t0 = time.monotonic()
        code, body = self.run_.json("/v1/services")
        self.assertLess(time.monotonic() - t0, 3)
        self.assertEqual(body["services"][0]["error"], "systemctl timed out")
        self.assertEqual(body["services"][1]["active"], "inactive")

    def test_missing_binary(self):
        os.remove(os.path.join(self.env.bin, "systemctl"))
        code, body = self.run_.json("/v1/services")
        self.assertEqual(body["services"][0]["error"], "systemctl not found")


class LogsTest(StubTest):
    def test_argv_and_default_lines(self):
        self.env.fixture("journalctl.log.out", "2026-10-26T10:00:00-0400 pc ollama[1]: hi\n")
        code, body = self.run_.json("/v1/logs?unit=ollama")
        self.assertEqual((code, body), (200, {
            "unit": "ollama", "truncated": False,
            "lines": ["2026-10-26T10:00:00-0400 pc ollama[1]: hi"]}))
        argv = list(LOG_ARGV)
        argv[3] = "200"
        self.assertEqual(self.argvs(), [("journalctl", argv)])
        self.run_.json("/v1/logs?unit=gpu-gate&lines=1000")
        self.assertEqual(self.argvs()[1][1][:4], ["-u", "gpu-gate.service", "-n", "1000"])

    def test_redaction_before_truncation(self):
        lines = [
            "a Authorization: Bearer abcdefghijklmnop",
            "b api_key=sk-SYNTHETIC0000 and password: s3cr3t-synthetic",
            "c the agent token " + TOKEN + " in a log",
            "d \x1b[31mred\x1b[0m\x07 ctrl",
            "e " + "x" * 1990 + TOKEN,                 # token straddles the 2000 cap
            "f " + "y" * 1985 + " token=" + "Z" * 40,  # so does a token= value
        ]
        self.env.fixture("journalctl.log.out", "\n".join(lines) + "\n")
        code, body = self.run_.json("/v1/logs?unit=ollama&lines=10")
        out = body["lines"]
        self.assertEqual(out[0], "a Authorization: Bearer [REDACTED]")
        self.assertEqual(out[1], "b api_key=[REDACTED] and password: [REDACTED]")
        self.assertEqual(out[2], "c the agent token [REDACTED] in a log")
        self.assertEqual(out[3], "d red ctrl")
        self.assertEqual(len(out[4]), 2000)
        self.assertEqual(out[4], ("e " + "x" * 1990 + "[REDACTED]")[:2000])
        self.assertTrue(out[5].endswith("token=[R") or "Z" not in out[5], out[5][-20:])
        raw = json.dumps(body)
        self.assertNotIn(TOKEN[:4], raw)
        self.assertNotIn("SYNTHETIC0000", raw)
        self.assertNotIn("s3cr3t-synthetic", raw)

    def test_key_block_across_journal_lines_and_the_8k_cut(self):
        lines = [
            "2026-10-26T10:00:00-0400 pc ollama[1]: -----BEGIN OPENSSH PRIVATE KEY-----",
            "2026-10-26T10:00:00-0400 pc ollama[1]: b3BlbnNzaC1rZXktdjEAAAAAsynthetic",
            "2026-10-26T10:00:00-0400 pc ollama[1]: -----END OPENSSH PRIVATE KEY----- after",
            "next " + "w " * 4094 + "sk-SYNTHETICSYNTHETIC0123456789",  # cut at 8192 mid-key
        ]
        self.env.fixture("journalctl.log.out", "\n".join(lines) + "\n")
        out = self.run_.json("/v1/logs?unit=ollama")[1]["lines"]
        self.assertEqual(out[0], "2026-10-26T10:00:00-0400 pc ollama[1]: [REDACTED]")
        self.assertEqual(out[1], "[REDACTED]")
        self.assertEqual(out[2], "[REDACTED] after")
        self.assertNotIn("sk-", out[3])  # the cut's partial word is dropped, not shown
        self.assertNotIn("b3Blbn", json.dumps(out))

    def test_output_cap_drops_the_partial_line(self):
        self.run_.close()
        from .fakes import Running
        self.run_ = Running(self.env.cfg(exec_max_bytes=65536))
        line = "L" * 99 + "\n"
        self.env.fixture("journalctl.log.out", line * 1000)  # 100 KB
        code, body = self.run_.json("/v1/logs?unit=ollama&lines=1000")
        self.assertTrue(body["truncated"])
        self.assertEqual(len(body["lines"]), 65536 // 100)
        self.assertTrue(all(ln == "L" * 99 for ln in body["lines"]))

    def test_failure_is_502(self):
        self.env.fixture("journalctl.log.rc", "1")
        self.assertEqual(self.run_.json("/v1/logs?unit=ollama"),
                         (502, {"error": "journalctl failed (exit 1)"}))


class SudoTest(StubTest):
    def setUp(self):
        super().setUp()
        self.env.fixture("journalctl.sudo.out", "".join([
            sudo_entry(1790000300000000, "  claude : TTY=pts/1 ; PWD=/home/claude ; USER=root ;"
                                         " COMMAND=/usr/bin/systemctl restart ollama"),
            sudo_entry(1790000200000000, "pam_unix(sudo:session): session opened for user "
                                         "root(uid=0) by claude(uid=1001)"),
            sudo_entry(1790000100000000, "localai : user NOT in sudoers ; TTY=pts/2 ; "
                                         "PWD=/tmp ; USER=root ; COMMAND=/usr/bin/id", uid=963),
            sudo_entry(1790000050000000, "  claude : TTY=pts/1 ; PWD=/srv ; USER=postgres ; "
                                         "COMMAND=/usr/bin/psql --password s3cr3t-synthetic "
                                         "-c \"a ; b\""),
            sudo_entry(1790000040000000, "  other : TTY=pts/3 ; PWD=/ ; USER=localai ; "
                                         "COMMAND=/usr/bin/whoami", uid=1500),
            sudo_entry(1790000030000000, "  claude : TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND="
                                         "/usr/bin/curl -H 'Authorization: Bearer " + TOKEN + "'"),
            '{"MESSAGE": [99, 108, 97, 117, 100, 101, 32, 58, 32, 67, 79, 77, 77, 65, 78, 68,'
            ' 61, 47, 98, 105, 110, 47, 116, 114, 117, 101], "__REALTIME_TIMESTAMP": "1",'
            ' "_EXE": "/usr/bin/sudo", "_UID": "962"}\n',
            "not json\n",
        ]))

    def test_claude(self):
        code, body = self.run_.json("/v1/sudo?user=claude")
        self.assertEqual(code, 200)
        self.assertEqual(body["user"], "claude")
        self.assertFalse(body["truncated"])
        e = body["entries"]
        self.assertEqual([x["ts"] for x in e], [1790000300, 1790000050, 1790000030, 0])
        self.assertEqual(e[0], {"ts": 1790000300, "user": "claude", "runas": "root",
                                "cwd": "/home/claude",
                                "command": "/usr/bin/systemctl restart ollama",
                                "event": "command", "ambiguous": False})
        self.assertEqual(e[1]["command"], '/usr/bin/psql --password [REDACTED] -c "a ; b"')
        self.assertEqual(e[1]["runas"], "postgres")
        self.assertNotIn(TOKEN[:6], json.dumps(body))
        self.assertEqual(e[3]["command"], "/bin/true")  # MESSAGE as a byte list
        self.assertEqual(self.argvs(), [("journalctl", SUDO_ARGV)])

    def test_localai_only_its_own_lines(self):
        """The row run *as* localai by another user is not localai's (the line must name the
        audited user, and its _UID is the other user's)."""
        e = self.run_.json("/v1/sudo?user=localai")[1]["entries"]
        self.assertEqual([(x["user"], x["runas"], x["event"]) for x in e],
                         [("localai", "root", "refused")])
        self.assertEqual(self.argvs()[0][1][:2], ["_EXE=/usr/bin/sudo", "_UID=963"])

    def test_sudo_pads_the_user_to_eight_characters(self):
        """Real lines are `%8s : ...`: `  claude : ` and ` localai : `, with leading spaces. Both
        (and the unpadded form) must count as naming the audited user."""
        self.env.fixture("journalctl.sudo.out", "".join([
            sudo_entry(3000000, "  claude : TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/usr/bin/a"),
            sudo_entry(2000000, "claude : TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/usr/bin/b"),
            sudo_entry(1000000, "  claude : (command continued) c"),
            sudo_entry(1000000, " localai : user NOT in sudoers ; TTY=pts/2 ; PWD=/ ; "
                                "USER=root ; COMMAND=/usr/bin/id", uid=963),
        ]))
        e = self.run_.json("/v1/sudo?user=claude")[1]["entries"]
        self.assertEqual([x["command"] for x in e], ["/usr/bin/a", "/usr/bin/b", "c"])
        e = self.run_.json("/v1/sudo?user=localai")[1]["entries"]
        self.assertEqual([(x["user"], x["event"]) for x in e], [("localai", "refused")])

    def test_forged_rows_are_excluded(self):
        """journalctl is asked for _EXE AND _UID, and Python checks both again: rows with
        another uid, another exe, or naming another user never show (the stub returns them
        all, as a journal ignoring the match would)."""
        good = "  claude : TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/usr/bin/true"
        self.env.fixture("journalctl.sudo.out", "".join([
            sudo_entry(5000000, good),
            sudo_entry(4000000, good.replace("true", "forged-uid"), uid=963),
            sudo_entry(3000000, good.replace("true", "forged-exe"), exe="/tmp/ns/usr/bin/sudo"),
            sudo_entry(2000000, "  localai : TTY=pts/1 ; PWD=/ ; USER=root ; "
                                "COMMAND=/usr/bin/forged-name"),  # claude's uid, other name
            sudo_entry(1000000, good.replace("true", "no-uid")).replace(', "_UID": "962"', ""),
        ]))
        e = self.run_.json("/v1/sudo?user=claude")[1]["entries"]
        self.assertEqual([x["command"] for x in e], ["/usr/bin/true"])

    def test_unknown_user_on_this_host(self):
        with mock.patch.object(sources, "user_uid", lambda name: None):
            self.assertEqual(self.run_.json("/v1/sudo?user=claude"),
                             (502, {"error": "user not found on this host"}))
        self.assertEqual(self.env.calls(), [])

    def test_ambiguous_rows(self):
        """A `; KEY=` sudo-field pattern in cwd or command, a field named twice, or a split
        `(command continued)` line: flagged, never dropped."""
        rows = [  # newest first, as journalctl -r prints them
            ("claude : TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/usr/bin/true", False),
            ("claude : TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/bin/echo x ; USER=root", True),
            ("claude : TTY=pts/1 ; PWD=/tmp/a;ENV=b ; USER=root ; COMMAND=/bin/ls", True),
            ("claude : TTY=pts/1 ; PWD=/tmp/a ; USER=nobody ; USER=root ; COMMAND=/bin/ls", True),
            ("claude : TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/bin/echo a;TSID=000001", True),
            ("claude : (command continued) tail-of-the-command", True),
            ("claude : TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/bin/echo head-of-it", True),
            ("claude : TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/bin/echo a ; b", False),
            ("claude : TTY=pts/1 ; PWD=/home/a b ; USER=root ; COMMAND=/bin/ls", False),
        ]
        self.env.fixture("journalctl.sudo.out", "".join(
            sudo_entry(9000000 - i, m) for i, (m, _) in enumerate(rows)))
        e = self.run_.json("/v1/sudo?user=claude")[1]["entries"]
        self.assertEqual([x["ambiguous"] for x in e], [a for _, a in rows])
        cont = e[5]
        self.assertEqual((cont["command"], cont["runas"], cont["cwd"], cont["event"]),
                         ("tail-of-the-command", None, None, "command"))

    def test_limit_and_since(self):
        body = self.run_.json("/v1/sudo?user=claude&limit=2&since=1790000000")[1]
        self.assertEqual(len(body["entries"]), 2)
        self.assertEqual(self.argvs()[-1][1], SUDO_ARGV + ["--since", "@1790000000"])
        self.run_.json("/v1/sudo?user=claude&since=0")
        self.assertEqual(self.argvs()[-1][1][-2:], ["--since", "@0"])
        self.run_.json("/v1/sudo?user=claude&since=")
        self.assertEqual(self.argvs()[-1][1], SUDO_ARGV)

    def test_since_at_most_one_day_ahead(self):
        now = int(time.time())
        n = len(self.argvs())
        code, _ = self.run_.json("/v1/sudo?user=claude&since=%d" % (now + 86400 - 30))
        self.assertEqual(code, 200)
        code, body = self.run_.json("/v1/sudo?user=claude&since=%d" % (now + 86400 + 30))
        self.assertEqual((code, body), (400, {"error": "since is more than a day in the future"}))
        self.assertEqual(len(self.argvs()), n + 1)  # the refused one ran nothing
        q = {"since": "1086400"}
        self.assertEqual(sources.since_param(q, now=lambda: 1000000), 1086400)
        with self.assertRaises(sources.BadRequest):
            sources.since_param({"since": "1086401"}, now=lambda: 1000000)


class SudoEventTest(StubTest):
    MESSAGES = [  # (sudo message, event) - sudo 1.9 wording, plus a sudo-rs style line
        ("claude : TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/usr/bin/true", "command"),
        ("claude : command not allowed ; TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/bin/sh",
         "refused"),
        ("claude : a password is required ; PWD=/ ; USER=root ; COMMAND=/usr/bin/ls", "refused"),
        ("claude : user NOT in sudoers ; TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/usr/bin/id",
         "refused"),
        ("claude : 1 incorrect password attempt ; TTY=pts/1 ; PWD=/ ; USER=root ; "
         "COMMAND=/usr/bin/ls", "auth_fail"),
        ("claude : 3 incorrect password attempts ; TTY=pts/1 ; PWD=/ ; USER=root ; "
         "COMMAND=/usr/bin/ls", "auth_fail"),
        ("claude : authentication failure ; TTY=pts/1 ; PWD=/ ; USER=root ; "
         "COMMAND=/usr/bin/ls", "auth_fail"),
        # both: auth_fail wins over refused (Lead, as X)
        ("claude : 3 incorrect password attempts ; command not allowed ; TTY=pts/1 ; PWD=/ ; "
         "USER=root ; COMMAND=/bin/sh", "auth_fail"),
        ("claude : user NOT in sudoers ; 1 incorrect password attempt ; TTY=pts/2 ; PWD=/ ; "
         "USER=root ; COMMAND=/usr/bin/id", "auth_fail"),
        # field values and the command never classify: only the free-text notes do
        ("claude : TTY=pts/1 ; PWD=/srv/command not allowed ; USER=root ; "
         "COMMAND=/bin/echo a password is required 3 incorrect password attempts", "command"),
    ]

    def test_events(self):
        self.env.fixture("journalctl.sudo.out", "".join(
            sudo_entry(1790000000000000 - i, m) for i, (m, _) in enumerate(self.MESSAGES)))
        code, body = self.run_.json("/v1/sudo?user=claude")
        self.assertEqual(code, 200)
        self.assertEqual([x["event"] for x in body["entries"]], [e for _, e in self.MESSAGES])
        for x in body["entries"]:
            self.assertEqual(set(x), {"ts", "user", "runas", "cwd", "command", "event",
                                      "ambiguous"})
        self.assertEqual([x["ts"] for x in body["entries"][:2]], [1790000000, 1789999999])
        self.assertEqual(self.argvs()[0][1][0], "_EXE=/usr/bin/sudo")

    def test_journal_match_is_sudo_exe_and_the_audited_uid(self):
        """_EXE=<sudo> AND _UID=<audited user>: _EXE alone can be faked from a user namespace,
        _UID is journald's own record of the sender. No _COMM (set by the process) and no
        _UID=0 (real lines carry the caller's uid, e.g. 962)."""
        self.run_.json("/v1/sudo?user=localai")
        argv = self.argvs()[0][1]
        self.assertEqual([a for a in argv if a.startswith("_")],
                         ["_EXE=/usr/bin/sudo", "_UID=963"])
        self.assertIn("--output-fields=MESSAGE,_EXE,_UID", argv)

    def test_configured_sudo_exes_are_ored(self):
        self.run_.close()
        from .fakes import Running
        exes = ["/usr/bin/sudo", "/usr/lib/cargo/bin/sudo"]
        self.run_ = Running(self.env.cfg(sudo_exes=exes))
        self.assertEqual(self.run_.json("/v1/sudo?user=claude")[0], 200)
        argv = self.argvs()[0][1]
        self.assertEqual([a for a in argv if a.startswith("_")],
                         ["_EXE=/usr/bin/sudo", "_EXE=/usr/lib/cargo/bin/sudo", "_UID=962"])

    def test_classifier(self):
        for message, event in self.MESSAGES:
            with self.subTest(message=message):
                self.assertEqual(sources.classify_sudo(sources.parse_sudo(message)[2]), event)


class EnumRefusalTest(StubTest):
    def test_refusals_run_nothing(self):
        for path in ("/v1/logs", "/v1/logs?unit=sshd", "/v1/logs?unit=ollama.service",
                     "/v1/logs?unit=OLLAMA", "/v1/logs?unit=ollama%00", "/v1/logs?unit=-k",
                     "/v1/logs?unit=ollama&lines=0", "/v1/logs?unit=ollama&lines=1001",
                     "/v1/logs?unit=ollama&lines=-5", "/v1/logs?unit=ollama&lines=5x",
                     "/v1/logs?unit=ollama&lines=%2B5", "/v1/logs?unit=ollama&unit=gpu-gate",
                     "/v1/logs?unit=ollama&grep=x", "/v1/logs?unit=ollama&lines=99999999999",
                     "/v1/logs?unit=%ff", "/v1/logs?a=1&b=2&c=3&d=4&e=5&f=6&g=7&h=8&i=9",
                     "/v1/sudo", "/v1/sudo?user=root", "/v1/sudo?user=claude%20",
                     "/v1/sudo?user=claude&limit=501", "/v1/sudo?user=claude&limit=0",
                     "/v1/sudo?user=claude&since=yesterday",
                     "/v1/sudo?user=claude&since=--until=now",
                     "/v1/sudo?user=claude&since=2026-09-21T13:46:40Z",  # ISO: not accepted
                     "/v1/sudo?user=claude&since=1790000000.5",
                     "/v1/sudo?user=claude&since=-1", "/v1/sudo?user=claude&since=99999999999",
                     "/v1/sudo?user=claude&since=@1", "/v1/services?x=1", "/v1/ollama?model=a",
                     "/v1/disk?path=/etc", "/healthz?x"):
            with self.subTest(path=path):
                code, body = self.run_.json(path)
                self.assertEqual(code, 400)
                self.assertNotIn("sshd", body["error"])  # no echo of the input
        self.assertEqual(self.env.calls(), [])


class OllamaTest(StubTest):
    def test_ollama_on_a_bind_address(self):
        """The live gaming PC: Ollama listens on the tailnet IP only (OLLAMA_HOST, §12.10), which
        is one of the agent's binds. 127.0.0.2 (Linux loopback) stands in for it here."""
        from .fakes import FakeOllama, Running
        tail = FakeOllama(host="127.0.0.2")
        self.addCleanup(tail.close)
        self.run_.close()
        self.run_ = Running(self.env.cfg(binds=["127.0.0.1", "127.0.0.2"],
                                         ollama_url="http://127.0.0.2:%d" % tail.port))
        code, body = self.run_.json("/v1/ollama")
        self.assertEqual(code, 200)
        self.assertEqual([m["name"] for m in body["models"]], ["example-model:9b", "tiny-model:1b"])
        self.assertEqual([m["name"] for m in body["loaded"]], ["example-model:9b"])
        self.assertEqual(sorted(tail.paths), ["/api/ps", "/api/tags"])
        self.assertEqual(self.ollama.paths, [])  # nothing went to the loopback one

    def test_reduced_to_contract_fields(self):
        code, body = self.run_.json("/v1/ollama")
        self.assertEqual(code, 200)
        self.assertEqual(body, {
            "models": [{"name": "example-model:9b", "size": 6000000000, "quant": "Q4_K_M",
                        "modified": 1792490400},
                       {"name": "tiny-model:1b", "size": 800000000, "quant": "Q8_0",
                        "modified": 1792576800}],
            "loaded": [{"name": "example-model:9b", "size_vram": 6500000000,
                        "expires_at": 1793016300}]})
        self.assertEqual(sorted(self.ollama.paths), ["/api/ps", "/api/tags"])

    def test_each_part_fails_on_its_own(self):
        self.ollama.routes["/api/ps"] = (500, b"{}")
        self.ollama.routes["/api/tags"] = (200, b"not json")
        self.assertEqual(self.run_.json("/v1/ollama")[1],
                         {"models": {"error": "bad response"}, "loaded": {"error": "http 500"}})
        self.ollama.routes["/api/tags"] = (200, b'{"models": "x"}')
        self.assertEqual(self.run_.json("/v1/ollama")[1]["models"], {"error": "bad response"})

    def test_size_cap(self):
        big = {"models": [dict(TAGS["models"][0], pad="p" * 2000000)]}
        self.ollama.routes["/api/tags"] = (200, json.dumps(big).encode())
        body = self.run_.json("/v1/ollama")[1]
        self.assertEqual(body["models"], {"error": "too large"})
        self.assertEqual(len(body["loaded"]), len(PS["models"]))

    def test_timeout_and_unreachable(self):
        self.run_.close()
        from .fakes import Running
        self.run_ = Running(self.env.cfg(ollama_url="http://127.0.0.1:%d" % self.ollama.port,
                                         ollama_timeout=0.5))
        self.ollama.delay = 2
        t0 = time.monotonic()
        body = self.run_.json("/v1/ollama")[1]
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertEqual(body, {"models": {"error": "timeout"}, "loaded": {"error": "timeout"}})
        self.ollama.close()
        self.ollama = type(self.ollama)()  # tearDown closes it
        body = self.run_.json("/v1/ollama")[1]
        self.assertEqual(body["models"], {"error": "unreachable"})

    def test_hostile_values_are_cleaned(self):
        tags = {"models": [{"name": "evil\x1b[2J\nname" + "n" * 500, "size": "big",
                            "details": "x", "modified_at": 5}, "junk"]}
        self.ollama.routes["/api/tags"] = (200, json.dumps(tags).encode())
        m = self.run_.json("/v1/ollama")[1]["models"]
        self.assertEqual(len(m), 1)
        self.assertEqual(len(m[0]["name"]), 256)
        self.assertNotIn("\x1b", m[0]["name"])
        self.assertEqual((m[0]["size"], m[0]["quant"], m[0]["modified"]), (None, None, None))


class DiskTest(StubTest):
    def test_root_always_share_only_when_mounted(self):
        body = self.run_.json("/v1/disk")[1]
        self.assertEqual([d["mount"] for d in body["disk"]], ["/"])
        st = os.statvfs("/")
        self.assertEqual(body["disk"][0]["total"], st.f_blocks * st.f_frsize)
        self.assertIsInstance(body["disk"][0]["avail"], int)
        self.env.set_mounts(["/", self.env.share])
        body = self.run_.json("/v1/disk")[1]
        self.assertEqual([d["mount"] for d in body["disk"]], ["/", self.env.share])
        self.assertEqual(set(body["disk"][1]), {"mount", "total", "avail"})

    def test_mountinfo_escapes(self):
        self.env.set_mounts(["/", "/mnt/my share"])
        self.assertIn("/mnt/my share", sources.mount_points(self.env.mountinfo))

    def test_hung_statfs_times_out_and_is_not_retried_while_stuck(self):
        calls = []

        def hang(path):
            calls.append(path)
            if len(calls) == 1:
                time.sleep(1.5)  # only the first call hangs (then the kernel "returns")
            return os.statvfs("/")

        statfs = sources.StatFS(0.2, statvfs=hang)
        cfg = self.env.cfg(disk_paths=["/"])
        t0 = time.monotonic()
        self.assertEqual(sources.disk(cfg, statfs),
                         {"disk": [{"mount": "/", "error": "statfs timed out"}]})
        self.assertEqual(sources.disk(cfg, statfs)["disk"][0]["error"], "statfs timed out")
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(calls, ["/"])  # the second request didn't start another thread
        time.sleep(1.6)
        self.assertIn("total", sources.disk(cfg, statfs)["disk"][0])
        self.assertEqual(calls, ["/", "/"])


class FinalScrubTest(StubTest):
    """The last pass over every 200 body, on its own: a source that returns the token in
    fields no per-field redaction touches (a dict key, a list item, a nested value)."""

    def test_scrub_catches_what_sources_missed(self):
        leaky = {"services": [{"id": "ollama", TOKEN: ["a " + TOKEN + " b", {"x": TOKEN}],
                               "n": 5, "none": None}]}
        with mock.patch.object(sources, "services", return_value=leaky):
            code, _, body = self.run_.get("/v1/services")
        self.assertEqual(code, 200)
        self.assertNotIn(TOKEN[:8].encode(), body)
        self.assertEqual(json.loads(body), {"services": [{
            "id": "ollama", "[REDACTED]": ["a [REDACTED] b", {"x": "[REDACTED]"}],
            "n": 5, "none": None}]})


class ProcessGroupKillTest(StubTest):
    cfg_over = {"exec_timeout": 0.5}

    def test_timeout_kills_the_whole_process_group(self):
        """journalctl's own children die with it: the runner kills the group, not the leader."""
        self.env.fixture("journalctl.log.spawn", "1")
        self.env.fixture("journalctl.log.sleep", "30")
        self.assertEqual(self.run_.json("/v1/logs?unit=ollama"),
                         (502, {"error": "journalctl timed out"}))
        pid_file = os.path.join(self.env.bin, "journalctl.log.pid")
        self.assertTrue(os.path.exists(pid_file))
        with open(pid_file) as f:
            pid = int(f.read())

        def gone():
            try:
                with open("/proc/%d/stat" % pid) as f:
                    return f.read().rsplit(")", 1)[1].split()[0] in ("Z", "X")  # zombie: dead
            except OSError:
                return True

        alive = not wait_for(gone, 3)
        if alive:
            os.kill(pid, 9)
        self.assertFalse(alive, "grandchild %d survived the timeout" % pid)


class TokenNeverReturnedTest(StubTest):
    def test_no_endpoint_returns_the_token(self):
        noisy = "token %s Authorization: Bearer %s\n" % (TOKEN, TOKEN)
        self.env.fixture("journalctl.log.out", noisy * 3)
        self.env.fixture("journalctl.sudo.out",
                         sudo_entry(1, "  claude : USER=root ; COMMAND=/x " + TOKEN))
        self.env.fixture("systemctl.ollama.service.out", "SubState=" + TOKEN + "\n")
        self.ollama.routes["/api/tags"] = (200, json.dumps({"models": [{"name": TOKEN}]}).encode())
        for path in ("/healthz", "/v1/services", "/v1/logs?unit=ollama", "/v1/ollama",
                     "/v1/sudo?user=claude", "/v1/disk", "/nope", "/v1/logs?unit=x"):
            with self.subTest(path=path):
                code, headers, body = self.run_.get(path)
                self.assertNotIn(TOKEN[:8].encode(), body + str(headers).encode())


class TimestampTest(unittest.TestCase):
    def test_ollama_times(self):
        for raw, want in (("2026-10-20T10:00:00Z", 1792490400),
                          ("2026-10-20T06:00:00.123456789-04:00", 1792490400),  # Ollama's form
                          ("0001-01-01T00:00:00Z", None), ("2026-13-01T00:00:00Z", None),
                          ("2026-10-20 10:00:00", None), ("yesterday", None), (5, None)):
            self.assertEqual(sources.epoch_of_rfc3339(raw), want, raw)


if __name__ == "__main__":
    unittest.main()
