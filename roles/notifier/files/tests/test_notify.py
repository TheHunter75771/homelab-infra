"""Unit tests for homelab-notify's logic with a stub sender (no network, no pywebpush, no root).

Run from the repo root:  python3 -m unittest discover -s roles/notifier/files/tests -v
"""

from __future__ import annotations

import http.server
import importlib.util
import io
import json
import os
import pathlib
import socket
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("homelab_notify", HERE.parent / "homelab_notify.py")
hn = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hn)


def sub(name, label="phone"):
    return {
        "endpoint": f"https://push.example.invalid/{name}",
        "keys": {"p256dh": "BPk-key", "auth": "auth-secret"},
        "label": label,
        "created": "2026-10-23T00:00:00+00:00",
    }


class StubSender:
    def __init__(self, statuses=None, raise_for=()):
        self.statuses = statuses or {}
        self.raise_for = set(raise_for)
        self.sent: list[tuple[str, dict]] = []

    def __call__(self, subscription, payload):
        name = subscription["endpoint"].rsplit("/", 1)[-1]
        if name in self.raise_for:
            raise ConnectionError("network unreachable")
        self.sent.append((name, json.loads(payload)))
        return self.statuses.get(name, 201)

    @property
    def names(self):
        return [n for n, _ in self.sent]


class RecordingUnsubscriber:
    """Stands in for the dashboard's /api/unsubscribe. status=None models the dashboard being down."""

    def __init__(self, status=200):
        self.status = status
        self.endpoints: list[str] = []

    def __call__(self, endpoint):
        self.endpoints.append(endpoint)
        return self.status


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class NotifyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.push = root / "push"
        self.state = root / "state"
        self.flag = root / "rehearsal"
        self.push.mkdir()
        self.config = root / "notify.json"
        self.config.write_text(
            json.dumps(
                {
                    "push_dir": str(self.push),
                    "state_dir": str(self.state),
                    "rehearsal_flag": str(self.flag),
                    # never the real dashboard on this machine: a port nobody listens on
                    "pushcore_url": f"http://127.0.0.1:{closed_port()}",
                }
            )
        )
        self.env = mock.patch.dict(
            os.environ, {"HOMELAB_NOTIFY_CONFIG": str(self.config), "HOMELAB_NOTIFY_SYSLOG": "0"}
        )
        self.env.start()
        self.err = io.StringIO()
        self.stderr = mock.patch("sys.stderr", self.err)
        self.stderr.start()
        self.unsub = RecordingUnsubscriber()

    def tearDown(self):
        self.stderr.stop()
        self.env.stop()
        self.push.chmod(0o755)
        self.tmp.cleanup()

    def write_subs(self, subs, mode=0o640):
        path = self.push / "subscriptions.json"
        path.write_text(json.dumps(subs))
        path.chmod(mode)
        return path

    def run_cli(self, *argv, sender=None, now=1_000_000.0, unsubscribe=None):
        return hn.main(list(argv), sender=sender or StubSender(), unsubscribe=unsubscribe or self.unsub, now=now)


