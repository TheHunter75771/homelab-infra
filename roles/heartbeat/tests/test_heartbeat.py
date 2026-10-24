#!/usr/bin/env python3
"""Tests for files/homelab_heartbeat.py: the 3-strike state machine, the log format
(Contract 8), push fan-out, and (when pywebpush is importable) a real Web Push round trip
against a local fake push service. Ping, TCP and push are stubbed; no network.

  python3 -m unittest discover -s roles/heartbeat/tests -v
  # with pywebpush (the integration test):  <venv>/bin/python -m unittest discover -s roles/heartbeat/tests
"""
import base64
import importlib.util
import json
import os
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("hb", os.path.join(HERE, "..", "files", "homelab_heartbeat.py"))
hb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hb)
if not os.environ.get("HEARTBEAT_TEST_VERBOSE"):
    hb.log = lambda msg: None  # the script's journal lines; set HEARTBEAT_TEST_VERBOSE=1 to see them

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)  # 08:00 EDT
CFG = dict(hb.CONFIG_DEFAULTS, target="box.example", name="mediaserver", display_tz="America/New_York")


class Sender:
    """Stub for step()'s send(title, body): records calls, returns queued results (default True)."""

    def __init__(self, *results):
        self.sent, self.results = [], list(results)

    def __call__(self, title, body):
        self.sent.append((title, body))
        return self.results.pop(0) if self.results else True

    @property
    def titles(self):
        return [t for t, _ in self.sent]


def run_seq(checks, send=None, state=None, start=T0):
    send = send if send is not None else Sender()
    s = state or {}
    for i, ok in enumerate(checks):
        s = hb.step(s, ok, start + timedelta(minutes=2 * i), CFG, send)
    return s, send


F, U = False, True


class ParsePong(unittest.TestCase):
    def test_derp(self):
        out = "pong from mediaserver (100.64.0.10) via DERP(mia) in 96ms\n"
        self.assertEqual(hb.parse_pong(out), ("100.64.0.10", 96.0))

    def test_direct(self):
        out = "pong from mediaserver (100.64.0.10) via 203.0.113.7:41641 in 23ms\n"
        self.assertEqual(hb.parse_pong(out), ("100.64.0.10", 23.0))

    def test_direct_ipv6_endpoint(self):
        out = "pong from mediaserver (100.64.0.10) via [2001:db8::1]:41641 in 2ms\n"
        self.assertEqual(hb.parse_pong(out), ("100.64.0.10", 2.0))

    def test_units(self):
        self.assertEqual(hb.parse_pong("pong from a (100.1.2.3) via DERP(x) in 1.234s")[1], 1234.0)
        self.assertEqual(hb.parse_pong("pong from a (100.1.2.3) via DERP(x) in 850µs")[1], 0.85)
        self.assertEqual(hb.parse_pong("pong from a (100.1.2.3) via DERP(x) in 0s")[1], 0.0)

    def test_ipv6_and_peerapi_port(self):
        self.assertEqual(hb.parse_pong("pong from a (fd7a:115c:a1e0::1) via DERP(x) in 5ms")[0], "fd7a:115c:a1e0::1")
        self.assertEqual(hb.parse_pong("pong from a (100.1.2.3, 34567) via DERP(x) in 5ms"), ("100.1.2.3", 5.0))

    def test_no_pong(self):
        self.assertIsNone(hb.parse_pong('ping "100.1.2.3" timed out\nno reply\n'))
        self.assertIsNone(hb.parse_pong(""))


class TailscalePing(unittest.TestCase):
    def fake_run(self, rc=0, out="", exc=None):
        calls = []

        def run(cmd, **kw):
            calls.append((cmd, kw))
            if exc:
                raise exc
            return subprocess.CompletedProcess(cmd, rc, out, "")

        return run, calls

    def test_command_accepts_derp_replies(self):
        run, calls = self.fake_run(out="pong from m (100.1.2.3) via DERP(mia) in 70ms\n")
        self.assertEqual(hb.tailscale_ping("box.example", 5, run=run), ("100.1.2.3", 70.0))
        cmd = calls[0][0]
        self.assertEqual(cmd[:2], ["tailscale", "ping"])
        for part in (["-c", "1"], ["--timeout", "5s"]):
            self.assertIn(part, [cmd[i : i + 2] for i in range(len(cmd) - 1)])
        self.assertIn("--until-direct=false", cmd)  # without it a DERP-only path exits 1
        self.assertEqual(cmd[-1], "box.example")
        self.assertGreater(calls[0][1]["timeout"], 5)

    def test_failures_are_none(self):
        run, _ = self.fake_run(rc=1, out="no reply")
        self.assertIsNone(hb.tailscale_ping("x", 5, run=run))
        run, _ = self.fake_run(exc=subprocess.TimeoutExpired("tailscale", 20))
        self.assertIsNone(hb.tailscale_ping("x", 5, run=run))
        run, _ = self.fake_run(exc=FileNotFoundError("tailscale"))
        self.assertIsNone(hb.tailscale_ping("x", 5, run=run))


