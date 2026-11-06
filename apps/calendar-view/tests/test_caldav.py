"""CalDAV client (calendar_view/caldav.py) against a scripted loopback HTTP server: the REPORT it sends,
the multistatus it accepts, DTD/entity refusal, no redirects or proxies, and failures that carry the
calendar path and status only (never the password)."""

from __future__ import annotations

import base64
import os
import threading
import unittest
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from .helpers import HAVE_LIBS, PASSWORD, SHARED, require_libs

if HAVE_LIBS:
    from calendar_view import caldav

START = datetime(2026, 11, 5, 5, 0, tzinfo=UTC)
END = datetime(2026, 11, 14, 5, 0, tzinfo=UTC)
EVENT = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\nUID:a@example.com\r\nDTSTAMP:20261101T120000Z\r\n"
         "SUMMARY:Lunch &amp; a walk\r\nDTSTART:20261106T170000Z\r\nDTEND:20261106T180000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


def multistatus(*responses: str) -> bytes:
    return ('<?xml version="1.0" encoding="utf-8"?>\n<multistatus xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            + "".join(responses) + "</multistatus>").encode()


def ok(href: str, data: str) -> str:
    return (f"<response><href>{href}</href><propstat><prop><getetag>\"1\"</getetag>"
            f"<C:calendar-data>{data}</C:calendar-data></prop><status>HTTP/1.1 200 OK</status></propstat></response>")


GOOD_BODY = multistatus(
    ok("/household/shared/a.ics", EVENT),
    "<response><href>/household/shared/gone.ics</href><status>HTTP/1.1 404 Not Found</status></response>",
    "<response><href>/household/shared/b.ics</href><propstat><prop><C:calendar-data/></prop>"
    "<status>HTTP/1.1 404 Not Found</status></propstat></response>",
)


class Scripted(BaseHTTPRequestHandler):
    """Answers each request with server.reply = (status, headers, body), and records it."""

    def do_REPORT(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.server.seen.append((self.command, self.path, dict(self.headers), self.rfile.read(length)))
        status, headers, body = self.server.reply
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_REPORT

    def log_message(self, *args) -> None:
        pass


def serve(reply):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
    server.daemon_threads = True
    server.reply, server.seen = reply, []
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    return server


class Base(unittest.TestCase):
    def setUp(self) -> None:
        require_libs()

    def server(self, status=207, headers=None, body=GOOD_BODY):
        srv = serve((status, {"Content-Type": "application/xml", **(headers or {})}, body))
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv

    def client(self, srv) -> caldav.Client:
        return caldav.Client(f"http://127.0.0.1:{srv.server_address[1]}", "viewer", PASSWORD)


class Multistatus(Base):
    def test_only_200_calendar_data(self) -> None:
        items = caldav.parse_multistatus(GOOD_BODY)
        self.assertEqual([href for href, _ in items], ["/household/shared/a.ics"])
        self.assertIn("SUMMARY:Lunch & a walk", items[0][1])  # the predefined entity, as XML defines it

    def test_prefixed_namespaces(self) -> None:
        body = (b'<D:multistatus xmlns:D="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav"><D:response>'
                b"<D:href>/x/y/a.ics</D:href><D:propstat><D:prop><cal:calendar-data>DATA</cal:calendar-data>"
                b"</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response></D:multistatus>")
        self.assertEqual(caldav.parse_multistatus(body), [("/x/y/a.ics", "DATA")])

    def test_dtds_and_entities_are_refused(self) -> None:
        bombs = {
            "billion laughs": b'<?xml version="1.0"?><!DOCTYPE m [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
                              b'<multistatus xmlns="DAV:">&b;</multistatus>',
            "external entity": b'<?xml version="1.0"?><!DOCTYPE m [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                               b'<multistatus xmlns="DAV:">&x;</multistatus>',
            "external dtd": b'<?xml version="1.0"?><!DOCTYPE m SYSTEM "http://192.0.2.1/x.dtd"><multistatus xmlns="DAV:"/>',
            "bare doctype": b'<!DOCTYPE multistatus><multistatus xmlns="DAV:"/>',
            "utf-16 doctype": '<?xml version="1.0" encoding="utf-16"?><!DOCTYPE m [<!ENTITY a "x">]><m>&a;</m>'.encode("utf-16"),
            "undefined entity": b'<multistatus xmlns="DAV:">&nope;</multistatus>',
            "not xml": b"<html><body>oops",
            "empty": b"",
        }
        for what, body in bombs.items():
            with self.subTest(what), self.assertRaises(caldav.XMLRefused):
                caldav.parse_multistatus(body)

    def test_not_a_multistatus(self) -> None:
        with self.assertRaises(caldav.XMLRefused):
            caldav.parse_multistatus(b'<error xmlns="DAV:"/>')

    def test_report_body(self) -> None:
        root = caldav.parse_xml(caldav.report_body(START, END))
        self.assertEqual(root.tag, "{urn:ietf:params:xml:ns:caldav}calendar-query")
        tr = root.find(".//{urn:ietf:params:xml:ns:caldav}time-range")
        self.assertEqual(tr.attrib, {"start": "20261105T050000Z", "end": "20261114T050000Z"})
        comps = [c.get("name") for c in root.iter("{urn:ietf:params:xml:ns:caldav}comp-filter")]
        self.assertEqual(comps, ["VCALENDAR", "VEVENT"])
        self.assertIsNotNone(root.find(".//{DAV:}prop/{urn:ietf:params:xml:ns:caldav}calendar-data"))


class Report(Base):
    def test_the_request(self) -> None:
        srv = self.server()
        items = self.client(srv).report("household/shared", START, END)
        self.assertEqual(len(items), 1)
        method, path, headers, body = srv.seen[0]
        self.assertEqual((method, path), ("REPORT", "/household/shared/"))
        self.assertEqual(headers["Depth"], "1")
        self.assertTrue(headers["Content-Type"].startswith("application/xml"))
        self.assertEqual(headers["Authorization"], "Basic " + base64.b64encode(f"viewer:{PASSWORD}".encode()).decode())
        self.assertIn(b'start="20261105T050000Z" end="20261114T050000Z"', body)

    def test_failures_name_the_path_and_status_only(self) -> None:
        cases = {
            "403": (self.server(status=403, body=b"<forbidden/>"), "HTTP 403"),
            "401": (self.server(status=401, body=b""), "HTTP 401"),
            "500": (self.server(status=500, body=b"boom " + PASSWORD.encode()), "HTTP 500"),
            "200 instead of 207": (self.server(status=200), "HTTP 200"),
            "bad multistatus": (self.server(body=b"<multistatus xmlns='DAV:'>"), "HTTP 207, unreadable multistatus"),
        }
        for what, (srv, status) in cases.items():
            with self.subTest(what), self.assertRaises(caldav.FetchError) as caught:
                self.client(srv).report("household/shared", START, END)
            self.assertEqual((caught.exception.path, caught.exception.status), ("household/shared", status))
            self.assertEqual(str(caught.exception), f"household/shared: {status}")
            # No chained exception to print (urllib's would carry the request).
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__context__ is None or caught.exception.__suppress_context__)

    def test_nothing_listening(self) -> None:
        srv = self.server()
        port = srv.server_address[1]
        srv.shutdown()
        srv.server_close()
        with self.assertRaises(caldav.FetchError) as caught:
            caldav.Client(f"http://127.0.0.1:{port}", "viewer", PASSWORD).report("household/shared", START, END)
        self.assertEqual(caught.exception.status, "no response (ConnectionRefusedError)")

    def test_a_redirect_is_not_followed(self) -> None:
        elsewhere = self.server()
        srv = self.server(status=302, headers={"Location": f"http://127.0.0.1:{elsewhere.server_address[1]}/x/"}, body=b"")
        with self.assertRaises(caldav.FetchError) as caught:
            self.client(srv).report("household/shared", START, END)
        self.assertEqual(caught.exception.status, "HTTP 302")
        self.assertEqual(elsewhere.seen, [], "the login must never be carried to another location")

    def test_proxy_settings_are_ignored(self) -> None:
        srv = self.server()
        env = {"http_proxy": "http://192.0.2.1:3128", "HTTP_PROXY": "http://192.0.2.1:3128", "no_proxy": "", "NO_PROXY": ""}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(len(self.client(srv).report("household/shared", START, END)), 1)

    def test_size_limit(self) -> None:
        srv = self.server()
        with mock.patch.object(caldav, "MAX_RESPONSE_BYTES", 100), self.assertRaises(caldav.FetchError) as caught:
            self.client(srv).report("household/shared", START, END)
        self.assertEqual(caught.exception.status, "HTTP 207, response over the size limit")

    def test_the_client_never_shows_the_password(self) -> None:
        client = caldav.Client("http://127.0.0.1:5232", "viewer", PASSWORD)
        self.assertNotIn(PASSWORD, repr(client))
        self.assertNotIn(base64.b64encode(f"viewer:{PASSWORD}".encode()).decode(), repr(client))


class FetchAll(Base):
    def test_every_calendar_in_order_and_a_bad_resource_skipped(self) -> None:
        body = multistatus(ok("/household/shared/a.ics", EVENT), ok("/household/shared/bad.ics", "NOT ICAL"))
        srv = self.server(body=body)
        with self.assertLogs("calendar_view.caldav", "WARNING") as logs:
            got = caldav.fetch_all(self.client(srv), (SHARED,), START, END)
        self.assertEqual(len(got), 1)
        self.assertIs(got[0][0], SHARED)
        self.assertEqual(len(got[0][1]), 1)
        self.assertEqual(str(got[0][1][0].walk("VEVENT")[0]["SUMMARY"]), "Lunch & a walk")
        self.assertEqual(logs.output, ["WARNING:calendar_view.caldav:skipped an unreadable event in household/shared"])

    def test_all_or_nothing(self) -> None:
        srv = self.server(status=403, body=b"")
        with self.assertRaises(caldav.FetchError):
            caldav.fetch_all(self.client(srv), (SHARED,), START, END)


if __name__ == "__main__":
    unittest.main()
