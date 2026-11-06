"""The cache (calendar_view/service.py): a failed fetch keeps the last good data and marks it stale with
its age; /healthz goes 503 once the last good fetch is 5 minutes old; no data at all is a 503, never an
empty week. Fake clocks, a scripted fetch, and the real HTTP server for /healthz and /api/agenda."""

from __future__ import annotations

import http.client
import json
import threading
import unittest

from .helpers import (
    HAVE_LIBS,
    PASSWORD,
    ROUTINES,
    SHARED,
    FakeClock,
    config,
    day,
    ny,
    require_libs,
    sources,
    titles,
)

if HAVE_LIBS:
    from calendar_view.caldav import FetchError
    from calendar_view.server import ViewServer
    from calendar_view.service import (
        HEALTHY_SECONDS,
        STALE_AFTER_SECONDS,
        AgendaService,
    )

API_KEYS = {"generated", "stale", "stale_seconds", "timezone", "calendars", "today", "days"}


class ScriptedFetch:
    """Returns the fixtures, or raises what it's told to; records the ranges it was asked for."""

    def __init__(self) -> None:
        self.data = sources(("today.ics", SHARED), ("overnight.ics", ROUTINES))
        self.fail: Exception | None = None
        self.ranges: list = []

    def __call__(self, start, end):
        self.ranges.append((start, end))
        if self.fail:
            raise self.fail
        return self.data


class Stale(unittest.TestCase):
    def setUp(self) -> None:
        require_libs()
        self.clock = FakeClock(ny(2026, 11, 6, 10, 30))
        self.fetch = ScriptedFetch()
        self.service = AgendaService(config(), self.fetch, clock=self.clock.now, monotonic=self.clock.monotonic)

    def test_no_data_yet_is_503_not_an_empty_week(self) -> None:
        status, body = self.service.agenda()
        self.assertEqual(status, 503)
        self.assertEqual((body["stale"], body["stale_seconds"], body["days"], body["today"]), (True, None, None, None))
        self.assertFalse(self.service.healthy())
        self.fetch.fail = FetchError("household/shared", "HTTP 403")
        self.assertFalse(self.service.refresh())
        self.assertEqual(self.service.agenda()[0], 503)

    def test_a_good_fetch(self) -> None:
        self.assertTrue(self.service.refresh())
        status, body = self.service.agenda()
        self.assertEqual(status, 200)
        self.assertEqual(set(body), API_KEYS)
        self.assertEqual((body["stale"], body["stale_seconds"]), (False, 0))
        self.assertEqual(body["generated"], "2026-11-06T10:30:00-05:00")
        self.assertEqual(body["timezone"], "America/New_York")
        self.assertEqual(body["calendars"][0], {"label": "Shared", "color": "#4f8cff"})
        self.assertEqual(len(body["days"]), 7)
        self.assertTrue(self.service.healthy())
        # The fetch asks for a day more than the window on each side.
        start, end = self.fetch.ranges[0]
        self.assertEqual((start.isoformat(), end.isoformat()), ("2026-11-05T00:00:00-05:00", "2026-11-14T00:00:00-05:00"))

    def test_a_failed_fetch_keeps_the_data_and_marks_it_stale(self) -> None:
        self.service.refresh()
        before = self.service.agenda()[1]
        self.clock.advance(60)
        self.fetch.fail = FetchError("household/shared", "HTTP 403")
        with self.assertLogs("calendar_view.service", "WARNING") as logs:
            self.assertFalse(self.service.refresh())
        self.assertEqual(logs.output, ["WARNING:calendar_view.service:fetch failed: household/shared, HTTP 403"])
        status, body = self.service.agenda()
        self.assertEqual(status, 200)
        self.assertEqual((body["stale"], body["stale_seconds"]), (True, 60))
        self.assertEqual(body["days"], before["days"])
        self.assertEqual(titles(day(body, "2026-11-06")), titles(day(before, "2026-11-06")))
        self.assertTrue(self.service.healthy(), "under 5 minutes old")
        self.clock.advance(HEALTHY_SECONDS - 60)
        self.assertFalse(self.service.healthy(), "5 minutes since the last good fetch")
        self.assertEqual(self.service.agenda()[1]["stale_seconds"], HEALTHY_SECONDS)

    def test_a_recovery_clears_stale(self) -> None:
        self.service.refresh()
        self.fetch.fail = FetchError("household/shared", "no response (ConnectionRefusedError)")
        self.clock.advance(60)
        self.service.refresh()
        self.clock.advance(60)
        self.service.refresh()
        self.fetch.fail = None
        self.clock.advance(60)
        with self.assertLogs("calendar_view.service", "INFO") as logs:
            self.assertTrue(self.service.refresh())
        self.assertEqual(logs.output, ["INFO:calendar_view.service:fetch ok again after 2 failed attempts"])
        body = self.service.agenda()[1]
        self.assertEqual((body["stale"], body["stale_seconds"]), (False, 0))

    def test_now_moves_on_while_stale(self) -> None:
        """Within a day of the last good fetch, NOW/NEXT and the days follow the clock."""
        self.service.refresh()
        self.fetch.fail = FetchError("household/shared", "HTTP 503")
        self.clock.advance(14 * 3600)  # Saturday 00:30
        self.service.refresh()
        body = self.service.agenda()[1]
        self.assertEqual(body["days"][0]["date"], "2026-11-07")
        self.assertEqual(body["generated"], "2026-11-07T00:30:00-05:00")
        self.assertEqual(sorted(titles(body["today"]["now"])), ["Dinner window", "Night shift"])
        self.assertTrue(body["stale"])

    def test_after_a_day_of_failures_the_agenda_is_the_last_good_one(self) -> None:
        """Past the fetched range the week would be partly unknown: serve it as of the last good fetch."""
        self.service.refresh()
        self.fetch.fail = FetchError("household/shared", "HTTP 503")
        self.clock.advance(2 * 86400)
        self.service.refresh()
        body = self.service.agenda()[1]
        self.assertEqual(body["generated"], "2026-11-06T10:30:00-05:00")
        self.assertEqual(body["days"][0]["date"], "2026-11-06")
        self.assertEqual((body["stale"], body["stale_seconds"]), (True, 2 * 86400))

    def test_overdue_without_a_seen_failure_is_stale(self) -> None:
        self.service.refresh()
        self.clock.advance(STALE_AFTER_SECONDS + 1)
        self.assertTrue(self.service.agenda()[1]["stale"])

    def test_an_unexpected_error_logs_its_type_only(self) -> None:
        self.service.refresh()
        self.fetch.fail = RuntimeError(f"password={PASSWORD}")
        with self.assertLogs("calendar_view.service", "WARNING") as logs:
            self.assertFalse(self.service.refresh())
        self.assertEqual(logs.output, ["ERROR:calendar_view.service:fetch failed: unexpected RuntimeError"])
        self.assertTrue(self.service.agenda()[1]["stale"])

    def test_one_build_per_minute(self) -> None:
        self.service.refresh()
        first = self.service.agenda()[1]
        self.clock.advance(20)
        self.assertIs(self.service.agenda()[1]["days"], first["days"])
        self.clock.advance(40)
        self.assertIsNot(self.service.agenda()[1]["days"], first["days"])

    def test_the_loop_fetches_at_once_and_stops(self) -> None:
        stop = threading.Event()
        calls = []

        def fetch(start, end):
            calls.append(1)
            stop.set()
            return self.fetch.data

        service = AgendaService(config(), fetch, clock=self.clock.now, monotonic=self.clock.monotonic)
        thread = threading.Thread(target=service.run, args=(stop, 30), daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [1])
        self.assertTrue(service.healthy())


