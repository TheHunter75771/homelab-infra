"""Settings: /etc/homelab-calendar-view/config.json (role calendar_view renders it, root:calview 0640).

Every key is required and no other key is accepted:
  base_url     the CalDAV server: http on a loopback IP literal (the password goes there in clear)
  user         the read-only viewer login (CONTRACTS §14.1)
  password     its password: never logged, never on the command line, never in an error message
  timezone     IANA name for the window and every label, e.g. America/New_York
  days         days shown, today included (1 to 14; the contract's 7)
  listen_host  loopback IP literal to serve on (tailscale serve proxies to it)
  listen_port  port to serve on (0: any free port, for tests)
  calendars    [{path: "<owner>/<calendar id>", label, color: "#rrggbb"}], 1 to 12, in display order

Only the standard library here, so `calendar-view --check-config` (the role's validate step) needs nothing else.
"""

from __future__ import annotations

import ipaddress
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_PATH = "/etc/homelab-calendar-view/config.json"
ENV_VAR = "CALENDAR_VIEW_CONFIG"

KEYS = ("base_url", "user", "password", "timezone", "days", "listen_host", "listen_port", "calendars")
CALENDAR_KEYS = ("path", "label", "color")
# The same name rules as the calendar role (CONTRACTS §13.1): <login>/<calendar id>.
USER_RE = re.compile(r"[a-z][a-z0-9_-]{1,31}")
PATH_RE = re.compile(r"[a-z][a-z0-9_-]{1,31}/[a-z0-9-]{1,40}")
COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_DAYS = 14
MAX_CALENDARS = 12
MAX_LABEL = 40


class ConfigError(ValueError):
    """A bad setting. The message names the key, and never holds the password."""


@dataclass(frozen=True)
class CalendarSource:
    path: str
    label: str
    color: str


@dataclass(frozen=True)
class Config:
    base_url: str
    user: str
    password: str = field(repr=False)
    timezone: str
    days: int
    listen_host: str
    listen_port: int
    calendars: tuple[CalendarSource, ...]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _int(raw, key: str, low: int, high: int) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or not low <= raw <= high:
        raise ConfigError(f"{key} must be an integer from {low} to {high}")
    return raw


def _loopback(raw, key: str) -> str:
    if not isinstance(raw, str):
        raise ConfigError(f"{key} must be a loopback IP address")
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        raise ConfigError(f"{key} must be an IP literal, not a name") from None
    if not ip.is_loopback:
        raise ConfigError(f"{key} must be a loopback address")
    return str(ip)


def _base_url(raw) -> str:
    if not isinstance(raw, str):
        raise ConfigError("base_url must be a string")
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme != "http" or parts.username or parts.password or parts.query or parts.fragment \
            or parts.path not in ("", "/") or not parts.hostname:
        raise ConfigError("base_url must be http://<loopback address>:<port>, with no path or credentials")
    _loopback(parts.hostname, "base_url's host")
    try:
        port = parts.port
    except ValueError:
        raise ConfigError("base_url has a bad port") from None
    if port is None:
        raise ConfigError("base_url needs an explicit port")
    return f"http://{parts.netloc}"


def _text(raw, key: str, limit: int) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > limit or CONTROL_RE.search(raw):
        raise ConfigError(f"{key} must be text of 1 to {limit} characters")
    return raw.strip()


def _calendars(raw) -> tuple[CalendarSource, ...]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_CALENDARS:
        raise ConfigError(f"calendars must be a list of 1 to {MAX_CALENDARS} entries")
    out: list[CalendarSource] = []
    for i, entry in enumerate(raw):
        where = f"calendars[{i}]"
        if not isinstance(entry, dict) or set(entry) != set(CALENDAR_KEYS):
            raise ConfigError(f"{where} must have exactly the keys {', '.join(CALENDAR_KEYS)}")
        path, color = entry["path"], entry["color"]
        if not isinstance(path, str) or not PATH_RE.fullmatch(path):
            raise ConfigError(f'{where}.path must be "<login>/<calendar id>" (the calendar role\'s name rules)')
        if not isinstance(color, str) or not COLOR_RE.fullmatch(color):
            raise ConfigError(f"{where}.color must be #rrggbb")
        out.append(CalendarSource(path, _text(entry["label"], f"{where}.label", MAX_LABEL), color.lower()))
    if len({c.path for c in out}) != len(out):
        raise ConfigError("calendars lists the same path twice")
    return tuple(out)


def validate(raw) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("the config must be a JSON object")
    missing = [k for k in KEYS if k not in raw]
    extra = sorted(set(raw) - set(KEYS))
    if missing or extra:
        raise ConfigError("config keys: " + "; ".join(
            part for part in (f"missing {', '.join(missing)}" if missing else "",
                              f"unknown {', '.join(extra)}" if extra else "") if part))
    user = raw["user"]
    if not isinstance(user, str) or not USER_RE.fullmatch(user):
        raise ConfigError("user must be a calendar login (a lowercase letter, then 1 to 31 of a-z 0-9 _ -)")
    password = raw["password"]
    if not isinstance(password, str) or not password or CONTROL_RE.search(password):
        raise ConfigError("password must be a non-empty string without control characters")
    tz_name = raw["timezone"]
    if not isinstance(tz_name, str) or not tz_name:
        raise ConfigError("timezone must be an IANA time zone name")
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"timezone {tz_name!r} isn't a known IANA time zone") from None
    return Config(
        base_url=_base_url(raw["base_url"]),
        user=user,
        password=password,
        timezone=tz_name,
        days=_int(raw["days"], "days", 1, MAX_DAYS),
        listen_host=_loopback(raw["listen_host"], "listen_host"),
        listen_port=_int(raw["listen_port"], "listen_port", 0, 65535),
        calendars=_calendars(raw["calendars"]),
    )


def load(path: str) -> Config:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise ConfigError(f"can't read {path}: {e.strerror}") from None
    try:
        raw = json.loads(text)
    except ValueError as e:
        # Only the position: the parser's message can quote the text around it (the password).
        raise ConfigError(f"{path} isn't valid JSON (line {getattr(e, 'lineno', '?')})") from None
    return validate(raw)