class Delivery(NotifyCase):
    def test_sends_to_every_subscription(self):
        self.write_subs([sub("a"), sub("b", "tablet")])
        s = StubSender()
        self.assertEqual(self.run_cli("--title", "T", "--body", "B", sender=s), hn.EXIT_OK)
        self.assertEqual(s.names, ["a", "b"])
        self.assertEqual(s.sent[0][1], {"title": "T", "body": "B", "tag": None, "url": "/"})

    def test_no_subscriptions_exit_2(self):
        self.assertEqual(self.run_cli("--title", "T", "--body", "B"), hn.EXIT_NO_SUBSCRIPTIONS)
        self.write_subs([])
        self.assertEqual(self.run_cli("--title", "T", "--body", "B"), hn.EXIT_NO_SUBSCRIPTIONS)

    def test_all_failures_exit_1_and_partial_success_exit_0(self):
        self.write_subs([sub("a"), sub("b")])
        self.assertEqual(
            self.run_cli("--title", "T", "--body", "B", sender=StubSender(raise_for={"a", "b"})), hn.EXIT_ERROR
        )
        self.assertEqual(self.run_cli("--title", "T", "--body", "B", sender=StubSender(raise_for={"a"})), hn.EXIT_OK)
        self.assertEqual(
            self.run_cli("--title", "T", "--body", "B", sender=StubSender(statuses={"a": 500, "b": 429})),
            hn.EXIT_ERROR,
        )

    def test_malformed_and_non_https_entries_are_skipped(self):
        bad = sub("c")
        bad["endpoint"] = "http://127.0.0.1:8085/steal"
        self.write_subs([{"endpoint": 3}, "nope", bad, sub("a")])
        s = StubSender()
        self.assertEqual(self.run_cli("--title", "T", "--body", "B", sender=s), hn.EXIT_OK)
        self.assertEqual(s.names, ["a"])

    def test_missing_vapid_is_an_error_with_the_real_sender(self):
        self.write_subs([sub("a")])
        self.assertEqual(hn.main(["--title", "T", "--body", "B"]), hn.EXIT_ERROR)


class Dedupe(NotifyCase):
    def test_same_tag_and_title_suppressed_for_30_minutes(self):
        self.write_subs([sub("a")])
        s = StubSender()
        args = ("--tag", "bootgate", "--title", "Media drive failed", "--body", "x")
        self.assertEqual(self.run_cli(*args, sender=s, now=1000.0), hn.EXIT_OK)
        self.assertEqual(self.run_cli(*args, sender=s, now=1000.0 + 29 * 60), hn.EXIT_OK)
        self.assertEqual(len(s.sent), 1, "duplicate inside the window was resent")
        self.assertEqual(self.run_cli(*args, sender=s, now=1000.0 + 31 * 60), hn.EXIT_OK)
        self.assertEqual(len(s.sent), 2, "not resent after the window")

    def test_different_title_or_tag_is_not_a_duplicate(self):
        self.write_subs([sub("a")])
        s = StubSender()
        self.run_cli("--tag", "bootgate", "--title", "Media drive lost", "--body", "x", sender=s)
        self.run_cli("--tag", "bootgate", "--title", "Media drive back", "--body", "x", sender=s)
        self.run_cli("--tag", "backup", "--title", "Media drive back", "--body", "x", sender=s)
        self.assertEqual(len(s.sent), 3)

    def test_no_tag_means_no_dedupe(self):
        self.write_subs([sub("a")])
        s = StubSender()
        self.run_cli("--title", "Same", "--body", "x", sender=s)
        self.run_cli("--title", "Same", "--body", "x", sender=s)
        self.assertEqual(len(s.sent), 2)

    def test_failed_delivery_is_not_recorded(self):
        self.write_subs([sub("a")])
        args = ("--tag", "t", "--title", "T", "--body", "x")
        self.assertEqual(self.run_cli(*args, sender=StubSender(raise_for={"a"})), hn.EXIT_ERROR)
        s = StubSender()
        self.assertEqual(self.run_cli(*args, sender=s), hn.EXIT_OK)
        self.assertEqual(len(s.sent), 1, "a failed send must not suppress the retry")

    def test_dedupe_state_is_pruned_and_private(self):
        self.write_subs([sub("a")])
        self.run_cli("--tag", "old", "--title", "T", "--body", "x", now=0.0)
        self.run_cli("--tag", "new", "--title", "T", "--body", "x", now=10_000.0)
        data = json.loads((self.state / "dedupe.json").read_text())
        self.assertEqual(list(data), [hn.dedupe_key("new", "T")])
        self.assertEqual(stat.S_IMODE((self.state / "dedupe.json").stat().st_mode), 0o600)

    def test_pure_helpers(self):
        state = {hn.dedupe_key("t", "T"): 100.0}
        self.assertTrue(hn.is_duplicate(state, hn.dedupe_key("t", "T"), 100.0 + 1799, 1800))
        self.assertFalse(hn.is_duplicate(state, hn.dedupe_key("t", "T"), 100.0 + 1800, 1800))
        self.assertFalse(hn.is_duplicate(state, hn.dedupe_key("t", "T"), 50.0, 1800), "clock went backwards")