class Probe(unittest.TestCase):
    def test_ping_fails_skips_tcp(self):
        tcp_calls = []
        ok = hb.probe(CFG, ping=lambda t, s: None, tcp=lambda *a: tcp_calls.append(a) or True)
        self.assertEqual(ok, (False, None))
        self.assertEqual(tcp_calls, [])

    def test_tcp_must_answer_on_the_pinged_address(self):
        seen = []
        res = hb.probe(CFG, ping=lambda t, s: ("100.1.2.3", 42.0), tcp=lambda h, p, s: seen.append((h, p)) or False)
        self.assertEqual(res, (False, None))
        self.assertEqual(seen, [("100.1.2.3", 22)])

    def test_up(self):
        self.assertEqual(hb.probe(CFG, ping=lambda t, s: ("100.1.2.3", 42.0), tcp=lambda *a: True), (True, 42.0))

    def test_tcp_open_real_socket(self):
        import socket

        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        self.assertTrue(hb.tcp_open("127.0.0.1", port, 2))
        srv.close()
        self.assertFalse(hb.tcp_open("127.0.0.1", port, 2))


class StateMachine(unittest.TestCase):
    def test_two_failures_do_not_alert(self):
        s, send = run_seq([F, F])
        self.assertEqual(send.sent, [])
        self.assertEqual(s["fails"], 2)
        self.assertFalse(s["alerted"])

    def test_third_failure_alerts_exactly_once(self):
        s, send = run_seq([F, F, F, F, F, F])
        self.assertEqual(send.titles, ["mediaserver offline"])
        self.assertTrue(s["alerted"])
        self.assertEqual(s["fails"], 6)
        self.assertEqual(s["down_since"], T0.isoformat(timespec="seconds"))

    def test_down_body_uses_local_time_of_first_failure(self):
        _, send = run_seq([F, F, F])
        self.assertIn("since 08:00 EDT", send.sent[0][1])
        self.assertIn("3 checks in a row", send.sent[0][1])

    def test_recovery_pushes_once_with_duration(self):
        s, send = run_seq([F, F, F, F, U, U, U])
        self.assertEqual(send.titles, ["mediaserver offline", "mediaserver back online"])
        self.assertIn("after 8 min offline", send.sent[1][1])  # first failure T0, first success T0+8m
        self.assertEqual((s["fails"], s["alerted"], s["recovery_pending"], s["down_since"]), (0, False, False, None))

    def test_blips_never_alert(self):
        _, send = run_seq([F, F, U, F, F, U, F, U])
        self.assertEqual(send.sent, [])

    def test_undelivered_offline_push_is_retried(self):
        s, send = run_seq([F, F, F, F, F], send=Sender(False, True))
        self.assertEqual(send.titles, ["mediaserver offline", "mediaserver offline"])
        self.assertTrue(s["alerted"])

    def test_undelivered_recovery_push_is_retried(self):
        s, send = run_seq([F, F, F, U, U, U], send=Sender(True, False, True))
        self.assertEqual(send.titles, ["mediaserver offline", "mediaserver back online", "mediaserver back online"])
        self.assertFalse(s["recovery_pending"])

    def test_new_outage_supersedes_pending_recovery(self):
        s, send = run_seq([F, F, F, U, F, F, F], send=Sender(True, False))
        self.assertEqual(send.titles, ["mediaserver offline", "mediaserver back online", "mediaserver offline"])
        self.assertTrue(s["alerted"])
        self.assertFalse(s["recovery_pending"])

    def test_no_recovery_push_without_an_offline_push(self):
        _, send = run_seq([F, F, U])
        self.assertEqual(send.sent, [])

    def test_unknown_state_keys_and_missing_keys(self):
        s = hb.step({"fails": 2, "junk": 1}, False, T0, CFG, Sender())
        self.assertEqual(s["fails"], 3)
        self.assertNotIn("junk", s)


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.cfg = dict(
            CFG,
            state_dir=d,
            vapid_file=os.path.join(d, "vapid.json"),
            subscriptions_file=os.path.join(d, "subscriptions.json"),
        )
        self.write("vapid.json", {"publicKey": "pub", "privateKey": "priv", "subject": "mailto:admin@localhost"})
        self.write(
            "subscriptions.json",
            [
                {"endpoint": "https://push.one/a", "keys": {"p256dh": "k1", "auth": "a1"}, "label": "phone", "created": "x"},
                {"endpoint": "https://push.two/b", "keys": {"p256dh": "k2", "auth": "a2"}, "label": "test", "created": "x"},
            ],
        )
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, obj):
        with open(os.path.join(self.tmp.name, name), "w") as f:
            json.dump(obj, f)

    def webpush(self, fail_endpoints=()):
        def push(subscription_info, data, vapid_private_key, vapid_claims, **kw):
            self.calls.append(
                {"sub": subscription_info, "data": json.loads(data), "key": vapid_private_key, "claims": dict(vapid_claims), **kw}
            )
            vapid_claims["aud"] = subscription_info["endpoint"]  # pywebpush mutates the dict it is given
            if subscription_info["endpoint"] in fail_endpoints:
                raise RuntimeError("410 Gone")

        return push

    def log_lines(self):
        with open(os.path.join(self.tmp.name, "log.jsonl")) as f:
            return [json.loads(line) for line in f]

    def check(self, ok, n=1, **kw):
        ping = (lambda t, s: ("100.1.2.3", 12.5)) if ok else (lambda t, s: None)
        for i in range(n):
            hb.run_check(self.cfg, now=kw.get("now", T0) + timedelta(minutes=2 * i), ping=ping, tcp=lambda *a: True,
                         webpush=self.webpush(kw.get("fail", ())))

    def test_log_has_exactly_contract_8_keys(self):
        self.check(True)
        self.check(False)
        lines = self.log_lines()
        self.assertEqual([set(x) for x in lines], [{"ts", "state", "rtt_ms"}] * 2)
        self.assertEqual(lines[0], {"ts": "2026-10-23T12:00:00+00:00", "state": "up", "rtt_ms": 12.5})
        self.assertEqual(lines[1]["state"], "down")
        self.assertIsNone(lines[1]["rtt_ms"])
        datetime.fromisoformat(lines[0]["ts"])

    def test_state_persists_across_runs_and_alerts_once(self):
        self.check(False, n=5)  # five separate timer runs
        self.assertEqual(len(self.calls), 2)  # one "offline" push, fanned out to both devices
        self.assertEqual({c["data"]["title"] for c in self.calls}, {"mediaserver offline"})
        self.check(True, now=T0 + timedelta(minutes=10))
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.calls[-1]["data"]["title"], "mediaserver back online")
        with open(os.path.join(self.tmp.name, "state.json")) as f:
            self.assertEqual(json.load(f)["fails"], 0)
        self.assertEqual(oct(os.stat(os.path.join(self.tmp.name, "state.json")).st_mode & 0o777), "0o600")

    def test_payload_matches_the_notifier_contract(self):
        self.check(False, n=3)
        c = self.calls[0]
        self.assertEqual(set(c["data"]), {"title", "body", "tag", "url"})
        self.assertEqual((c["data"]["tag"], c["data"]["url"]), ("heartbeat", "/"))
        self.assertEqual(c["key"], "priv")
        self.assertEqual(c["ttl"], 86400)
        self.assertEqual(c["headers"], {"Urgency": "high"})

    def test_each_device_gets_fresh_vapid_claims(self):
        self.check(False, n=3)
        self.assertEqual([c["claims"] for c in self.calls], [{"sub": "mailto:admin@localhost"}] * 2)

    def test_one_dead_device_still_counts_as_delivered(self):
        self.check(False, n=4, fail={"https://push.one/a"})
        self.assertEqual(len(self.calls), 2)  # delivered to push.two on the 3rd check: no retry on the 4th

    def test_push_failures_never_log_the_endpoint(self):
        lines = []
        orig, hb.log = hb.log, lines.append
        try:
            def push(subscription_info, **kw):
                raise RuntimeError(f"410 Gone for url: {subscription_info['endpoint']}")

            self.assertEqual(hb.send_push(self.cfg, "t", "b", webpush=push), hb.FAILED)
        finally:
            hb.log = orig
        text = "\n".join(lines)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("push.one", text)
        self.assertNotIn("push.two", text)

    def test_all_devices_failing_retries_next_check(self):
        self.check(False, n=4, fail={"https://push.one/a", "https://push.two/b"})
        self.assertEqual(len(self.calls), 4)  # 3rd and 4th checks both tried both devices

    def test_rehearsal_sends_only_to_test_devices_with_prefix(self):
        self.cfg["rehearsal"] = True
        self.check(False, n=3)
        self.assertEqual([c["sub"]["endpoint"] for c in self.calls], ["https://push.two/b"])
        self.assertEqual(self.calls[0]["data"]["title"], "[REHEARSAL] mediaserver offline")

    def test_no_devices_is_settled_not_retried(self):
        self.write("subscriptions.json", [])
        self.check(False, n=5)
        self.assertEqual(self.calls, [])
        with open(os.path.join(self.tmp.name, "state.json")) as f:
            self.assertTrue(json.load(f)["alerted"])

    def test_missing_push_files_is_a_retryable_failure(self):
        os.unlink(self.cfg["vapid_file"])
        self.assertEqual(hb.send_push(self.cfg, "t", "b", webpush=self.webpush()), hb.FAILED)

    def test_corrupt_state_starts_fresh(self):
        with open(os.path.join(self.tmp.name, "state.json"), "w") as f:
            f.write("{not json")
        self.check(False)
        with open(os.path.join(self.tmp.name, "state.json")) as f:
            self.assertEqual(json.load(f)["fails"], 1)

    def test_log_is_trimmed(self):
        self.cfg.update(log_keep_lines=5, log_max_bytes=400)
        self.check(True, n=30)
        lines = self.log_lines()
        self.assertLessEqual(len(lines), 10)
        self.assertEqual(lines[-1]["ts"], (T0 + timedelta(minutes=58)).isoformat(timespec="seconds"))
        self.assertEqual(oct(os.stat(os.path.join(self.tmp.name, "log.jsonl")).st_mode & 0o777), "0o644")

    def test_main_test_push_exit_codes(self):
        path = os.path.join(self.tmp.name, "config.json")
        with open(path, "w") as f:
            json.dump(dict(self.cfg, subscriptions_file=os.path.join(self.tmp.name, "none.json")), f)
        self.assertEqual(hb.main(["--config", path, "--test-push"]), 1)  # missing subscriptions file
        self.write("none.json", [])
        self.assertEqual(hb.main(["--config", path, "--test-push"]), 2)  # nobody subscribed

    def test_config_requires_target(self):
        path = os.path.join(self.tmp.name, "bad.json")
        with open(path, "w") as f:
            json.dump({"name": "x"}, f)
        with self.assertRaises(ValueError):
            hb.load_config(path)


