"""The briefing: content, determinism, the lock-screen text at each level, and the CLI (--print / --send)."""

from __future__ import annotations

import contextlib
import io
import json
import re

from mission_control import alerts, briefing
from mission_control.__main__ import run_briefing

from . import fakes
from .harness import Harness

# Everything that must never reach a lock screen at the default level.
PRIVATE = ["$", "Essay", "Rhetorical", "Quiz", "Project", "ABCD", "MATH", "CS 118", "Car insurance", "Phone", "Rent",
           "SimpleFin", "mnt/share", "gaming PC", "1,395", "150.25"]


class BriefingTests(Harness):
    async def compose(self, **overrides: object) -> dict:
        cfg = self.config(**overrides)
        assert self.state.collector is not None
        snap = await self.state.collector.collect()
        active, _ = alerts.evaluate(snap, cfg, fakes.NOW)
        return briefing.compose(snap, active, cfg, fakes.NOW)

    async def test_content(self) -> None:
        b = await self.compose()
        self.assertEqual(b["date"], "2026-10-28")
        self.assertEqual(b["headline"], "3 deadlines, 3 bills, 2 alerts")
        self.assertEqual(b["summary"], "Next deadline: ABCD 101 Due, today at 11:59 PM. "
                                       "3 bills due this week, $1,395.25 in all. Backups: 3 OK.")
        titles = [s["title"] for s in b["sections"]]
        self.assertEqual(titles, ["School: next 48 h", "Bills this week", "Alerts", "Backups"])
        school = b["sections"][0]["items"]
        self.assertEqual(school[0], {"label": "1 deadline past due", "value": "", "status": "warn"})
        self.assertEqual(school[1], {"label": "Today 11:59 PM · ABCD 101 Due", "value": "Essay 2: Rhetorical analysis",
                                     "status": None})
        self.assertEqual(school[2]["label"], "Tomorrow 11:30 AM · CS 118 Start (early)")
        self.assertEqual(school[3]["label"], "Tomorrow 9:30 PM · MATH 111 Due")
        bills = b["sections"][1]["items"]
        self.assertEqual([(i["label"], i["value"]) for i in bills],
                         [("Today · Car insurance", "$150.25"), ("Fri 30 Oct · Phone", "$45.00"),
                          ("Sun 1 Nov · Rent", "$1,200.00")])
        self.assertEqual([i["status"] for i in b["sections"][2]["items"]], ["down", "warn"])
        self.assertEqual([i["value"] for i in b["sections"][3]["items"]],
                         ["OK, 6 h ago", "OK, 3 h ago", "OK, 5 h ago"])
        self.assertEqual(b["counts"], {"deadlines": 3, "bills": 3, "alerts": 2, "backups_bad": 0})

    async def test_deterministic(self) -> None:
        self.assertEqual(await self.compose(), await self.compose())

    async def test_lockscreen_minimal_has_no_detail(self) -> None:
        b = await self.compose()
        self.assertEqual(b["lockscreen"], {"title": "Mission Control", "body": "Briefing ready: 3 deadlines, 3 bills, 2 alerts"})
        push = briefing.briefing_push(b)
        self.assertEqual(set(push), {"title", "body", "tag", "url"})
        blob = json.dumps(push)
        for word in PRIVATE:
            self.assertNotIn(word, blob, word)
        self.assertEqual(re.findall(r"\d+", push["body"]), ["3", "3", "2"])

    async def test_lockscreen_levels(self) -> None:
        none = await self.compose(lockscreen_detail="none")
        self.assertEqual(none["lockscreen"]["body"], "Your briefing is ready.")
        named = await self.compose(lockscreen_detail="names")
        body = named["lockscreen"]["body"]
        self.assertEqual(body, "Briefing ready: 3 deadlines (ABCD 101, CS 118, MATH 111), 3 bills (Car insurance, Phone, "
                               "Rent), 2 alerts (Disk over 90%: gaming PC /mnt/share, Bank sync failing)")
        for word in ("$", "Essay", "Quiz", "Project", "SimpleFin"):
            self.assertNotIn(word, body)

    async def test_alert_push_levels(self) -> None:
        a = [alerts.Alert("backup:x", "warn", "Backup stale: Secret Label (older than 36 h)", "Backup stale",
                          "Backup stale: Secret Label")]
        self.assertEqual(briefing.alert_push(a, "minimal")["body"], "Backup stale")
        self.assertEqual(briefing.alert_push(a, "names")["body"], "Backup stale: Secret Label")
        self.assertEqual(briefing.alert_push(a, "none")["body"], "1 new alert.")

    async def test_empty_and_unavailable(self) -> None:
        self.erau.payload["snapshot"]["tasks"] = []
        for p in self.hub.bills:
            self.hub.bills[p] = []
        b = await self.compose()
        self.assertEqual(b["headline"], "No deadlines, no bills, 2 alerts")
        self.assertEqual(b["sections"][1]["items"], [])
        await self.restart(erau_url=None)
        b = await self.compose(erau_url=None)
        self.assertEqual(b["lockscreen"]["body"], "Briefing ready: deadlines unavailable, no bills, 2 alerts")


