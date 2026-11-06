# calendar-view

A read-only wall-screen page for the household calendar: today plus the next 6 days, for the pantry
station's 15.6" 1080p screen, readable from about 3 m. It also works on the PC and phones. Spec:
`docs/CONTRACTS.md` §14.2. Deployed by `roles/calendar_view` (§14.3). Python 3 standard library, plus
`icalendar` and `recurring-ical-events` from the Ubuntu archive. No web framework, no build step, no
external requests.

```
calendar-view [--config PATH]        # default: $CALENDAR_VIEW_CONFIG, else /etc/homelab-calendar-view/config.json
calendar-view --check-config PATH    # 0 when good; 2 and a one-line reason (never a value) when not
```

## Settings
`config.json`, every key required and nothing else accepted (`config.example.json`):

| Key | Rule |
|---|---|
| `base_url` | `http://<loopback IP>:<port>`, no path or credentials (the password goes there in clear) |
| `user`, `password` | the read-only viewer login (§14.1) |
| `timezone` | IANA name; the window and every label are in it |
| `days` | 1 to 14 (the contract's 7) |
| `listen_host`, `listen_port` | a loopback IP literal, and a port |
| `calendars` | 1 to 12 `{path: "<login>/<calendar id>", label, color: "#rrggbb"}`, in display order |

## How it works
- **Fetch** (`caldav.py`), every 60 s in a background thread: one `REPORT` per calendar, a
  calendar-query for VEVENTs in a time-range one day wider than the window on each side, `Depth: 1`,
  Basic auth as the viewer.
  - It uses the configured paths only, never discovery: the viewer gets 403 on `/` and on every home.
  - urllib, with no proxies and no redirects (urllib would carry the Authorization header to the new
    location).
  - The multistatus is parsed into an xml.etree tree by expat, with every DTD and entity hook refused.
  - All or nothing: the first calendar that fails fails the fetch, so a calendar is never silently
    missing. One resource that doesn't parse is skipped with a warning naming the calendar path.
  - A failure is logged as `fetch failed: <path>, HTTP <status>` (or `no response (<error type>)`).
- **Cache** (`service.py`): the last good fetch, its time and age.
  - The agenda is rebuilt from it for the current minute, so NOW/NEXT and the days follow the clock
    while the data is a few minutes old.
  - `stale` is true when the last fetch failed, or the data is over 150 s old.
  - Past the fetched range (over a day of failures), the agenda is the one as of the last good fetch.
  - With no good fetch yet, `/api/agenda` is 503: never an empty week that looks real.
- **Agenda** (`agenda.py`), pure functions of (calendars, now, zone, days):
  - The window is [local midnight today, + `days` days), one day per local date, so 2026-11-01 is a
    25-hour day.
  - Recurrence, `EXDATE`, `RECURRENCE-ID` overrides and time zones come from `recurring-ical-events`
    and `icalendar` only. Instants are compared in UTC (Python compares same-zone datetimes by wall
    clock, which is wrong in the repeated fall-back hour) and turned local only for labels.
  - A timed event shows on the day it starts. On a later day it shows again only if it ends more than
    0 minutes after 06:00 of that day, as `Until 08:00` (or `Until WED 17:00`). So a 23:00–01:00 dinner
    window shows on its start day only.
  - All-day events show on every date they cover. Floating times are local. Cancelled occurrences
    (`STATUS:CANCELLED`) don't show.
  - NOW: timed events in progress, wherever they started. NEXT: the earliest timed event starting
    after now and before midnight, or null.
  - Labels are English and 24-hour (`FRI NOV 6`, `Friday, November 6`, `11:00`/`14:00`, `All day`).
    Titles are one line of plain text, at most 120 characters, with `(No title)` for none.
- **HTTP** (`server.py`): `ThreadingHTTPServer`, at most 32 connections, 10 s per socket operation.
  - Routes: `/`, `/static/app.css`, `/static/app.js` (read once at start), `/api/agenda`
    (`Cache-Control: no-store`) and `/healthz` (200 `ok` while the last good fetch is under 5 minutes
    old, else 503 `stale`).
  - Routing is an exact match on the request target as sent, so `/static/../config.json`, its encoded
    forms and `//static/app.js` are 404. There's no directory listing.
  - GET and HEAD only, 405 otherwise. An HTTP/0.9 request gets a 400.
  - Every response, errors included, carries `Content-Security-Policy: default-src 'self';
    frame-ancestors 'self'; base-uri 'none'; form-action 'none'`, `X-Content-Type-Options: nosniff`
    and `Referrer-Policy: no-referrer`. Error bodies never echo the request.

## /api/agenda
```json
{
  "generated": "2026-11-06T10:30:00-05:00", "stale": false, "stale_seconds": 12,
  "timezone": "America/New_York",
  "calendars": [{"label": "Shared", "color": "#4f8cff"}],
  "today": {"date_label": "Friday, November 6", "now": [event], "next": event|null},
  "days": [{"date": "2026-11-06", "label": "FRI NOV 6", "events": [event]}]
}
event = {"title", "calendar", "color", "start_label", "end_label", "all_day"}
```
`generated` is the minute the agenda was built for. `timezone` (the page clock's zone) and
`calendars` (the legend) are additions to §14.2's list.

## The page (`web/`)
- One HTML, one CSS and one JS file; no inline script or style, so the CSP needs no exceptions.
- **Wall layout** (1000 px and wider): today on the left (date, a large clock ticking locally in the
  configured zone, NOW, NEXT and today's list), the next 6 days on the right in a 3×2 grid.
  - Sizes scale with the viewport width. At 1920 px the clock is about 165 px and today's events about
    33 px.
  - A list shows its first few events and a "+N more" line. Today's list fades out at the bottom
    instead of cutting a line.
- **Below 1000 px** it's one scrolling column with every event (the week in 2 columns, or 1 under 600 px).
- It polls `/api/agenda` every 60 s. When the data is stale, or the fetch fails, a banner reads "Not
  updated for N min" and the agenda dims.
- Text only through `textContent`. Colors only through the CSSOM, and only when they match `#rrggbb`.
  The JS has no date logic: it shows the labels it's given.

## Tests
```bash
python3 -m unittest discover -s apps/calendar-view/tests -t apps/calendar-view -v
```
Standard-library `unittest`. `icalendar` and `recurring-ical-events` are needed for most tests (missing:
a skip locally, a failure under `CI=true`). `radicale` is needed for `test_radicale.py`, which skips
without it.
- `test_agenda.py`: the fixtures in `tests/fixtures/`.
  - A weekly series with an `EXDATE`, a moved instance and a cancelled one; a daily series across the
    DST change.
  - All-day and 3-day all-day events.
  - The event crossing 2026-11-01's fall-back, the 25-hour day, and NOW inside the repeated hour.
  - The 23:00–01:00 rule, a night shift, a 3-day timed event, an event ending exactly at 06:00.
  - Floating and UTC times, and window edges on both sides.
  - NOW/NEXT at fixed clocks: ties, starting now, ending now, a zero-length event, after midnight.
  - Title capping, a broken resource, calendar order.
- `test_radicale.py`: a real Radicale 3.6.0 with its own rights file, in the shape of §14.1.
  - The viewer reads its two calendars through the app's REPORT.
  - It's refused (403) on an unlisted calendar, another home, the root and every write. A refused fetch
    is logged with the path and status, never the password.
- `test_service.py`: stale keeps the data and reports its age, `/healthz` goes 503 at 5 minutes, no
  data is a 503, recovery, more than a day of failures.
- `test_http.py`: the headers on every response (malformed requests included), the route table, 404
  for traversal and its encoded forms, 405 for every other method, HEAD.
- `test_caldav.py`: the request sent, multistatus parsing, DTDs/entities refused, no redirects, proxy
  variables ignored, the size limit, failures without the password.
- `test_config.py`: strict keys and types, loopback only, errors without the password, and
  `--check-config` with the standard library only.
- `test_web.py`: static checks on the page (no external requests, nothing the CSP blocks, no HTML
  sinks, no date logic), and `node --check` when node is installed.
