"""The agenda builder (CONTRACTS §14.2) with fixtures and fixed clocks: recurrence with EXDATE and a moved
and a cancelled instance, all-day and multi-day all-day, the 2026-11-01 DST change, midnight-crossing
events (the 23:00-01:00 rule), floating times, UTC times, window edges, NOW/NEXT, title capping."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .helpers import (
    HAVE_LIBS,
    NY,
    ROUTINES,
    SCHOOL,
    SHARED,
    day,
    ny,
    placements,
    require_libs,
    sources,
    titles,
)

if HAVE_LIBS:
    import icalendar
    from calendar_view import agenda

FRIDAY = ny(2026, 11, 6, 10, 30)  # window: Fri 2026-11-06 00:00 .. Fri 2026-11-13 00:00


class Base(unittest.TestCase):
    def setUp(self) -> None:
        require_libs()

    def build(self, now: datetime, *pairs, days: int = 7, tz: ZoneInfo = NY) -> dict:
        return agenda.build(sources(*pairs), now, tz, days)


class Labels(Base):
    def test_labels(self) -> None:
        self.assertEqual(agenda.day_label(date(2026, 11, 6)), "FRI NOV 6")
        self.assertEqual(agenda.day_label(date(2026, 12, 28)), "MON DEC 28")
        self.assertEqual(agenda.date_label(date(2026, 11, 6)), "Friday, November 6")
        self.assertEqual(agenda.clock(ny(2026, 11, 6, 7, 5)), "07:05")
        self.assertEqual(agenda.clock(ny(2026, 11, 6, 23, 59)), "23:59")

    def test_end_labels(self) -> None:
        s = ny(2026, 11, 6, 11, 0)
        self.assertEqual(agenda.end_label(s, ny(2026, 11, 6, 14, 0), NY), "14:00")
        self.assertEqual(agenda.end_label(s, s, NY), "")
        self.assertEqual(agenda.end_label(ny(2026, 11, 6, 23, 0), ny(2026, 11, 7, 1, 0), NY), "01:00")
        self.assertEqual(agenda.end_label(ny(2026, 11, 6, 22, 0), ny(2026, 11, 7, 0, 0), NY), "00:00")
        # Over a day, or ending later in the clock than it started: the weekday says which day.
        self.assertEqual(agenda.end_label(ny(2026, 11, 6, 10, 0), ny(2026, 11, 7, 14, 0), NY), "SAT 14:00")
        self.assertEqual(agenda.end_label(ny(2026, 11, 9, 9, 0), ny(2026, 11, 11, 17, 0), NY), "WED 17:00")
        # Instants in UTC are labelled in local time.
        utc = ZoneInfo("UTC")
        self.assertEqual(agenda.end_label(datetime(2026, 11, 6, 15, 0, tzinfo=utc), datetime(2026, 11, 6, 16, 0, tzinfo=utc), NY),
                         "11:00")

    def test_the_window_is_seven_local_days(self) -> None:
        result = self.build(FRIDAY, ("today.ics", SHARED))
        self.assertEqual([d["date"] for d in result["days"]],
                         ["2026-11-06", "2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"])
        self.assertEqual([d["label"] for d in result["days"]][:3], ["FRI NOV 6", "SAT NOV 7", "SUN NOV 8"])
        self.assertEqual(result["today"]["date_label"], "Friday, November 6")
        self.assertEqual(set(result), {"today", "days"})
        self.assertEqual(set(result["today"]), {"date_label", "now", "next"})
        for d in result["days"]:
            self.assertEqual(set(d), {"date", "label", "events"})
        self.assertEqual(len(self.build(FRIDAY, ("today.ics", SHARED), days=3)["days"]), 3)

    def test_today_follows_the_configured_zone_not_utc(self) -> None:
        # 2026-11-07 02:00 UTC is still Friday evening in New York.
        result = self.build(datetime(2026, 11, 7, 2, 0, tzinfo=ZoneInfo("UTC")), ("today.ics", SHARED))
        self.assertEqual(result["days"][0]["date"], "2026-11-06")

    def test_event_fields(self) -> None:
        result = self.build(FRIDAY, ("today.ics", SHARED))
        lunch = next(e for e in day(result, "2026-11-06") if e["title"] == "Lunch")
        self.assertEqual(lunch, {"title": "Lunch", "calendar": "Shared", "color": "#4f8cff",
                                 "start_label": "12:00", "end_label": "13:00", "all_day": False})


class Recurrence(Base):
    def test_weekly_with_exdate_moved_and_cancelled_instances(self) -> None:
        result = self.build(ny(2026, 11, 2, 8, 0), ("weekly-exdate.ics", SHARED), days=14)
        self.assertEqual(placements(result, "Weekly practice"),
                         {"2026-11-02": ("18:00", "19:30", False), "2026-11-09": ("18:00", "19:30", False)})
        # 2026-11-16 is the EXDATE: nothing that day.
        later = self.build(ny(2026, 11, 16, 8, 0), ("weekly-exdate.ics", SHARED), days=14)
        self.assertEqual(day(later, "2026-11-16"), [])
        # The 23rd was moved to 19:00 and renamed (RECURRENCE-ID); the 30th was cancelled (STATUS:CANCELLED).
        self.assertEqual(placements(later, "Weekly practice (moved)"), {"2026-11-23": ("19:00", "20:30", False)})
        self.assertEqual(placements(later, "Weekly practice"), {})
        cancelled = self.build(ny(2026, 11, 30, 8, 0), ("weekly-exdate.ics", SHARED), days=1)
        self.assertEqual(day(cancelled, "2026-11-30"), [])
        # The series' last instance (COUNT=10) is 2027-01-04.
        self.assertEqual(placements(self.build(ny(2027, 1, 4, 8, 0), ("weekly-exdate.ics", SHARED), days=14),
                                    "Weekly practice"), {"2027-01-04": ("18:00", "19:30", False)})

    def test_a_daily_series_keeps_its_wall_clock_time_across_the_dst_change(self) -> None:
        result = self.build(ny(2026, 10, 31, 12, 0), ("dst-crossing.ics", ROUTINES))
        self.assertEqual(placements(result, "Morning routine"),
                         {d: ("07:00", "07:30", False) for d in
                          ("2026-10-31", "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06")})


class AllDay(Base):
    def test_all_day_and_multi_day_all_day(self) -> None:
        result = self.build(FRIDAY, ("all-day.ics", SHARED), ("today.ics", SHARED))
        self.assertEqual(placements(result, "Family visit"),
                         {d: ("All day", "", True) for d in ("2026-11-06", "2026-11-07", "2026-11-08")})
        # No DTEND: one day.
        self.assertEqual(placements(result, "Trash day"), {"2026-11-10": ("All day", "", True)})
        self.assertEqual(placements(result, "Holiday"), {})  # 2026-11-26: outside the window
        # All-day events come first in a day.
        self.assertEqual(titles(day(result, "2026-11-06"))[0], "Family visit")
        self.assertEqual(self.build(ny(2026, 11, 26, 9, 0), ("all-day.ics", SHARED))["days"][0]["events"][0]["title"],
                         "Holiday")


class DaylightSaving(Base):
    def test_the_event_crossing_the_2026_11_01_change(self) -> None:
        result = self.build(ny(2026, 10, 31, 12, 0), ("dst-crossing.ics", SHARED))
        self.assertEqual(placements(result, "Overnight shift across the fall-back change"),
                         {"2026-11-01": ("00:30", "03:30", False)})

    def test_the_25_hour_day_has_local_midnight_boundaries(self) -> None:
        starts = agenda.day_starts(ny(2026, 11, 1, 12, 0), NY, 2)
        self.assertEqual([s.isoformat() for s in starts],
                         ["2026-11-01T00:00:00-04:00", "2026-11-02T00:00:00-05:00", "2026-11-03T00:00:00-05:00"])
        self.assertEqual(starts[1].timestamp() - starts[0].timestamp(), timedelta(hours=25).total_seconds())
        # An event at 23:30 EST on the long day belongs to that day, not the next.
        cal = icalendar.Calendar.from_ical(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\nUID:late@example.com\r\n"
            "DTSTAMP:20261101T120000Z\r\nSUMMARY:Late on the long day\r\nDTSTART:20261102T043000Z\r\n"
            "DTEND:20261102T045000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        result = agenda.build(((SHARED, (cal,)),), ny(2026, 11, 1, 12, 0), NY, 2)
        self.assertEqual(placements(result, "Late on the long day"), {"2026-11-01": ("23:30", "23:50", False)})


    def test_now_inside_the_repeated_hour(self) -> None:
        """01:00-02:00 happens twice on 2026-11-01. Wall-clock comparison would call the first pass's event
        "now" during the second pass; instants are compared in UTC instead."""
        cal = icalendar.Calendar.from_ical(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\n"
            "BEGIN:VEVENT\r\nUID:first@example.com\r\nDTSTAMP:20261101T120000Z\r\nSUMMARY:First pass\r\n"
            "DTSTART:20261101T050000Z\r\nDTEND:20261101T054500Z\r\nEND:VEVENT\r\n"  # 01:00-01:45 EDT
            "BEGIN:VEVENT\r\nUID:second@example.com\r\nDTSTAMP:20261101T120000Z\r\nSUMMARY:Second pass\r\n"
            "DTSTART:20261101T061500Z\r\nDTEND:20261101T065000Z\r\nEND:VEVENT\r\n"  # 01:15-01:50 EST
            "END:VCALENDAR\r\n")
        now = datetime(2026, 11, 1, 6, 30, tzinfo=ZoneInfo("UTC"))  # 01:30 EST, the second pass
        result = agenda.build(((SHARED, (cal,)),), now, NY, 1)
        self.assertEqual([(e["title"], e["start_label"], e["end_label"]) for e in result["today"]["now"]],
                         [("Second pass", "01:15", "01:50")])
        self.assertEqual(titles(day(result, "2026-11-01")), ["First pass", "Second pass"])


class Midnight(Base):
    def test_the_dinner_window_shows_on_its_start_day_only(self) -> None:
        result = self.build(FRIDAY, ("overnight.ics", ROUTINES))
        self.assertEqual(placements(result, "Dinner window"), {"2026-11-06": ("23:00", "01:00", False)})

    def test_an_event_past_six_continues_the_next_day(self) -> None:
        result = self.build(FRIDAY, ("overnight.ics", ROUTINES))
        self.assertEqual(placements(result, "Night shift"),
                         {"2026-11-06": ("22:00", "08:00", False), "2026-11-07": ("Until 08:00", "", False)})
        # Exactly 06:00 is 0 minutes after: not continued.
        self.assertEqual(placements(result, "Ends at six"), {"2026-11-07": ("23:00", "06:00", False)})
        self.assertEqual(placements(result, "Conference"),
                         {"2026-11-09": ("09:00", "WED 17:00", False), "2026-11-10": ("Until WED 17:00", "", False),
                          "2026-11-11": ("Until 17:00", "", False)})

    def test_a_continuing_event_sorts_first_among_timed_events(self) -> None:
        result = self.build(FRIDAY, ("overnight.ics", ROUTINES), ("floating.ics", SHARED))
        self.assertEqual(titles(day(result, "2026-11-07"))[:2], ["Night shift", "Floating breakfast"])

    def test_yesterdays_event_still_running_shows_today(self) -> None:
        result = self.build(ny(2026, 11, 7, 7, 0), ("overnight.ics", ROUTINES))
        self.assertEqual(titles(day(result, "2026-11-07")), ["Night shift", "Ends at six"])
        self.assertEqual(day(result, "2026-11-07")[0]["start_label"], "Until 08:00")


class Floating(Base):
    def test_floating_times_are_local(self) -> None:
        result = self.build(FRIDAY, ("floating.ics", SHARED))
        self.assertEqual(placements(result, "Floating breakfast"), {"2026-11-07": ("09:00", "10:00", False)})
        self.assertEqual(placements(result, "Floating late"), {"2026-11-12": ("23:30", "00:30", False)})
        self.assertEqual(placements(result, "Floating after the window"), {})

    def test_floating_times_follow_the_configured_zone(self) -> None:
        utc = ZoneInfo("UTC")
        result = self.build(datetime(2026, 11, 6, 15, 0, tzinfo=utc), ("floating.ics", SHARED), tz=utc)
        self.assertEqual(placements(result, "Floating breakfast"), {"2026-11-07": ("09:00", "10:00", False)})

    def test_utc_times_are_converted(self) -> None:
        result = self.build(FRIDAY, ("edges.ics", SHARED))
        self.assertEqual(placements(result, "UTC call"), {"2026-11-06": ("10:00", "11:00", False)})
        # 03:00Z on the 7th is 22:00 on the 6th in New York.
        self.assertEqual(placements(result, "UTC late"), {"2026-11-06": ("22:00", "23:00", False)})


class WindowEdges(Base):
    def test_window_edges(self) -> None:
        result = self.build(FRIDAY, ("edges.ics", SHARED))
        self.assertEqual(placements(result, "Ends at window start"), {})
        self.assertEqual(placements(result, "Starts at window start"), {"2026-11-06": ("00:00", "00:30", False)})
        self.assertEqual(placements(result, "Last minute"), {"2026-11-12": ("23:30", "23:59", False)})
        self.assertEqual(placements(result, "Starts at window end"), {})
        self.assertEqual(placements(result, "All-day before"), {})
        self.assertEqual(placements(result, "All-day across the end"), {"2026-11-12": ("All day", "", True)})

    def test_one_minute_before_midnight_is_still_today(self) -> None:
        result = self.build(ny(2026, 11, 6, 23, 59), ("edges.ics", SHARED))
        self.assertEqual(result["days"][0]["date"], "2026-11-06")
        result = self.build(ny(2026, 11, 7, 0, 0), ("edges.ics", SHARED))
        self.assertEqual(result["days"][0]["date"], "2026-11-07")
        self.assertEqual(placements(result, "Starts at window end"), {"2026-11-13": ("00:00", "01:00", False)})


class NowAndNext(Base):
    def at(self, *clock) -> dict:
        return self.build(ny(*clock), ("today.ics", SHARED), ("overnight.ics", ROUTINES))["today"]

    def test_before_the_first_event(self) -> None:
        today = self.at(2026, 11, 6, 9, 59)
        self.assertEqual(today["now"], [])
        # Two start at 10:00: the one ending first.
        self.assertEqual((today["next"]["title"], today["next"]["start_label"], today["next"]["end_label"]),
                         ("Standup", "10:00", "10:15"))

    def test_an_event_starting_now_is_now_not_next(self) -> None:
        today = self.at(2026, 11, 6, 10, 0)
        self.assertEqual(titles(today["now"]), ["Standup", "Workout"])
        self.assertEqual(today["next"]["title"], "Quick call")

    def test_an_event_ending_now_is_over(self) -> None:
        today = self.at(2026, 11, 6, 10, 15)
        self.assertEqual(titles(today["now"]), ["Workout"])

    def test_a_zero_length_event_is_next_but_never_now(self) -> None:
        today = self.at(2026, 11, 6, 15, 0)
        self.assertEqual(today["now"], [])
        self.assertEqual((today["next"]["title"], today["next"]["start_label"], today["next"]["end_label"]),
                         ("Reminder", "16:00", ""))
        self.assertEqual(self.at(2026, 11, 6, 16, 0)["now"], [])

    def test_late_evening(self) -> None:
        today = self.at(2026, 11, 6, 23, 30)
        self.assertEqual(titles(today["now"]), ["Night shift", "Dinner window"])
        self.assertIsNone(today["next"])  # nothing else starts before midnight

    def test_after_midnight_yesterdays_events_are_still_now(self) -> None:
        result = self.build(ny(2026, 11, 7, 0, 30), ("today.ics", SHARED), ("overnight.ics", ROUTINES))
        now = {e["title"]: (e["start_label"], e["end_label"]) for e in result["today"]["now"]}
        self.assertEqual(now, {"Night shift": ("Until 08:00", ""), "Dinner window": ("Until 01:00", "")})
        # The dinner window doesn't show in Saturday's list (the 23:00-01:00 rule); the night shift does.
        self.assertEqual(titles(day(result, "2026-11-07")), ["Night shift", "Ends at six"])
        self.assertEqual(result["today"]["next"]["title"], "Ends at six")

    def test_next_is_today_only(self) -> None:
        result = self.build(ny(2026, 11, 8, 12, 0), ("overnight.ics", ROUTINES))
        self.assertIsNone(result["today"]["next"])  # the conference is tomorrow
        self.assertEqual(result["today"]["now"], [])


class Titles(Base):
    def test_title_capping_and_plain_text(self) -> None:
        result = self.build(FRIDAY, ("long-title.ics", SHARED))
        long_title = day(result, "2026-11-06")[0]["title"]
        self.assertEqual(len(long_title), 120)
        self.assertTrue(long_title.startswith("Parent-teacher conference about the spring science fair project, the field"))
        self.assertTrue(long_title.endswith("…"))
        self.assertNotIn("\n", long_title)
        self.assertEqual(day(result, "2026-11-07")[0]["title"], "(No title)")

    def test_title_of(self) -> None:
        self.assertEqual(agenda.title_of("  Pick\nup\tthe\x00 kids now  "), "Pick up the kids now")
        self.assertEqual(agenda.title_of(None), "(No title)")
        self.assertEqual(agenda.title_of(" \n "), "(No title)")
        self.assertEqual(agenda.title_of("<b>Bold</b> & co"), "<b>Bold</b> & co")  # plain text, never markup
        self.assertEqual(agenda.title_of("x" * 120), "x" * 120)
        self.assertEqual(agenda.title_of("x" * 121), "x" * 119 + "…")


class Robustness(Base):
    def test_a_broken_resource_is_skipped_and_the_rest_shown(self) -> None:
        broken = icalendar.Calendar.from_ical(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\nUID:broken@example.com\r\n"
            "DTSTAMP:20261101T120000Z\r\nSUMMARY:No start\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        good = sources(("today.ics", SHARED))[0][1]
        with self.assertLogs("calendar_view.agenda", "WARNING") as logs:
            result = agenda.build(((SHARED, (broken, *good)),), FRIDAY, NY, 7)
        self.assertIn("Lunch", titles(day(result, "2026-11-06")))
        self.assertEqual(logs.output, ["WARNING:calendar_view.agenda:skipped an event that couldn't be expanded in household/shared"])

    def test_calendar_order_breaks_ties(self) -> None:
        cal = sources(("today.ics", SHARED))[0][1]
        result = agenda.build(((SCHOOL, cal), (SHARED, cal)), FRIDAY, NY, 1)
        lunch = [e["calendar"] for e in day(result, "2026-11-06") if e["title"] == "Lunch"]
        self.assertEqual(lunch, ["School", "Shared"])

    def test_no_calendars_gives_empty_days_not_an_error(self) -> None:
        result = agenda.build(((SHARED, ()),), FRIDAY, NY, 7)
        self.assertEqual([d["events"] for d in result["days"]], [[]] * 7)
        self.assertEqual(result["today"], {"date_label": "Friday, November 6", "now": [], "next": None})


if __name__ == "__main__":
    unittest.main()
