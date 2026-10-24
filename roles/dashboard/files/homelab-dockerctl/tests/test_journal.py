"""last-pull (successful backuppull SSH sessions from the journal) and boots (journalctl --list-boots).
The journal lines mirror what sshd-session logs on the host (OpenSSH 10, Ubuntu 26.04), with
documentation addresses (RFC 5737 / RFC 3849) instead of real ones."""

from __future__ import annotations

import json
import unittest

from fakes import JOURNALCTL, Env, dockerctl

T0 = 1_792_836_000_000_000  # microseconds, 2026-10-24


def entry(us: int, message, ident: str = "sshd-session") -> str:
    return json.dumps({"__REALTIME_TIMESTAMP": str(us), "__CURSOR": "s=x", "MESSAGE": message,
                       "SYSLOG_IDENTIFIER": ident})


def session(start_us: int, port: int, *, addr: str = "192.0.2.20", user: str = "backuppull", clean: bool = True,
            seconds: int = 2) -> list[str]:
    lines = [entry(start_us, f"Accepted publickey for {user} from {addr} port {port} ssh2: ED25519 SHA256:abc")]
    if clean:
        lines.append(entry(start_us + seconds * 1_000_000, f"Disconnected from user {user} {addr} port {port}"))
    return lines


def newest_first(*sessions: list[str]) -> list[str]:
    lines = [line for s in sessions for line in s]
    return sorted(lines, key=lambda line: int(json.loads(line)["__REALTIME_TIMESTAMP"]), reverse=True)


class LastPull(unittest.TestCase):
    def test_last_clean_session_wins(self) -> None:
        lines = newest_first(session(T0, 41702), session(T0 + 3600_000_000, 43108, seconds=5))
        got = dockerctl.parse_last_pull(lines, "backuppull")
        self.assertEqual(got["last_pull"], dockerctl.iso_utc((T0 + 3600_000_000 + 5_000_000) / 1e6))
        self.assertEqual(got["started_at"], dockerctl.iso_utc((T0 + 3600_000_000) / 1e6))
        self.assertEqual(got["duration_seconds"], 5.0)
        self.assertEqual(got["last_attempt"], got["started_at"])

    def test_an_unfinished_newer_session_does_not_count(self) -> None:
        lines = newest_first(session(T0, 41702), session(T0 + 7200_000_000, 50000, clean=False))
        got = dockerctl.parse_last_pull(lines, "backuppull")
        self.assertEqual(got["last_pull"], dockerctl.iso_utc((T0 + 2_000_000) / 1e6))
        self.assertEqual(got["last_attempt"], dockerctl.iso_utc((T0 + 7200_000_000) / 1e6))

    def test_other_users_ports_and_noise_are_ignored(self) -> None:
        lines = newest_first(
            session(T0 + 10_000_000, 2222, user="mediaadmin"),
            [entry(T0 + 9_000_000, "Disconnected from user backuppull 192.0.2.20 port 9999")],  # no matching accept
            session(T0, 41702, addr="2001:db8::20"),
            [entry(T0 + 500, ["binary", "message"]),
             entry(T0 + 600, "pam_unix(sshd:session): session opened for user backuppull(uid=999)")],
        ) + ["not json", json.dumps({"MESSAGE": "no timestamp"})]
        got = dockerctl.parse_last_pull(lines, "backuppull")
        self.assertEqual(got["started_at"], dockerctl.iso_utc(T0 / 1e6))
        self.assertEqual(got["duration_seconds"], 2.0)

    def test_a_user_whose_name_is_a_prefix_does_not_match(self) -> None:
        lines = newest_first(session(T0, 1, user="backuppull2"))
        self.assertIsNone(dockerctl.parse_last_pull(lines, "backuppull")["last_pull"])

    def test_no_pull_yet(self) -> None:
        self.assertEqual(dockerctl.parse_last_pull([], "backuppull"),
                         {"last_pull": None, "started_at": None, "duration_seconds": None, "last_attempt": None})

    def test_the_journal_query(self) -> None:
        env = Env()
        try:
            env.sys.journal_output = "\n".join(newest_first(session(T0, 41702)))
            outcome, lines = env.call({"op": "last-pull"})
            self.assertEqual(outcome, "ok")
            self.assertEqual(lines[0]["data"]["duration_seconds"], 2.0)
            argv = env.sys.calls[-1]
            self.assertEqual(argv[0], JOURNALCTL)
            for want in ("--output=json", "--reverse", "--since=-45d", "SYSLOG_IDENTIFIER=sshd-session",
                         "SYSLOG_IDENTIFIER=sshd", r"--grep=^(Accepted \S+ for|Disconnected from user) backuppull "):
                self.assertIn(want, argv)
            self.assertEqual(env.sys.docker_calls(), [], "last-pull never needs Docker")
        finally:
            env.close()

    def test_grep_without_matches_is_not_an_error(self) -> None:
        env = Env()
        try:
            env.sys.journal_rc = 1
            outcome, lines = env.call({"op": "last-pull"})
            self.assertEqual(outcome, "ok")
            self.assertIsNone(lines[0]["data"]["last_pull"])
            env.sys.journal_rc = 2
            outcome, lines = env.call({"op": "last-pull"})
            self.assertEqual(outcome, "failed")
        finally:
            env.close()


class Boots(unittest.TestCase):
    def test_boot_list(self) -> None:
        env = Env()
        try:
            env.sys.boots_output = json.dumps([
                {"index": -1, "boot_id": "0372e1be0ddc4b8ab472d7db60a1dc55", "first_entry": T0, "last_entry": T0 + 60_000_000},
                {"index": 0, "boot_id": "e0af35b69a764ddd97d943f0ef37be20", "first_entry": T0 + 70_000_000,
                 "last_entry": T0 + 3600_000_000},
                {"index": 1, "boot_id": "not-a-boot-id", "first_entry": 1, "last_entry": 2},
                {"index": 2, "boot_id": "e0af35b69a764ddd97d943f0ef37be21"},
            ])
            outcome, lines = env.call({"op": "boots"})
            self.assertEqual(outcome, "ok")
            boots = lines[0]["data"]["boots"]
            self.assertEqual([b["index"] for b in boots], [-1, 0])
            self.assertEqual(boots[1], {"index": 0, "boot_id": "e0af35b69a764ddd97d943f0ef37be20",
                                        "first_entry": dockerctl.iso_utc((T0 + 70_000_000) / 1e6),
                                        "last_entry": dockerctl.iso_utc((T0 + 3600_000_000) / 1e6)})
            self.assertIn("--list-boots", env.sys.calls[-1])
            self.assertEqual(env.sys.docker_calls(), [])
        finally:
            env.close()

    def test_bad_output_is_a_failure(self) -> None:
        env = Env()
        try:
            env.sys.boots_output = "{nope"
            outcome, _ = env.call({"op": "boots"})
            self.assertEqual(outcome, "failed")
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()
