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
    """Stands in for push-core's /api/unsubscribe. status=None models push-core being down."""

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
                    # never the real push-core on this machine: a port nobody listens on
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
    """B2: push-core is the single writer of subscriptions.json; the notifier only asks it."""

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
        self.assertEqual(len(down.endpoints), 1, "stop at the first sign push-core is down")
        logged = self.err.getvalue()
        self.assertNotIn("push-core refused", logged)
        self.assertNotIn("push-core dropped", logged)
        self.assertNotIn(": error:", logged)
        self.assertEqual(self.snapshot(path), before)

    def test_push_core_refusal_is_logged_but_does_not_change_the_exit_code(self):
        self.write_subs([sub("gone"), sub("ok")])
        refused = RecordingUnsubscriber(status=403)
        rc = self.run_cli("--title", "T", "--body", "B", sender=StubSender(statuses={"gone": 410}), unsubscribe=refused)
        self.assertEqual(rc, hn.EXIT_OK)
        self.assertIn("push-core refused to unsubscribe a dead subscription (HTTP 403)", self.err.getvalue())

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
    """The real HTTP call, against a local recording server (no push-core, no network)."""

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


if __name__ == "__main__":
    unittest.main()
