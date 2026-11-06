"""ERAU stale-while-revalidate (Lead decision after deploy, 2026-10-28): ERAU's cold cache takes 7-9 s to rebuild,
and a fetch cancelled at the round's 5 s budget never let it finish. The fetch now runs in a background task
bounded by erau_timeout_s, never cancelled by a round; rounds serve the last good result (up to erau_max_age_s) or
"still loading"."""

from __future__ import annotations

import asyncio
import io
import time
from typing import Any, ClassVar

from aiohttp import web

from mission_control import alerts as alerts_mod
from mission_control.__main__ import run_briefing

from . import fakes
from .harness import Harness


class ColdErau(fakes.FakeErau):
    """ERAU's own cache: the first /api/snapshot after a cold start takes `cold` s, later ones `warm` s. The cache
    warms only when a rebuild runs to the end."""

    def __init__(self, cold: float, warm: float) -> None:
        super().__init__()
        self.cold, self.warm, self.is_warm = cold, warm, False
        self.completed = 0

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.path == "/api/snapshot":
            await asyncio.sleep(self.warm if self.is_warm else self.cold)
            self.is_warm = True
            self.completed += 1
        return await super().handle(request)


class SwrHarness(Harness):
    cold, warm = 7.0, 0.3
    extra: ClassVar[dict[str, Any]] = {"source_timeout_s": 5.0}

    async def asyncSetUp(self) -> None:
        self.cold_erau = ColdErau(self.cold, self.warm)
        await self.cold_erau.start()
        await super().asyncSetUp()
        self.fakes.append(self.cold_erau)
        await self.restart(erau_url=self.cold_erau.url)

    @property
    def collector(self) -> Any:
        return self.state.collector

    async def landed(self, timeout: float = 15) -> None:
        task = self.collector._erau_task
        assert task is not None
        await asyncio.wait_for(asyncio.shield(task), timeout)

    async def school(self) -> dict[str, Any]:
        self.clock.now += 61  # past the cards cache
        _, body = await self.get("/api/cards")
        return body["cards"]["school"]


class ColdThenWarm(SwrHarness):
    async def test_round1_loading_then_round2_ok(self) -> None:
        started = time.monotonic()
        card = await self.school()
        self.assertLess(time.monotonic() - started, 6.5, "a round never waits on ERAU past source_timeout_s")
        self.assertEqual((card["status"], card["headline"], card["error"]), ("unknown", "School data is loading", "still loading"))
        snap = self.collector.current
        active, _ = alerts_mod.evaluate(snap, self.cfg, self.clock.now)
        self.assertFalse([a for a in active if "erau" in a.id or "school" in a.id])  # loading is not an alert
        await self.landed()  # the background fetch runs to the end (about 7 s) ...
        self.assertTrue(self.cold_erau.is_warm)  # ... so ERAU's cache warms
        card = await self.school()
        self.assertEqual((card["status"], card["error"]), ("warn", None))  # 1 overdue: warn, real data
        self.assertEqual(card["headline"], "3 deadlines in the next 48 h")
        self.assertEqual(self.cold_erau.completed, 2)