class OverHTTP(unittest.TestCase):
    """/healthz and /api/agenda as the page and the role's health check see them."""

    def setUp(self) -> None:
        require_libs()
        self.clock = FakeClock(ny(2026, 11, 6, 10, 30))
        self.fetch = ScriptedFetch()
        self.service = AgendaService(config(), self.fetch, clock=self.clock.now, monotonic=self.clock.monotonic)
        self.server = ViewServer(("127.0.0.1", 0), self.service)
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path: str):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            conn.request("GET", path)
            r = conn.getresponse()
            return r.status, r.getheader("Cache-Control"), r.read()
        finally:
            conn.close()

    def test_stale_then_unhealthy(self) -> None:
        self.assertEqual(self.get("/healthz")[0], 503)
        status, cache, body = self.get("/api/agenda")
        self.assertEqual((status, cache, json.loads(body)["stale"]), (503, "no-store", True))

        self.service.refresh()
        self.assertEqual(self.get("/healthz")[::2], (200, b"ok\n"))
        good = json.loads(self.get("/api/agenda")[2])

        self.fetch.fail = FetchError("household/shared", "HTTP 403")
        self.clock.advance(HEALTHY_SECONDS)
        with self.assertLogs("calendar_view.service", "WARNING"):
            self.service.refresh()
        self.assertEqual(self.get("/healthz")[::2], (503, b"stale\n"))
        stale = json.loads(self.get("/api/agenda")[2])
        self.assertEqual((stale["stale"], stale["stale_seconds"]), (True, HEALTHY_SECONDS))
        self.assertEqual(stale["days"][1:], good["days"][1:])
        self.assertNotIn(PASSWORD, json.dumps(stale))


if __name__ == "__main__":
    unittest.main()
