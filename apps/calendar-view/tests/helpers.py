"""Shared by the tests: fixtures as parsed calendars, placeholder settings, fake clocks.

Placeholders only: generic logins (alice, household, viewer), example.com UIDs, test-only passwords.
icalendar and recurring-ical-events are required (the agenda can't be built without them); under CI
(CI=true) a missing one is a failure, locally a skip.
"""

from __future__ import annotations

import importlib.util
import os
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from calendar_view.config import CalendarSource, validate

FIXTURES = Path(__file__).resolve().parent / "fixtures"
APP = Path(__file__).resolve().parent.parent
NY = ZoneInfo("America/New_York")
IN_CI = os.environ.get("CI", "").lower() == "true"
HAVE_LIBS = all(importlib.util.find_spec(m) for m in ("icalendar", "recurring_ical_events"))
PASSWORD = "test-only-viewer-password-0001"

SHARED = CalendarSource("household/shared", "Shared", "#4f8cff")
SCHOOL = CalendarSource("household/school", "School", "#3ccf91")
ROUTINES = CalendarSource("alice/routines", "Routines", "#f2a93b")


def require_libs() -> None:
    if not HAVE_LIBS:
        if IN_CI:
            raise AssertionError("icalendar and recurring-ical-events are required in CI")
        raise unittest.SkipTest("icalendar / recurring-ical-events not installed")


def ny(*args) -> datetime:
    return datetime(*args, tzinfo=NY)


def calendar(name: str):
    import icalendar

    return icalendar.Calendar.from_ical((FIXTURES / name).read_text())


def sources(*pairs):
    """sources(("today.ics", SHARED), ("overnight.ics", ROUTINES)) -> the shape fetch_all returns.
    One source per calendar config, in order; several fixtures may share a source."""
    grouped: dict = {}
    for name, source in pairs:
        grouped.setdefault(source, []).append(calendar(name))
    return tuple((source, tuple(objects)) for source, objects in grouped.items())


def config(**overrides):
    raw = {
        "base_url": "http://127.0.0.1:5232",
        "user": "viewer",
        "password": PASSWORD,
        "timezone": "America/New_York",
        "days": 7,
        "listen_host": "127.0.0.1",
        "listen_port": 0,
        "calendars": [{"path": s.path, "label": s.label, "color": s.color} for s in (SHARED, SCHOOL, ROUTINES)],
    }
    raw.update(overrides)
    return validate(raw)


class FakeClock:
    """A wall clock (aware) and a monotonic clock that only move when told to."""

    def __init__(self, wall: datetime) -> None:
        self.wall = wall
        self.mono = 1000.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


def day(result: dict, iso: str) -> list[dict]:
    return next(d["events"] for d in result["days"] if d["date"] == iso)


def titles(events: list[dict]) -> list[str]:
    return [e["title"] for e in events]


def placements(result: dict, title: str) -> dict[str, tuple[str, str, bool]]:
    """{date: (start_label, end_label, all_day)} for every day an event titled `title` shows on."""
    out = {}
    for d in result["days"]:
        for e in d["events"]:
            if e["title"] == title:
                out[d["date"]] = (e["start_label"], e["end_label"], e["all_day"])
    return out
