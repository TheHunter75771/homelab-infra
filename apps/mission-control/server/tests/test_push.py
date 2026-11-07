"""Web Push: VAPID loading, subscription validation, pruning (404/410 only), atomic writes and the lock, and a
real pywebpush round trip to a fake push service (when pywebpush is importable)."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from mission_control import util
from mission_control.config import from_dict
from mission_control.push import (
    PRUNE_STATUSES,
    PushError,
    SubscriptionStore,
    load_vapid,
    make_pywebpush_sender,
    send_all,
)

from . import fakes
from .harness import Harness

try:
    import pywebpush  # noqa: F401

    HAVE_PYWEBPUSH = True
except ImportError:
    HAVE_PYWEBPUSH = False


def sub(i: int) -> dict:
    return {"endpoint": f"https://fcm.googleapis.com/fcm/send/{i}", "keys": {"p256dh": "k", "auth": "a"}, "label": f"d{i}"}


class StoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "subscriptions.json")
        self.store = SubscriptionStore(self.path, 50)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_prune_only_404_and_410(self) -> None:
        sender = fakes.RecordingSender()
        statuses = {1: 201, 2: 404, 3: 410, 4: 500, 5: 429, 6: 400, 7: 413, 8: 202}
        for i, status in statuses.items():
            self.store.add(sub(i))
            sender.status[sub(i)["endpoint"]] = status
        self.store.add(sub(9))
        sender.raise_for.add(sub(9)["endpoint"])
        result = await send_all(self.store, sender, {"title": "t", "body": "b"})
        self.assertEqual(result, {"devices": 9, "delivered": 2, "failed": 5, "pruned": 2})
        left = {s["label"] for s in self.store.all()}
        self.assertEqual(left, {"d1", "d4", "d5", "d6", "d7", "d8", "d9"})
        self.assertEqual(PRUNE_STATUSES, frozenset({404, 410}))

    async def test_only_one_device(self) -> None:
        self.store.add(sub(1))
        self.store.add(sub(2))
        sender = fakes.RecordingSender()
        result = await send_all(self.store, sender, {"title": "t"}, only=sub(2)["endpoint"])
        self.assertEqual(result["devices"], 1)
        self.assertEqual([c[0] for c in sender.calls], [sub(2)["endpoint"]])

    def test_corrupt_file_is_not_a_crash(self) -> None:
        Path(self.path).write_text("{not json")
        self.assertEqual(self.store.all(), [])
        Path(self.path).write_text('{"a": 1}')
        self.assertEqual(self.store.all(), [])

    def test_concurrent_adds_under_the_lock(self) -> None:
        stores = [SubscriptionStore(self.path, 100) for _ in range(8)]
        threads = [threading.Thread(target=lambda s=s, i=i: [s.add(sub(i * 10 + j)) for j in range(5)])
                   for i, s in enumerate(stores)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.store.all()), 40)


class AtomicWriteTests(unittest.TestCase):
    def test_failure_keeps_the_old_file_and_no_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "subscriptions.json")
            util.write_json_atomic(path, [{"endpoint": "old"}])
            for where in ("os.replace", "os.fsync", "os.write"):
                with mock.patch(where, side_effect=OSError("disk full")), self.assertRaises(OSError):
                    util.write_json_atomic(path, [{"endpoint": "new"}])
                self.assertEqual(json.loads(Path(path).read_text()), [{"endpoint": "old"}], where)
                self.assertEqual(sorted(os.listdir(d)), ["subscriptions.json"], where)

    def test_mode_and_replace(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "alerts.json")
            Path(path).write_text("old")
            os.chmod(path, 0o666)
            util.write_json_atomic(path, {"a": 1}, 0o600)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(Path(path).read_text()), {"a": 1})

    def test_no_nan(self) -> None:
        with tempfile.TemporaryDirectory() as d, self.assertRaises(ValueError):
            util.write_json_atomic(os.path.join(d, "x.json"), {"a": float("nan")})


class VapidTests(unittest.TestCase):
    def test_load(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "vapid.json"
            v = fakes.write_vapid(p)
            self.assertEqual(load_vapid(str(p))["publicKey"], v["publicKey"])
            for bad in ({**v, "privateKey": "short"}, {**v, "publicKey": "A" * 87}, {**v, "subject": "noah"}, [], "x"):
                p.write_text(json.dumps(bad))
                with self.assertRaises(PushError):
                    load_vapid(str(p))
            p.write_text("{")
            with self.assertRaises(PushError) as cm:
                load_vapid(str(p))
            self.assertNotIn(v["privateKey"], str(cm.exception))


@unittest.skipUnless(HAVE_PYWEBPUSH, "pywebpush not installed")
class RealPywebpushTests(Harness):
    """The real sender: pywebpush encrypts with the subscription's keys and signs with the VAPID key; the fake
    push service answers 201, 410 or 500."""

    start_server = False

    async def test_round_trip_and_prune(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        push = fakes.FakePushService()
        await push.start()
        self.fakes.append(push)
        push.status = {"gone": 410, "broken": 500}
        store = SubscriptionStore(str(self.state_dir / "subscriptions.json"))
        for name in ("ok", "gone", "broken"):
            key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
            enc = base64.urlsafe_b64encode(key).rstrip(b"=").decode()
            store.add({"endpoint": f"{push.url}/{name}", "keys": {"p256dh": enc, "auth": fakes.b64(16)}, "label": name})
        cfg = from_dict({**self.cfg_raw, "push_timeout_s": 5})
        sender = make_pywebpush_sender(load_vapid(cfg.vapid_file), cfg)
        result = await send_all(store, sender, {"title": "Mission Control", "body": "Briefing ready: 1 deadline"})
        self.assertEqual(result, {"devices": 3, "delivered": 1, "failed": 1, "pruned": 1})
        self.assertEqual(sorted(s["label"] for s in store.all()), ["broken", "ok"])
        _key, headers, body = push.bodies[0]
        self.assertTrue(headers["Authorization"].startswith("vapid t="))
        self.assertEqual(headers["Content-Encoding"], "aes128gcm")
        self.assertEqual(headers["TTL"], str(cfg.push_ttl_s))
        self.assertNotIn(b"Briefing", body, "the payload is encrypted")


if __name__ == "__main__":
    unittest.main()
