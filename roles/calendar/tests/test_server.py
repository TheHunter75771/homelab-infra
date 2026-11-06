"""(b) A real Radicale server from the rendered config (docs/CONTRACTS.md §13.1), on a free loopback port
with temp paths. Logins are hashed the way community.general.htpasswd hashes them (passlib, bcrypt);
the collections repo is set up with the argv and settings of tasks/history.yml; calendars are created
with the role's own MKCALENDAR body. Skipped when radicale (or git) isn't available.

It proves:
  - 401 without a login or with a wrong password, 207 with one;
  - MKCALENDAR 201, display name kept (and 409 on a second create: why the role looks first);
  - one login can't read or write another login's calendars;
  - a whole calendar can't be deleted or overwritten, while a single event can be deleted;
  - PUT then GET round-trips the four fixtures with every property intact;
  - each change adds exactly one git commit, by the role's placeholder identity with no home
    directory and no global git config (as under the unit), and an unchanged PUT adds none.
"""

from __future__ import annotations

import base64
import collections
import importlib.util
import os
import pathlib
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass

import role_render as rr

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
HAVE_RADICALE = importlib.util.find_spec("radicale") is not None
USERS = ("alice", "bob", "household")
NS = {"D": "DAV:", "C": "urn:ietf:params:xml:ns:caldav"}
SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "files/homelab-calendar-recovered.sh"


@dataclass
class Response:
    status: int
    headers: object
    body: bytes


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ical_properties(text: str) -> collections.Counter:
    """Every content line as (component path, NAME, sorted params, value), after unfolding (RFC 5545)."""
    unfolded: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += line[1:]
        elif line:
            unfolded.append(line)
    out: collections.Counter = collections.Counter()
    path: list[str] = []
    for line in unfolded:
        head, _, value = line.partition(":")
        name, *params = head.split(";")
        name = name.upper()
        if name == "BEGIN":
            path.append(value.upper())
        elif name == "END":
            path.pop()
        else:
            norm = tuple(sorted(k.upper() + "=" + v for k, _, v in (p.partition("=") for p in params)))
            out[("/".join(path), name, norm, value)] += 1
    return out


def event(uid: str, summary: str) -> str:
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//homelab-infra//calendar tests//EN",
        "BEGIN:VEVENT", f"UID:{uid}", "DTSTAMP:20261101T120000Z", f"SUMMARY:{summary}",
        "DTSTART:20261105T150000Z", "DTEND:20261105T160000Z", "END:VEVENT", "END:VCALENDAR", ""])


