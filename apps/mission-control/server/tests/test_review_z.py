"""Reviewer Z's findings: alert hysteresis (Z-1, incl. Z's AlertBehaviour cases), a killing test for each
surviving mutant (Z-2) and the Lows (Z-3 .. Z-8)."""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import os
import threading
import time
from collections.abc import Callable
from typing import Any, ClassVar
from unittest import mock

from aiohttp import web

from mission_control import alerts as alerts_mod
from mission_control import briefing as briefing_mod
from mission_control import net, sources, util
from mission_control.__main__ import run_briefing
from mission_control.push import SubscriptionStore

from . import fakes
from .harness import Harness

BIG = 3 * 1024 * 1024  # over net.MAX_JSON (2 MiB)


def big_json() -> bytes:
    return b'{"services": [], "x": "' + b"a" * BIG + b'"}'


class AlertBehaviour(Harness):
    """Z's probes, now asserting the Lead's hysteresis rules."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.post("/api/push/subscribe", {"subscription": fakes.fake_subscription(1)})

    async def rounds(self, n: int, mutate: Callable[[int], None]) -> list[list[str]]:
        sent = []
        for i in range(n):
            mutate(i)
            self.clock.now += 300
            sent.append([a.id for a in await self.state.alert_round()])
        return sent

    async def test_flapping_webui(self) -> None:
        await self.state.alert_round()

        def m(i: int) -> None:
            if i % 2 == 0:
                self.webui.override["/health"] = lambda: web.Response(status=502)
            else:
                self.webui.override.pop("/health", None)

        sent = await self.rounds(8, m)  # down/up every other round: raised once (2 of 3), then the same episode
        self.assertEqual(sum("service:webui" in s for s in sent), 1, sent)
        self.assertIn("service:webui", sent[2])

    async def test_webui_down_two_rounds_pushes_once(self) -> None:
        await self.state.alert_round()
        self.webui.override["/health"] = lambda: web.Response(status=502)
        sent = await self.rounds(4, lambda i: None)
        self.assertEqual(sent, [[], ["service:webui"], [], []])

    async def test_flapping_pc_disk(self) -> None:
        await self.state.alert_round()
        full = self.aic.summary["resources"]["pc"]

        def m(i: int) -> None:  # PC asleep (no PC resources in AI Control's summary) / awake
            if i % 2 == 0:
                self.aic.summary["resources"].pop("pc", None)
            else:
                self.aic.summary["resources"]["pc"] = full

        sent = await self.rounds(6, m)
        self.assertEqual(sum(any(x.startswith("disk:pc") for x in s) for s in sent), 0, sent)
        state = json.loads((self.state_dir / "alerts.json").read_text())
        self.assertEqual(state["disk:pc:/mnt/share"]["phase"], "active")

    async def test_pc_asleep_for_hours_holds_its_disk_alert(self) -> None:
        await self.state.alert_round()
        full = self.aic.summary["resources"].pop("pc")
        sent = await self.rounds(12, lambda i: None)  # an hour asleep: longer than the 30 min clearing time
        self.aic.summary["resources"]["pc"] = full
        sent += await self.rounds(2, lambda i: None)
        self.assertEqual(sum(any(x.startswith("disk:pc") for x in s) for s in sent), 0, sent)

    async def test_nrestarts_cumulative(self) -> None:
        units = fakes.default_units()
        units["forge-api.service"]["NRestarts"] = "3"  # 3 restarts at some point since the last manual start
        fakes.set_units(self.dir / "sysd", units)
        self.clock.now += 1000
        first = await self.state.alert_round()
        await self.rounds(3, lambda i: None)
        _, cards = await self.get("/api/cards")
        self.assertNotIn("forge-api", json.dumps(cards["cards"]["alerts"]))
        self.clock.now += 25 * 3600
        again = await self.state.alert_round()
        ids = [a.id for a in first] + [a.id for a in again]
        self.assertNotIn("unit:forge-api.service", ids)

    async def test_bank_sync_message(self) -> None:
        active, _ = alerts_mod.evaluate(await self.state.collector.snapshot(), self.cfg, self.clock.now)
        bank = next(a for a in active if a.id == "bank-sync")
        self.assertIn("Oct 05", bank.text)
        self.assertEqual(bank.minimal, "Bank sync failing")

    async def test_escalation_flap(self) -> None:
        await self.state.alert_round()  # 95 % used: crit, delivered
        terabyte = 1_000_000_000_000
        d = self.aic.summary["resources"]["pc"]["disk"][1]

        def m(i: int) -> None:
            d["avail"] = int(0.04 * 4 * terabyte) if i % 2 == 0 else int(0.06 * 4 * terabyte)  # 96 % / 94 %

        sent = await self.rounds(6, m)
        self.assertEqual(sum(any(x.startswith("disk:pc") for x in s) for s in sent), 0, sent)

    async def test_warn_then_crit_pushes_once_more(self) -> None:
        terabyte = 1_000_000_000_000
        d = self.aic.summary["resources"]["pc"]["disk"][1]
        d["avail"] = int(0.08 * 4 * terabyte)  # 92 %: warn
        self.assertIn("disk:pc:/mnt/share", [a.id for a in await self.state.alert_round()])
        steps = [0.04, 0.08, 0.04, 0.08]  # 96 crit, 92 warn, 96 crit, 92 warn

        def m(i: int) -> None:
            d["avail"] = int(steps[i] * 4 * terabyte)

        sent = await self.rounds(4, m)
        self.assertEqual([any(x.startswith("disk:pc") for x in s) for s in sent], [True, False, False, False])

    async def test_cleared_for_30_min_is_forgotten(self) -> None:
        self.hub.state["sync_last_at"] = "2026-10-20T13:00:00"
        await self.state.alert_round()
        self.hub.state["sync_last_at"] = "2026-10-28T09:00:00"
        self.hub.state["sync_failing"] = False
        sent = await self.rounds(7, lambda i: None)  # 35 min clean
        self.assertTrue(all("bank-sync" not in s for s in sent))
        state = json.loads((self.state_dir / "alerts.json").read_text())
        self.assertNotIn("bank-sync", state)


class SurvivorKillers(Harness):
    """One test per mutant Z found surviving."""

    extra: ClassVar[dict[str, Any]] = {"source_timeout_s": 2.0}

    async def test_z8_streaming_cap_chunked_without_length(self) -> None:
        async def chunked(request: web.Request) -> web.StreamResponse:
            resp = web.StreamResponse(headers={"Content-Type": "application/json"})
            resp.enable_chunked_encoding()
            await resp.prepare(request)
            body = big_json()
            for i in range(0, len(body), 65536):
                await resp.write(body[i:i + 65536])
            await resp.write_eof()
            return resp

        self.aic.override["/api/summary"] = chunked
        _, body = await self.get("/api/cards")
        self.assertEqual(body["cards"]["ai_control"]["error"], "reply too large")

    async def test_z8_streaming_cap_gzip(self) -> None:
        packed = gzip.compress(big_json())
        self.assertLess(len(packed), net.MAX_JSON)  # the declared length is small; the inflated body is not
        self.aic.override["/api/summary"] = lambda: web.Response(
            body=packed, headers={"Content-Encoding": "gzip", "Content-Type": "application/json"})
        _, body = await self.get("/api/cards")
        self.assertEqual(body["cards"]["ai_control"]["error"], "reply too large")

    async def test_z8_cap_is_2_mib(self) -> None:
        self.assertEqual(net.MAX_JSON, 2 * 1024 * 1024)
        self.aic.override["/api/summary"] = lambda: web.Response(body=big_json(), content_type="application/json")
        _, body = await self.get("/api/cards")
        self.assertEqual(body["cards"]["ai_control"]["error"], "reply too large")

    async def test_z12_hung_systemctl_is_killed(self) -> None:
        await self.restart(source_timeout_s=0.5)
        fakes.set_units(self.dir / "sysd", fakes.default_units(), sleep=30)
        _, body = await self.get("/api/cards")
        self.assertIn("timed out", body["cards"]["servers"]["items"][0]["value"])
        pid = int((self.dir / "sysd" / "pid").read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        else:
            self.fail(f"systemctl pid {pid} is still running")

    async def test_z13_per_source_timeout(self) -> None:
        await self.restart(source_timeout_s=0.3)

        async def never(*_a: Any) -> dict[str, Any]:  # a source that ignores its own timeouts
            await asyncio.sleep(10)
            return {}

        with mock.patch.object(sources, "aicontrol", never):
            started = time.monotonic()
            _, body = await self.get("/api/cards")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(body["cards"]["ai_control"]["error"], "timed out after 0.3s")

    async def test_z32_prune_takes_the_lock(self) -> None:
        path = str(self.state_dir / "subscriptions.json")
        store = SubscriptionStore(path)
        store.add({"endpoint": "https://fcm.googleapis.com/a", "keys": {}, "label": "a"})
        done = threading.Event()

        def prune() -> None:
            store.prune({"https://fcm.googleapis.com/a"})
            done.set()

        with util.file_lock(path):
            t = threading.Thread(target=prune)
            t.start()
            await asyncio.sleep(0.3)
            self.assertFalse(done.is_set(), "prune ran while another writer held the lock")
        t.join(5)
        self.assertTrue(done.is_set())
        self.assertEqual(store.all(), [])

    async def test_z22_test_push_only_to_the_requester(self) -> None:
        subs = [fakes.fake_subscription(i) for i in (1, 2, 3)]
        for s in subs:
            await self.post("/api/push/subscribe", {"subscription": s})
        status, _ = await self.post("/api/push/test", {"endpoint": subs[1]["endpoint"]})
        self.assertEqual(status, 200)
        self.assertEqual([e for e, _p in self.sender.calls], [subs[1]["endpoint"]])

    async def test_z30_crit_disk_threshold(self) -> None:
        terabyte = 1_000_000_000_000
        d = self.aic.summary["resources"]["pc"]["disk"][1]
        for avail, level in ((0.08, "warn"), (0.06, "warn"), (0.05, "crit"), (0.01, "crit"), (0.11, None)):
            d["avail"] = int(avail * 4 * terabyte)
            snap = await self.state.collector.collect()
            active, _ = alerts_mod.evaluate(snap, self.cfg, fakes.NOW)
            got = next((a.level for a in active if a.id == "disk:pc:/mnt/share"), None)
            self.assertEqual(got, level, avail)

    async def test_z9_nan_and_infinity_rejected(self) -> None:
        for raw in (b'{"services": [], "x": NaN}', b'{"services": [], "x": Infinity}',
                    b'{"services": [], "x": -Infinity}', b'{"services": [], "x": 1e999}'):
            self.aic.override["/api/summary"] = lambda raw=raw: web.Response(body=raw, content_type="application/json")
            snap = await self.state.collector.collect()
            self.assertEqual(snap["sources"]["aicontrol"]["error"], "invalid JSON reply", raw)

    async def test_z10_static_symlink_out_of_root(self) -> None:
        (self.dir / "outside.txt").write_text("OUTSIDE-SECRET")
        os.symlink(self.dir / "outside.txt", self.dir / "web" / "link.txt")
        status, body = await self.get("/link.txt")
        self.assertEqual(status, 200)  # the SPA fallback, not the file
        self.assertNotIn("OUTSIDE-SECRET", str(body))
        self.assertIn("Mission Control", str(body))

    async def test_z11_dotfiles_not_served(self) -> None:
        (self.dir / "web" / ".env").write_text("DOT-SECRET")
        (self.dir / "web" / ".git").mkdir()
        (self.dir / "web" / ".git" / "config").write_text("DOT-SECRET")
        for path in ("/.env", "/.git/config"):
            _, body = await self.get(path)
            self.assertNotIn("DOT-SECRET", str(body), path)

    async def test_z14_sync_error_is_cleaned(self) -> None:
        self.hub.state["sync_error"] = "SimpleFinError:\u202e\n\x00HTTP\u200b 403 " + "x" * 300
        _, body = await self.get("/api/cards")
        item = next(i for i in body["cards"]["money"]["items"] if i["label"] == "Sync error")
        for bad in ("\u202e", "\n", "\x00", "\u200b"):
            self.assertNotIn(bad, item["value"])
        self.assertTrue(item["value"].startswith("SimpleFinError: HTTP 403"))
        self.assertLessEqual(len(item["value"]), 100)

    async def test_z28_subscription_label_is_cleaned(self) -> None:
        label = "Phone\u202e\n\x07" + "y" * 100
        status, _ = await self.post("/api/push/subscribe", {"subscription": fakes.fake_subscription(1), "label": label})
        self.assertEqual(status, 200)
        stored = self.state.subs.all()[0]["label"]
        self.assertTrue(stored.startswith("Phone y"))
        self.assertLessEqual(len(stored), 40)
        for bad in ("\u202e", "\n", "\x07"):
            self.assertNotIn(bad, stored)


class Lows(Harness):
    async def test_z3_restart_increase_within_window(self) -> None:
        await self.post("/api/push/subscribe", {"subscription": fakes.fake_subscription(1)})
        units = fakes.default_units()
        pushed = []
        for n in (0, 1, 2, 2, 2):  # +2 within 15 min
            units["forge-api.service"]["NRestarts"] = str(n)
            fakes.set_units(self.dir / "sysd", units)
            self.clock.now += 300
            pushed.append([a.id for a in await self.state.alert_round()])
        self.assertEqual([("unit:forge-api.service" in p) for p in pushed], [False, False, True, False, False])
        _, cards = await self.get("/api/cards")
        forge = next(i for i in cards["cards"]["servers"]["items"] if i["label"] == "forge-api.service")
        self.assertEqual((forge["status"], forge["value"]), ("warn", "restarted 2 times in the last 60 min"))
        self.clock.now += 3700  # an hour later, stable: no longer restarting
        await self.state.alert_round()
        _, cards = await self.get("/api/cards")
        forge = next(i for i in cards["cards"]["servers"]["items"] if i["label"] == "forge-api.service")
        self.assertEqual(forge["status"], "ok")
        hist = json.loads((self.state_dir / "restarts.json").read_text())
        self.assertEqual([s[1] for s in hist["forge-api.service"]], [2])

    async def test_z3_counter_reset_starts_over(self) -> None:
        cfg = self.cfg
        snap = {"sources": {"units": {"state": "ok", "data": {"forge-api.service": {"restarts": 0}}}}}
        hist = {"forge-api.service": [[fakes.NOW - 600, 5]]}
        hist = alerts_mod.update_restarts(hist, snap, cfg, fakes.NOW)
        self.assertEqual(hist["forge-api.service"], [[fakes.NOW, 0]])

    async def test_z4_bills_fetched_in_parallel(self) -> None:
        self.hub.delay = 1.2  # each request 1.2 s; budget 2 s; the window spans two months
        _, body = await self.get("/api/cards")
        money = body["cards"]["money"]
        self.assertIsNone(money["error"])
        self.assertIn({"label": "Bills this week", "value": "3 · $1,395.25", "status": None}, money["items"])

    async def test_z5_backup_line_counts_unknown(self) -> None:
        self.last_ok.write_text("garbage\n")
        _, b = await self.get("/api/briefing")
        self.assertIn("Backups: 2 OK, 1 unknown.", b["summary"])
        self.assertNotIn("Backups OK", b["summary"])
        self.assertEqual(briefing_mod.backup_line([{"status": "ok"}, {"status": "stale"}, {"status": "failed"},
                                                   {"status": "unknown"}]), "Backups: 1 OK, 1 stale, 1 failed, 1 unknown.")

    async def test_z6_future_sync_time(self) -> None:
        self.hub.state["sync_last_at"] = "2027-01-01T00:00:00"
        snap = await self.state.collector.collect()
        sync = snap["sources"]["moneyhub"]["data"]["sync"]
        self.assertIsNone(sync["last_ok"])
        self.assertTrue(sync["last_ok_future"])
        active, _ = alerts_mod.evaluate(snap, self.cfg, fakes.NOW)
        bank = next(a for a in active if a.id == "bank-sync")
        self.assertIn("time unknown", bank.text)
        _, cards = await self.get("/api/cards")
        self.assertIn("time unknown", cards["cards"]["money"]["items"][0]["value"])
        self.hub.state["sync_failing"] = False  # unknown time, not failing: no alert
        active, _ = alerts_mod.evaluate(await self.state.collector.collect(), self.cfg, fakes.NOW)
        self.assertNotIn("bank-sync", [a.id for a in active])
        self.hub.state["sync_last_at"] = "2026-10-28T09:45:00"  # 15 min ahead: plain clock drift, fine
        snap = await self.state.collector.collect()
        self.assertFalse(snap["sources"]["moneyhub"]["data"]["sync"]["last_ok_future"])

    async def test_z7_briefing_waits_for_a_slow_erau(self) -> None:
        """The CLI (briefing --send at 05:30) waits for ERAU up to erau_timeout_s; the others keep their 0.3 s."""
        await self.restart(source_timeout_s=0.3)
        await self.state.alert_round()
        self.erau.delay = 0.8  # far slower than the round budget, within erau_timeout_s
        self.state.subs.add({"endpoint": "https://fcm.googleapis.com/1", "keys": {"p256dh": "x", "auth": "y"},
                             "label": "d"})
        before = self.erau.hits("/api/snapshot")
        started = time.monotonic()
        code = await run_briefing(self.cfg, send=True, sender=self.sender, clock=self.clock, out=io.StringIO())
        self.assertEqual(code, 0)
        self.assertGreater(time.monotonic() - started, 0.7)
        self.assertEqual(self.sender.calls[0][1]["body"], "Briefing ready: 3 deadlines, 3 bills, 2 alerts")
        self.assertEqual(self.erau.hits("/api/snapshot") - before, 1, "no retry: one fetch, waited for")

    async def test_z7_cli_gives_up_at_erau_timeout(self) -> None:
        await self.restart(source_timeout_s=0.3, erau_timeout_s=5)
        self.erau.delay = 30
        started = time.monotonic()
        out = io.StringIO()
        await run_briefing(self.cfg, send=False, clock=self.clock, out=out)
        self.assertLess(time.monotonic() - started, 7)
        self.assertIn("School data unavailable: timed out after 5s", out.getvalue())
