"""sudo-sessions and sudo-replay against fake sudo I/O log trees: plain, gzip and zlib streams, log.json
and the legacy log, ANSI-laden output with secrets, symlinks, and path-traversal ids."""

from __future__ import annotations

import gzip
import json
import os
import unittest
from unittest import mock

from harness import Env, helper_mod, make_session

ANSI_OUT = (b"\x1b[?2004l\x1b]0;claude@host: ~\x07\x1b[1;32mReading package lists...\x1b[0m Done\r\n"
            b"Progress: 10%\rProgress: 50%\rProgress: 100%\r\n"
            b"curl -H 'Authorization: Bearer live-abcdef123456' https://user:pa55@example.invalid/\r\n"
            b"\x1b[31mexport OLLAMA_API_KEY=sk-0123456789abcdefXYZ\x1b[0m\r\n"
            b"bad utf8: \xff\xfe\r\n")


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()
        io = self.env.iolog
        (io / "claude").mkdir(mode=0o700)
        (io / "claude" / "seq").write_text("000005\n")
        make_session(io, "claude", "00/00/01", epoch=1793000000, command=["/usr/bin/apt", "update"], mode="gzip",
                     streams={"ttyout": ANSI_OUT, "ttyin": b"TYPED-PASSWORD\r", "stdin": b"NO-LOG-SECRET"},
                     timing=[(4, len(ANSI_OUT)), (3, 15)])
        make_session(io, "claude", "00/00/02", epoch=1793000100, mode="plain", tty="",
                     command=["/bin/sh", "-c", "echo token=SEKRET-1; ls 'a b'"],
                     streams={"stdout": b"out1\nout2\n", "stderr": b"err1\n"},
                     timing=[(1, 5), (2, 5), (1, 5)])
        make_session(io, "claude", "00/00/03", epoch=1793000200, mode="zlib", legacy=True,
                     command=["/usr/bin/systemctl", "restart", "ollama"],
                     streams={"ttyout": b"\x1b[0;1;32m\xe2\x97\x8f\x1b[0m ollama.service\r\n"},
                     timing=[(4, 30)])
        (io / "claude" / "00" / "00" / "05").mkdir()  # no log: not a session
        make_session(io, "localai", "00/00/01", epoch=1793000300, command=["/usr/bin/id"],
                     streams={"ttyout": b"LOCALAI-ONLY\r\n"}, timing=[(4, 14)])
        # Symlinks planted in claude's tree, pointing at localai's session.
        os.symlink(io / "localai" / "00" / "00" / "01", io / "claude" / "00" / "00" / "04")
        os.symlink(io / "localai" / "00", io / "claude" / "01")

    def tearDown(self) -> None:
        self.env.close()

    def replay(self, sid: str, **extra) -> dict:
        return self.env.ok({"op": "sudo-replay", "user": "claude", "id": sid, **extra})