try:
    import http_ece
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    import pywebpush  # noqa: F401

    HAVE_PYWEBPUSH = True
except ImportError:
    HAVE_PYWEBPUSH = False


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64u_dec(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@unittest.skipUnless(HAVE_PYWEBPUSH, "pywebpush not importable (run with the heartbeat venv's python)")
class RealWebPush(unittest.TestCase):
    """send_push through real pywebpush to two local fake push services: the encrypted payload
    decrypts to the contract JSON and each service sees a VAPID token for its own origin."""

    def start_service(self):
        received = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append((dict(self.headers), body))
                self.send_response(201)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}", received

    def test_round_trip(self):
        vapid_key = ec.generate_private_key(ec.SECP256R1())
        vapid_raw = vapid_key.private_numbers().private_value.to_bytes(32, "big")
        vapid_pub = vapid_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        devices = []
        for _ in range(2):
            origin, received = self.start_service()
            k = ec.generate_private_key(ec.SECP256R1())
            auth = os.urandom(16)
            pub = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
            devices.append((origin, received, k, auth, {"endpoint": f"{origin}/push/x", "keys": {"p256dh": b64u(pub), "auth": b64u(auth)}, "label": "phone"}))
        with tempfile.TemporaryDirectory() as d:
            cfg = dict(CFG, vapid_file=os.path.join(d, "v.json"), subscriptions_file=os.path.join(d, "s.json"))
            with open(cfg["vapid_file"], "w") as f:
                json.dump({"publicKey": b64u(vapid_pub), "privateKey": b64u(vapid_raw), "subject": "mailto:admin@localhost"}, f)
            with open(cfg["subscriptions_file"], "w") as f:
                json.dump([dev[4] for dev in devices], f)
            self.assertEqual(hb.send_push(cfg, "mediaserver offline", "No reply."), hb.DELIVERED)
        for origin, received, k, auth, _ in devices:
            self.assertEqual(len(received), 1)
            headers, body = received[0]
            h = {k.lower(): v for k, v in headers.items()}
            self.assertEqual(h["content-encoding"], "aes128gcm")
            self.assertEqual(h["ttl"], "86400")
            self.assertEqual(h["urgency"], "high")
            plain = http_ece.decrypt(body, private_key=k, auth_secret=auth, version="aes128gcm")
            self.assertEqual(json.loads(plain), {"title": "mediaserver offline", "body": "No reply.", "tag": "heartbeat", "url": "/"})
            scheme, _, params = h["authorization"].partition(" ")
            self.assertEqual(scheme, "vapid")
            fields = dict(p.strip().split("=", 1) for p in params.split(","))
            self.assertEqual(fields["k"], b64u(vapid_pub))
            claims = json.loads(b64u_dec(fields["t"].split(".")[1]))
            self.assertEqual(claims["aud"], origin)  # per-service audience: claims dict not reused
            self.assertEqual(claims["sub"], "mailto:admin@localhost")


if __name__ == "__main__":
    unittest.main()