@unittest.skipUnless(HAVE_RADICALE, "radicale not installed (pip install radicale==<versions.yml pin> bcrypt)")
@unittest.skipUnless(shutil.which("git"), "git not installed")
class RealServer(unittest.TestCase):
    proc: subprocess.Popen
    tmp: pathlib.Path

    @classmethod
    def setUpClass(cls) -> None:
        # Radicale's own dependency (libpass); the same call community.general.htpasswd makes for bcrypt.
        from passlib.apache import HtpasswdFile, htpasswd_context

        rr.require_tools()
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="calendar-test-"))
        cls.port = free_port()
        cls.repo = cls.tmp / "collections"
        cls.vars = rr.load_vars({"calendar_port": cls.port, "calendar_users_file": str(cls.tmp / "users"),
                                 "calendar_collections_dir": str(cls.repo)})
        (cls.tmp / "config").write_text(rr.render("config.j2", cls.vars))

        # Logins, as community.general.htpasswd writes them: Apache-style "$2y$" bcrypt.
        cls.passwords = {u: secrets.token_urlsafe(18) for u in USERS}
        ht = HtpasswdFile(str(cls.tmp / "users"), new=True, default_scheme="bcrypt", context=htpasswd_context)
        for user, password in cls.passwords.items():
            ht.set_password(user, password)
        ht.save()
        hashes = [line.split(":", 1)[1] for line in (cls.tmp / "users").read_text().splitlines()]
        assert len(hashes) == 3 and all(h.startswith("$2y$") and len(h) == 60 for h in hashes), "not $2y$ bcrypt"

        # The repo, exactly as tasks/history.yml sets it up.
        cls.repo.mkdir()
        tasks = {t["name"]: t for t in rr.load_tasks("history.yml")}
        init = tasks["History | create the repository (once)"]["ansible.builtin.command"]
        cls.git_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
                       # Like the unit: HOME=/nonexistent and no system config, so only repo-local settings count.
                       "HOME": str(cls.tmp / "nonexistent"), "GIT_CONFIG_NOSYSTEM": "1"}

        def as_owner(argv: list[str]) -> list[str]:
            """The task's command without its `runuser -u calendar --` prefix (the test already is the owner)."""
            assert argv[:4] == ["runuser", "-u", "{{ calendar_user }}", "--"], argv
            return argv[4:]

        subprocess.run(as_owner(init["argv"]), cwd=cls.repo, env=cls.git_env, check=True)
        env = rr.environment()
        read = tasks["History | repo-local git settings now"]
        write = tasks["History | repo-local git identity and settings"]["ansible.builtin.command"]["argv"]
        for item in read["loop"]:
            argv = [env.from_string(a).render({**cls.vars, "item": {"item": item}}) for a in as_owner(write)]
            argv = [env.from_string(a).render(cls.vars) for a in argv]  # the values are templates themselves
            subprocess.run(argv, cwd=cls.repo, env=cls.git_env, check=True)
        (cls.repo / ".gitignore").write_text(rr.render("collections.gitignore.j2", cls.vars))

        cls.log = open(cls.tmp / "server.log", "wb")  # noqa: SIM115 - closed in tearDownClass
        cls.proc = subprocess.Popen([sys.executable, "-m", "radicale", "--config", str(cls.tmp / "config")],
                                    cwd=cls.tmp, env=cls.git_env, stdout=cls.log, stderr=subprocess.STDOUT)
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
        # First logins create each principal (/<user>/); later counts start from here.
        for user in USERS:
            assert cls.request("PROPFIND", "/", user, headers={"Depth": "0"}).status == 207, cls.server_log()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proc.terminate()
        try:
            cls.proc.wait(10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait()
        cls.log.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def server_log(cls) -> str:
        cls.log.flush()
        return (cls.tmp / "server.log").read_text(errors="replace")[-4000:]

    @classmethod
    def request(cls, method: str, path: str, user: str | None = None, password: str | None = None,
                body: bytes | str | None = None, headers: dict | None = None) -> Response:
        import http.client

        h = dict(headers or {})
        if user:
            secret = cls.passwords[user] if password is None else password
            h["Authorization"] = "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=15)
        try:
            conn.request(method, path, body=body.encode() if isinstance(body, str) else body, headers=h)
            resp = conn.getresponse()
            return Response(resp.status, resp.msg, resp.read())
        finally:
            conn.close()

    # --- helpers ---------------------------------------------------------------------------------------
    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.repo, env=self.git_env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commits(self) -> int:
        return int(self.git("rev-list", "--all", "--count"))

    def mkcalendar(self, user: str, cal_id: str, name: str) -> Response:
        """The role's own request: the body from tasks/calendars.yml, rendered as Ansible would."""
        task = next(t for t in rr.load_tasks("calendars.yml") if t["name"] == "Calendars | create the missing ones")
        uri = task["ansible.builtin.uri"]
        item = {"item": [{"user": user}, {"id": cal_id, "name": name}]}
        body = rr.environment().from_string(uri["body"]).render(item=item)
        return self.request(uri["method"], f"/{user}/{cal_id}/", user, body=body, headers=uri["headers"])

    def mkcalendar_as(self, user: str, path: str) -> Response:
        return self.request("MKCALENDAR", path, user)

    def put(self, user: str, path: str, text: str) -> Response:
        return self.request("PUT", path, user, body=text, headers={"Content-Type": "text/calendar; charset=utf-8"})

    def prop(self, user: str, path: str, prop: str):
        """PROPFIND Depth 0 for one property (e.g. "D:displayname"); returns its element or None."""
        from defusedxml import ElementTree  # Radicale's own dependency

        body = ('<?xml version="1.0" encoding="utf-8"?><D:propfind xmlns:D="DAV:" '
                f'xmlns:C="urn:ietf:params:xml:ns:caldav"><D:prop><{prop}/></D:prop></D:propfind>')
        r = self.request("PROPFIND", path, user, body=body, headers={"Depth": "0", "Content-Type": "application/xml"})
        self.assertEqual(r.status, 207)
        return ElementTree.fromstring(r.body).find(f".//D:propstat[D:status='HTTP/1.1 200 OK']/D:prop/{prop}", NS)

    def displayname(self, user: str, path: str) -> str | None:
        node = self.prop(user, path, "D:displayname")
        return None if node is None else node.text

    # --- tests -----------------------------------------------------------------------------------------
    def test_a_login_is_required(self) -> None:
        r = self.request("PROPFIND", "/", headers={"Depth": "0"})
        self.assertEqual(r.status, 401)
        self.assertEqual(r.headers.get("WWW-Authenticate"), 'Basic realm="Calendar"')
        self.assertEqual(self.request("PROPFIND", "/", "alice", "not-the-password-0000", headers={"Depth": "0"}).status, 401)
        self.assertEqual(self.request("PROPFIND", "/", "alice", headers={"Depth": "0"}).status, 207)

    def test_the_recovered_script_recognises_this_server(self) -> None:
        """Its raw HTTP/1.0 PROPFIND gets Radicale's 401, so a marker leads to one push (stubs for
        homelab-notify and systemctl; test_recovered.py covers the refusals)."""
        bindir = self.tmp / "recovered-bin"
        bindir.mkdir()
        (bindir / "homelab-notify").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$PUSH_LOG"\n')
        (bindir / "systemctl").write_text('#!/bin/sh\n[ "$1" = show ] && echo inv-1\nexit 0\n')
        for stub in bindir.iterdir():
            stub.chmod(0o755)
        marker, log = self.tmp / "down", self.tmp / "pushes.log"
        marker.touch()
        env = {"PATH": f"{bindir}:/usr/bin:/bin", "PUSH_LOG": str(log), "CALENDAR_ALERT_MARKER": str(marker),
               "CALENDAR_NOTIFY_BIN": "homelab-notify",  # resolved from PATH: the stub
               "CALENDAR_HOST": "127.0.0.1", "CALENDAR_PORT": str(self.port), "CALENDAR_WAIT_SECONDS": "5",
               "CALENDAR_SETTLE_SECONDS": "0"}
        r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(marker.exists(), r.stdout)
        self.assertEqual(log.read_text().splitlines(),
                         ["--tag calendar --title Calendar back up --body homelab-calendar.service is running again."])

    def test_web_none_serves_only_a_fixed_line(self) -> None:
        r = self.request("GET", "/")
        self.assertEqual(r.status, 302)
        self.assertTrue(r.headers.get("Location", "").endswith("/.web"))
        r = self.request("GET", "/.web")
        self.assertEqual((r.status, r.body), (200, b"Radicale works!"))

    def test_mkcalendar_with_the_roles_request(self) -> None:
        name = "Personal & Family <test>"
        # The role's lookup first: PROPFIND Depth 0 answers 404 for a calendar that isn't there.
        self.assertEqual(self.request("PROPFIND", "/alice/personal/", "alice", headers={"Depth": "0"}).status, 404)
        before = self.commits()
        self.assertEqual(self.mkcalendar("alice", "personal", name).status, 201, self.server_log())
        self.assertEqual(self.commits(), before + 1)
        self.assertEqual(self.request("PROPFIND", "/alice/personal/", "alice", headers={"Depth": "0"}).status, 207)
        self.assertEqual(self.displayname("alice", "/alice/personal/"), name)
        # Events only, so iOS doesn't also list the calendar under Reminders.
        comps = self.prop("alice", "/alice/personal/", "C:supported-calendar-component-set")
        self.assertIsNotNone(comps)
        self.assertEqual([c.get("name") for c in comps.findall("C:comp", NS)], ["VEVENT"])
        self.assertEqual(self.mkcalendar("alice", "personal", name).status, 409)
        self.assertEqual(self.commits(), before + 1)

    def test_one_login_cannot_touch_another_logins_calendars(self) -> None:
        self.assertEqual(self.mkcalendar("alice", "private", "Private").status, 201)
        self.assertEqual(self.put("alice", "/alice/private/a.ics", event("a@example.com", "Mine")).status, 201)
        before, original = self.commits(), self.request("GET", "/alice/private/a.ics", "alice").body
        attempts = {
            "PROPFIND principal": self.request("PROPFIND", "/alice/", "bob", headers={"Depth": "1"}),
            "PROPFIND calendar": self.request("PROPFIND", "/alice/private/", "bob", headers={"Depth": "1"}),
            "REPORT": self.request("REPORT", "/alice/private/", "bob", headers={"Depth": "1"}, body=(
                '<?xml version="1.0"?><C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
                '<D:prop><D:getetag/></D:prop><C:filter><C:comp-filter name="VCALENDAR"/></C:filter>'
                "</C:calendar-query>")),
            "GET": self.request("GET", "/alice/private/a.ics", "bob"),
            "PUT new": self.put("bob", "/alice/private/b.ics", event("b@example.com", "Not yours")),
            "PUT over": self.put("bob", "/alice/private/a.ics", event("a@example.com", "Changed")),
            "DELETE event": self.request("DELETE", "/alice/private/a.ics", "bob"),
            "DELETE calendar": self.request("DELETE", "/alice/private/", "bob"),
            "MKCALENDAR": self.mkcalendar_as("bob", "/alice/bobs/"),
        }
        for what, r in attempts.items():
            with self.subTest(attempt=what):
                self.assertEqual(r.status, 403)
                self.assertNotIn(b"Mine", r.body)
        listing = self.request("PROPFIND", "/", "bob", headers={"Depth": "1"})
        self.assertEqual(listing.status, 207)
        self.assertNotIn(b"/alice/", listing.body)
        self.assertEqual(self.commits(), before)
        self.assertEqual(self.request("GET", "/alice/private/a.ics", "alice").body, original)

    def test_whole_calendar_delete_and_overwrite_are_refused(self) -> None:
        self.assertEqual(self.mkcalendar("alice", "keep", "Keep").status, 201)
        self.assertEqual(self.put("alice", "/alice/keep/k.ics", event("k@example.com", "Stays")).status, 201)
        before = self.commits()
        self.assertEqual(self.request("DELETE", "/alice/keep/", "alice").status, 403)
        self.assertEqual(self.put("alice", "/alice/keep/", event("whole@example.com", "Replaces all")).status, 403)
        self.assertEqual(self.request("PROPFIND", "/alice/keep/", "alice", headers={"Depth": "0"}).status, 207)
        self.assertIn(b"Stays", self.request("GET", "/alice/keep/k.ics", "alice").body)
        self.assertEqual(self.commits(), before)
        # A single event still can be deleted (one commit).
        self.assertIn(self.request("DELETE", "/alice/keep/k.ics", "alice").status, (200, 204))
        self.assertEqual(self.request("GET", "/alice/keep/k.ics", "alice").status, 404)
        self.assertEqual(self.commits(), before + 1)

    def test_the_fixtures_round_trip_intact(self) -> None:
        self.assertEqual(self.mkcalendar("household", "roundtrip", "Round trip").status, 201)
        names = sorted(p.name for p in FIXTURES.glob("*.ics"))
        self.assertEqual(names, ["all-day.ics", "dst-crossing.ics", "valarm.ics", "weekly-exdate.ics"])
        returned = {}
        for name in names:
            with self.subTest(fixture=name):
                sent = (FIXTURES / name).read_text()
                self.assertEqual(self.put("household", f"/household/roundtrip/{name}", sent).status, 201)
                got = self.request("GET", f"/household/roundtrip/{name}", "household")
                self.assertEqual(got.status, 200)
                want, have = ical_properties(sent), ical_properties(got.body.decode())
                self.assertGreaterEqual(sum(want.values()), 8, "the fixture parsed to almost nothing")
                self.assertEqual(want - have, collections.Counter(), "properties lost or changed")
                self.assertEqual(have - want, collections.Counter(), "properties added")
                returned[name] = have

        def has(name: str, component: str, prop: str, params: tuple, value: str) -> bool:
            return returned[name][(component, prop, params, value)] == 1

        ev = "VCALENDAR/VEVENT"
        tz = ("TZID=America/New_York",)
        self.assertTrue(has("weekly-exdate.ics", ev, "RRULE", (), "FREQ=WEEKLY;BYDAY=MO;COUNT=10"))
        self.assertTrue(has("weekly-exdate.ics", ev, "EXDATE", tz, "20261116T180000"))
        self.assertTrue(has("all-day.ics", ev, "DTSTART", ("VALUE=DATE",), "20261126"))
        self.assertTrue(has("dst-crossing.ics", ev, "DTSTART", tz, "20261101T003000"))
        self.assertTrue(has("dst-crossing.ics", ev, "DTEND", tz, "20261101T033000"))
        self.assertTrue(has("dst-crossing.ics", "VCALENDAR/VTIMEZONE/STANDARD", "RRULE", (), "FREQ=YEARLY;BYMONTH=11;BYDAY=1SU"))
        self.assertTrue(has("valarm.ics", f"{ev}/VALARM", "TRIGGER", ("RELATED=START",), "-PT15M"))
        self.assertTrue(has("valarm.ics", f"{ev}/VALARM", "TRIGGER", ("VALUE=DATE-TIME",), "20261110T120000Z"))
        self.assertTrue(has("valarm.ics", f"{ev}/VALARM", "X-WR-ALARMUID", (), "valarm-15min@example.com"))

    def test_one_commit_per_change_and_none_for_no_change(self) -> None:
        self.assertEqual(self.mkcalendar("alice", "history", "History").status, 201)
        steps = [
            ("create", lambda: self.put("alice", "/alice/history/h.ics", event("h@example.com", "First")), 201, 1),
            ("same again", lambda: self.put("alice", "/alice/history/h.ics", event("h@example.com", "First")), 204, 0),
            ("edit", lambda: self.put("alice", "/alice/history/h.ics", event("h@example.com", "Second")), 204, 1),
            ("delete", lambda: self.request("DELETE", "/alice/history/h.ics", "alice"), 200, 1),
        ]
        for what, call, status, added in steps:
            with self.subTest(step=what):
                before = self.commits()
                self.assertEqual(call().status, status)
                self.assertEqual(self.commits(), before + added)
        name, email = self.vars["calendar_git_name"], self.vars["calendar_git_email"]
        for line in self.git("log", "-4", "--format=%an|%ae|%s").splitlines():
            self.assertEqual(line, f"{name}|{email}|Changes by alice")
        self.assertFalse((self.tmp / "nonexistent").exists(), "git must not need a home directory")
        # Everything is either history or ignored, and no cache, lock or temp file is history.
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertNotRegex(self.git("ls-files"), r"\.Radicale\.(cache|lock|tmp-)")
        self.assertIn("collection-root/alice/history/.Radicale.props", self.git("ls-files").splitlines())


if __name__ == "__main__":
    unittest.main()