class Sessions(Fixture):
    def test_lists_sessions_newest_first_in_every_format(self) -> None:
        data = self.env.ok({"op": "sudo-sessions", "user": "claude"})
        self.assertEqual((data["user"], data["total"], data["complete"]), ("claude", 3, True))
        self.assertEqual(data["sessions"], [
            {"id": "00/00/03", "ts": 1793000200, "runas": "root", "tty": "/dev/pts/0",
             "cwd": "/home/claude", "command": "/usr/bin/systemctl restart ollama"},
            {"id": "00/00/02", "ts": 1793000100, "runas": "root", "tty": "",
             "cwd": "/home/claude", "command": "/bin/sh -c 'echo token=[REDACTED]; ls '\"'\"'a b'\"'\"''"},
            {"id": "00/00/01", "ts": 1793000000, "runas": "root", "tty": "/dev/pts/0",
             "cwd": "/home/claude", "command": "/usr/bin/apt update"},
        ])
        blob = json.dumps(data)
        for leak in ("LOCALAI", "env-must-not-appear", "SEKRET", "example-host"):
            self.assertNotIn(leak, blob)
        self.assertEqual(self.env.calls, [], "no sudoreplay or any other program is run")

    def test_limit_keeps_the_newest(self) -> None:
        data = self.env.ok({"op": "sudo-sessions", "user": "claude", "limit": 1})
        self.assertEqual([s["id"] for s in data["sessions"]], ["00/00/03"])
        self.assertEqual(data["total"], 3)

    def test_a_user_without_logs_has_no_sessions(self) -> None:
        import shutil
        shutil.rmtree(self.env.iolog / "localai")
        self.assertEqual(self.env.ok({"op": "sudo-sessions", "user": "localai"}),
                         {"user": "localai", "sessions": [], "total": 0, "complete": True})

    def test_a_symlinked_user_directory_is_refused(self) -> None:
        import shutil
        shutil.rmtree(self.env.iolog / "localai")
        os.symlink(self.env.iolog / "claude", self.env.iolog / "localai")
        self.env.err({"op": "sudo-sessions", "user": "localai"}, "failed")

    def test_unknown_users_are_refused(self) -> None:
        for user in ("root", "noah", "../claude", "claude/..", "CLAUDE", ""):
            with self.subTest(user=user):
                self.env.err({"op": "sudo-sessions", "user": user}, None)


