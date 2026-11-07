"""No secrets in logs (push endpoints and keys, the HA token, the VAPID private key), and the config rules."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from typing import ClassVar

from aiohttp import web

from mission_control.__main__ import check_config, main, run_briefing
from mission_control.config import ConfigError, from_dict

from . import fakes
from .harness import Harness, LogCapture

HERE = Path(__file__).resolve().parent.parent


class NoSecretsInLogsTests(Harness):
    async def test_logs_carry_no_secrets(self) -> None:
        subs = [fakes.fake_subscription(i) for i in (1, 2, 3, 4)]
        self.ha_token.write_text(fakes.HA_TOKEN)

        def echo_token() -> web.Response:  # a misbehaving HA that echoes the token in an error page
            return web.Response(status=500, text=f"bad token {fakes.HA_TOKEN}")

        self.ha.override["/api/states/lock.front_door"] = echo_token
        self.hub.override["/api/state"] = web.json_response({"error": "x"}, status=500)
        with LogCapture() as logs:
            for s in subs:
                self.assertEqual((await self.post("/api/push/subscribe", {"subscription": s, "label": "Phone"}))[0], 200)
            self.sender.status[subs[1]["endpoint"]] = 410
            self.sender.status[subs[2]["endpoint"]] = 500
            self.sender.raise_for.add(subs[3]["endpoint"])
            await self.state.alert_round()
            await self.post("/api/push/test", {"endpoint": subs[0]["endpoint"]})
            await self.post("/api/push/unsubscribe", {"endpoint": subs[0]["endpoint"]})
            out = io.StringIO()
            await run_briefing(self.cfg, send=True, force=True, sender=self.sender, clock=self.clock, out=out)
            _, cards = await self.get("/api/cards")
            await self.get("/api/cards", login="intruder@example.com")
        text = logs.text + out.getvalue()
        self.assertIn("HTTP 410", text)  # the log does say what happened ...
        self.assertIn("ConnectionError", text)
        secrets = [fakes.HA_TOKEN, self.vapid["privateKey"], "secretcapability", "fcm.googleapis.com"]
        for s in subs:
            secrets += [s["endpoint"], s["keys"]["p256dh"], s["keys"]["auth"]]
        for secret in secrets:  # ... but never with whom or with what
            self.assertNotIn(secret, text)
        self.assertNotIn(fakes.HA_TOKEN, json.dumps(cards))

    async def test_check_config(self) -> None:
        out = io.StringIO()
        self.assertEqual(check_config(self.cfg, "x.json", out=out), 0)
        (self.dir / "vapid.json").write_text("{}")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(check_config(self.cfg, "x.json", out=io.StringIO()), 2)
        self.assertIn("VAPID", err.getvalue())


class ConfigTests(unittest.TestCase):
    BASE: ClassVar[dict[str, str]] = {"allowed_login": fakes.LOGIN}

    def test_minimal(self) -> None:
        cfg = from_dict(self.BASE)
        self.assertEqual((cfg.listen_host, cfg.listen_port, cfg.lockscreen_detail), ("127.0.0.1", 8097, "minimal"))
        self.assertIsNone(cfg.moneyhub_url)

    def test_refusals(self) -> None:
        bad = [
            {},  # no login
            {"allowed_login": "noah"},
            {**self.BASE, "surprise": 1},
            {**self.BASE, "listen_host": "0.0.0.0"},
            {**self.BASE, "listen_host": "100.64.0.10"},
            {**self.BASE, "lockscreen_detail": "full"},
            {**self.BASE, "moneyhub_url": "ftp://x"},
            {**self.BASE, "moneyhub_url": "http://user:pw@x:1"},
            {**self.BASE, "erau_url": "https://x/?q=1"},
            {**self.BASE, "units": ["forge-api.service; rm -rf /"]},
            {**self.BASE, "units": ["--all.service"]},
            {**self.BASE, "ha_entities": ["../x"]},
            {**self.BASE, "host_map": {"x.ts.net": "not-an-ip"}},
            {**self.BASE, "backups": [{"id": "a", "label": "A", "kind": "file"}]},
            {**self.BASE, "backups": [{"id": "a", "label": "A", "kind": "unit", "unit": "x.service", "path": "/x"}]},
            {**self.BASE, "backups": [{"id": "A!", "label": "A", "kind": "mediaserver"}]},
            {**self.BASE, "links": {"nope": "https://x"}},
            {**self.BASE, "source_timeout_s": 60},
            {**self.BASE, "timezone": "Mars/Base"},
            {**self.BASE, "push_endpoint_hosts": []},
            {**self.BASE, "log_level": "LOUD"},
        ]
        for raw in bad:
            with self.assertRaises(ConfigError, msg=raw):
                from_dict(raw)

    def test_unit_names_cannot_be_options(self) -> None:
        # systemctl is called with "--" before the unit list too; this is the first line of defence.
        with self.assertRaises(ConfigError):
            from_dict({**self.BASE, "units": ["-H.service"]})

    def test_example_config(self) -> None:
        raw = json.loads((HERE / "config.example.json").read_text())
        cfg = from_dict(raw)
        self.assertEqual(cfg.listen_port, 8097)
        self.assertEqual(cfg.lockscreen_detail, "minimal")

    def test_cli_bad_config_exit_2(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({"allowed_login": fakes.LOGIN, "bogus": True}))
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(main(["--config", str(p), "check-config"]), 2)
            self.assertIn("unknown config keys: bogus", err.getvalue())


if __name__ == "__main__":
    unittest.main()
