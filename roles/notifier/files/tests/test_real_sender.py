"""The real pywebpush path, with the HTTP POST intercepted (still no network).

Skipped unless pywebpush is importable, e.g. run it with the notifier venv or any venv holding
the pinned version:
  /opt/homelab-notify/bin/python3 -m unittest discover -s roles/notifier/files/tests -v
It proves: a raw base64url VAPID key from vapid.json is accepted; every push service gets its
own VAPID 'aud' (pywebpush mutates the claims dict, so a shared dict would send the first
origin to everyone); TTL/Urgency/encoding headers; the payload decrypts on the device side;
and a 410 from the real exception path is handed to push-core's unsubscribe API.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlparse

try:
    import http_ece
    import requests
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    HAVE_PYWEBPUSH = importlib.util.find_spec("pywebpush") is not None
except ImportError:  # pragma: no cover - depends on the interpreter running the tests
    HAVE_PYWEBPUSH = False

HERE = pathlib.Path(__file__).resolve().parent


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@unittest.skipUnless(HAVE_PYWEBPUSH, "pywebpush not installed in this interpreter")
class RealSender(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("homelab_notify", HERE.parent / "homelab_notify.py")
        self.hn = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hn)
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.push = root / "push"
        self.push.mkdir()
        vapid_key = ec.generate_private_key(ec.SECP256R1())
        self.vapid_pub = vapid_key.public_key()
        (self.push / "vapid.json").write_text(
            json.dumps(
                {
                    "publicKey": b64u(
                        self.vapid_pub.public_bytes(
                            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
                        )
                    ),
                    "privateKey": b64u(vapid_key.private_numbers().private_value.to_bytes(32, "big")),
                    "subject": "mailto:admin@localhost",
                }
            )
        )
        self.devices = {}
        subs = []
        for name, endpoint in (
            ("phone", "https://fcm.googleapis.invalid/fcm/send/abc"),
            ("old-tablet", "https://updates.push.services.mozilla.invalid/wpush/v2/def"),
        ):
            key = ec.generate_private_key(ec.SECP256R1())
            auth = os.urandom(16)
            self.devices[endpoint] = (key, auth)
            pub = key.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
            subs.append({"endpoint": endpoint, "keys": {"p256dh": b64u(pub), "auth": b64u(auth)}, "label": name})
        (self.push / "subscriptions.json").write_text(json.dumps(subs))
        cfg = root / "notify.json"
        cfg.write_text(
            json.dumps({"push_dir": str(self.push), "state_dir": str(root / "state"), "rehearsal_flag": "/nonexistent"})
        )
        self.env = mock.patch.dict(os.environ, {"HOMELAB_NOTIFY_CONFIG": str(cfg), "HOMELAB_NOTIFY_SYSLOG": "0"})
        self.env.start()
        self.devnull = open(os.devnull, "w")  # noqa: SIM115
        self.stderr = mock.patch("sys.stderr", self.devnull)
        self.stderr.start()

    def tearDown(self):
        self.stderr.stop()
        self.devnull.close()
        self.env.stop()
        self.tmp.cleanup()

    def test_real_pywebpush_round_trip(self):
        posts = []

        def fake_post(endpoint, timeout=None, data=None, headers=None, **_):
            posts.append((endpoint, headers, data))
            resp = requests.Response()
            resp.status_code = 410 if "mozilla" in endpoint else 201
            resp._content = b""
            return resp

        asked = []

        def unsubscribe(endpoint):
            asked.append(endpoint)
            return 200

        before = (self.push / "subscriptions.json").read_bytes()
        with mock.patch("requests.post", side_effect=fake_post):
            rc = self.hn.main(
                ["--tag", "bootgate", "--title", "Media drive failed", "--body", "held down"], unsubscribe=unsubscribe
            )
        self.assertEqual(rc, 0)
        self.assertEqual(len(posts), 2)
        for endpoint, headers, data in posts:
            scheme, token = headers["Authorization"].split(" ", 1)
            self.assertEqual(scheme, "vapid")
            jwt = dict(part.strip().split("=", 1) for part in token.split(","))["t"]
            claims = json.loads(b64u_decode(jwt.split(".")[1]))
            url = urlparse(endpoint)
            self.assertEqual(claims["aud"], f"{url.scheme}://{url.netloc}", "VAPID aud leaked across push services")
            self.assertEqual(claims["sub"], "mailto:admin@localhost")
            self.assertEqual(str(headers["TTL"]), "86400")
            self.assertEqual(headers["Urgency"], "high")
            self.assertEqual(headers["Content-Encoding"], "aes128gcm")
            key, auth = self.devices[endpoint]
            plain = http_ece.decrypt(data, private_key=key, auth_secret=auth, version="aes128gcm")
            payload = json.loads(plain)
            self.assertEqual(set(payload), {"title", "body", "tag", "url"})
            self.assertEqual(payload["url"], "/")
            self.assertEqual(
                (payload["title"], payload["body"], payload["tag"]), ("Media drive failed", "held down", "bootgate")
            )
        # 410 came back through the real WebPushException path; push-core is asked to drop it and
        # the notifier itself leaves subscriptions.json untouched (push-core is its only writer).
        self.assertEqual(asked, ["https://updates.push.services.mozilla.invalid/wpush/v2/def"])
        self.assertEqual((self.push / "subscriptions.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