class TagClearing(NotifyCase):
    """CONTRACTS §4 (amended): a different title on a tag clears that tag's dedupe entries."""

    MIN = 60.0

    def history_titles(self):
        path = self.state / "history.jsonl"
        return [json.loads(x)["title"] for x in path.read_text().splitlines()] if path.exists() else []

    def send(self, sender, title, tag="bootgate", at=0.0):
        return self.run_cli("--tag", tag, "--title", title, "--body", "b", sender=sender, now=1000.0 + at)

    def test_reviewer_sequence_lost_back_lost_pushes_and_records_the_second_loss(self):
        self.write_subs([sub("a")])
        s = StubSender()
        self.send(s, "Media drive lost", at=0)
        self.send(s, "Media drive back", at=5 * self.MIN)
        self.assertEqual(self.send(s, "Media drive lost", at=10 * self.MIN), hn.EXIT_OK)
        sent = [p["title"] for _, p in s.sent]
        self.assertEqual(sent, ["Media drive lost", "Media drive back", "Media drive lost"])
        self.assertEqual(self.history_titles(), sent, "the dashboard must see the re-loss")
        self.send(s, "Media drive lost", at=12 * self.MIN)  # same title again: still a duplicate
        self.assertEqual(len(s.sent), 3)

    def test_same_title_twice_is_still_suppressed(self):
        self.write_subs([sub("a")])
        s = StubSender()
        self.send(s, "Media drive lost", at=0)
        self.send(s, "Media drive lost", at=10 * self.MIN)
        self.assertEqual((len(s.sent), len(self.history_titles())), (1, 1))

    def test_a_different_tag_does_not_clear_another_tag(self):
        self.write_subs([sub("a")])
        s = StubSender()
        self.send(s, "Media drive lost", tag="bootgate", at=0)
        self.send(s, "sonarr is down", tag="alertfloor:sonarr:down", at=1 * self.MIN)
        self.send(s, "sonarr recovered", tag="alertfloor:sonarr:down", at=2 * self.MIN)
        self.send(s, "Media drive lost", tag="bootgate", at=3 * self.MIN)
        self.assertEqual([p["title"] for _, p in s.sent], ["Media drive lost", "sonarr is down", "sonarr recovered"])
        state = json.loads((self.state / "dedupe.json").read_text())
        self.assertEqual(
            sorted(state),
            sorted(
                [
                    hn.dedupe_key("bootgate", "Media drive lost"),
                    hn.dedupe_key("alertfloor:sonarr:down", "sonarr recovered"),
                ]
            ),
        )

    def test_rehearsal_prefixed_titles(self):
        self.flag.write_text("")
        self.write_subs([sub("drill", "test")])
        s = StubSender()
        self.send(s, "Media drive lost", at=0)
        self.send(s, "Media drive back", at=5 * self.MIN)
        self.send(s, "Media drive lost", at=10 * self.MIN)
        self.send(s, "[REHEARSAL] Media drive lost", at=11 * self.MIN)  # already prefixed: same alert
        sent = [p["title"] for _, p in s.sent]
        self.assertEqual(
            sent, ["[REHEARSAL] Media drive lost", "[REHEARSAL] Media drive back", "[REHEARSAL] Media drive lost"]
        )
        self.assertEqual(self.history_titles(), sent)

    def test_clear_and_new_entry_are_one_atomic_write_with_private_state(self):
        self.write_subs([sub("a")])
        s = StubSender()
        self.send(s, "Media drive lost", at=0)
        with mock.patch.object(hn, "save_dedupe", wraps=hn.save_dedupe) as save:
            self.send(s, "Media drive back", at=5 * self.MIN)
        self.assertEqual(save.call_count, 1, "clear + record must land in a single state write")
        state = json.loads((self.state / "dedupe.json").read_text())
        self.assertEqual(list(state), [hn.dedupe_key("bootgate", "Media drive back")])
        self.assertEqual(stat.S_IMODE((self.state / "dedupe.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.state / "history.jsonl").stat().st_mode), 0o644)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), [".lock", "dedupe.json", "history.jsonl"])

    def test_a_recovery_that_reached_nobody_still_clears(self):
        self.write_subs([sub("a")])
        s = StubSender()
        self.send(s, "Media drive lost", at=0)
        self.write_subs([])
        self.assertEqual(self.send(s, "Media drive back", at=5 * self.MIN), hn.EXIT_NO_SUBSCRIPTIONS)
        self.write_subs([sub("a")])
        self.send(s, "Media drive lost", at=10 * self.MIN)
        self.assertEqual([p["title"] for _, p in s.sent], ["Media drive lost", "Media drive lost"])
        self.assertEqual(self.history_titles(), ["Media drive lost", "Media drive back", "Media drive lost"])

    def test_a_recovery_whose_delivery_failed_still_clears(self):
        self.write_subs([sub("a")])
        self.send(StubSender(), "Media drive lost", at=0)
        self.assertEqual(self.send(StubSender(raise_for={"a"}), "Media drive back", at=5 * self.MIN), hn.EXIT_ERROR)
        s = StubSender()
        self.send(s, "Media drive lost", at=10 * self.MIN)
        self.assertEqual(len(s.sent), 1)

    def test_clear_tag_pure(self):
        state = {hn.dedupe_key("a", "x"): 1.0, hn.dedupe_key("a", "y"): 2.0, hn.dedupe_key("ab", "x"): 3.0}
        self.assertEqual(hn.clear_tag(state, "a", "y"), [hn.dedupe_key("a", "x")])
        self.assertEqual(state, {hn.dedupe_key("a", "y"): 2.0, hn.dedupe_key("ab", "x"): 3.0})
        self.assertEqual(hn.clear_tag(state, "a", "y"), [])


class Rehearsal(NotifyCase):
    def test_rehearsal_prefixes_and_targets_test_label_only(self):
        self.flag.write_text("")
        self.write_subs([sub("phone"), sub("drill", "test")])
        s = StubSender()
        self.assertEqual(self.run_cli("--title", "Media drive failed", "--body", "B", sender=s), hn.EXIT_OK)
        self.assertEqual(s.names, ["drill"])
        self.assertEqual(
            s.sent[0][1], {"title": "[REHEARSAL] Media drive failed", "body": "B", "tag": None, "url": "/"}
        )

    def test_rehearsal_with_no_test_subscription_sends_nothing(self):
        self.flag.write_text("")
        self.write_subs([sub("phone")])
        s = StubSender()
        self.assertEqual(self.run_cli("--title", "T", "--body", "B", sender=s), hn.EXIT_NO_SUBSCRIPTIONS)
        self.assertEqual(s.sent, [])

    def test_test_flag_targets_test_label_without_prefix(self):
        self.write_subs([sub("phone"), sub("drill", "test")])
        s = StubSender()
        self.assertEqual(self.run_cli("--title", "T", "--body", "B", "--test", sender=s), hn.EXIT_OK)
        self.assertEqual(s.names, ["drill"])
        self.assertEqual(s.sent[0][1]["title"], "T")

    def test_prefix_not_doubled(self):
        self.assertEqual(hn.format_title("[REHEARSAL] X", True), "[REHEARSAL] X")
        self.assertEqual(hn.format_title("X", False), "X")


class Pruning(NotifyCase):
    """B2: the dashboard (formerly push-core) is the single writer of subscriptions.json; the
    notifier only asks it."""

    def freeze_push_dir(self, subs):
        path = self.write_subs(subs, mode=0o444)
        self.push.chmod(0o555)  # any write attempt by the notifier would now raise
        st = path.stat()
        return path, (path.read_bytes(), st.st_ino, st.st_mtime_ns, stat.S_IMODE(st.st_mode))

    def snapshot(self, path):
        st = path.stat()
        return path.read_bytes(), st.st_ino, st.st_mtime_ns, stat.S_IMODE(st.st_mode)

    def test_404_and_410_go_to_push_core_others_do_not(self):
        path, before = self.freeze_push_dir([sub("gone"), sub("expired"), sub("busy"), sub("ok")])
        s = StubSender(statuses={"gone": 404, "expired": 410, "busy": 429})
        self.assertEqual(self.run_cli("--title", "T", "--body", "B", sender=s), hn.EXIT_OK)
        self.assertEqual(
            self.unsub.endpoints, ["https://push.example.invalid/expired", "https://push.example.invalid/gone"]
        )
        self.assertEqual(self.snapshot(path), before, "the notifier must never write subscriptions.json")
        self.assertEqual([p.name for p in self.push.iterdir()], ["subscriptions.json"])

    def test_push_core_down_skips_pruning_silently(self):
        path, before = self.freeze_push_dir([sub("gone"), sub("other-gone"), sub("ok")])
        down = RecordingUnsubscriber(status=None)
        s = StubSender(statuses={"gone": 410, "other-gone": 404})
        self.assertEqual(self.run_cli("--title", "T", "--body", "B", sender=s, unsubscribe=down), hn.EXIT_OK)
        self.assertEqual(len(down.endpoints), 1, "stop at the first sign the dashboard is down")
        logged = self.err.getvalue()
        self.assertNotIn("the dashboard refused", logged)
        self.assertNotIn("the dashboard dropped", logged)
        self.assertNotIn(": error:", logged)
        self.assertEqual(self.snapshot(path), before)

    def test_push_core_refusal_is_logged_but_does_not_change_the_exit_code(self):
        self.write_subs([sub("gone"), sub("ok")])
        refused = RecordingUnsubscriber(status=403)
        rc = self.run_cli("--title", "T", "--body", "B", sender=StubSender(statuses={"gone": 410}), unsubscribe=refused)
        self.assertEqual(rc, hn.EXIT_OK)
        self.assertIn("the dashboard refused to unsubscribe a dead subscription (HTTP 403)", self.err.getvalue())

    def test_non_loopback_pushcore_url_is_never_used(self):
        self.assertIsNone(hn.make_pushcore_unsubscriber("http://192.0.2.10:8085", 3))
        self.assertIsNone(hn.make_pushcore_unsubscriber("https://127.0.0.1:8085", 3))
        self.assertIsNotNone(hn.make_pushcore_unsubscriber("http://127.0.0.1:8085", 3))


class _Recorder(http.server.BaseHTTPRequestHandler):
    seen: list = []
    respond = True

    def do_POST(self):  # noqa: N802 - http.server API
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).seen.append((self.command, self.path, dict(self.headers), body))
        if not type(self).respond:
            time.sleep(2)
            return
        payload = b'{"ok": true, "removed": true, "devices": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class PushCoreClient(unittest.TestCase):
    """The real HTTP call, against a local recording server (no dashboard, no network)."""

    def setUp(self):
        _Recorder.seen = []
        _Recorder.respond = True
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_request_matches_push_core_api_contract(self):
        endpoint = "https://push.example.invalid/wpush/v2/abc"
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://192.0.2.1:9", "http_proxy": "http://192.0.2.1:9"}):
            status = hn.make_pushcore_unsubscriber(self.url, 3)(endpoint)
        self.assertEqual(status, 200)
        method, path, headers, body = _Recorder.seen[0]
        self.assertEqual((method, path), ("POST", "/api/unsubscribe"))
        self.assertEqual(headers["X-Homelab"], "1")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn("Sec-Fetch-Site", headers)
        self.assertEqual(json.loads(body), {"endpoint": endpoint})

    def test_closed_port_returns_none_quickly(self):
        started = time.monotonic()
        self.assertIsNone(hn.make_pushcore_unsubscriber(f"http://127.0.0.1:{closed_port()}", 3)("https://x.invalid/1"))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_hanging_push_core_times_out(self):
        _Recorder.respond = False
        started = time.monotonic()
        self.assertIsNone(hn.make_pushcore_unsubscriber(self.url, 0.3)("https://x.invalid/1"))
        self.assertLess(time.monotonic() - started, 1.5)


class LogHygiene(NotifyCase):
    """B5: push endpoints are capability URLs and must never reach the journal."""

    def test_exception_text_with_the_endpoint_is_not_logged(self):
        self.write_subs([sub("SECRET-TOKEN-123")])

        def leaky_sender(subscription, payload):
            raise ConnectionError(
                "HTTPSConnectionPool(host='push.example.invalid', port=443): Max retries exceeded with url: "
                f"{subscription['endpoint']} (Caused by NewConnectionError)"
            )

        self.assertEqual(self.run_cli("--title", "T", "--body", "B", sender=leaky_sender), hn.EXIT_ERROR)
        logged = self.err.getvalue()
        self.assertIn("push to phone failed: ConnectionError", logged)
        self.assertNotIn("SECRET-TOKEN-123", logged)
        self.assertNotIn("push.example.invalid", logged)

    def test_logger_strips_urls_and_control_characters_everywhere(self):
        log = hn.Logger(use_syslog=False)
        log("label is https://evil.invalid/x?y=1\n\x1b[31mred")
        self.assertEqual(log.lines[-1], "info: label is <url> [31mred")

    def test_sanitize_text_matches_push_core(self):
        self.assertEqual(hn.sanitize_text("go http://a.b/c now"), "go <url> now")
        self.assertEqual(hn.sanitize_text("x" * 200), "x" * 160 + "\u2026")
        self.assertEqual(hn.sanitize_text("a\x00\x01b"), "a b")


class Pure(NotifyCase):
    def test_select_targets_pure(self):
        subs = [sub("a"), sub("t", "test")]
        self.assertEqual(len(hn.select_targets(subs, rehearsal=False, test_only=False)), 2)
        self.assertEqual([s["label"] for s in hn.select_targets(subs, rehearsal=True, test_only=False)], ["test"])
        self.assertEqual([s["label"] for s in hn.select_targets(subs, rehearsal=False, test_only=True)], ["test"])


class History(NotifyCase):
    """§11.5: every send is appended to history.jsonl (0644) for the dashboard."""

    DAY = 86400.0

    def history(self):
        path = self.state / "history.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def test_send_is_recorded_with_exactly_the_contract_fields(self):
        self.write_subs([sub("a"), sub("b"), sub("gone"), sub("busy")])
        s = StubSender(statuses={"gone": 410, "busy": 500})
        rc = self.run_cli("--tag", "bootgate", "--title", "Media drive failed", "--body", "held down", sender=s)
        self.assertEqual(rc, hn.EXIT_OK)
        (entry,) = self.history()
        self.assertEqual(
            entry,
            {
                "ts": "1970-01-12T13:46:40+00:00",
                "title": "Media drive failed",
                "body": "held down",
                "tag": "bootgate",
                "delivered": 2,
                "failed": 2,
            },
        )

    def test_history_never_holds_endpoints_or_keys(self):
        self.write_subs([sub("SECRET-ENDPOINT-1"), sub("gone")])
        self.run_cli("--title", "T", "--body", "B", sender=StubSender(statuses={"gone": 404}))
        text = (self.state / "history.jsonl").read_text()
        for needle in ("SECRET-ENDPOINT-1", "push.example.invalid", "https://", "BPk-key", "auth-secret"):
            self.assertNotIn(needle, text)

    def test_dedupe_suppressed_sends_are_not_recorded(self):
        self.write_subs([sub("a")])
        args = ("--tag", "bootgate", "--title", "Media drive lost", "--body", "x")
        self.run_cli(*args, now=1000.0)
        self.run_cli(*args, now=1060.0)  # suppressed duplicate
        self.assertEqual(len(self.history()), 1)

    def test_rehearsal_is_recorded_with_the_title_as_sent(self):
        self.flag.write_text("")
        self.write_subs([sub("drill", "test")])
        self.run_cli("--title", "Media drive failed", "--body", "B")
        self.assertEqual(self.history()[0]["title"], "[REHEARSAL] Media drive failed")

    def test_a_send_that_reaches_nobody_is_recorded(self):
        self.assertEqual(self.run_cli("--title", "Media drive failed", "--body", "B"), hn.EXIT_NO_SUBSCRIPTIONS)
        entry = self.history()[0]
        self.assertEqual(
            (entry["title"], entry["tag"], entry["delivered"], entry["failed"]), ("Media drive failed", None, 0, 0)
        )

    def test_failed_send_is_recorded(self):
        self.write_subs([sub("a"), sub("b")])
        self.assertEqual(hn.main(["--title", "T", "--body", "B"], unsubscribe=self.unsub, now=5.0), hn.EXIT_ERROR)
        self.assertEqual((self.history()[0]["delivered"], self.history()[0]["failed"]), (0, 2))  # no vapid.json

    def test_modes_history_public_dedupe_private_even_with_umask_077(self):
        self.write_subs([sub("a")])
        old = os.umask(0o077)
        try:
            self.run_cli("--tag", "t", "--title", "T", "--body", "B")
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE((self.state / "history.jsonl").stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((self.state / "dedupe.json").stat().st_mode), 0o600)

    def test_entries_older_than_90_days_are_trimmed_on_write(self):
        self.write_subs([sub("a")])
        now = 200 * self.DAY
        self.state.mkdir()
        old = hn.history_entry(now - 91 * self.DAY, "old", "b", tag=None, delivered=1, failed=0)
        recent = hn.history_entry(now - 10 * self.DAY, "recent", "b", tag=None, delivered=1, failed=0)
        (self.state / "history.jsonl").write_text(json.dumps(old) + "\n" + json.dumps(recent) + "\n")
        self.run_cli("--title", "new", "--body", "b", now=now)
        self.assertEqual([e["title"] for e in self.history()], ["recent", "new"])
        self.assertEqual(stat.S_IMODE((self.state / "history.jsonl").stat().st_mode), 0o644)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), [".lock", "history.jsonl"])

    def test_a_normal_append_does_not_rewrite_the_file(self):
        self.write_subs([sub("a")])
        self.run_cli("--title", "one", "--body", "b", now=100 * self.DAY)
        inode = (self.state / "history.jsonl").stat().st_ino
        self.run_cli("--title", "two", "--body", "b", now=100 * self.DAY + 60)
        self.assertEqual((self.state / "history.jsonl").stat().st_ino, inode, "cheap path must only append")
        self.assertEqual([e["title"] for e in self.history()], ["one", "two"])

    def test_size_cap_keeps_the_newest_entries(self):
        cfg = json.loads(self.config.read_text())
        cfg["history_max_bytes"] = 4000
        self.config.write_text(json.dumps(cfg))
        self.write_subs([sub("a")])
        for i in range(60):
            self.run_cli("--title", f"alert {i:02d}", "--body", "x" * 40, now=100 * self.DAY + i)
        size = (self.state / "history.jsonl").stat().st_size
        titles = [e["title"] for e in self.history()]
        self.assertLessEqual(size, 4000)
        self.assertEqual(titles[-1], "alert 59")
        self.assertEqual(titles, sorted(titles), "order kept, oldest dropped first")
        self.assertNotIn("alert 00", titles)

    def test_unwritable_history_does_not_break_the_push(self):
        self.write_subs([sub("a")])
        self.state.mkdir()
        (self.state / "history.jsonl").mkdir()  # cannot be opened for append
        s = StubSender()
        self.assertEqual(self.run_cli("--title", "T", "--body", "B", sender=s), hn.EXIT_OK)
        self.assertEqual(len(s.sent), 1)
        self.assertIn("could not record the send", self.err.getvalue())

    def test_garbage_first_line_is_dropped_on_write(self):
        self.write_subs([sub("a")])
        self.state.mkdir()
        (self.state / "history.jsonl").write_text("not json\n")
        self.run_cli("--title", "T", "--body", "B", now=100 * self.DAY)
        self.assertEqual([e["title"] for e in self.history()], ["T"])


if __name__ == "__main__":
    unittest.main()
