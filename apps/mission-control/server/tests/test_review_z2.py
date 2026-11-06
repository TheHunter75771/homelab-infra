"""Reviewer Z round 2: poisoned numbers in the state files (Z2-1), the briefing counting only raised alerts
(Z2-2) and 2-of-3 raising (Z2-3, see also test_alerts and test_review_z)."""

from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path

from aiohttp import web

from mission_control import alerts as alerts_mod
from mission_control.__main__ import run_briefing

from . import fakes
from .harness import Harness, LogCapture

ENTRY = {"phase": "active", "recent": [], "level": "warn", "since": fakes.NOW - 600, "last_sent": fakes.NOW - 600,
         "max_sent": "warn", "clear_since": None, "text": "Bank sync failing", "minimal": "Bank sync failing",
         "named": "Bank sync failing"}


class PoisonedStateFiles(Harness):
    """R2Persist.test_poison_persists, asserting the safe outcome: every round and every /api/cards succeeds."""

    def rpath(self) -> str:
        return os.path.join(self.cfg.state_dir, "restarts.json")

    def apath(self) -> str:
        return os.path.join(self.cfg.state_dir, "alerts.json")

    async def three_rounds(self) -> list[str]:
        out = []
        for _ in range(3):
            self.clock.now += 300
            await self.state.alert_round()  # must not raise
            status, _ = await self.get("/api/cards")
            out.append(status)
            status, _ = await self.get("/api/briefing")
            out.append(status)
        return out

    async def test_poison_persists(self) -> None:
        restarts = {
            "bigint": b'{"forge-api.service": [[' + b"9" * 400 + b", 0]]}",
            "inf": b'{"forge-api.service": [[1e999, 0]]}',
            "nan": b'{"forge-api.service": [[NaN, 0]]}',
            "negative": b'{"forge-api.service": [[-5, 0]]}',
            "bigcount": b'{"forge-api.service": [[1793000000, ' + b"9" * 400 + b"]]}",
            "far past": b'{"forge-api.service": [[1000, 0]]}',
            "bool": b'{"forge-api.service": [[true, 0]]}',
            "past the int digit limit": b'{"forge-api.service": [[' + b"9" * 5000 + b", 0]]}",
            "corrupt": b'{"forge-api.service": [[1793',
        }
        with LogCapture() as logs:
            for name, body in restarts.items():
                Path(self.rpath()).write_bytes(body)
                self.assertEqual(await self.three_rounds(), [200] * 6, name)
                hist = self.state.restart_store.load(self.clock.now)
                self.assertTrue(all(abs(t - self.clock.now) < 3600 for s in hist.values() for t, _n in s), name)
            for name, entry in {
                "since 1e999": {**ENTRY, "since": 1e999},
                "since bigint": {**ENTRY, "since": 10**400},
                "last_sent nan": {**ENTRY, "last_sent": float("nan")},
                "clear_since -inf": {**ENTRY, "phase": "clearing", "clear_since": float("-inf")},
                "since 20 years ago": {**ENTRY, "since": fakes.NOW - 20 * 365 * 86400},
                "recent junk": {**ENTRY, "phase": "pending", "recent": [1, 7, "x"]},
            }.items():
                raw = json.dumps({"bank-sync": entry}).replace("Infinity", "1e999")
                Path(self.apath()).write_text(raw)
                self.assertEqual(await self.three_rounds(), [200] * 6, name)
            Path(self.apath()).write_text('{"bank-sync": ' + json.dumps(ENTRY)[:-1] + ', "since": ' + "9" * 5000 + "}}")
            self.assertEqual(await self.three_rounds(), [200] * 6, "a 5000-digit int (int parse limit)")
        self.assertIn("unreadable (ValueError); starting empty", logs.text)

    def test_loaders_drop_bad_entries_and_survive_deep_nesting(self) -> None:
        now = fakes.NOW
        hist = alerts_mod._valid_hist({"a.service": [[now, True], [now, 1.5], [now, -1], [now, 3]]}, now)
        self.assertEqual(hist, {"a.service": [[now, 3]]})  # a boolean count is not a count
        long = {**ENTRY, "text": "x" * 501}
        self.assertEqual(alerts_mod._valid_state({"bank-sync": long, "ok": ENTRY}, now), {"ok": alerts_mod._valid_state(
            {"ok": ENTRY}, now)["ok"]})
        Path(self.rpath()).write_text("[" * 100000)
        Path(self.apath()).write_text('{"a": ' * 100000)
        with LogCapture() as logs:
            self.assertEqual(self.state.restart_store.load(now), {})
            self.assertEqual(self.state.alert_store.load(now), {})
        self.assertIn("restart history unreadable (RecursionError)", logs.text)
        self.assertIn("alert state unreadable (RecursionError)", logs.text)

    def test_num(self) -> None:
        num = alerts_mod._num
        now = fakes.NOW
        for ok in (0, 1, 1793000000, now, now - 9 * 365 * 86400, 2**53 - 1):
            self.assertTrue(num(ok), ok)
        for bad in (-1, 2**53, 10**400, float("inf"), float("nan"), True, "1", None):
            self.assertFalse(num(bad), bad)
        self.assertTrue(num(now + 86400, now))
        self.assertFalse(num(now - 11 * 365 * 86400, now))
        self.assertFalse(num(now + 11 * 365 * 86400, now))
        self.assertFalse(num(1000, now))


