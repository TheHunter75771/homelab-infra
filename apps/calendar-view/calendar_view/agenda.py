"""The agenda: parsed calendars + a fixed "now" -> the JSON the page renders. Pure functions (no I/O,
no clock), so every rule is testable at any instant.

Recurrence, exceptions (EXDATE, RECURRENCE-ID overrides) and time zones come only from
recurring-ical-events and icalendar; zoneinfo gives the local midnights. What this module adds is the
placement rules of CONTRACTS §14.2:

- The window is [local midnight today, local midnight + `days` days), one day per local date. Day
  boundaries are wall-clock midnights, so 2026-11-01 (America/New_York's fall-back) is 25 hours long.
- A timed event shows on the day it starts. On each later day it shows again only if it ends more than
  0 minutes after 06:00 of that day: a 23:00-01:00 dinner window shows on its start day only, a 22:00-08:00
  night shift on both days.
- An all-day event shows on every date from DTSTART up to (not including) DTEND.
- Floating times (no TZID, no Z) are local. A UTC or TZID time is converted to local.
- Cancelled occurrences (STATUS:CANCELLED, e.g. one meeting of a series called off) don't show.
- NOW: timed events in progress (start <= now < end), wherever they started. NEXT: the earliest timed
  event that starts after now and before the end of today, or null.
- Every label is formatted here, in English, 24-hour: the page holds no date logic.

Instants are compared in UTC and turned into local time only for labels and dates. Python compares two
datetimes that share a tzinfo by their wall-clock fields, which is wrong inside the repeated 01:00-02:00
hour of a fall-back night.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import recurring_ical_events

log = logging.getLogger("calendar_view.agenda")

TITLE_MAX = 120
NO_TITLE = "(No title)"
CONTINUES_AFTER = time(6, 0)
DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
               "October", "November", "December")
# recurring-ical-events is asked for a day more on each side than the window; the placement below is exact.
QUERY_MARGIN = timedelta(days=1)
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Occurrence:
    title: str
    calendar: int  # index into the sources, i.e. display order
    start: datetime | date  # a UTC datetime, or a local date for all-day
    end: datetime | date  # exclusive; >= start
    all_day: bool


# --- labels ---------------------------------------------------------------------------------------------
def day_label(d: date) -> str:
    """FRI NOV 6"""
    return f"{DAYS[d.weekday()]} {MONTHS[d.month - 1]} {d.day}"


def date_label(d: date) -> str:
    """Friday, November 6"""
    return f"{DAY_NAMES[d.weekday()]}, {MONTH_NAMES[d.month - 1]} {d.day}"


def clock(moment: datetime) -> str:
    """11:00 (24-hour, in the moment's own zone; pass a local time)"""
    return f"{moment.hour:02d}:{moment.minute:02d}"


def _at(moment: datetime, day: date, tz: ZoneInfo) -> str:
    """The local time, with the weekday when it falls on another date than `day`: 17:00 or WED 17:00."""
    local = moment.astimezone(tz)
    return clock(local) if local.date() == day else f"{DAYS[local.weekday()]} {clock(local)}"


def end_label(start: datetime, end: datetime, tz: ZoneInfo) -> str:
    """The end, as shown next to the start on the start day. The weekday is added when the end is on a
    later date, except for an overnight event under a day (23:00-01:00 reads plainly)."""
    if end <= start:
        return ""
    s, e = start.astimezone(tz), end.astimezone(tz)
    if e.date() == s.date():
        return clock(e)
    if end.astimezone(UTC) - start.astimezone(UTC) < timedelta(days=1) and (e.hour, e.minute) <= (s.hour, s.minute):
        return clock(e)
    return _at(end, s.date(), tz)


def title_of(summary) -> str:
    """Plain text on one line, at most TITLE_MAX characters (an ellipsis marks a cut)."""
    text = "" if summary is None else str(summary)
    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in text)
    text = _SPACE_RE.sub(" ", text).strip()
    if not text:
        return NO_TITLE
    return text if len(text) <= TITLE_MAX else text[:TITLE_MAX - 1].rstrip() + "…"


# --- window and occurrences -----------------------------------------------------------------------------
def day_starts(now: datetime, tz: ZoneInfo, days: int) -> list[datetime]:
    """Local midnights: today's, then one per following date (days + 1 of them; the last ends the window)."""
    today = now.astimezone(tz).date()
    return [datetime.combine(today + timedelta(days=i), time(0), tzinfo=tz) for i in range(days + 1)]


def window(now: datetime, tz: ZoneInfo, days: int) -> tuple[datetime, datetime]:
    starts = day_starts(now, tz, days)
    return starts[0], starts[-1]


def _instant(value, tz: ZoneInfo):
    """A datetime as UTC (a floating one is local first); a date stays a date."""
    if isinstance(value, datetime):
        return (value.replace(tzinfo=tz) if value.tzinfo is None else value).astimezone(UTC)
    return value


