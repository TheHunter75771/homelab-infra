"""logs (JSON-lines stream, follow limits, client close) and disk-split (24 h cache, nice/ionice)."""

from __future__ import annotations

import fcntl
import json
import os
import unittest

from fakes import DOCKER, Env, cid, read_lines


class Logs(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()

    def tearDown(self) -> None:
        self.env.close()

    def test_lines_are_streamed_with_their_timestamps(self) -> None:
        self.env.sys.logs_output = (
            b"2026-10-24T06:00:00.123456789Z [Info] Sonarr started\n"
            b"2026-10-24T06:00:01.000000000Z \x1b[31m[Error]\x1b[0m db locked\x07\n"
            b"no timestamp line"
        )
        self.env.sys.logs_err = b"2026-10-24T06:00:02.000000000Z warn: on stderr\n"
        outcome, lines = self.env.call({"op": "logs", "name": "sonarr"})
        self.assertEqual(outcome, "ok")
        self.assertEqual(lines[0], {"ok": True, "op": "logs", "stream": True, "name": "sonarr", "follow": False})
        self.assertEqual(lines[-1], {"type": "end", "ok": True, "reason": "eof"})
        self.assertEqual(sorted(lines[1:-1], key=lambda x: (x["ts"] or "~")), [
            {"type": "line", "ts": "2026-10-24T06:00:00.123456789Z", "text": "[Info] Sonarr started", "stream": "stdout"},
            {"type": "line", "ts": "2026-10-24T06:00:01.000000000Z", "text": "[Error] db locked", "stream": "stdout"},
            {"type": "line", "ts": "2026-10-24T06:00:02.000000000Z", "text": "warn: on stderr", "stream": "stderr"},
            {"type": "line", "ts": None, "text": "no timestamp line", "stream": "stdout"},
        ])

    def test_the_command(self) -> None:
        self.env.call({"op": "logs", "name": "sonarr"})
        self.env.call({"op": "logs", "name": "sonarr", "tail": 2000, "since": "10m"})
        logs = [c for c in self.env.sys.docker_calls() if c[1] == "logs"]
        self.assertEqual(logs, [
            [DOCKER, "logs", "--timestamps", "--tail", "200", cid(1)],
            [DOCKER, "logs", "--timestamps", "--tail", "2000", "--since", "10m", cid(1)],
        ])

    def test_long_lines_are_cut(self) -> None:
        self.env.sys.logs_output = b"x" * 40000 + b"\n"
        _, lines = self.env.call({"op": "logs", "name": "sonarr"})
        texts = [line["text"] for line in lines if line.get("type") == "line"]
        self.assertTrue(texts and all(len(t) <= 16384 for t in texts))

    def test_docker_error_ends_the_stream_not_ok(self) -> None:
        self.env.sys.logs_rc = 1
        self.env.sys.logs_output = b"Error response from daemon: no such container\n"
        outcome, lines = self.env.call({"op": "logs", "name": "sonarr"})
        self.assertEqual(outcome, "failed")
        self.assertEqual(lines[-1], {"type": "end", "ok": False, "reason": "failed", "message": "docker logs exited 1"})

    def test_follow_ends_after_the_maximum_duration(self) -> None:
        self.env.sys.logs_hang = True
        self.env.sys.logs_output = b"2026-10-24T06:00:00Z hello\n"
        self.env.sys.tick = 400.0  # each clock read = 400 s, so 30 min pass in a few loops
        outcome, lines = self.env.call({"op": "logs", "name": "sonarr", "follow": True})
        self.assertEqual(outcome, "ok")
        self.assertIn([DOCKER, "logs", "--timestamps", "--tail", "200", "--follow", cid(1)], self.env.sys.calls)
        self.assertEqual(lines[-1], {"type": "end", "ok": True, "reason": "max-duration"})
        self.assertTrue(self.env.sys.procs[-1].killed, "docker logs --follow is stopped")

    def test_a_plain_read_that_hangs_times_out(self) -> None:
        self.env.sys.logs_hang = True
        self.env.sys.tick = 30.0
        outcome, lines = self.env.call({"op": "logs", "name": "sonarr"})
        self.assertEqual(outcome, "timeout")
        self.assertEqual(lines[-1]["reason"], "timeout")

    def test_closing_the_client_stops_the_follow(self) -> None:
        self.env.sys.logs_hang = True
        self.env.sys.logs_output = b"2026-10-24T06:00:00Z first\n"
        client, thread, result = self.env.call_threaded({"op": "logs", "name": "sonarr", "follow": True})
        got = read_lines(client, 2)
        self.assertEqual(got[1]["text"], "first")
        client.close()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "the helper noticed the close without any new output")
        self.assertEqual(result["outcome"], "client-closed")
        self.assertTrue(self.env.sys.procs[-1].killed)

    def test_logs_only_for_labelled_containers(self) -> None:
        for name in ("stranger", "nosuch", cid(2)[:12]):
            with self.subTest(name=name):
                self.assertEqual(self.env.call({"op": "logs", "name": name})[0], "not-allowed")
        self.assertFalse([c for c in self.env.sys.docker_calls() if c[1] == "logs"])

    def test_log_contents_never_reach_the_journal(self) -> None:
        self.env.sys.logs_output = b"2026-10-24T06:00:00Z apikey=supersecret\n"
        self.env.call({"op": "logs", "name": "sonarr"})
        self.assertNotIn("supersecret", self.env.log)