class Replay(Fixture):
    def test_command_line_only_by_default(self) -> None:
        reads: list[str] = []
        real = helper_mod.StreamReader.read

        def spy(reader, dir_fd, name, cap):
            reads.append(name)
            return real(reader, dir_fd, name, cap)

        with mock.patch.object(helper_mod.StreamReader, "read", spy):
            data = self.replay("00/00/01")
        self.assertEqual(data, {"id": "00/00/01", "ts": 1793000000, "runas": "root", "tty": "/dev/pts/0",
                                "cwd": "/home/claude", "command": "/usr/bin/apt update", "output": None,
                                "output_truncated": False})
        self.assertEqual(reads, ["log.json"])

    def test_output_is_plain_redacted_text_and_input_is_never_read(self) -> None:
        reads: list[str] = []
        real = helper_mod.StreamReader.read

        def spy(reader, dir_fd, name, cap):
            reads.append(name)
            return real(reader, dir_fd, name, cap)

        with mock.patch.object(helper_mod.StreamReader, "read", spy):
            data = self.replay("00/00/01", output=True)
        self.assertEqual(data["output"],
                         "Reading package lists... Done\n"
                         "Progress: 100%\n"
                         "curl -H 'Authorization: Bearer [REDACTED]' https://user:[REDACTED]@example.invalid/\n"
                         "export OLLAMA_API_KEY=[REDACTED]\n"
                         "bad utf8: ��\n")
        self.assertFalse(data["output_truncated"])
        self.assertNotIn("ttyin", reads)
        self.assertNotIn("stdin", reads)
        for leak in ("TYPED-PASSWORD", "NO-LOG-SECRET", "\x1b", "\r", "live-abcdef", "pa55", "sk-0123"):
            self.assertNotIn(leak, json.dumps(data, ensure_ascii=False))
        self.assertEqual(self.env.calls, [])

    def test_plain_streams_are_interleaved_by_the_timing_file(self) -> None:
        data = self.replay("00/00/02", output=True)
        self.assertEqual(data["output"], "out1\nerr1\nout2\n")
        self.assertEqual(data["command"], "/bin/sh -c 'echo token=[REDACTED]; ls '\"'\"'a b'\"'\"''")

    def test_without_a_timing_file_streams_are_concatenated(self) -> None:
        os.unlink(self.env.iolog / "claude" / "00" / "00" / "02" / "timing")
        self.assertEqual(self.replay("00/00/02", output=True)["output"], "out1\nout2\nerr1\n")

    def test_zlib_streams_and_the_legacy_log(self) -> None:
        data = self.replay("00/00/03", output=True)
        self.assertEqual(data["output"], "● ollama.service\n")
        self.assertEqual((data["command"], data["tty"]), ("/usr/bin/systemctl restart ollama", "/dev/pts/0"))

    def test_a_six_character_tsid_is_the_seq_path(self) -> None:
        self.assertEqual(self.replay("000002")["id"], "00/00/02")

    def test_redaction_happens_before_truncation(self) -> None:
        token = b"ghp_" + b"Q" * 36
        make_session(self.env.iolog, "claude", "00/00/06", epoch=1793000400, command=["/usr/bin/true"],
                     streams={"ttyout": b"a" * 100 + b" " + token + b"\r\n" + b"z" * 500}, timing=[(4, 641)])
        data = self.replay("00/00/06", output=True, max_bytes=110)
        self.assertTrue(data["output_truncated"])
        self.assertEqual(len(data["output"].encode()), 110)
        self.assertEqual(data["output"], "a" * 100 + " [REDACTED"[:10])
        self.assertNotIn("QQ", data["output"])
        self.assertNotIn("ghp_", data["output"])

    def test_truncation_is_on_a_utf8_boundary(self) -> None:
        make_session(self.env.iolog, "claude", "00/00/07", epoch=1793000500, command=["/usr/bin/true"],
                     streams={"ttyout": "é".encode() * 100}, timing=[(4, 200)])
        data = self.replay("00/00/07", output=True, max_bytes=5)
        self.assertEqual((data["output"], data["output_truncated"]), ("éé", True))

    def test_a_decompression_bomb_stops_at_the_cap(self) -> None:
        d = make_session(self.env.iolog, "claude", "00/00/08", epoch=1793000600, command=["/usr/bin/yes"])
        (d / "ttyout").write_bytes(gzip.compress(b"y\n" * (25 * 1024 * 1024)))  # 50 MiB inflated
        data = self.replay("00/00/08", output=True, max_bytes=1000)
        self.assertTrue(data["output_truncated"])
        self.assertEqual(data["output"], "y\n" * 500)

    def test_damaged_and_unfinished_streams(self) -> None:
        d = make_session(self.env.iolog, "claude", "00/00/09", epoch=1793000700, command=["/usr/bin/true"])
        good = gzip.compress(b"partial output token=abc\n" * 4)
        (d / "ttyout").write_bytes(good[:-8])  # a running session: no gzip trailer yet
        data = self.replay("00/00/09", output=True)
        self.assertEqual(data["output"].count("token=[REDACTED]"), 4)
        self.assertNotIn("output_damaged", data)
        (d / "ttyout").write_bytes(good[:20] + b"\x00garbage\xff" * 10)
        data = self.replay("00/00/09", output=True)
        self.assertTrue(data["output_damaged"])

    def test_path_traversal_and_bad_ids_are_refused(self) -> None:
        for sid in ("../localai/00/00/01", "00/../../localai/00/00/01", "..", "00/00/..", "/etc", "/00/00/01",
                    "00//00/01", "./00/00/01", "00/00/01/", "00/00/01/.", "a" * 65, "00/00/0\n", "00 00 01",
                    "00/00/01;id", "~root", "$(id)", "00/00/01\x00", "00\\00"):
            with self.subTest(sid=sid):
                self.env.err({"op": "sudo-replay", "user": "claude", "id": sid}, "bad-request")
        self.assertEqual(self.env.calls, [])

    def test_symlinks_are_never_followed(self) -> None:
        for sid in ("00/00/04", "01/00/01"):  # a symlinked session dir, a symlinked middle dir
            with self.subTest(sid=sid):
                err = self.env.err({"op": "sudo-replay", "user": "claude", "id": sid, "output": True}, "not-found")
                self.assertNotIn("LOCALAI", json.dumps(err))
        sessions = self.env.ok({"op": "sudo-sessions", "user": "claude"})
        self.assertNotIn("00/00/04", [s["id"] for s in sessions["sessions"]])
        # a symlinked stream file inside a real session
        d = self.env.iolog / "claude" / "00" / "00" / "02"
        os.unlink(d / "stdout")
        os.symlink(self.env.iolog / "localai" / "00" / "00" / "01" / "ttyout", d / "stdout")
        self.env.err({"op": "sudo-replay", "user": "claude", "id": "00/00/02", "output": True}, "failed")

    def test_unknown_sessions_and_a_file_where_a_dir_should_be(self) -> None:
        self.env.err({"op": "sudo-replay", "user": "claude", "id": "00/00/99"}, "not-found")
        self.env.err({"op": "sudo-replay", "user": "claude", "id": "seq"}, "not-found")
        self.env.err({"op": "sudo-replay", "user": "claude", "id": "00/00/05"}, "not-found")  # dir without a log

    def test_the_other_users_session_needs_that_user(self) -> None:
        data = self.env.ok({"op": "sudo-replay", "user": "localai", "id": "00/00/01", "output": True})
        self.assertEqual(data["output"], "LOCALAI-ONLY\n")


