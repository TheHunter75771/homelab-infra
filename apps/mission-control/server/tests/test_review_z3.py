"""Reviewer Z round 3 (Z3-1): the briefing drops a raised alert that has recovered when its source is readable,
and keeps the stored text only for a held one (source unreadable now)."""

from __future__ import annotations

import io

from aiohttp import web

from mission_control import alerts as alerts_mod
from mission_control.__main__ import compose_now, run_briefing
from mission_control.alerts import Alert

from . import fakes
from .harness import Harness

FAILED = "ai-control.service failed (exit-code)"


class StaleStateInBriefing(Harness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.state.alert_round()  # bank-sync and the full PC disk
        units = fakes.default_units()
        units["ai-control.service"].update(ActiveState="failed", SubState="failed", Result="exit-code")
        fakes.set_units(self.dir / "sysd", units)
        for _ in range(2):  # raised (2 of 3 rounds)
            self.clock.now += 300
            await self.state.alert_round()
        self.assertEqual(self.state.alert_store.load()["unit:ai-control.service"]["phase"], "active")
        self.failed_units = units

    @staticmethod
    def labels(b: dict) -> list[str]:
        return [i["label"] for i in b["sections"][2]["items"]]

    async def test_server_down_stale_state(self) -> None:
        """Z's repro: the unit recovered, the server was down for 6 h (alerts.json not updated); the 05:30
        briefing --send must not list the recovered unit."""
        fakes.set_units(self.dir / "sysd", fakes.default_units())
        self.clock.now += 6 * 3600
        cli = await compose_now(self.cfg, self.clock)
        self.assertNotIn(FAILED, self.labels(cli))
        self.assertEqual(cli["counts"]["alerts"], 2)
        self.state.subs.add({"endpoint": "https://fcm.googleapis.com/1", "keys": {"p256dh": "x", "auth": "y"},
                             "label": "d"})
        await run_briefing(self.cfg, send=True, sender=self.sender, clock=self.clock, out=io.StringIO())
        self.assertEqual(self.sender.calls[-1][1]["body"], "Briefing ready: 3 deadlines, 3 bills, 2 alerts")

    async def test_cleared_but_loop_not_yet_run(self) -> None:
        """Z's repro: recovered and readable, the briefing runs before the next alert round."""
        fakes.set_units(self.dir / "sysd", fakes.default_units())
        self.clock.now += 61  # past the cache
        _, srv = await self.get("/api/briefing")
        cli = await compose_now(self.cfg, self.clock)
        for name, b in (("server", srv), ("cli", cli)):
            self.assertNotIn(FAILED, self.labels(b), name)
            self.assertEqual(b["counts"]["alerts"], 2, name)
        self.assertEqual(srv["lockscreen"], cli["lockscreen"])
        self.assertEqual(self.state.alert_store.load()["unit:ai-control.service"]["phase"], "active")  # untouched

    async def test_unreadable_source_keeps_the_stored_alert(self) -> None:
        """Z's first r3 case: systemd and Money Hub unreadable at 05:30: both raised alerts are still listed."""
        fakes.set_units(self.dir / "sysd", self.failed_units, exit=1)
        self.hub.override = {p: (lambda: web.Response(status=500)) for p in ("/api/state", "/api/bills", "/api/budget")}
        self.clock.now += 61
        _, srv = await self.get("/api/briefing")
        cli = await compose_now(self.cfg, self.clock)
        for name, b in (("server", srv), ("cli", cli)):
            labels = self.labels(b)
            self.assertTrue(any(FAILED in x for x in labels), name)
            self.assertTrue(any("Bank sync failing" in x for x in labels), name)
        self.assertEqual(srv["counts"], cli["counts"])
        self.assertEqual(srv["lockscreen"], cli["lockscreen"])

    async def test_still_failing_uses_fresh_text(self) -> None:
        self.clock.now += 61
        _, srv = await self.get("/api/briefing")
        self.assertIn(FAILED, self.labels(srv))
        self.assertEqual(srv["counts"]["alerts"], 3)


class RaisedUnit(Harness):
    start_server = False

    def test_raised(self) -> None:
        a = Alert("unit:x.service", "crit", "x failed (fresh)", "Service failed", "Service failed: x")
        b = Alert("bank-sync", "warn", "Bank sync failing", "Bank sync failing", "Bank sync failing")
        _, state = alerts_mod.plan({}, [a, b], set(), 0.0, 86400)
        _, state = alerts_mod.plan(state, [a, b], set(), 300.0, 86400)
        state["unit:x.service"]["text"] = "x failed (stored)"
        self.assertEqual([x.text for x in alerts_mod.raised(state, [a, b], set())], ["x failed (fresh)", "Bank sync failing"])
        self.assertEqual([x.id for x in alerts_mod.raised(state, [b], set())], ["bank-sync"])  # recovered: dropped
        self.assertEqual([x.text for x in alerts_mod.raised(state, [b], {"unit:"})],
                         ["x failed (stored)", "Bank sync failing"])  # held: stored text
        self.assertEqual([x.text for x in alerts_mod.raised(state, [], {"unit:x.service", "bank-sync"})],
                         ["x failed (stored)", "Bank sync failing"])
