"""The last good fetch, its age, and the agenda built from it.

A background loop fetches every calendar every REFRESH_SECONDS. A fetch is all or nothing: when one
calendar fails, the previous snapshot stays (so a calendar is never silently missing), and the agenda is
served from it, marked stale with its age. There is never an empty week that looks real: with no good
fetch yet, /api/agenda answers 503.

The agenda is rebuilt from the snapshot for the current minute, so NOW/NEXT and the day boundaries stay
right while the data is a few minutes old. The fetch asks for a day more on each side than the window,
so a snapshot covers the window for up to a day after it was taken; past that (more than a day of
failures) the agenda is the one as of the snapshot's own time, still marked stale.

/healthz is 200 while the last good fetch is under HEALTHY_SECONDS old (CONTRACTS §14.2).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import agenda
from .caldav import FetchError
from .config import Config

log = logging.getLogger("calendar_view.service")

REFRESH_SECONDS = 60
HEALTHY_SECONDS = 300
# Stale even without a seen failure once a fetch is this overdue (a fetch stuck on a slow server).
STALE_AFTER_SECONDS = 150
FETCH_MARGIN = timedelta(days=1)


@dataclass(frozen=True)
class Snapshot:
    sources: tuple  # ((CalendarSource, (icalendar.Calendar, ...)), ...)
    fetched_at: datetime  # local, aware
    fetched_mono: float
    covers_from: datetime
    covers_to: datetime


class AgendaService:
    def __init__(self, config: Config, fetch: Callable[[datetime, datetime], tuple],
                 clock: Callable[[], datetime] | None = None, monotonic: Callable[[], float] = time.monotonic):
        self._config = config
        self._tz = config.tz
        self._fetch = fetch
        self._clock = clock or (lambda: datetime.now(self._tz))
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._failures = 0
        self._memo: tuple | None = None

    # --- fetching ---------------------------------------------------------------------------------------
    def refresh(self) -> bool:
        """One fetch of every calendar. True when it replaced the snapshot."""
        now = self._clock().astimezone(self._tz)
        start, end = agenda.window(now, self._tz, self._config.days)
        start, end = start - FETCH_MARGIN, end + FETCH_MARGIN
        try:
            sources = self._fetch(start, end)
        except FetchError as e:
            self._failed()
            log.warning("fetch failed: %s, %s", e.path, e.status)
            return False
        except Exception as e:
            self._failed()
            log.error("fetch failed: unexpected %s", type(e).__name__)
            log.debug("fetch failure detail", exc_info=True)
            return False
        snapshot = Snapshot(sources, now, self._monotonic(), start, end)
        with self._lock:
            self._snapshot, failures, self._failures = snapshot, self._failures, 0
        if failures:
            log.info("fetch ok again after %d failed attempt%s", failures, "" if failures == 1 else "s")
        return True

    def _failed(self) -> None:
        with self._lock:
            self._failures += 1

    def run(self, stop: threading.Event, interval: float = REFRESH_SECONDS) -> None:
        """Fetch now, then every `interval` seconds until `stop` is set."""
        while True:
            self.refresh()
            if stop.wait(interval):
                return

    # --- reading ----------------------------------------------------------------------------------------
    def _age(self, snapshot: Snapshot) -> float:
        return max(0.0, self._monotonic() - snapshot.fetched_mono)

    def healthy(self) -> bool:
        with self._lock:
            snapshot = self._snapshot
        return snapshot is not None and self._age(snapshot) < HEALTHY_SECONDS

    def agenda(self) -> tuple[int, dict]:
        """(HTTP status, body) for /api/agenda."""
        with self._lock:
            snapshot, failures = self._snapshot, self._failures
        common = {"timezone": self._config.timezone,
                  "calendars": [{"label": c.label, "color": c.color} for c in self._config.calendars]}
        if snapshot is None:
            return 503, {"error": "no calendar data yet", "generated": None, "stale": True, "stale_seconds": None,
                         **common, "today": None, "days": None}
        age = self._age(snapshot)
        now = self._clock().astimezone(self._tz)
        start, end = agenda.window(now, self._tz, self._config.days)
        as_of = now if snapshot.covers_from <= start and end <= snapshot.covers_to else snapshot.fetched_at
        as_of = as_of.replace(second=0, microsecond=0)
        body = self._build(snapshot, as_of)
        return 200, {"generated": as_of.isoformat(), "stale": failures > 0 or age > STALE_AFTER_SECONDS,
                     "stale_seconds": int(age), **common, **body}

    def _build(self, snapshot: Snapshot, as_of: datetime) -> dict:
        """One build per snapshot and minute; requests in between reuse it (and never build concurrently)."""
        key = (id(snapshot), as_of)
        with self._build_lock:
            if self._memo is not None and self._memo[0] == key and self._memo[1] is snapshot:
                return self._memo[2]
            body = agenda.build(snapshot.sources, as_of, self._tz, self._config.days)
            self._memo = (key, snapshot, body)
            return body
