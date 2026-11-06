"""The fetch against a real Radicale 3.6.0 (skipped when radicale isn't importable), with its own rights file
in the shape of CONTRACTS §14.1 (roles/calendar/templates/rights.j2): owner_only for the accounts, then one
rule giving the viewer `r` on exactly its calendars. Nothing here depends on the calendar role's files.

It proves the viewer reads its two calendars through the app's own REPORT (recurrence, EXDATE and all), and
is refused elsewhere: an unlisted calendar, another home, the root, and every write. A refused fetch logs
the calendar path and the status, never the password.
"""

from __future__ import annotations

import base64
import http.client
import importlib.util
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from .helpers import (
    FIXTURES,
    HAVE_LIBS,
    NY,
    ROUTINES,
    SCHOOL,
    SHARED,
    day,
    ny,
    placements,
    require_libs,
    titles,
)

HAVE_RADICALE = importlib.util.find_spec("radicale") is not None
ACCOUNTS = ("alice", "household")
VIEWER = "viewer"
VIEWER_READS = ("household/shared", "alice/routines")
# Owner rules for the accounts only (never a viewer), then the viewer's reads; first match wins, no match
# means no access. Radicale str.format()s both patterns and full-matches them.
RIGHTS = f"""
[owner-root]
user: (?:{'|'.join(ACCOUNTS)})
collection:
permissions: R

[owner-home]
user: (?:{'|'.join(ACCOUNTS)})
collection: {{user}}
permissions: RW

[owner-calendars]
user: (?:{'|'.join(ACCOUNTS)})
collection: {{user}}/[^/]+
permissions: rw

[viewer-{VIEWER}]
user: {VIEWER}
collection: (?:{'|'.join(VIEWER_READS)})
permissions: r
"""
PRIVATE = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\nUID:private@example.com\r\n"
           "DTSTAMP:20261101T120000Z\r\nSUMMARY:Private appointment\r\nDTSTART:20261106T160000Z\r\n"
           "DTEND:20261106T170000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")

if HAVE_LIBS:
    from calendar_view import agenda, caldav
    from calendar_view.service import AgendaService

    from .helpers import FakeClock, config


def one_per_uid(text: str) -> dict[str, str]:
    """A fixture split the way a CalDAV client stores it: one resource per UID (with the VTIMEZONEs)."""
    import icalendar

    whole = icalendar.Calendar.from_ical(text)
    zones = [c for c in whole.subcomponents if c.name == "VTIMEZONE"]
    out: dict[str, icalendar.Calendar] = {}
    for component in whole.subcomponents:
        if component.name != "VEVENT":
            continue
        uid = str(component["UID"])
        if uid not in out:
            out[uid] = icalendar.Calendar()
            for key in ("VERSION", "PRODID", "CALSCALE"):
                if key in whole:
                    out[uid].add(key, whole[key])
            for zone in zones:
                out[uid].add_component(zone)
        out[uid].add_component(component)
    return {uid.split("@")[0] + ".ics": cal.to_ical().decode() for uid, cal in out.items()}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(HAVE_RADICALE, "radicale not installed (pip install radicale==<versions.yml pin>)")