class BriefingCountsRaised(Harness):
    async def test_pending_alert_is_not_counted(self) -> None:
        await self.state.alert_round()  # raises bank-sync and the full PC disk
        self.webui.override["/health"] = lambda: web.Response(status=502)
        self.clock.now += 300
        await self.state.alert_round()  # Open WebUI down for one round: pending
        self.assertEqual(self.state.alert_store.load()["service:webui"]["phase"], "pending")
        _, b = await self.get("/api/briefing")
        self.assertEqual(b["counts"]["alerts"], 2)
        self.assertEqual(b["lockscreen"]["body"], "Briefing ready: 3 deadlines, 3 bills, 2 alerts")
        self.assertNotIn("Open WebUI", json.dumps(b["sections"][2]))
        out = io.StringIO()
        await run_briefing(self.cfg, send=False, clock=self.clock, out=out)
        self.assertIn("Lock screen: Briefing ready: 3 deadlines, 3 bills, 2 alerts", out.getvalue())
        self.clock.now += 300
        await self.state.alert_round()  # second round down: raised
        _, b = await self.get("/api/briefing")
        self.assertEqual(b["counts"]["alerts"], 3)

    async def test_card_since_only_for_raised(self) -> None:
        await self.state.alert_round()
        self.webui.override["/health"] = lambda: web.Response(status=502)
        self.clock.now += 300
        await self.state.alert_round()  # pending
        _, c = await self.get("/api/cards")
        items = {i["label"]: i["value"] for i in c["cards"]["alerts"]["items"]}
        self.assertEqual(items["Open WebUI is down (HTTP 502)"], "")  # pending: no "since"
        self.assertEqual(items["Bank sync failing, last good sync 22 days ago (Oct 05): SimpleFinError: /accounts "
                               "failed: HTTP 403"], "since 5 min ago")
        self.clock.now += 300
        await self.state.alert_round()  # raised
        _, c = await self.get("/api/cards")
        items = {i["label"]: i["value"] for i in c["cards"]["alerts"]["items"]}
        self.assertEqual(items["Open WebUI is down (HTTP 502)"], "since 5 min ago")

    async def test_no_alert_round_yet_counts_none(self) -> None:
        _, b = await self.get("/api/briefing")
        self.assertEqual(b["counts"]["alerts"], 0)

    async def test_clearing_is_not_counted_and_held_still_is(self) -> None:
        await self.state.alert_round()
        self.hub.override["/api/state"] = lambda: web.json_response({}, status=503)  # Money Hub down: held
        self.aic.summary["resources"]["pc"]["disk"][1]["avail"] = 4 * 10**12  # disk freed: clearing
        self.clock.now += 300
        await self.state.alert_round()
        state = self.state.alert_store.load()
        self.assertEqual((state["bank-sync"]["phase"], state["disk:pc:/mnt/share"]["phase"]), ("active", "clearing"))
        _, b = await self.get("/api/briefing")
        self.assertTrue(b["sections"][2]["items"][0]["label"].startswith("Bank sync failing"))
        self.assertEqual(b["counts"]["alerts"], 1)

    async def test_cli_and_server_agree_with_a_restart_warning(self) -> None:
        units = fakes.default_units()
        for n in (0, 2):  # +2 restarts within the window: a restart-based warning, raised at once
            units["forge-api.service"]["NRestarts"] = str(n)
            fakes.set_units(self.dir / "sysd", units)
            self.clock.now += 300
            await self.state.alert_round()
        self.assertEqual(self.state.alert_store.load()["unit:forge-api.service"]["phase"], "active")
        _, b = await self.get("/api/briefing")
        self.assertEqual(b["counts"]["alerts"], 3)
        self.state.subs.add({"endpoint": "https://fcm.googleapis.com/1", "keys": {"p256dh": "x", "auth": "y"},
                             "label": "d"})
        self.assertEqual(await run_briefing(self.cfg, send=True, sender=self.sender, clock=self.clock,
                                            out=io.StringIO()), 0)
        cli_body = self.sender.calls[0][1]["body"]
        self.assertEqual(cli_body, b["lockscreen"]["body"])
        self.assertEqual(re.findall(r"(\d+) alerts", cli_body), ["3"])
        out = io.StringIO()
        await run_briefing(self.cfg, send=False, clock=self.clock, out=out)
        self.assertIn("forge-api.service restarted 2 times", out.getvalue())
        # One more restart since the last alert round: the CLI's own evaluation (with the history read from
        # restarts.json) gives the fresh text, not the one stored in alerts.json.
        units["forge-api.service"]["NRestarts"] = "3"
        fakes.set_units(self.dir / "sysd", units)
        out = io.StringIO()
        await run_briefing(self.cfg, send=False, clock=self.clock, out=out)
        self.assertIn("forge-api.service restarted 3 times", out.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.cfg.state_dir, "restarts.json.tmp")))
