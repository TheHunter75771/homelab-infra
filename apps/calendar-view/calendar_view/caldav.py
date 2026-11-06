"""CalDAV: one REPORT (calendar-query with a time-range) per calendar, sent with urllib as the viewer.

- No proxies (the environment's *_proxy variables are ignored) and no redirects (urllib would carry the
  Authorization header to the new location).
- The multistatus is parsed into an xml.etree tree by expat with every DTD hook refused, so no entity is
  ever declared or expanded. The server is trusted loopback; this is defence in depth.
- A failure raises FetchError(path, status). Its text is the calendar path and the HTTP status (or the
  kind of network error) only: never the password, the Authorization header or the response body.
"""

from __future__ import annotations

import base64
import http.client
import logging
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from xml.parsers import expat

import icalendar

from .config import CalendarSource

log = logging.getLogger("calendar_view.caldav")

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
TIMEOUT_SECONDS = 15
# The unit's MemoryMax is 128M; a week of three household calendars is a few hundred KB.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class FetchError(Exception):
    def __init__(self, path: str, status: str) -> None:
        self.path = path
        self.status = status
        super().__init__(f"{path}: {status}")


class XMLRefused(ValueError):
    """The response wasn't a plain XML document (a DTD, an entity, or not well-formed)."""


def utc_stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def report_body(start: datetime, end: datetime) -> bytes:
    """RFC 4791 calendar-query: every VEVENT resource that overlaps [start, end), with its data."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<C:calendar-query xmlns:D="{DAV}" xmlns:C="{CALDAV}">'
        "<D:prop><D:getetag/><C:calendar-data/></D:prop>"
        '<C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT">'
        f'<C:time-range start="{utc_stamp(start)}" end="{utc_stamp(end)}"/>'
        "</C:comp-filter></C:comp-filter></C:filter>"
        "</C:calendar-query>"
    ).encode()


def _clark(name: str) -> str:
    uri, sep, local = name.rpartition("}")
    return "{" + uri + "}" + local if sep else local


def parse_xml(data: bytes) -> ET.Element:
    """An xml.etree tree, built by expat with DOCTYPE and entity declarations refused outright."""
    builder = ET.TreeBuilder()
    parser = expat.ParserCreate(namespace_separator="}")

    def refuse(*_args):
        raise XMLRefused("DTDs and entity declarations are refused")

    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.UnparsedEntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.buffer_text = True
    parser.StartElementHandler = lambda name, attrs: builder.start(
        _clark(name), {_clark(k): v for k, v in attrs.items()})
    parser.EndElementHandler = lambda name: builder.end(_clark(name))
    parser.CharacterDataHandler = builder.data
    try:
        parser.Parse(data, True)
    except expat.ExpatError as e:
        raise XMLRefused(f"not well-formed XML ({expat.ErrorString(e.code)})") from None
    return builder.close()


def parse_multistatus(data: bytes) -> list[tuple[str, str]]:
    """(href, calendar-data) for every resource the server answered 200 for."""
    root = parse_xml(data)
    if root.tag != f"{{{DAV}}}multistatus":
        raise XMLRefused("not a DAV multistatus")
    out: list[tuple[str, str]] = []
    for response in root.iter(f"{{{DAV}}}response"):
        href = (response.findtext(f"{{{DAV}}}href") or "").strip()
        for propstat in response.findall(f"{{{DAV}}}propstat"):
            status = (propstat.findtext(f"{{{DAV}}}status") or "").split()
            if len(status) < 2 or status[1] != "200":
                continue
            node = propstat.find(f"{{{DAV}}}prop/{{{CALDAV}}}calendar-data")
            if node is not None and node.text and node.text.strip():
                out.append((href, node.text))
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError(3xx): a redirect is a failed fetch


def make_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class Client:
    def __init__(self, base_url: str, user: str, password: str, opener=None, timeout: float = TIMEOUT_SECONDS):
        self.base_url = base_url.rstrip("/")
        self.user = user
        self._authorization = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        self._opener = opener or make_opener()
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"Client({self.base_url!r}, user={self.user!r})"

    def url(self, path: str) -> str:
        return f"{self.base_url}/{urllib.parse.quote(path)}/"

    def report(self, path: str, start: datetime, end: datetime) -> list[tuple[str, str]]:
        request = urllib.request.Request(
            self.url(path), data=report_body(start, end), method="REPORT",
            headers={"Depth": "1", "Content-Type": 'application/xml; charset="utf-8"',
                     "Accept": "application/xml", "Authorization": self._authorization})
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status = response.status
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as e:
            e.close()
            raise FetchError(path, f"HTTP {e.code}") from None
        except urllib.error.URLError as e:
            reason = e.reason if isinstance(e.reason, BaseException) else e
            raise FetchError(path, f"no response ({type(reason).__name__})") from None
        except (OSError, http.client.HTTPException) as e:
            raise FetchError(path, f"no response ({type(e).__name__})") from None
        if status != 207:
            raise FetchError(path, f"HTTP {status}")
        if len(body) > MAX_RESPONSE_BYTES:
            raise FetchError(path, "HTTP 207, response over the size limit")
        try:
            return parse_multistatus(body)
        except XMLRefused:
            raise FetchError(path, "HTTP 207, unreadable multistatus") from None


def fetch_all(client: Client, sources: tuple[CalendarSource, ...], start: datetime, end: datetime):
    """Every configured calendar, in order, as (source, (icalendar.Calendar, ...)). All or nothing: the first
    failure raises FetchError, so a calendar is never silently missing from an agenda. One resource that
    doesn't parse is skipped with a warning naming the calendar path."""
    out = []
    for source in sources:
        objects = []
        for _href, text in client.report(source.path, start, end):
            try:
                objects.append(icalendar.Calendar.from_ical(text))
            except Exception:  # noqa: BLE001 - one bad resource must not blank the week
                log.warning("skipped an unreadable event in %s", source.path)
        out.append((source, tuple(objects)))
    return tuple(out)