def _span(component, tz: ZoneInfo):
    """(start, end, all_day), from icalendar's own DTSTART/DTEND/DURATION handling."""
    start, end = _instant(component.start, tz), _instant(component.end, tz)
    if isinstance(start, datetime):
        if not isinstance(end, datetime):
            end = datetime.combine(end, time(0), tzinfo=tz).astimezone(UTC)
        return start, max(start, end), False
    if isinstance(end, datetime):
        end = end.date()
    return start, end if end > start else start + timedelta(days=1), True


def occurrences(sources, start: datetime, end: datetime, tz: ZoneInfo) -> list[Occurrence]:
    """Every VEVENT occurrence overlapping [start, end) in the sources: [(CalendarSource, [Calendar])]."""
    out: list[Occurrence] = []
    for index, (source, objects) in enumerate(sources):
        for calendar in objects:
            try:
                query = recurring_ical_events.of(calendar, components=["VEVENT"], skip_bad_series=True)
                for component in query.between(start, end):
                    if str(component.get("STATUS", "")).upper() == "CANCELLED":
                        continue
                    s, e, all_day = _span(component, tz)
                    out.append(Occurrence(title_of(component.get("SUMMARY")), index, s, e, all_day))
            except Exception:  # noqa: BLE001 - one broken resource (no DTSTART, a bad RRULE) must not blank the week
                log.warning("skipped an event that couldn't be expanded in %s", source.path)
    return out


# --- placement ------------------------------------------------------------------------------------------
def _entry(occ: Occurrence, sources, start_label: str, end_label_: str) -> dict:
    source = sources[occ.calendar][0]
    return {"title": occ.title, "calendar": source.label, "color": source.color,
            "start_label": start_label, "end_label": end_label_, "all_day": occ.all_day}


def _timed_labels(occ: Occurrence, day_start: datetime, tz: ZoneInfo) -> tuple[str, str]:
    """On its start day: 11:00 / 14:00. On a day it continues into: "Until 08:00" (or "Until WED 17:00")."""
    if occ.start >= day_start:
        return clock(occ.start.astimezone(tz)), end_label(occ.start, occ.end, tz)
    return "Until " + _at(occ.end, day_start.astimezone(tz).date(), tz), ""


def _sort_key(occ: Occurrence, day_start: datetime):
    day_start = day_start.astimezone(UTC)
    if occ.all_day:
        return (0, day_start, day_start, occ.calendar, occ.title)
    return (1, max(occ.start, day_start), occ.end, occ.calendar, occ.title)


def shows_on(occ: Occurrence, day_start: datetime, day_end: datetime, tz: ZoneInfo) -> bool:
    """day_start/day_end: local midnights (aware)."""
    local_date = day_start.astimezone(tz).date()
    if occ.all_day:
        return occ.start <= local_date < occ.end
    day_start, day_end = day_start.astimezone(UTC), day_end.astimezone(UTC)
    if day_start <= occ.start < day_end:
        return True
    cutoff = datetime.combine(local_date, CONTINUES_AFTER, tzinfo=tz).astimezone(UTC)
    return occ.start < day_start and occ.end > cutoff


def build(sources, now: datetime, tz: ZoneInfo, days: int) -> dict:
    """{today: {date_label, now: [event], next: event|null}, days: [{date, label, events: [event]}]}

    sources: [(CalendarSource, [icalendar.Calendar, ...]), ...] in display order. now: an aware datetime.
    event: {title, calendar, color, start_label, end_label, all_day}.
    """
    now = now.astimezone(UTC)
    starts = day_starts(now, tz, days)
    occs = occurrences(sources, starts[0] - QUERY_MARGIN, starts[-1] + QUERY_MARGIN, tz)

    out_days = []
    for i in range(days):
        ds, de = starts[i], starts[i + 1]
        placed = sorted((o for o in occs if shows_on(o, ds, de, tz)), key=lambda o, ds=ds: _sort_key(o, ds))
        events = [_entry(o, sources, "All day", "") if o.all_day else _entry(o, sources, *_timed_labels(o, ds, tz))
                  for o in placed]
        out_days.append({"date": ds.date().isoformat(), "label": day_label(ds.date()), "events": events})

    today_start, today_end = starts[0].astimezone(UTC), starts[1].astimezone(UTC)
    timed = [o for o in occs if not o.all_day]
    in_progress = sorted((o for o in timed if o.start <= now < o.end), key=lambda o: _sort_key(o, today_start))
    upcoming = sorted((o for o in timed if now < o.start < today_end), key=lambda o: _sort_key(o, today_start))
    return {
        "today": {
            "date_label": date_label(starts[0].date()),
            "now": [_entry(o, sources, *_timed_labels(o, today_start, tz)) for o in in_progress],
            "next": _entry(upcoming[0], sources, *_timed_labels(upcoming[0], today_start, tz)) if upcoming else None,
        },
        "days": out_days,
    }