class Fast(SwrHarness):
    cold, warm = 0.6, 0.6
    extra: ClassVar[dict[str, Any]] = {"source_timeout_s": 0.2}

    async def test_background_fetch_is_not_cancelled_by_the_round(self) -> None:
        card = await self.school()
        self.assertEqual(card["error"], "still loading")
        task = self.collector._erau_task
        self.assertFalse(task.done())
        await self.landed()
        self.assertFalse(task.cancelled())
        self.assertEqual(task.result()["state"], "ok")
        self.assertEqual(self.cold_erau.completed, 1)

    async def test_single_flight(self) -> None:
        for _ in range(3):  # three rounds while the first fetch is still running
            await self.school()
        await asyncio.gather(*(self.collector.collect() for _ in range(3)))
        self.assertEqual(self.cold_erau.hits("/api/snapshot"), 1)
        await self.landed()
        self.assertEqual(self.cold_erau.completed, 1)

    async def test_stale_result_with_as_of_then_past_max_age(self) -> None:
        await self.school()
        await self.landed()
        card = await self.school()  # the landed result; a new fetch started and is still running
        self.assertIn("(as of 5:31 AM)", card["headline"])  # landed at the clock of the first round
        self.assertEqual(card["error"], None)
        self.clock.now += 900  # older than 10 min: the briefing says so
        _, b = await self.get("/api/briefing")
        school = b["sections"][0]["items"]
        self.assertEqual(school[0]["label"], "School data as of 5:31 AM")
        self.assertEqual(b["counts"]["deadlines"], 3)
        await self.landed()
        self.clock.now += self.cfg.erau_max_age_s + 1  # the last good result is now too old
        self.cold_erau.warm = 5
        card = await self.school()
        self.assertEqual((card["status"], card["error"]), ("unknown", "still loading"))

    async def test_failed_fetch_reports_its_error(self) -> None:
        self.cold_erau.override["/api/snapshot"] = lambda: web.json_response({}, status=502)
        card = await self.school()
        self.assertEqual((card["status"], card["error"]), ("unknown", "HTTP 502"))

    async def test_cancelled_cleanly_on_shutdown(self) -> None:
        self.cold_erau.cold = 30
        await self.school()
        task = self.collector._erau_task
        self.assertFalse(task.done())
        runner, self.runner = self.runner, None
        started = time.monotonic()
        await runner.cleanup()
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(task.cancelled())

    async def test_briefing_send_waits_for_erau(self) -> None:
        await self.state.alert_round()
        self.state.subs.add({"endpoint": "https://fcm.googleapis.com/1", "keys": {"p256dh": "x", "auth": "y"},
                             "label": "d"})
        self.cold_erau.cold = self.cold_erau.warm = 1.5  # over the 0.2 s round budget, under erau_timeout_s
        code = await run_briefing(self.cfg, send=True, sender=self.sender, clock=self.clock, out=io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(self.sender.calls[0][1]["body"], "Briefing ready: 3 deadlines, 3 bills, 2 alerts")


class LastTry(SwrHarness):
    """Z5-1: with no usable last-good result, "still loading" carries the last finished fetch's error."""

    cold, warm = 0.6, 0.6
    extra: ClassVar[dict[str, Any]] = {"source_timeout_s": 0.2}

    async def test_last_error_shown_while_loading(self) -> None:
        self.cold_erau.override["/api/snapshot"] = lambda: web.json_response({}, status=502)
        card = await self.school()
        self.assertEqual(card["error"], "HTTP 502")  # finished within the round
        self.cold_erau.override.clear()
        self.cold_erau.cold = self.cold_erau.warm = 2  # slow now: the round sees it still loading
        card = await self.school()
        self.assertEqual((card["status"], card["error"]), ("unknown", "still loading (last try: HTTP 502)"))
        _, b = await self.get("/api/briefing")
        self.assertEqual(b["sections"][0]["items"][0], {"label": "School data is still loading",
                                                        "value": "last try: HTTP 502", "status": "unknown"})
        await self.landed()  # a success clears it
        self.assertIsNone(self.collector._erau_last_error)

    async def test_hung_erau_reports_its_timeout(self) -> None:
        await self.restart(erau_timeout_s=5)
        self.cold_erau.cold = self.cold_erau.warm = 30  # hung
        card = await self.school()
        self.assertEqual(card["error"], "still loading")  # nothing has finished yet
        await self.landed(10)  # the fetch gives up at erau_timeout_s
        card = await self.school()
        self.assertEqual(card["error"], "still loading (last try: timed out after 5s)")


class LandingTime(SwrHarness):
    """The last-good result is stamped when it lands, not when its round started (Z's V12)."""

    cold, warm = 0.6, 0.6
    extra: ClassVar[dict[str, Any]] = {"source_timeout_s": 0.2}

    async def test_as_of_is_the_landing_time(self) -> None:
        await self.school()  # 05:31:01, still loading
        self.clock.now += 600  # ten minutes pass (fake clock) while the fetch runs
        await self.landed()
        self.assertEqual(self.collector._erau_good["at"], int(self.clock.now))
        card = await self.school()  # a new fetch is in flight: the landed result is served
        self.assertIn("(as of 5:41 AM)", card["headline"])


class LastErrorField(SwrHarness):
    """Z7-2: the last error is carried as its own field, so text ending in ")" survives intact."""

    cold, warm = 2.0, 2.0
    extra: ClassVar[dict[str, Any]] = {"source_timeout_s": 0.2}

    async def test_error_with_parentheses(self) -> None:
        self.collector._erau_last_error = "request failed (ClientOSError)"
        card = await self.school()
        self.assertEqual(card["error"], "still loading (last try: request failed (ClientOSError))")
        self.assertEqual(self.collector.current["sources"]["erau"]["last_error"], "request failed (ClientOSError)")
        _, b = await self.get("/api/briefing")
        self.assertEqual(b["sections"][0]["items"][0]["value"], "last try: request failed (ClientOSError)")
