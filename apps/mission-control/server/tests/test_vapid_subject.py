"""The VAPID subject: load_vapid applies py_vapid's own rule (an https subject is a host only, no port or path;
or mailto:), so a subject py_vapid won't sign with is refused at load and by check-config, and a signing failure
is logged as "VAPID config invalid" (live bug 2026-10-28: subject https://ai-box.<tailnet>.ts.net:8443)."""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from mission_control import push
from mission_control.__main__ import check_config
from mission_control.push import SUBJECT_ERROR, PushError, SubscriptionStore, load_vapid, send_all

from . import fakes
from .harness import Harness, LogCapture

try:
    import py_vapid
    import pywebpush  # noqa: F401

    HAVE_PYWEBPUSH = True
except ImportError:
    HAVE_PYWEBPUSH = False

PORT = "https://ai-box.example-tailnet.ts.net:8443"
BAD = [PORT, "https://ai-box.example-tailnet.ts.net/", "https://example.com/path", "https://example.com:443",
       "http://example.com", "https://", "mailto:noah", "mailto: noah@example.com", "https://exa mple.com",
       "noah@example.com", ""]
GOOD = ["https://ai-box.example-tailnet.ts.net", "https://example.com", "mailto:noreply@example.com",
        "MAILTO:noah@example.com"]


class MirroredRule(unittest.TestCase):
    def test_static_rule(self) -> None:
        for s in BAD:
            self.assertFalse(push.subject_matches(s), s)
        for s in GOOD:
            self.assertTrue(push.subject_matches(s), s)

    @unittest.skipUnless(HAVE_PYWEBPUSH, "py_vapid not installed")
    def test_copy_is_exact(self) -> None:
        """Guards an upgrade: the mirrored pattern must stay byte-identical to the installed py_vapid's."""
        src = Path(py_vapid.__file__).read_text()
        m = re.search(r'pattern = r"(.*)"  # noqa', src)
        self.assertIsNotNone(m, "py_vapid's _check_sub changed shape: re-check push._PY_VAPID_SUB_RE")
        self.assertEqual(push._PY_VAPID_SUB_RE.pattern, m.group(1))

    @unittest.skipUnless(HAVE_PYWEBPUSH, "py_vapid not installed")
    def test_rule_never_looser_than_py_vapid(self) -> None:
        # ours = py_vapid's pattern + "no whitespace", so it may refuse more ("mailto: noah@…"), never less
        for s in BAD + GOOD:
            if not push.subject_signs(s):
                self.assertFalse(push.subject_matches(s), s)
        for s in GOOD:
            self.assertTrue(push.subject_signs(s), s)


class LoadVapid(Harness):
    start_server = False

    def write(self, subject: str) -> str:
        path = self.dir / "vapid.json"
        v = json.loads(path.read_text())
        path.write_text(json.dumps({**v, "subject": subject}))
        return str(path)

    def test_port_refused_at_load_and_by_check_config(self) -> None:
        with self.assertRaises(PushError) as cm:
            load_vapid(self.write(PORT))
        self.assertEqual(str(cm.exception), SUBJECT_ERROR)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(check_config(self.config(), "c.json", out=io.StringIO()), 2)
        self.assertIn("VAPID subject must be https://host (no port or path) or mailto:", err.getvalue())

    def test_host_only_and_mailto_load(self) -> None:
        for s in GOOD:
            self.assertEqual(load_vapid(self.write(s))["subject"], s)
        self.assertEqual(check_config(self.config(), "c.json", out=io.StringIO()), 0)

    @unittest.skipUnless(HAVE_PYWEBPUSH, "py_vapid not installed")
    def test_py_vapid_is_asked_even_if_the_mirror_were_loose(self) -> None:
        with mock.patch.object(push, "_PY_VAPID_SUB_RE", re.compile(r".*")):
            with self.assertRaises(PushError):
                load_vapid(self.write(PORT))
            load_vapid(self.write("https://ai-box.example-tailnet.ts.net"))