class DiskSplit(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()
        m = self.env.media
        self.env.sys.du_output = f"100\t{m}/tv\n200\t{m}/movies\n300\t{m}/downloads\n"

    def tearDown(self) -> None:
        self.env.close()

    def du_calls(self) -> list[list[str]]:
        return [c for c in self.env.sys.calls if c[0].endswith("ionice")]

    def test_computed_under_nice_and_ionice_then_cached_for_a_day(self) -> None:
        outcome, lines = self.env.call({"op": "disk-split"})
        self.assertEqual(outcome, "ok")
        data = lines[0]["data"]
        self.assertEqual(data["sizes"], {"tv": 100, "movies": 200, "downloads": 300})
        self.assertEqual((data["cached"], data["stale"]), (False, False))
        self.assertRegex(data["computed_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        m = self.env.media
        self.assertEqual(self.du_calls(), [[
            "/usr/bin/ionice", "-c", "3", "/usr/bin/nice", "-n", "19", "/usr/bin/du", "--summarize",
            "--one-file-system", "--block-size=1", "--", f"{m}/tv", f"{m}/movies", f"{m}/downloads"]])
        self.env.sys.wall += 86399
        _, lines = self.env.call({"op": "disk-split"})
        self.assertEqual((lines[0]["data"]["cached"], lines[0]["data"]["stale"]), (True, False))
        self.assertEqual(len(self.du_calls()), 1)
        self.env.sys.wall += 2
        _, lines = self.env.call({"op": "disk-split"})
        self.assertFalse(lines[0]["data"]["cached"])
        self.assertEqual(len(self.du_calls()), 2)
        self.assertEqual(self.env.sys.docker_calls(), [])

    def test_never_measures_an_unverified_mount(self) -> None:
        for setup in ("gate", "mount", "sentinel"):
            with self.subTest(setup=setup):
                env = Env()
                try:
                    if setup == "gate":
                        env.set_gate("media-lost")
                    elif setup == "mount":
                        env.sys.mounted = False
                    else:
                        (env.media / ".homelab-media-sentinel").unlink()
                    outcome, _ = env.call({"op": "disk-split"})
                    self.assertEqual(outcome, "media-unavailable")
                    self.assertEqual([c for c in env.sys.calls if c[0].endswith("ionice")], [])
                finally:
                    env.close()

    def test_stale_cache_is_served_while_the_drive_is_offline(self) -> None:
        self.env.call({"op": "disk-split"})
        self.env.sys.wall += 90000
        self.env.set_gate("failed")
        outcome, lines = self.env.call({"op": "disk-split"})
        self.assertEqual(outcome, "ok")
        data = lines[0]["data"]
        self.assertEqual((data["cached"], data["stale"], data["reason"]), (True, True, "gate status failed"))
        self.assertEqual(data["sizes"]["tv"], 100)

    def test_a_concurrent_computation(self) -> None:
        locks = self.env.cache / "locks"
        locks.mkdir(parents=True)
        fd = os.open(locks / "disk-split.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.env.call({"op": "disk-split"})[0], "busy")
            (self.env.cache / "disk-split.json").write_text(json.dumps({"sizes": {"tv": 1}, "computed_epoch": 1}))
            outcome, lines = self.env.call({"op": "disk-split"})
            self.assertEqual(outcome, "ok")
            self.assertEqual((lines[0]["data"]["stale"], lines[0]["data"]["reason"]), (True, "computing"))
        finally:
            os.close(fd)

    def test_a_missing_folder_is_null(self) -> None:
        self.env.sys.du_rc = 1
        self.env.sys.du_output = f"100\t{self.env.media}/tv\n"
        _, lines = self.env.call({"op": "disk-split"})
        self.assertEqual(lines[0]["data"]["sizes"], {"tv": 100, "movies": None, "downloads": None})

    def test_no_result_at_all_is_a_failure(self) -> None:
        self.env.sys.du_rc = 1
        self.env.sys.du_output = ""
        self.assertEqual(self.env.call({"op": "disk-split"})[0], "failed")

    def test_a_corrupt_cache_is_recomputed(self) -> None:
        self.env.cache.mkdir()
        (self.env.cache / "disk-split.json").write_text("{broken")
        _, lines = self.env.call({"op": "disk-split"})
        self.assertFalse(lines[0]["data"]["cached"])


if __name__ == "__main__":
    unittest.main()
