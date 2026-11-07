"""Configuration: one JSON object. Unknown keys are refused. `allowed_login` has no default (the role renders
Noah's Tailscale login from private host_vars); every source URL defaults to null (not configured)."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import zoneinfo
from dataclasses import dataclass
from typing import Any

CARD_IDS = ("ai", "ai_control", "money", "school", "home", "servers", "alerts")
LOCKSCREEN_LEVELS = ("none", "minimal", "names")
BACKUP_KINDS = ("file", "unit", "mediaserver")

# Push services a subscription endpoint may point at (host suffix match). Anything else is refused at subscribe
# time, so a subscription can't turn the server into a POST relay to an arbitrary host.
DEFAULT_PUSH_HOSTS = [
    "fcm.googleapis.com",  # Chrome, Edge on Android, Samsung Internet
    "updates.push.services.mozilla.com",  # Firefox
    "push.apple.com",  # Safari / iOS home-screen apps (web.push.apple.com)
    "notify.windows.com",  # Edge on Windows (WNS)
]

DEFAULTS: dict[str, Any] = {
    "listen_host": "127.0.0.1",
    "listen_port": 8097,
    "allowed_login": None,  # required
    "timezone": "America/New_York",
    "static_dir": "/opt/mission-control/web",
    "state_dir": "/var/lib/mission-control",
    "vapid_file": "/etc/mission-control/vapid.json",
    "push_ttl_s": 14400,
    "push_timeout_s": 10.0,
    "push_endpoint_hosts": DEFAULT_PUSH_HOSTS,
    "max_subscriptions": 20,
    "lockscreen_detail": "minimal",
    "cache_ttl_s": 60.0,
    "source_timeout_s": 5.0,
    "alert_interval_s": 300.0,
    "alert_repeat_hours": 24.0,
    # Sources. null = not configured (the card says "set up").
    "aicontrol_url": "http://127.0.0.1:8095",
    "webui_url": None,
    "moneyhub_url": None,
    "moneyhub_timezone": "UTC",
    "erau_url": None,
    "erau_timeout_s": 20.0,
    "erau_max_age_s": 1800.0,
    "mediaserver_url": None,
    "ha_url": None,
    "ha_token_file": "/etc/mission-control/ha-token",
    "ha_entities": [],
    # host name -> IP literal, used instead of DNS (ai-box has no MagicDNS; pins the address the IP filter allows).
    "host_map": {},
    "systemctl": "/usr/bin/systemctl",
    "units": [],
    "unit_restart_alert": 2,
    "unit_restart_window_s": 3600.0,
    "backups": [],
    "bank_sync_alert_hours": 48.0,
    "disk_alert_pct": 90.0,
    "deadline_window_hours": 48.0,
    "bills_window_days": 7,
    "links": {},
    # POST /api/notify (B2): senders, quiet hours, dedupe window and per-sender rate. [] = everyone gets a 401.
    "notify_senders": [],
    "quiet_hours": None,
    "notify_dedupe_seconds": 1800,
    "notify_rate_per_hour": 30,
    # B1: the "Apps" card, [{label, url}] in page order. [] = no card.
    "app_links": [],
    "log_level": "INFO",
}

_LOGIN_RE = re.compile(r"[^\s@]{1,128}@[^\s@]{1,128}")
_UNIT_RE = re.compile(r"[A-Za-z0-9:_.@][A-Za-z0-9:_.@-]{0,119}\.(service|socket|timer|mount|path|target)")
_ENTITY_RE = re.compile(r"[a-z_]{1,40}\.[a-z0-9_]{1,120}")
_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
_HOST_RE = re.compile(r"(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*\.?")
_LINK_RE = re.compile(r"https?://[^\s/?#@]+(/[^\s#]*)?")  # a tap-through link: http(s), no credentials or fragment
_SENDER_RE = re.compile(r"[a-z0-9-]{1,32}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_HHMM_RE = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()  # never a sender's hash: a missing token must not match it
_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Backup:
    id: str
    label: str
    kind: str
    path: str | None = None
    unit: str | None = None
    max_age_hours: float = 36.0
    timer: str | None = None  # kind "unit": the Persistent=true timer that starts it (default: <unit>.timer)


@dataclass(frozen=True)
class NotifySender:
    name: str
    token_sha256: str  # hex, lowercase: the server never holds the token itself
    allow_from: tuple[str, ...] = ()  # normalised IP literals; empty = any address


@dataclass(frozen=True)
class Config:
    allowed_login: str
    listen_host: str = "127.0.0.1"
    listen_port: int = 8097
    timezone: str = "America/New_York"
    static_dir: str = DEFAULTS["static_dir"]
    state_dir: str = DEFAULTS["state_dir"]
    vapid_file: str = DEFAULTS["vapid_file"]
    push_ttl_s: int = 14400
    push_timeout_s: float = 10.0
    push_endpoint_hosts: tuple[str, ...] = tuple(DEFAULT_PUSH_HOSTS)
    max_subscriptions: int = 20
    lockscreen_detail: str = "minimal"
    cache_ttl_s: float = 60.0
    source_timeout_s: float = 5.0
    alert_interval_s: float = 300.0
    alert_repeat_hours: float = 24.0
    aicontrol_url: str | None = "http://127.0.0.1:8095"
    webui_url: str | None = None
    moneyhub_url: str | None = None
    moneyhub_timezone: str = "UTC"
    erau_url: str | None = None
    erau_timeout_s: float = 20.0
    erau_max_age_s: float = 1800.0
    mediaserver_url: str | None = None
    ha_url: str | None = None
    ha_token_file: str = DEFAULTS["ha_token_file"]
    ha_entities: tuple[str, ...] = ()
    host_map: tuple[tuple[str, str], ...] = ()
    systemctl: str = "/usr/bin/systemctl"
    units: tuple[str, ...] = ()
    unit_restart_alert: int = 2
    unit_restart_window_s: float = 3600.0
    backups: tuple[Backup, ...] = ()
    bank_sync_alert_hours: float = 48.0
    disk_alert_pct: float = 90.0
    deadline_window_hours: float = 48.0
    bills_window_days: int = 7
    links: tuple[tuple[str, str], ...] = ()
    notify_senders: tuple[NotifySender, ...] = ()
    quiet_hours: tuple[int, int] | None = None  # (start, end) in minutes after local midnight; may cross midnight
    notify_dedupe_seconds: int = 1800
    notify_rate_per_hour: int = 30
    app_links: tuple[tuple[str, str], ...] = ()  # (label, url)
    log_level: str = "INFO"

    def link(self, card: str) -> str | None:
        return dict(self.links).get(card)


# ---- field validators ------------------------------------------------------------------------------------
def _url(raw: dict[str, Any], key: str, *, nullable: bool = True) -> str | None:
    value = raw.get(key, DEFAULTS[key])
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"https?://[^\s/?#@]+(/[^\s?#]*)?", value):
        raise ConfigError(f"{key} must be an http(s) URL without credentials, query or fragment" + (" (or null)" if nullable else ""))
    return value.rstrip("/")


def _abs_path(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key, DEFAULTS[key])
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value or "/../" in value + "/":
        raise ConfigError(f"{key} must be an absolute path")
    return value


def _number(raw: dict[str, Any], key: str, lo: float, hi: float) -> float:
    value = raw.get(key, DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
        raise ConfigError(f"{key} must be a number {lo:g}-{hi:g}")
    return float(value)


def _int(raw: dict[str, Any], key: str, lo: int, hi: int) -> int:
    value = raw.get(key, DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ConfigError(f"{key} must be an integer {lo}-{hi}")
    return value


def _zone(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key, DEFAULTS[key])
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*", value):
        raise ConfigError(f"{key} must be an IANA time zone name")
    try:
        zoneinfo.ZoneInfo(value)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"{key} {value!r} is not a known time zone") from None
    return value


def _str_list(raw: dict[str, Any], key: str, pattern: re.Pattern[str], what: str, max_len: int) -> tuple[str, ...]:
    value = raw.get(key, DEFAULTS[key])
    if not isinstance(value, list) or len(value) > max_len:
        raise ConfigError(f"{key} must be a list of at most {max_len} {what}")
    for v in value:
        if not isinstance(v, str) or not pattern.fullmatch(v):
            raise ConfigError(f"{key}: {str(v)[:60]!r} is not a valid {what}")
    if len(set(value)) != len(value):
        raise ConfigError(f"{key} lists a value twice")
    return tuple(value)


def _host_map(raw: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    value = raw.get("host_map", DEFAULTS["host_map"])
    if not isinstance(value, dict) or len(value) > 16:
        raise ConfigError("host_map must be an object of at most 16 {host name: IP address}")
    out = []
    for host, ip in value.items():
        if not isinstance(host, str) or not _HOST_RE.fullmatch(host):
            raise ConfigError(f"host_map: {str(host)[:60]!r} is not a host name")
        try:
            ipaddress.ip_address(ip if isinstance(ip, str) else "")
        except ValueError:
            raise ConfigError(f"host_map[{host!r}] must be an IP address literal") from None
        out.append((host.lower().rstrip("."), ip))
    return tuple(out)


def _backups(raw: dict[str, Any]) -> tuple[Backup, ...]:
    value = raw.get("backups", DEFAULTS["backups"])
    if not isinstance(value, list) or len(value) > 12:
        raise ConfigError("backups must be a list of at most 12 entries")
    out: list[Backup] = []
    for b in value:
        if not isinstance(b, dict):
            raise ConfigError("each backups entry must be an object")
        kind = b.get("kind")
        allowed = {"id", "label", "kind", "max_age_hours"} | ({"path"} if kind == "file" else {"unit", "timer"} if kind == "unit" else set())
        if kind not in BACKUP_KINDS:
            raise ConfigError(f"backups: kind must be one of {', '.join(BACKUP_KINDS)}")
        extra = set(b) - allowed
        if extra:
            raise ConfigError(f"backups: unknown keys for kind {kind}: {', '.join(sorted(extra))}")
        bid, label = b.get("id"), b.get("label")
        if not isinstance(bid, str) or not _ID_RE.fullmatch(bid):
            raise ConfigError("backups: id must match [a-z0-9][a-z0-9_-]{0,39}")
        if not isinstance(label, str) or not 0 < len(label) <= 60 or not label.isprintable():
            raise ConfigError(f"backups[{bid}]: label must be 1-60 printable characters")
        age = b.get("max_age_hours", 36.0)
        if isinstance(age, bool) or not isinstance(age, (int, float)) or not 1 <= age <= 24 * 60:
            raise ConfigError(f"backups[{bid}]: max_age_hours must be a number 1-1440")
        path = unit = timer = None
        if kind == "file":
            path = b.get("path")
            if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
                raise ConfigError(f"backups[{bid}]: path must be an absolute path")
        elif kind == "unit":
            unit = b.get("unit")
            if not isinstance(unit, str) or not _UNIT_RE.fullmatch(unit):
                raise ConfigError(f"backups[{bid}]: unit must be a systemd unit name")
            timer = b.get("timer", unit.removesuffix(".service") + ".timer" if unit.endswith(".service") else None)
            if timer is not None and (not isinstance(timer, str) or not _UNIT_RE.fullmatch(timer)
                                      or not timer.endswith(".timer")):
                raise ConfigError(f"backups[{bid}]: timer must be a systemd .timer unit name (or null)")
        out.append(Backup(bid, label, kind, path, unit, float(age), timer))
    if len({b.id for b in out}) != len(out):
        raise ConfigError("backups: ids must be unique")
    if sum(b.kind == "mediaserver" for b in out) > 1:
        raise ConfigError("backups: at most one mediaserver entry")
    return tuple(out)


def _links(raw: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    value = raw.get("links", DEFAULTS["links"])
    if not isinstance(value, dict):
        raise ConfigError("links must be an object {card id: URL}")
    out = []
    for card, url in value.items():
        if card not in CARD_IDS:
            raise ConfigError(f"links: unknown card {str(card)[:40]!r} (cards: {', '.join(CARD_IDS)})")
        if not isinstance(url, str) or not _LINK_RE.fullmatch(url) or len(url) > 300:
            raise ConfigError(f"links[{card}] must be an http(s) URL without credentials")
        out.append((card, url))
    return tuple(out)


def _notify_senders(raw: dict[str, Any]) -> tuple[NotifySender, ...]:
    value = raw.get("notify_senders", DEFAULTS["notify_senders"])
    if not isinstance(value, list) or len(value) > 32:
        raise ConfigError("notify_senders must be a list of at most 32 {name, token_sha256, allow_from}")
    out: list[NotifySender] = []
    for s in value:
        if not isinstance(s, dict):
            raise ConfigError("each notify_senders entry must be an object")
        extra = set(s) - {"name", "token_sha256", "allow_from"}
        if extra:
            raise ConfigError(f"notify_senders: unknown keys: {', '.join(sorted(str(k)[:40] for k in extra))}")
        name, digest = s.get("name"), s.get("token_sha256")
        if not isinstance(name, str) or not _SENDER_RE.fullmatch(name):
            raise ConfigError("notify_senders: name must match [a-z0-9-]{1,32}")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest) or digest == EMPTY_SHA256:
            raise ConfigError(f"notify_senders[{name}]: token_sha256 must be 64 lowercase hex characters "
                              "(the SHA-256 of a non-empty token)")
        allow = s.get("allow_from", [])
        if not isinstance(allow, list) or len(allow) > 16:
            raise ConfigError(f"notify_senders[{name}]: allow_from must be a list of at most 16 IP addresses")
        ips = []
        for ip in allow:
            try:
                ips.append(str(ipaddress.ip_address(ip if isinstance(ip, str) else "")))
            except ValueError:
                raise ConfigError(f"notify_senders[{name}]: allow_from: {str(ip)[:60]!r} is not an IP address") from None
        if len(set(ips)) != len(ips):
            raise ConfigError(f"notify_senders[{name}]: allow_from lists an address twice")
        out.append(NotifySender(name, digest, tuple(ips)))
    if len({x.name for x in out}) != len(out):
        raise ConfigError("notify_senders: names must be unique")
    if len({x.token_sha256 for x in out}) != len(out):
        raise ConfigError("notify_senders: token_sha256 values must be unique")
    return tuple(out)


def _quiet_hours(raw: dict[str, Any]) -> tuple[int, int] | None:
    value = raw.get("quiet_hours", DEFAULTS["quiet_hours"])
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise ConfigError('quiet_hours must be {"start": "HH:MM", "end": "HH:MM"} or null')
    mins = []
    for k in ("start", "end"):
        m = _HHMM_RE.fullmatch(value[k]) if isinstance(value[k], str) else None
        if m is None:
            raise ConfigError(f"quiet_hours.{k} must be HH:MM (00:00-23:59)")
        mins.append(int(m.group(1)) * 60 + int(m.group(2)))
    if mins[0] == mins[1]:
        raise ConfigError("quiet_hours: start and end must differ (use null for no quiet hours)")
    return mins[0], mins[1]


def _app_links(raw: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    value = raw.get("app_links", DEFAULTS["app_links"])
    if not isinstance(value, list) or len(value) > 12:
        raise ConfigError("app_links must be a list of at most 12 {label, url}")
    out = []
    for a in value:
        if not isinstance(a, dict) or set(a) != {"label", "url"}:
            raise ConfigError("each app_links entry must be exactly {label, url}")
        label, url = a["label"], a["url"]
        if not isinstance(label, str) or not 0 < len(label) <= 32 or not label.isprintable() or not label.strip():
            raise ConfigError("app_links: label must be 1-32 printable characters")
        if not isinstance(url, str) or not _LINK_RE.fullmatch(url) or len(url) > 300:
            raise ConfigError(f"app_links[{label}]: url must be an http(s) URL without credentials")
        out.append((label, url))
    return tuple(out)


def from_dict(raw: dict[str, Any]) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")

    host = raw.get("listen_host", DEFAULTS["listen_host"])
    if host != "127.0.0.1":
        raise ConfigError("listen_host must be 127.0.0.1 (tailscale serve is the only way in)")

    login = raw.get("allowed_login")
    if not isinstance(login, str) or not _LOGIN_RE.fullmatch(login):
        raise ConfigError("allowed_login is required: Noah's Tailscale login (user@domain)")

    lock = raw.get("lockscreen_detail", DEFAULTS["lockscreen_detail"])
    if lock not in LOCKSCREEN_LEVELS:
        raise ConfigError(f"lockscreen_detail must be one of {', '.join(LOCKSCREEN_LEVELS)}")

    hosts = raw.get("push_endpoint_hosts", DEFAULTS["push_endpoint_hosts"])
    if not isinstance(hosts, list) or not hosts or len(hosts) > 16 or not all(
        isinstance(h, str) and _HOST_RE.fullmatch(h) and "." in h for h in hosts
    ):
        raise ConfigError("push_endpoint_hosts must be a non-empty list of host names (suffix match)")

    level = str(raw.get("log_level", DEFAULTS["log_level"])).upper()
    if level not in _LEVELS:
        raise ConfigError(f"log_level must be one of {', '.join(_LEVELS)}")

    return Config(
        allowed_login=login,
        listen_host=host,
        listen_port=_int(raw, "listen_port", 1, 65535),
        timezone=_zone(raw, "timezone"),
        static_dir=_abs_path(raw, "static_dir"),
        state_dir=_abs_path(raw, "state_dir"),
        vapid_file=_abs_path(raw, "vapid_file"),
        push_ttl_s=_int(raw, "push_ttl_s", 60, 2419200),
        push_timeout_s=_number(raw, "push_timeout_s", 1, 30),
        push_endpoint_hosts=tuple(h.lower() for h in hosts),
        max_subscriptions=_int(raw, "max_subscriptions", 1, 100),
        lockscreen_detail=lock,
        cache_ttl_s=_number(raw, "cache_ttl_s", 0, 3600),
        source_timeout_s=_number(raw, "source_timeout_s", 0.05, 15),
        alert_interval_s=_number(raw, "alert_interval_s", 1, 86400),
        alert_repeat_hours=_number(raw, "alert_repeat_hours", 0.01, 720),
        aicontrol_url=_url(raw, "aicontrol_url"),
        webui_url=_url(raw, "webui_url"),
        moneyhub_url=_url(raw, "moneyhub_url"),
        moneyhub_timezone=_zone(raw, "moneyhub_timezone"),
        erau_url=_url(raw, "erau_url"),
        erau_timeout_s=_number(raw, "erau_timeout_s", 5, 60),
        erau_max_age_s=_number(raw, "erau_max_age_s", 60, 86400),
        mediaserver_url=_url(raw, "mediaserver_url"),
        ha_url=_url(raw, "ha_url"),
        ha_token_file=_abs_path(raw, "ha_token_file"),
        ha_entities=_str_list(raw, "ha_entities", _ENTITY_RE, "Home Assistant entity id", 12),
        host_map=_host_map(raw),
        systemctl=_abs_path(raw, "systemctl"),
        units=_str_list(raw, "units", _UNIT_RE, "systemd unit name", 32),
        unit_restart_alert=_int(raw, "unit_restart_alert", 1, 1000),
        unit_restart_window_s=_number(raw, "unit_restart_window_s", 60, 86400),
        backups=_backups(raw),
        bank_sync_alert_hours=_number(raw, "bank_sync_alert_hours", 1, 24 * 60),
        disk_alert_pct=_number(raw, "disk_alert_pct", 50, 99.9),
        deadline_window_hours=_number(raw, "deadline_window_hours", 1, 24 * 14),
        bills_window_days=_int(raw, "bills_window_days", 1, 31),
        links=_links(raw),
        notify_senders=_notify_senders(raw),
        quiet_hours=_quiet_hours(raw),
        notify_dedupe_seconds=_int(raw, "notify_dedupe_seconds", 0, 86400),
        notify_rate_per_hour=_int(raw, "notify_rate_per_hour", 1, 1000),
        app_links=_app_links(raw),
        log_level=level,
    )


def load(path: str) -> Config:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc.strerror or type(exc).__name__}") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"config {path} is not valid JSON ({type(exc).__name__})") from None
    return from_dict(raw)
