"""Reviewer A round 1: linear-time redaction and the shared reference cases, the replay cut, damaged
session metadata, the overall request deadline, process-group kills, bounded decompression, uid 0."""

from __future__ import annotations

import gzip
import json
import os
import pathlib
import socket
import threading
import time
import tracemalloc
import unittest
from unittest import mock

from harness import HERE, Env, helper_mod, make_session

CASES = json.loads((HERE / "redact_cases.json").read_text())

# Inputs that make a backtracking rule quadratic (or worse) if it isn't bounded. 64 KiB each.
N = 65536
PATHOLOGICAL = {
    "alnum": "aB3" * (N // 3),
    "word-chars": "a_b.c-" * (N // 6),
    "keyword-repeat": "token" * (N // 5),
    "keyword-then-spaces": "password" + " " * N,
    "authorization-spaces": "authorization:" + " " * N,
    "bearer-spaces": "bearer" + " " * N,
    "jwt-prefixes": "eyJ-" * (N // 4),
    "jwt-no-dots": "eyJ" + "a" * N,
    "sk-repeat": "sk-" * (N // 3),
    "tskey-repeat": "tskey-" * (N // 6),
    "ghp-repeat": "ghp_" * (N // 4),
    "github-pat-repeat": "github_pat_" * (N // 11),
    "url-schemes": "a://" * (N // 4),
    "url-user-no-colon": "://" + "u" * N,
    "url-creds-no-at": "://u:" + "p" * N,
    "curl-repeat": "curl " * (N // 5),
    "curl-u-no-colon": "curl -u " + "a" * N,
    "mysql-repeat": "mysql " * (N // 6),
    "ansible-repeat": "ansible_" * (N // 8),
    "vault-hex": "$ANSIBLE_VAULT;1.1;AES256" + " ab" * (N // 3) + " zz",
    "pem-begins": "-----BEGIN RSA PRIVATE KEY-----" * (N // 31),
    "quotes": 'password="' + "x" * N,
    "flags": "--password" + " " * N,
    "colons": "=:" * (N // 2),
    "spaces": " " * N,
    "dashes": "-" * N,
    "yaml-long-name": "a" * N + ": |",
    "yaml-long-indent": " " * N + "token: |",
    "yaml-block-indicators": "password: " + "|" * N,
    "yaml-dashes": "- " * (N // 2) + "token: |",
    "yaml-indicator-comments": "token: | #" * (N // 10),  # a lookahead scanning to the line end is quadratic here
    "yaml-indicators-no-space": "token: |" * (N // 8),
    "sshpass-repeat": "sshpass " * (N // 8),
    "docker-login-repeat": "docker login " * (N // 13),
    "redis-repeat": "redis-cli " * (N // 10),
    "cookie-repeat": "cookie:" * (N // 7),
    "pass-flag-repeat": "--pass " * (N // 7),
    "escaped-quotes": 'password: "' + '\\"' * (N // 2),
    "escaped-backslashes": "secret='" + "\\" * N,
    "marker-repeat": "[REDACTED]" * (N // 10),
    "marker-dots": "[REDACTED]." * (N // 11),
    "pwd-repeat": "_pwd=" * (N // 5),
    "pgp-begins": "-----BEGIN PGP PRIVATE KEY BLOCK-----" * (N // 37),
}
# The same, as many lines: a secret block followed by a huge indented body.
PATHOLOGICAL_LINES = {
    "yaml-block-body": "password: |\n" + "  x\n" * (N // 4),
    "yaml-alternating": "token: |\n  s\n" * (N // 12),
}


# One dense unit per rule: 1000 lines of 8 KiB each, made of it, is that rule's worst maximum-size reply.
DENSE_UNITS = ["Authorization: ", "bearer ", "token: | ", "password=", "--password ", "curl -u ", "mysql -u x ",
               "sshpass ", "docker login ", "redis-cli -h ", "cookie: ", "://a:", "$ANSIBLE_VAULT;1.1;AES256 ",
               "tskey-", "sk-", "gh", "github_pat_", "glpat-", "hf_", "xox", "AKIA", "eyJ-", "[REDACTED]",
               "-----BEGIN RSA PRIVATE KEY-----", "key=", 'password: "\\', "token:|#", " ",
               # and one line that triggers every rule at once
               "Authorization: bearer token: | password= --password curl -u mysql -p sshpass docker login redis-cli "
               "cookie: ://a: $ANSIBLE_VAULT;1.1;AES256 tskey- sk- gh_ github_pat_ glpat- hf_ xoxb- AKIA eyJ [REDACTED] "]


class RedactionReference(unittest.TestCase):
    def test_every_reference_case(self) -> None:
        self.assertGreaterEqual(len(CASES), 50)
        for case in CASES:
            with self.subTest(case=case["in"][:60]):
                self.assertEqual(set(case), {"in", "out"})
                self.assertEqual(helper_mod.redact(case["in"]), case["out"])
                if "\n" not in case["in"]:  # single-line callers (logs, journal) agree
                    self.assertEqual(helper_mod.redact_line(case["in"]), case["out"])

    def test_every_rule_has_a_reference_case(self) -> None:
        for pattern, _ in helper_mod.REDACT_RULES:
            with self.subTest(rule=pattern.pattern[:60]):
                self.assertTrue(any(pattern.search(c["in"]) for c in CASES))
        self.assertTrue(any(helper_mod.PEM_BEGIN_RE.search(c["in"]) for c in CASES))
        self.assertTrue(any(helper_mod.VAULT_HEADER_RE.search(line) for c in CASES for line in c["in"].split("\n")))

    def test_the_fixtures_are_synthetic(self) -> None:
        blob = json.dumps(CASES)
        for marker in (".ts.net", "100.", "192.168.", "@gmail", "homebk"):
            self.assertNotIn(marker, blob)


class Triggers(unittest.TestCase):
    def test_the_prefilter_never_changes_a_result(self) -> None:
        texts = [c["in"] for c in CASES] + [t[:8192] for t in PATHOLOGICAL.values()]
        texts += [(u * (8192 // len(u) + 1))[:8192] for u in DENSE_UNITS]
        texts += [c["in"].upper() for c in CASES] + [c["in"].title() for c in CASES]
        with_triggers = [helper_mod.redact(t) for t in texts]
        # Scoped rules keep their trigger (it's their scope); every other trigger is switched off.
        speed_only_off = [t if i in helper_mod.SCOPED_RULES else () for i, t in enumerate(helper_mod.RULE_TRIGGERS)]
        with mock.patch.object(helper_mod, "RULE_TRIGGERS", speed_only_off):
            without = [helper_mod.redact(t) for t in texts]
        for text, a, b in zip(texts, with_triggers, without):
            with self.subTest(text=text[:60]):
                self.assertEqual(a, b)


    def test_folded_matching_equals_ignorecase_matching(self) -> None:
        texts = [c["in"] for c in CASES] + [c["in"].upper() for c in CASES] + [c["in"].swapcase() for c in CASES]
        texts += [t[:8192] for t in PATHOLOGICAL.values()]
        folded = [helper_mod.redact(t) for t in texts]
        with mock.patch.object(helper_mod, "FOLDED_RULES", frozenset()):
            ignorecase = [helper_mod.redact(t) for t in texts]
        for text, a, b in zip(texts, folded, ignorecase):
            with self.subTest(text=text[:60]):
                self.assertEqual(a, b)


    def test_scoped_rules_need_their_command_on_the_line(self) -> None:
        for line in ("x -u admin:pw-1", "x -ppw-2", "x -p pw-3", "x -a pw-4"):
            with self.subTest(line=line):
                self.assertEqual(helper_mod.redact(line), line)
        self.assertEqual(helper_mod.redact("docker run -p 8080:80 img"), "docker run -p 8080:80 img")  # no "login"
        self.assertEqual(helper_mod.redact("docker login -p pw-5"), "docker login -p [REDACTED]")


class MaximumReply(unittest.TestCase):
    def test_a_dense_maximum_size_reply_redacts_within_two_seconds(self) -> None:
        # 1000 lines x 8 KiB (the most one reply can carry), worst case for each rule in turn.
        for unit in DENSE_UNITS:
            text = "\n".join([(unit * (8192 // len(unit) + 1))[:8192]] * 1000)
            with self.subTest(unit=unit[:40]):
                started = time.perf_counter()
                helper_mod.redact(text)
                elapsed = time.perf_counter() - started
                self.assertLess(elapsed, 2.0, f"{unit[:40]!r}: {elapsed:.2f} s")

    def test_a_dense_logs_reply_end_to_end(self) -> None:
        env = Env()
        try:
            msg = ("curl -u " * 1000)[:7000]
            env.set_rule("journalctl", [{"stdout": "".join(
                json.dumps({"__REALTIME_TIMESTAMP": "1793000000000000", "MESSAGE": msg}) + "\n" for _ in range(1000))}])
            started = time.perf_counter()
            data = env.ok({"op": "logs", "source": "docker", "lines": 1000})
            self.assertLess(time.perf_counter() - started, 3.0)
            self.assertEqual(len(data["lines"]), 1000)
        finally:
            env.close()

    def test_the_deadline_is_checked_between_entries(self) -> None:
        # Deterministic: the deadline expires right after journalctl returns, so only a check between
        # entries can turn the reply into a timeout.
        for op, extra in (("logs", {"source": "docker", "lines": 50}), ("localai-activity", {"limit": 50}),
                          ("sudo-commands", {"user": "claude", "limit": 50})):
            with self.subTest(op=op):
                env = Env()
                try:
                    msg = "claude : TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/usr/bin/id"
                    env.set_rule("journalctl", [{"stdout": "".join(json.dumps({
                        "__REALTIME_TIMESTAMP": "1793000000000000", "MESSAGE": msg, "_UID": "999",
                        "_EXE": "/usr/bin/sudo.ws", "_PID": "1"}) + "\n" for _ in range(50))}])
                    real = env.helper.journal

                    def journal_then_expire(*a, **kw):
                        out = real(*a, **kw)
                        env.helper.deadline_at = float("-inf")
                        return out

                    with mock.patch.object(env.helper, "journal", journal_then_expire):
                        env.err({"op": op, **extra}, "timeout")
                finally:
                    env.close()

    def test_the_deadline_is_checked_between_container_log_lines(self) -> None:
        env = Env()
        try:
            real = env.helper.docker

            def docker_then_expire(args, **kw):
                out = real(args, **kw)
                if args and args[0] == "logs":
                    env.helper.deadline_at = float("-inf")
                return out

            with mock.patch.object(env.helper, "docker", docker_then_expire):
                env.err({"op": "logs", "source": "gpu-broker"}, "timeout")
        finally:
            env.close()

    def test_redact_calls_the_deadline_check_between_lines(self) -> None:
        calls = []

        def check() -> None:
            calls.append(1)
            if len(calls) > 3:
                raise helper_mod.OpError("timeout", "deadline")

        with self.assertRaises(helper_mod.OpError):
            helper_mod.redact("x\n" * 1000, check)
        self.assertEqual(len(calls), 4)


class Linear(unittest.TestCase):
    def assert_fast(self, fn, text: str, what: str) -> None:
        started = time.perf_counter()
        fn(text)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.0, f"{what} took {elapsed:.2f} s on 64 KiB")

    def test_each_rule_is_linear_on_pathological_input_uncapped(self) -> None:
        # The rules themselves, without the 8 KiB line cap that normally protects them.
        for i, (pattern, repl) in enumerate(helper_mod.REDACT_RULES):
            for name, text in PATHOLOGICAL.items():
                with self.subTest(rule=i, input=name):
                    self.assert_fast(lambda t: pattern.sub(repl, t), text, f"rule {i} on {name}")
        for name, text in PATHOLOGICAL.items():
            with self.subTest(pem=name):
                self.assert_fast(helper_mod._redact_pem, text, f"pem on {name}")
            with self.subTest(yaml=name):
                self.assert_fast(helper_mod.yaml_secret_block, text, f"yaml block start on {name}")

    def test_dense_keywords_at_512k_stay_linear(self) -> None:
        # A rescan to the line end at every keyword (the pre-fix block-indicator lookahead) is quadratic but
        # fast in C at 64 KiB (~0.08 s); at 512 KiB it takes seconds. Linear rules take milliseconds.
        big = 512 * 1024
        for name, unit in (("indicator-comment", "token:|#"), ("indicator-space", "token: | #"),
                           ("indicator-redacted", "token: | [REDACTED]"), ("assign", "password="),
                           ("quote", 'secret:"'), ("vault-tag", "token: !vault ")):
            text = unit * (big // len(unit))
            for i, (pattern, repl) in enumerate(helper_mod.REDACT_RULES):
                with self.subTest(rule=i, input=name):
                    started = time.perf_counter()
                    pattern.sub(repl, text)
                    elapsed = time.perf_counter() - started
                    self.assertLess(elapsed, 1.0, f"rule {i} on {name} took {elapsed:.2f} s on 512 KiB")

    def test_a_64k_line_redacts_in_under_a_second(self) -> None:
        for name, text in PATHOLOGICAL.items():
            with self.subTest(input=name):
                self.assert_fast(helper_mod.redact, text, f"redact on {name}")
                self.assert_fast(helper_mod.clean_line, text, f"clean_line on {name}")
                self.assert_fast(helper_mod.redact, (text + "\n") * 4, f"redact on 4 lines of {name}")
        for name, text in PATHOLOGICAL_LINES.items():
            with self.subTest(lines=name):
                self.assert_fast(helper_mod.redact, text, f"redact on {name}")
                self.assert_fast(helper_mod.clean_line, text, f"clean_line on {name}")

    def test_lines_are_capped_before_redaction_and_a_cut_word_is_dropped(self) -> None:
        token = "ghp_" + "Q" * 36
        line = "x " * 4090 + token  # the 8192-char cap falls inside the token
        out = helper_mod.redact(line)
        self.assertLessEqual(len(out), helper_mod.REDACT_LINE_CAP)
        self.assertNotIn("ghp_", out)
        self.assertNotIn("Q", out)
        self.assertEqual(helper_mod.cap_line("a" * 9000), ("", True))  # one giant word: nothing kept
        # redact() itself applies the cap, on every line of multi-line text
        capped = helper_mod.redact("a " * 5000 + "\nshort\n" + "b " * 5000)
        self.assertEqual([len(x) for x in capped.split("\n")], [8192, 5, 8192])
        self.assertLessEqual(len(helper_mod.clean_line("c " * 5000, cap=10**6)), helper_mod.REDACT_LINE_CAP)


class YamlBlocks(unittest.TestCase):
    def test_a_multi_line_journal_message_is_redacted_line_by_line_then_flattened(self) -> None:
        msg = "loaded vars:\n  ansible_become_pass: |\n    pw-fake-1\n  other: 1"
        self.assertEqual(helper_mod.clean_line(msg), "loaded vars:   ansible_become_pass: |     [REDACTED]   other: 1")

    def test_a_block_in_replay_output(self) -> None:
        env = Env()
        try:
            stream = b"$ cat vars.yml\r\nvault:\r\n  token: >\r\n    tok-fake-123\r\n  host: x\r\n"
            make_session(env.iolog, "claude", "00/00/01", epoch=1793000000, command=["/usr/bin/cat", "vars.yml"],
                         streams={"ttyout": stream}, timing=[(4, len(stream))])
            data = env.ok({"op": "sudo-replay", "user": "claude", "id": "000001", "output": True})
            self.assertEqual(data["output"], "$ cat vars.yml\nvault:\n  token: >\n    [REDACTED]\n  host: x\n")
        finally:
            env.close()


class ReplayCuts(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()

    def tearDown(self) -> None:
        self.env.close()

    def test_a_partial_token_at_the_raw_cap_is_dropped(self) -> None:
        # Escape sequences strip to nothing, so text near the raw cap lands inside max_bytes.
        max_bytes = 1000
        raw_cap = max_bytes + helper_mod.REDACTION_MARGIN
        escapes = b"\x1b[0m" * ((raw_cap - 12) // 4)
        pad = b"x" * (raw_cap - 12 - len(escapes))
        stream = escapes + pad + b" ghp_" + b"Q" * 36 + b"\r\n"  # the cap cuts after "ghp_QQQQQQ"
        make_session(self.env.iolog, "claude", "00/00/01", epoch=1793000000, command=["/usr/bin/true"],
                     streams={"ttyout": stream}, timing=[(4, len(stream))])
        data = self.env.ok({"op": "sudo-replay", "user": "claude", "id": "000001", "output": True,
                            "max_bytes": max_bytes})
        self.assertTrue(data["output_truncated"])
        self.assertNotIn("ghp_", data["output"])
        self.assertNotIn("QQ", data["output"])
        self.assertEqual(data["output"], "x" * len(pad) + " ")

    def test_an_over_long_line_is_capped_and_flagged(self) -> None:
        stream = ("y " * 4094 + "token=" + "S" * 100 + "\r\nnext line\r\n").encode()
        make_session(self.env.iolog, "claude", "00/00/02", epoch=1793000000, command=["/usr/bin/true"],
                     streams={"ttyout": stream}, timing=[(4, len(stream))])
        data = self.env.ok({"op": "sudo-replay", "user": "claude", "id": "000002", "output": True})
        self.assertTrue(data["output_truncated"])
        self.assertNotIn("S", data["output"])
        self.assertTrue(data["output"].endswith("\nnext line\n"))
        self.assertLessEqual(len(data["output"].split("\n")[0]), helper_mod.REDACT_LINE_CAP)


class DamagedMetadata(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()
        io = self.env.iolog
        make_session(io, "claude", "00/00/01", epoch=1793000000, command=["/usr/bin/id"],
                     streams={"ttyout": b"good\r\n"}, timing=[(4, 6)])
        bad = make_session(io, "claude", "00/00/02", epoch=1793000100, command=["/usr/bin/x"])
        (bad / "log.json").write_text("[" * 60000)  # RecursionError in json.loads
        odd = make_session(io, "claude", "00/00/03", epoch=1793000200, command=["/usr/bin/x"])
        (odd / "log.json").write_text(json.dumps({"timestamp": [], "runargv": {"a": 1}, "ttyname": 5}))
        legacy = make_session(io, "claude", "00/00/04", epoch=1793000300, command=["/usr/bin/legacy"], legacy=True)
        (legacy / "log.json").write_text("{not json")  # falls back to the legacy log
        link = make_session(io, "claude", "00/00/05", epoch=1793000400, command=["/usr/bin/x"])
        os.unlink(link / "log.json")
        os.symlink(io / "claude" / "00" / "00" / "01" / "log.json", link / "log.json")
        for d in ("02", "05"):
            os.utime(io / "claude" / "00" / "00" / d / "log.json", (1793000500, 1793000500), follow_symlinks=False)

    def tearDown(self) -> None:
        self.env.close()

    def test_bad_metadata_skips_that_session_only(self) -> None:
        data = self.env.ok({"op": "sudo-sessions", "user": "claude"})
        self.assertEqual([s["id"] for s in data["sessions"]], ["00/00/04", "00/00/01", "00/00/03"])
        # 05 (a symlinked log.json) is not even counted as a session; 02 (RecursionError) is skipped
        self.assertEqual((data["total"], data["skipped"], data["complete"]), (4, 1, True))
        self.assertEqual(data["sessions"][0]["command"], "/usr/bin/legacy")
        self.assertEqual(data["sessions"][2]["ts"], None)  # odd shapes become nulls/empties (sorted last), not errors

    def test_replay_of_a_damaged_session_is_an_error_and_others_still_work(self) -> None:
        self.env.err({"op": "sudo-replay", "user": "claude", "id": "000002"}, "damaged")
        self.env.err({"op": "sudo-replay", "user": "claude", "id": "000005"}, "damaged")
        self.assertEqual(self.env.ok({"op": "sudo-replay", "user": "claude", "id": "000001", "output": True})["output"],
                         "good\n")


class Deadline(unittest.TestCase):
    def test_one_slow_command_stops_at_the_request_deadline(self) -> None:
        env = Env(command_timeout=5, request_deadline=1)
        try:
            env.set_rule("journalctl", [{"sleep": 30}])
            started = time.monotonic()
            env.err({"op": "localai-activity"}, "timeout")
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            env.close()

    def test_the_deadline_spans_several_commands(self) -> None:
        env = Env(command_timeout=5, request_deadline=1)
        try:
            timers = [r for r in env.scenario["systemctl"] if r["match"] == ["list-timers"]][0]
            show = [r for r in env.scenario["systemctl"] if r["match"] == ["show"]][0]
            env.set_rule("systemctl", [{**timers, "sleep": 0.6}, {**show, "sleep": 0.6}])
            started = time.monotonic()
            err = env.err({"op": "timers"}, "timeout")  # each step alone is within command_timeout
            self.assertLess(time.monotonic() - started, 1.8)
            self.assertTrue(err["message"])
        finally:
            env.close()

    def test_a_trickling_client_is_cut_off_at_the_deadline(self) -> None:
        env = Env(request_timeout=5, request_deadline=0.5)
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        stop = threading.Event()

        def trickle() -> None:
            for ch in b'{"op": "timers"' + b" " * 100:
                if stop.is_set():
                    return
                try:
                    client.send(bytes([ch]))
                except OSError:
                    return
                time.sleep(0.05)

        thread = threading.Thread(target=trickle, daemon=True)
        try:
            thread.start()
            started = time.monotonic()
            outcome = env.helper.serve(server)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(outcome, "bad-request")
        finally:
            stop.set()
            thread.join(2)
            server.close()
            client.close()
            env.close()

    def test_the_deadline_is_capped_at_ten_seconds(self) -> None:
        with self.assertRaises(ValueError):
            helper_mod.Config.from_mapping({"request_deadline": 10.5})
        self.assertEqual(helper_mod.Config().request_deadline, 10)


class ProcessGroup(unittest.TestCase):
    def test_a_timeout_kills_the_whole_process_group(self) -> None:
        env = Env(command_timeout=0.5)
        try:
            env.set_rule("journalctl", [{"sleep": 30, "child_pidfile": "child.pid"}])
            env.err({"op": "localai-activity"}, "timeout")
            child = int((env.bin / "child.pid").read_text())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                os.kill(child, 9)
                self.fail("the stub's child survived: only the leader was killed")
        finally:
            env.close()


class BoundedDecompression(unittest.TestCase):
    def test_memory_stays_bounded_on_a_decompression_bomb(self) -> None:
        env = Env()
        try:
            d = make_session(env.iolog, "claude", "00/00/01", epoch=1793000000, command=["/usr/bin/yes"])
            (d / "ttyout").write_bytes(gzip.compress(b"\0" * (200 * 1024 * 1024)))  # 200 MiB of zeros
            tracemalloc.start()
            try:
                data = env.ok({"op": "sudo-replay", "user": "claude", "id": "000001", "output": True,
                               "max_bytes": 1000})
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertTrue(data["output_truncated"])
            self.assertLess(peak, 16 * 1024 * 1024, f"peak {peak / 2**20:.1f} MiB")
        finally:
            env.close()

    def test_decompress_is_always_given_a_max_length(self) -> None:
        calls: list[tuple] = []
        real = helper_mod.zlib.decompressobj

        class Spy:
            def __init__(self, *a):
                self.d = real(*a)

            def decompress(self, data, *rest):
                calls.append(rest)
                return self.d.decompress(data, *rest)

            def __getattr__(self, name):
                return getattr(self.d, name)

        reader = helper_mod.StreamReader(lambda: False)
        with mock.patch.object(helper_mod.zlib, "decompressobj", Spy):
            out = reader._inflate(_BytesFile(gzip.compress(b"z" * 100000)), b"", 500, 16 + 15)
        self.assertEqual((len(out[0]), out[1]), (500, True))
        self.assertTrue(calls)
        self.assertTrue(all(len(c) == 1 and 0 < c[0] <= 501 for c in calls), calls)


class _BytesFile:
    def __init__(self, data: bytes) -> None:
        self.data, self.pos = data, 0

    def read(self, n: int) -> bytes:
        chunk = self.data[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk


class RootUser(unittest.TestCase):
    def test_a_configured_user_that_maps_to_uid_0_refuses_everyone(self) -> None:
        env = Env()
        try:
            with mock.patch.object(env.helper.sys, "uid_of", return_value=0), \
                    mock.patch.object(helper_mod, "peer_credentials", return_value=(1, 0, 0)):
                outcome, reply = env.call({"op": "timers"})
            self.assertEqual((outcome, reply["error"]["code"]), ("forbidden", "forbidden"))
            self.assertEqual(env.calls, [])
            self.assertIn("(or is root)", env.log)
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()