class Cleaning(unittest.TestCase):
    def test_timestamps_are_epoch_seconds(self) -> None:
        f = helper_mod.rfc3339_epoch
        self.assertEqual(f("2026-10-26T06:00:00.123456789Z rest"), (1792994400, 31))
        self.assertEqual(f("2026-10-26T08:00:00+02:00"), (1792994400, 25))
        self.assertEqual(f("0001-01-01T00:00:00Z")[0], None)  # Docker's "never started"
        self.assertEqual(f("no time here"), (None, 0))
        self.assertEqual(f(None), (None, 0))
        e = helper_mod._epoch
        self.assertEqual([e(1793000000), e(1793000000.9), e(float("inf")), e(float("nan")), e(-5), e(True), e("1")],
                         [1793000000, 1793000000, None, None, None, None, None])

    def test_strip_terminal(self) -> None:
        f = helper_mod.strip_terminal
        self.assertEqual(f("\x1b[1;31mred\x1b[0m"), "red")
        self.assertEqual(f("\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\"), "link")
        self.assertEqual(f("abcdef\rXY"), "XYcdef")
        self.assertEqual(f("abc\bX"), "abX")
        self.assertEqual(f("a\x00b\x07c\x9b31md\tz"), "abcd\tz")
        self.assertEqual(f("\x1bPdcs-payload\x1b\\ok"), "ok")
        self.assertEqual(f("\x1b(Bplain"), "plain")

    def test_redact(self) -> None:
        r = helper_mod.redact
        cases = {
            "Authorization: Bearer abc.def-123": "Authorization: Bearer [REDACTED]",
            "authorization=xyz": "authorization=[REDACTED]",
            "token=abc&x=1": "token=[REDACTED]&x=1",
            '{"api_key": "a b c", "n": 1}': '{"api_key": "[REDACTED]", "n": 1}',
            "PASSWORD: hunter2": "PASSWORD: [REDACTED]",
            "--password hunter2 --token=t1": "--password [REDACTED] --token=[REDACTED]",
            "TS_AUTHKEY=tskey-auth-k123456789-abcdef": "TS_AUTHKEY=[REDACTED]",
            "key tskey-auth-k123456789-abcdef": "key [REDACTED]",
            "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlXw": "jwt [REDACTED]",
            "max_tokens: 512, tokenizer: x": "max_tokens: 512, tokenizer: x",
            "[sudo] password for claude:": "[sudo] password for claude:",
            "PWD=/home/claude ; USER=root": "PWD=/home/claude ; USER=root",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(r(raw), want)
        key = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----"
        self.assertEqual(r(f"x {key} y"), "x [REDACTED]\n[REDACTED]\n[REDACTED] y")
        self.assertEqual(r(f"x {key} y".replace("\n", " ")), "x [REDACTED] y")


if __name__ == "__main__":
    unittest.main()