@unittest.skipUnless(HAVE_PYWEBPUSH, "pywebpush not installed")
class RealSigning(Harness):
    start_server = False

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.svc = fakes.FakePushService()
        await self.svc.start()
        self.fakes.append(self.svc)
        self.store = SubscriptionStore(str(self.state_dir / "subscriptions.json"))
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        p256 = __import__("base64").urlsafe_b64encode(key).rstrip(b"=").decode()
        self.store.add({"endpoint": f"{self.svc.url}/dev", "keys": {"p256dh": p256, "auth": fakes.b64(16)},
                        "label": "iPhone"})

    async def test_subject_without_port_signs(self) -> None:
        vapid = {**self.vapid, "subject": "https://ai-box.example-tailnet.ts.net"}
        sender = push.make_pywebpush_sender(vapid, self.config())
        result = await send_all(self.store, sender, {"title": "t", "body": "b"})
        self.assertEqual(result["delivered"], 1)
        _k, headers, _b = self.svc.bodies[0]
        self.assertTrue(headers["Authorization"].startswith("vapid t="))

    async def test_subject_with_port_is_logged_as_config_error(self) -> None:
        vapid = {**self.vapid, "subject": PORT}  # bypassing load_vapid, as a file edited after start would
        sender = push.make_pywebpush_sender(vapid, self.config())
        with LogCapture() as logs:
            result = await send_all(self.store, sender, {"title": "t", "body": "b"})
        self.assertEqual((result["delivered"], result["failed"], result["pruned"]), (0, 1, 0))
        self.assertIn("push to iPhone failed: VAPID config invalid", logs.text)
        self.assertNotIn("no HTTP answer", logs.text)
        self.assertEqual(self.svc.bodies, [])
        self.assertEqual(len(self.store.all()), 1, "a config error never prunes a device")


class BrokenPyVapid(Harness):
    """Z6-1: a py_vapid check that breaks (not a VapidException) falls back to the mirrored pattern."""

    start_server = False

    def write(self, subject: str) -> str:
        path = self.dir / "vapid.json"
        v = json.loads(path.read_text())
        path.write_text(json.dumps({**v, "subject": subject}))
        return str(path)

    @unittest.skipUnless(HAVE_PYWEBPUSH, "py_vapid not installed")
    def test_sign_raising_something_else(self) -> None:
        with mock.patch.object(py_vapid.Vapid, "sign", side_effect=RuntimeError("backend exploded")), LogCapture() as logs:
            self.assertIsNone(push.subject_signs("https://ai-box.example-tailnet.ts.net"))
            self.assertEqual(load_vapid(self.write("https://ai-box.example-tailnet.ts.net"))["subject"],
                             "https://ai-box.example-tailnet.ts.net")
            with self.assertRaises(PushError):
                load_vapid(self.write(PORT))  # the mirror still guards
            self.assertEqual(check_config(self.config(), "c.json", out=io.StringIO()), 2)  # PORT is on file
            self.write("mailto:noreply@example.com")
            self.assertEqual(check_config(self.config(), "c.json", out=io.StringIO()), 0)
        self.assertIn("py_vapid check failed (RuntimeError)", logs.text)
        self.assertNotIn(self.vapid["privateKey"], logs.text)
        self.assertNotIn("backend exploded", logs.text)

    def test_import_raising_something_else(self) -> None:
        class Broken(types.ModuleType):
            def __getattr__(self, name: str) -> object:
                raise RuntimeError("half-installed")

        with mock.patch.dict(sys.modules, {"py_vapid": Broken("py_vapid")}), LogCapture() as logs:
            self.assertIsNone(push.subject_signs("mailto:noreply@example.com"))
            self.assertEqual(load_vapid(self.write("mailto:noreply@example.com"))["subject"], "mailto:noreply@example.com")
        self.assertIn("py_vapid unusable (RuntimeError)", logs.text)

    def test_trailing_newline_refused(self) -> None:
        """Z6-3: `$` in py_vapid's pattern also matches before a final newline; the subject must still be refused."""
        for s in ("https://a.b\n", "mailto:noreply@example.com\n", "https://ai-box.example-tailnet.ts.net\r\n"):
            with self.assertRaises(PushError, msg=repr(s)):
                load_vapid(self.write(s))
            self.assertFalse(push.subject_matches(s), repr(s))