class CliTests(Harness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.state.alert_round()  # the server raises bank-sync and the full PC disk (no devices yet)

    async def test_print(self) -> None:
        out = io.StringIO()
        code = await run_briefing(self.cfg, send=False, clock=self.clock, out=out)
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("Mission Control briefing for 2026-10-28", text)
        self.assertIn("  - Today 11:59 PM · ABCD 101 Due: Essay 2: Rhetorical analysis", text)
        self.assertIn("Lock screen: Briefing ready: 3 deadlines, 3 bills, 2 alerts", text)

    async def test_send_once_a_day(self) -> None:
        for i in (1, 2):
            self.state.subs.add({"endpoint": f"https://fcm.googleapis.com/{i}", "keys": {"p256dh": "x", "auth": "y"},
                                 "label": f"d{i}"})
        out = io.StringIO()
        self.assertEqual(await run_briefing(self.cfg, send=True, sender=self.sender, clock=self.clock, out=out), 0)
        self.assertEqual(len(self.sender.calls), 2)
        self.assertEqual(self.sender.calls[0][1], {"title": "Mission Control", "body": "Briefing ready: 3 deadlines, 3 bills, 2 alerts",
                                                   "tag": "mc-briefing", "url": "/#card-briefing"})
        self.assertIn("delivered to 2/2", out.getvalue())
        self.assertEqual(json.loads((self.state_dir / "briefing.json").read_text())["date"], "2026-10-28")
        self.assertEqual(await run_briefing(self.cfg, send=True, sender=self.sender, clock=self.clock, out=io.StringIO()), 0)
        self.assertEqual(len(self.sender.calls), 2, "a second run the same day sends nothing")
        self.assertEqual(await run_briefing(self.cfg, send=True, force=True, sender=self.sender, clock=self.clock,
                                            out=io.StringIO()), 0)
        self.assertEqual(len(self.sender.calls), 4)
        self.clock.now += 86400
        await run_briefing(self.cfg, send=True, sender=self.sender, clock=self.clock, out=io.StringIO())
        self.assertEqual(len(self.sender.calls), 6)

    async def test_send_no_devices_and_all_failed(self) -> None:
        self.assertEqual(await run_briefing(self.cfg, send=True, sender=self.sender, clock=self.clock, out=io.StringIO()), 0)
        self.assertFalse((self.state_dir / "briefing.json").exists())
        ep = "https://fcm.googleapis.com/1"
        self.state.subs.add({"endpoint": ep, "keys": {"p256dh": "x", "auth": "y"}, "label": "d"})
        self.sender.status[ep] = 500
        self.assertEqual(await run_briefing(self.cfg, send=True, sender=self.sender, clock=self.clock, out=io.StringIO()), 1)
        self.assertFalse((self.state_dir / "briefing.json").exists())
        self.assertEqual(len(self.state.subs.all()), 1, "a 500 keeps the device")

    async def test_send_without_vapid_is_a_setup_error(self) -> None:
        (self.dir / "vapid.json").unlink()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(await run_briefing(self.cfg, send=True, clock=self.clock, out=io.StringIO()), 2)
        self.assertIn("push not set up", err.getvalue())