class RealRadicale(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_libs()
        cls.tmp = Path(tempfile.mkdtemp(prefix="calendar-view-radicale-"))
        # Class cleanups run even when setUpClass fails half-way (tearDownClass wouldn't): no leaked server.
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.port = free_port()
        cls.passwords = {u: f"test-only-{u}-password-0001" for u in (*ACCOUNTS, VIEWER)}
        # htpasswd plain: this test is about rights and the fetch, not hashing (role calendar tests bcrypt).
        (cls.tmp / "users").write_text("".join(f"{u}:{p}\n" for u, p in cls.passwords.items()))
        (cls.tmp / "rights").write_text(RIGHTS)
        (cls.tmp / "config").write_text(f"""
[server]
hosts = 127.0.0.1:{cls.port}
[auth]
type = htpasswd
htpasswd_filename = {cls.tmp / 'users'}
htpasswd_encryption = plain
delay = 0
[rights]
type = from_file
file = {cls.tmp / 'rights'}
permit_delete_collection = False
permit_overwrite_collection = False
[storage]
filesystem_folder = {cls.tmp / 'collections'}
[web]
type = none
[logging]
level = info
mask_passwords = True
""")
        cls.log = open(cls.tmp / "server.log", "wb")  # noqa: SIM115 - closed by a class cleanup
        cls.addClassCleanup(cls.log.close)
        cls.proc = subprocess.Popen([sys.executable, "-m", "radicale", "--config", str(cls.tmp / "config")],
                                    cwd=cls.tmp, stdout=cls.log, stderr=subprocess.STDOUT)
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 30
        while True:
            if cls.proc.poll() is not None:
                raise AssertionError("radicale exited at start:\n" + cls.server_log())
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=1).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise AssertionError("radicale never listened:\n" + cls.server_log()) from None
                time.sleep(0.2)
        # The owners make their calendars and fill them; the viewer only ever reads.
        for user, cal in (("household", "shared"), ("household", "school"), ("alice", "routines"), ("alice", "personal")):
            assert cls.request("PROPFIND", "/", user, headers={"Depth": "0"})[0] == 207, cls.server_log()
            assert cls.request("MKCALENDAR", f"/{user}/{cal}/", user)[0] == 201, cls.server_log()
        for collection, text in (("household/shared", (FIXTURES / "weekly-exdate.ics").read_text()),
                                 ("household/shared", (FIXTURES / "all-day.ics").read_text()),
                                 ("household/school", (FIXTURES / "today.ics").read_text()),
                                 ("alice/routines", (FIXTURES / "overnight.ics").read_text()),
                                 ("alice/routines", (FIXTURES / "dst-crossing.ics").read_text()),
                                 ("alice/personal", PRIVATE)):
            owner = collection.split("/", 1)[0]
            for name, resource in one_per_uid(text).items():
                path = f"/{collection}/{name}"
                status = cls.request("PUT", path, owner, body=resource, headers={"Content-Type": "text/calendar"})[0]
                assert status == 201, (path, status, cls.server_log())

    @classmethod
    def stop_server(cls) -> None:
        cls.proc.terminate()
        try:
            cls.proc.wait(10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait()

    @classmethod
    def server_log(cls) -> str:
        cls.log.flush()
        return (cls.tmp / "server.log").read_text(errors="replace")[-4000:]

    @classmethod
    def request(cls, method, path, user=None, body=None, headers=None):
        h = dict(headers or {})
        if user:
            h["Authorization"] = "Basic " + base64.b64encode(f"{user}:{cls.passwords[user]}".encode()).decode()
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=15)
        try:
            conn.request(method, path, body=body.encode() if isinstance(body, str) else body, headers=h)
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def client(self) -> caldav.Client:
        return caldav.Client(f"http://127.0.0.1:{self.port}", VIEWER, self.passwords[VIEWER])

    # --- the viewer reads its calendars ------------------------------------------------------------------
    def test_the_viewer_reads_its_calendars_through_the_apps_report(self) -> None:
        now = ny(2026, 11, 6, 10, 30)
        start, end = agenda.window(now, NY, 7)
        got = caldav.fetch_all(self.client(), (SHARED, ROUTINES), start - agenda.QUERY_MARGIN, end + agenda.QUERY_MARGIN)
        self.assertEqual([source.path for source, _ in got], ["household/shared", "alice/routines"])
        result = agenda.build(got, now, NY, 7)
        self.assertEqual(placements(result, "Weekly practice"), {"2026-11-09": ("18:00", "19:30", False)})
        self.assertEqual(placements(result, "Family visit"),
                         {d: ("All day", "", True) for d in ("2026-11-06", "2026-11-07", "2026-11-08")})
        self.assertEqual(placements(result, "Night shift"),
                         {"2026-11-06": ("22:00", "08:00", False), "2026-11-07": ("Until 08:00", "", False)})
        self.assertEqual(placements(result, "Dinner window"), {"2026-11-06": ("23:00", "01:00", False)})
        shown = {e["title"] for d in result["days"] for e in d["events"]}
        self.assertNotIn("Private appointment", shown)
        self.assertNotIn("Lunch", shown, "School isn't one of this config's calendars")

    def test_the_time_range_comes_from_the_server(self) -> None:
        """The REPORT's time-range filters on the server: a window with no events returns none, and the
        recurring series is returned (as one resource) for a window holding one of its instances."""
        cal_far = caldav.fetch_all(self.client(), (SHARED,), ny(2030, 1, 1), ny(2030, 1, 8))
        self.assertEqual(cal_far[0][1], ())
        nov23 = caldav.fetch_all(self.client(), (SHARED,), ny(2026, 11, 23), ny(2026, 11, 24))
        self.assertEqual([str(c.walk("VEVENT")[0]["UID"]) for c in nov23[0][1]], ["weekly-exdate@example.com"])
        result = agenda.build(nov23, ny(2026, 11, 23, 8, 0), NY, 1)
        self.assertEqual(titles(day(result, "2026-11-23")), ["Weekly practice (moved)"])

    def test_the_dst_event_through_the_server(self) -> None:
        got = caldav.fetch_all(self.client(), (ROUTINES,), ny(2026, 10, 30), ny(2026, 11, 8))
        result = agenda.build(got, ny(2026, 10, 31, 12, 0), NY, 7)
        self.assertEqual(placements(result, "Overnight shift across the fall-back change"),
                         {"2026-11-01": ("00:30", "03:30", False)})

    # --- and is refused everywhere else ------------------------------------------------------------------
    def test_an_unlisted_calendar_is_refused_and_logged_without_the_password(self) -> None:
        for source in (SCHOOL, type(SCHOOL)("alice/personal", "Personal", "#000000")):
            with self.subTest(source.path):
                clock = FakeClock(ny(2026, 11, 6, 10, 30))
                service = AgendaService(config(base_url=f"http://127.0.0.1:{self.port}"),
                                        lambda s, e, source=source: caldav.fetch_all(self.client(), (SHARED, source), s, e),
                                        clock=clock.now, monotonic=clock.monotonic)
                with self.assertLogs("calendar_view", "INFO") as logs:
                    self.assertFalse(service.refresh())
                self.assertEqual(logs.output, [f"WARNING:calendar_view.service:fetch failed: {source.path}, HTTP 403"])
                self.assertNotIn(self.passwords[VIEWER], "".join(logs.output))
                self.assertEqual(service.agenda()[0], 503, "a calendar refused: no partial week")

    def test_the_viewer_cant_write_or_list(self) -> None:
        event = PRIVATE.replace("private@", "viewer@")
        attempts = {
            "PROPFIND /": ("PROPFIND", "/", {"Depth": "1"}, None),
            "PROPFIND another home": ("PROPFIND", "/household/", {"Depth": "1"}, None),
            "GET unlisted event": ("GET", "/alice/personal/private.ics", {}, None),
            "REPORT unlisted": ("REPORT", "/household/school/", {"Depth": "1"}, caldav.report_body(ny(2026, 11, 1), ny(2026, 12, 1))),
            "PUT into a listed calendar": ("PUT", "/household/shared/new.ics", {"Content-Type": "text/calendar"}, event),
            "PUT over an event": ("PUT", "/household/shared/weekly-exdate.ics", {"Content-Type": "text/calendar"}, event),
            "DELETE an event": ("DELETE", "/household/shared/weekly-exdate.ics", {}, None),
            "DELETE a calendar": ("DELETE", "/household/shared/", {}, None),
            "MKCALENDAR": ("MKCALENDAR", "/household/viewers/", {}, None),
            "MKCALENDAR own home": ("MKCALENDAR", f"/{VIEWER}/mine/", {}, None),
            "PROPPATCH": ("PROPPATCH", "/household/shared/", {"Content-Type": "application/xml"},
                          ('<?xml version="1.0"?><D:propertyupdate xmlns:D="DAV:"><D:set><D:prop>'
                           "<D:displayname>Mine</D:displayname></D:prop></D:set></D:propertyupdate>")),
        }
        for what, (method, path, headers, body) in attempts.items():
            with self.subTest(what):
                status, reply = self.request(method, path, VIEWER, body=body, headers=headers)
                self.assertEqual(status, 403, reply[:200])
                self.assertNotIn(b"Private appointment", reply)
        # The listed calendar is untouched, and still readable with a plain GET.
        status, body = self.request("GET", "/household/shared/weekly-exdate.ics", VIEWER)
        self.assertEqual(status, 200)
        self.assertIn(b"Weekly practice", body)

    def test_a_wrong_password_is_401(self) -> None:
        client = caldav.Client(f"http://127.0.0.1:{self.port}", VIEWER, "not-the-password-0000")
        with self.assertRaises(caldav.FetchError) as caught:
            client.report("household/shared", ny(2026, 11, 1), ny(2026, 11, 8))
        self.assertEqual(caught.exception.status, "HTTP 401")

    def test_the_server_log_has_no_viewer_password(self) -> None:
        self.assertNotIn(self.passwords[VIEWER], self.server_log())
        self.assertNotIn(json.dumps(self.passwords[VIEWER]), self.server_log())


if __name__ == "__main__":
    unittest.main()
