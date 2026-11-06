"""The read-only collectors. Each one returns plain JSON-able data or raises SourceError; `Collector` runs them
in parallel, each under its own timeout, so a slow or dead source only turns its own result into an error.
Results are cached for `cache_ttl_s` (single flight: concurrent callers share one collection).

Nothing here writes anywhere. Money Hub has no auth (any tailnet client can write to it), so its client
refuses every method but GET and every path outside a frozen allowlist, before a request is built.
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import datetime as dt
import logging
import re
import time
import types
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from .config import Config
from .net import SourceError, describe, get_json, get_status, request_json
from .util import cents, clean, finite, hhmm, local, parse_ts

log = logging.getLogger("mission_control.sources")

# ---- Money Hub: GET only, frozen allowlist ---------------------------------------------------------------------
MONEYHUB_METHODS = frozenset({"GET"})
MONEYHUB_ALLOWED = types.MappingProxyType(
    {
        "/api/state": frozenset(),
        "/api/bills": frozenset({"period"}),
        "/api/budget": frozenset({"month"}),
    }
)
_MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
_ORDINAL_RE = re.compile(r"([0-9]{1,2})(?:st|nd|rd|th)")
_QUARTERLY_RE = re.compile(r"qtrly ([0-9]{1,2})/([0-9]{1,2})")
MAX_BILLS = 50


class MoneyHub:
    """The only way this app talks to Money Hub."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str, timeout: float) -> None:
        self.session = session
        self.base_url = base_url
        self.timeout = timeout

    async def call(self, method: str, path: str, params: dict[str, str] | None = None) -> Any:
        if method not in MONEYHUB_METHODS:
            raise SourceError("refused: Money Hub is read-only here")
        allowed = MONEYHUB_ALLOWED.get(path)
        if allowed is None:
            raise SourceError("refused: path not on the Money Hub allowlist")
        params = dict(params or {})
        for key, value in params.items():
            if key not in allowed or not isinstance(value, str) or not _MONTH_RE.fullmatch(value):
                raise SourceError("refused: query parameter not allowed")
        return await request_json(self.session, method, self.base_url + path, self.timeout, params=params or None,
                                  limit=2 * 1024 * 1024)

    async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        return await self.call("GET", path, params)


def month_of(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def due_dates(rule: Any, period: str) -> list[dt.date] | None:
    """A bill's due dates within month `period` (Money Hub's due_rule): "5th" -> the 5th (clamped to the
    month's end); "qtrly 7/20" -> the 20th of every third month from July. None: no fixed day (biweekly...)."""
    year, month = int(period[:4]), int(period[5:])
    last = calendar.monthrange(year, month)[1]
    r = str(rule or "").strip().lower()
    m = _ORDINAL_RE.fullmatch(r)
    if m:
        d = int(m.group(1))
        return [dt.date(year, month, min(d, last))] if 1 <= d <= 31 else None
    m = _QUARTERLY_RE.fullmatch(r)
    if m:
        qm, d = int(m.group(1)), int(m.group(2))
        if not (1 <= qm <= 12 and 1 <= d <= 31):
            return None
        return [dt.date(year, month, min(d, last))] if (month - qm) % 3 == 0 else []
    return None


def _rows(obj: Any, key: str) -> list[dict[str, Any]]:
    rows = obj.get(key) if isinstance(obj, dict) else None
    if not isinstance(rows, list):
        raise SourceError("unexpected reply shape")
    return [r for r in rows if isinstance(r, dict)]


async def moneyhub(session: aiohttp.ClientSession, cfg: Config, now: float) -> dict[str, Any]:
    assert cfg.moneyhub_url is not None
    hub = MoneyHub(session, cfg.moneyhub_url, cfg.source_timeout_s)
    today = local(now, cfg.timezone).date()
    window_end = today + dt.timedelta(days=cfg.bills_window_days - 1)
    periods = sorted({month_of(today), month_of(window_end)})

    async def part(fn: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except SourceError as exc:
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - bad upstream data
            return {"error": describe(exc)}

    async def sync() -> dict[str, Any]:
        s = await hub.get("/api/state")
        if not isinstance(s, dict):
            raise SourceError("unexpected reply shape")
        stale = s.get("stale_accounts")
        last_ok = parse_ts(s.get("sync_last_at"), cfg.moneyhub_timezone)
        future = last_ok is not None and last_ok > now + 3600  # a clock problem: the time is unknown
        return {
            "failing": s.get("sync_failing") is True,
            "last_ok": None if future else last_ok,
            "last_ok_future": future,
            "last_attempt": parse_ts(s.get("sync_last_attempt_at"), cfg.moneyhub_timezone),
            "sync_error": clean(s.get("sync_error"), 100) or None,
            "stale_accounts": stale if isinstance(stale, int) and not isinstance(stale, bool) else None,
        }

    async def bills() -> dict[str, Any]:
        due: list[dict[str, Any]] = []
        undated = 0
        replies = await asyncio.gather(*(hub.get("/api/bills", {"period": p}) for p in periods))
        for period, reply in zip(periods, replies):
            for b in _rows(reply, "rows"):
                if b.get("paid") is True:
                    continue
                dates = due_dates(b.get("due_rule"), period)
                if dates is None:
                    undated += 1 if period == periods[0] else 0
                    continue
                for d in dates:
                    if today <= d <= window_end:
                        due.append({"name": clean(b.get("name"), 60) or "Bill", "amount_cents": cents(b.get("amount_cents")),
                                    "due": d.isoformat(), "check": clean(b.get("check"), 8) or None})
        due.sort(key=lambda r: (r["due"], r["name"].lower()))
        return {"due": due[:MAX_BILLS], "count": len(due), "undated_unpaid": undated,
                "total_cents": sum(r["amount_cents"] or 0 for r in due), "window_days": cfg.bills_window_days}

    async def budget() -> dict[str, Any]:
        month = month_of(today)
        cats = _rows(await hub.get("/api/budget", {"month": month}), "categories")
        left = over = near = capped = 0
        for c in cats:
            cap, spent = cents(c.get("cap_cents")), cents(c.get("spent_cents"))
            if cap is None or cap <= 0 or spent is None:
                continue
            capped += 1
            left += cap - spent
            if spent > cap:
                over += 1
            elif spent * 100 >= 85 * cap:
                near += 1
        return {"month": month, "left_cents": left, "over": over, "near": near, "categories": capped}

    s, b, g = await asyncio.gather(part(sync), part(bills), part(budget))
    if all("error" in x for x in (s, b, g)):
        raise SourceError(s["error"])
    return {"sync": s, "bills": b, "budget": g}


# ---- ERAU dashboard ------------------------------------------------------------------------------------------
MAX_DEADLINES = 12


def _iso_epoch(v: Any) -> float | None:
    return parse_ts(v, "UTC") if isinstance(v, str) else None


async def erau(session: aiohttp.ClientSession, cfg: Config, now: float) -> dict[str, Any]:
    """GET /api/snapshot through homebk-edge's `tailscale serve` (identity: ai-box's own node login). Only the
    open deadlines are kept, computed like the dashboard's openDeadlines(); nothing else of the payload is
    stored or passed on."""
    assert cfg.erau_url is not None
    p = await get_json(session, cfg.erau_url + "/api/snapshot", cfg.erau_timeout_s)
    if not isinstance(p, dict):
        raise SourceError("unexpected reply shape")
    st = p.get("status") if isinstance(p.get("status"), dict) else {}
    settings = p.get("settings") if isinstance(p.get("settings"), dict) else {}
    hidden_raw = settings.get("hiddenCourses")
    hidden = {x for x in hidden_raw if isinstance(x, (int, str))} if isinstance(hidden_raw, list) else set()
    out: dict[str, Any] = {
        "stale": st.get("stale") is True,
        "needs_sign_in": st.get("needsSignIn") is True,
        "fetched_at": _iso_epoch(st.get("fetchedAt")),
        "no_data": False,
    }
    snap = p.get("snapshot")
    if snap is None:
        out.update(no_data=True, upcoming=[], overdue=0, count=0)
        return out
    if not isinstance(snap, dict) or not isinstance(snap.get("tasks"), list):
        raise SourceError("unexpected reply shape")
    horizon = now + cfg.deadline_window_hours * 3600
    upcoming: list[dict[str, Any]] = []
    overdue = 0
    for t in snap["tasks"]:
        if not isinstance(t, dict) or any(t.get(k) is True for k in ("done", "skipped", "lapsed", "optional")):
            continue
        if t.get("courseId") in hidden:
            continue
        for m in t.get("milestones") or []:
            if not isinstance(m, dict) or m.get("done") is True:
                continue
            at = _iso_epoch(m.get("at"))
            if at is None or (m.get("soft") is True and at < now):
                continue
            if at < now:
                overdue += 1 if t.get("overdue") is True else 0
            elif at <= horizon:
                upcoming.append({
                    "at": int(at),
                    "course": clean(t.get("courseCode"), 20),
                    "what": clean(m.get("label"), 30) or "Due",
                    "title": clean(t.get("title"), 100),
                    "early": m.get("early") is True,
                    "soft": m.get("soft") is True,
                })
    upcoming.sort(key=lambda x: (x["at"], x["course"], x["title"]))
    out.update(upcoming=upcoming[:MAX_DEADLINES], count=len(upcoming), overdue=overdue)
    return out


# ---- AI Control, Open WebUI ------------------------------------------------------------------------------------
SERVICE_STATUSES = ("ok", "warn", "down", "unknown")


def _disks(section: Any) -> list[dict[str, Any]]:
    if not isinstance(section, dict) or not isinstance(section.get("disk"), list):
        return []
    out = []
    for d in section["disk"][:8]:
        if not isinstance(d, dict):
            continue
        total, avail = finite(d.get("total")), finite(d.get("avail"))
        if total and total > 0 and avail is not None and 0 <= avail <= total:
            out.append({"mount": clean(d.get("mount"), 60), "used_pct": round((total - avail) * 100 / total, 1)})
    return out


async def aicontrol(session: aiohttp.ClientSession, cfg: Config, now: float) -> dict[str, Any]:
    assert cfg.aicontrol_url is not None
    s = await get_json(session, cfg.aicontrol_url + "/api/summary", cfg.source_timeout_s)
    if not isinstance(s, dict) or not isinstance(s.get("services"), list):
        raise SourceError("unexpected reply shape")
    routing = s.get("routing") if isinstance(s.get("routing"), dict) else {}
    services = []
    for x in s["services"][:16]:
        if isinstance(x, dict) and isinstance(x.get("id"), str):
            status = x.get("status") if x.get("status") in SERVICE_STATUSES else "unknown"
            services.append({"id": clean(x["id"], 30), "status": status, "detail": clean(x.get("detail"), 80)})
    resources = s.get("resources") if isinstance(s.get("resources"), dict) else {}
    alerts = [a for a in s.get("alerts") or [] if isinstance(a, dict)] if isinstance(s.get("alerts"), list) else []
    return {
        "routing_to": routing.get("to") if routing.get("to") in ("gpu", "cpu") else None,
        "routing_reason": clean(routing.get("reason"), 40) or None,
        "services": services,
        "disks": {"aibox": _disks(resources.get("aibox")), "pc": _disks(resources.get("pc"))},
        "alerts": [{"level": "crit" if a.get("level") == "crit" else "warn", "text": clean(a.get("text"), 120)}
                   for a in alerts[:10]],
    }


async def webui(session: aiohttp.ClientSession, cfg: Config, now: float) -> dict[str, Any]:
    assert cfg.webui_url is not None
    status = await get_status(session, cfg.webui_url + "/health", cfg.source_timeout_s)
    if status != 200:
        raise SourceError(f"HTTP {status}", status=status)
    return {"healthy": True}


# ---- systemd units (local, unprivileged `systemctl show`) ------------------------------------------------------
UNIT_PROPS = ("Id,LoadState,ActiveState,SubState,Result,NRestarts,ExecMainExitTimestamp,StateChangeTimestamp,"
              "LastTriggerUSec")
MAX_SYSTEMCTL_OUTPUT = 1024 * 1024


def _unix_ts(v: str | None) -> int | None:
    if v and v.startswith("@") and v[1:].isdigit():
        n = int(v[1:])
        return n if n > 0 else None
    return None


def parse_show(text: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for block in text.split("\n\n"):
        props: dict[str, str] = {}
        for line in block.splitlines():
            k, sep, v = line.partition("=")
            if sep:
                props[k] = v
        uid = props.get("Id")
        if not uid:
            continue
        restarts = props.get("NRestarts", "")
        out[uid] = {
            "unit": clean(uid, 120),
            "load": clean(props.get("LoadState"), 20),
            "active": clean(props.get("ActiveState"), 20),
            "sub": clean(props.get("SubState"), 30),
            "result": clean(props.get("Result"), 30),
            "restarts": int(restarts) if restarts.isdigit() else None,
            "exit_ts": _unix_ts(props.get("ExecMainExitTimestamp")),
            "changed_ts": _unix_ts(props.get("StateChangeTimestamp")),
            "last_trigger": _unix_ts(props.get("LastTriggerUSec")),  # timers only; Persistent=true keeps it over reboots
        }
    return out


async def systemctl_show(systemctl: str, units: list[str], timeout: float) -> dict[str, dict[str, Any]]:
    """`systemctl show` for the configured units (names validated by config). Read-only, no privileges."""
    if not units:
        return {}
    args = [systemctl, "show", "--no-pager", "--timestamp=unix", "-p", UNIT_PROPS, "--", *units]
    env = {"LC_ALL": "C", "PATH": "/usr/bin:/bin", "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env)
    except OSError as exc:
        raise SourceError(f"systemctl not runnable ({type(exc).__name__})") from None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise SourceError(f"systemctl exited {proc.returncode}")
    if len(out) > MAX_SYSTEMCTL_OUTPUT:
        raise SourceError("systemctl output too large")
    return parse_show(out.decode("utf-8", "replace"))


def unit_status(u: dict[str, Any] | None) -> tuple[str, str]:
    """(ok | warn | down | unknown, detail). Restarts within a window are judged by views.units (history)."""
    if u is None or u.get("load") in ("not-found", "", None):
        return "unknown", "not found"
    active, sub = u.get("active"), u.get("sub")
    if active == "failed":
        return "down", f"failed ({u.get('result') or 'unknown'})"
    if sub == "auto-restart":
        return "warn", "restarting"
    if active in ("active", "reloading", "activating"):
        return "ok", f"{active} ({sub})" if sub else str(active)
    if active == "inactive":
        return ("ok", "idle") if u.get("result") in ("success", "") else ("warn", f"stopped ({u.get('result')})")
    return "unknown", clean(f"{active} ({sub})", 60)


# ---- backups ---------------------------------------------------------------------------------------------------
def backup_from_file(path: str, max_age_h: float, now: float) -> dict[str, Any]:
    """A LAST_OK-style file: an ISO 8601 UTC timestamp on its first line."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(4096)
    except FileNotFoundError:
        return {"status": "unknown", "detail": "no status file", "last_ok": None}
    except OSError:
        return {"status": "unknown", "detail": "status file not readable", "last_ok": None}
    lines = text.strip().splitlines()
    ts = parse_ts(lines[0].strip(), "UTC") if lines else None
    if ts is None:
        return {"status": "unknown", "detail": "status file is not a timestamp", "last_ok": None}
    return _age_status(ts, max_age_h, now)


def _age_status(ts: float, max_age_h: float, now: float) -> dict[str, Any]:
    if ts > now + 3600:
        return {"status": "unknown", "detail": "timestamp in the future", "last_ok": int(ts)}
    if now - ts > max_age_h * 3600:
        return {"status": "stale", "detail": f"older than {max_age_h:g} h", "last_ok": int(ts)}
    return {"status": "ok", "detail": "", "last_ok": int(ts)}


def backup_from_unit(u: dict[str, Any] | None, max_age_h: float, now: float,
                     timer: dict[str, Any] | None = None, tz: str = "UTC") -> dict[str, Any]:
    """The backup's oneshot service: its last run's Result and exit time (LAST_OK moves only on success).

    systemd forgets a service's result and exit time across a reboot. With no run since boot, the timer's
    LastTriggerUSec (kept on disk for a Persistent=true timer) says only that a run *started*, not how it ended
    (Reviewer Z7-1): a trigger within the max age gives **unknown** ("result lost at reboot"; unknown holds the
    alert state, so a failure raised before the reboot stays raised and nothing new fires), or "running now"
    while the catch-up run is going; an older trigger is stale. A failure since boot always wins."""
    if u is None or u.get("load") in ("not-found", "", None):
        return {"status": "unknown", "detail": "unit not found", "last_ok": None}
    if u.get("active") == "failed" or u.get("result") not in ("success", ""):
        return {"status": "failed", "detail": f"last run {u.get('result') or 'failed'}", "last_ok": None}
    ts = u.get("exit_ts")
    if ts is not None:
        return _age_status(float(ts), max_age_h, now)
    trig = timer.get("last_trigger") if timer and timer.get("load") not in ("not-found", "", None) else None
    if trig is None:
        return {"status": "stale", "detail": "no run since boot", "last_ok": None}
    v = _age_status(float(trig), max_age_h, now)
    if v["status"] == "ok":
        if u.get("active") == "activating":
            v["detail"] = "running now"
        else:
            fired = local(float(trig), tz)
            v = {"status": "unknown", "detail": f"result lost at reboot; timer last fired {fired:%a} {hhmm(fired)}",
                 "last_ok": None}
    elif v["status"] == "stale":
        v["detail"] = f"no run since boot; timer last fired over {max_age_h:g} h ago"
    return v


async def mediaserver(session: aiohttp.ClientSession, cfg: Config, now: float) -> dict[str, Any]:
    """The mediaserver dashboard's /api/summary (tailnet access, no identity needed): only `backups` is kept."""
    assert cfg.mediaserver_url is not None
    s = await get_json(session, cfg.mediaserver_url + "/api/summary", cfg.source_timeout_s)
    b = s.get("backups") if isinstance(s, dict) else None
    if not isinstance(b, dict):
        raise SourceError("unexpected reply shape")
    status = b.get("status")
    last_ok = parse_ts(b.get("lastOk"), "UTC")
    mapped = {"ok": "ok", "stale": "stale", "failed": "failed", "none": "stale"}.get(status, "unknown")
    detail = {"none": "no backup yet", "unknown": "status unknown"}.get(status, "")
    return {"status": mapped, "detail": detail, "last_ok": int(last_ok) if last_ok else None}


# ---- Home Assistant ----------------------------------------------------------------------------------------------
def read_token(path: str) -> str | None:
    """A long-lived token from a file, or None when the file doesn't exist. Never logged or returned."""
    try:
        with open(path, encoding="utf-8") as fh:
            token = fh.read(8192).strip()
    except FileNotFoundError:
        return None
    except OSError:
        raise SourceError("token file not readable") from None
    if not token or any(c.isspace() for c in token) or not token.isprintable():
        raise SourceError("token file is empty or malformed")
    return token


async def home_assistant(session: aiohttp.ClientSession, cfg: Config, now: float) -> dict[str, Any]:
    assert cfg.ha_url is not None
    token = read_token(cfg.ha_token_file)
    if token is None:
        return {"connect": True, "entities": []}
    headers = {"Authorization": f"Bearer {token}"}

    async def one(entity: str) -> dict[str, Any]:
        url = f"{cfg.ha_url}/api/states/{urllib.parse.quote(entity, safe='._')}"
        try:
            e = await get_json(session, url, cfg.source_timeout_s, headers=headers, limit=256 * 1024)
        except SourceError as exc:
            if exc.status == 401:
                raise SourceError("Home Assistant rejected the token (401)", status=401) from None
            return {"entity": entity, "name": entity, "state": None, "unit": None,
                    "error": "not found" if exc.status == 404 else str(exc).replace(token, "[redacted]")}
        attrs = e.get("attributes") if isinstance(e, dict) and isinstance(e.get("attributes"), dict) else {}
        return {"entity": entity, "name": clean(attrs.get("friendly_name"), 60) or entity,
                "state": clean(e.get("state"), 40) if isinstance(e, dict) else None,
                "unit": clean(attrs.get("unit_of_measurement"), 12) or None, "error": None}

    return {"connect": False, "entities": list(await asyncio.gather(*(one(x) for x in cfg.ha_entities)))}


# ---- the collector -----------------------------------------------------------------------------------------------
SOURCE_NAMES = ("aicontrol", "webui", "moneyhub", "erau", "mediaserver", "units", "home")


class Collector:
    """Runs every configured source in parallel and caches the snapshot for cache_ttl_s.

    ERAU is stale-while-revalidate (Lead decision 2026-10-28): its server rebuilds a cold cache in 7-9 s, and a
    fetch cancelled at the round's 5 s budget never lets it finish. So the ERAU fetch runs in a background task
    of its own, bounded by `erau_timeout_s` and never cancelled by a round. A round waits for it at most
    `erau_wait` (default source_timeout_s); if it hasn't landed, the last good result is used while it is at
    most `erau_max_age_s` old (marked `stale`, with `as_of`), else the source is "still loading". One fetch at
    a time; it updates the cache when it lands. `close()` cancels it (shutdown)."""

    def __init__(self, cfg: Config, session: aiohttp.ClientSession, clock: Callable[[], float] = time.time,
                 *, erau_wait: float | None = None) -> None:
        self.cfg = cfg
        self.session = session
        self.clock = clock
        self.current: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self.erau_wait = cfg.source_timeout_s if erau_wait is None else erau_wait
        self._erau_task: asyncio.Task[dict[str, Any]] | None = None
        self._erau_good: dict[str, Any] | None = None  # the last ok result
        self._erau_last_error: str | None = None  # the last finished fetch's error (cleared by a success)

    def _configured(self) -> dict[str, Callable[[float], Awaitable[Any]] | None]:
        c, s = self.cfg, self.session
        unit_names = list(dict.fromkeys([*c.units, *(b.unit for b in c.backups if b.kind == "unit" and b.unit),
                                         *(b.timer for b in c.backups if b.kind == "unit" and b.timer)]))
        need_ms = c.mediaserver_url is not None and any(b.kind == "mediaserver" for b in c.backups)
        return {
            "aicontrol": (lambda now: aicontrol(s, c, now)) if c.aicontrol_url else None,
            "webui": (lambda now: webui(s, c, now)) if c.webui_url else None,
            "moneyhub": (lambda now: moneyhub(s, c, now)) if c.moneyhub_url else None,
            "erau": None,  # run by _erau_round
            "mediaserver": (lambda now: mediaserver(s, c, now)) if need_ms else None,
            "units": (lambda now: systemctl_show(c.systemctl, unit_names, c.source_timeout_s)) if unit_names else None,
            "home": (lambda now: home_assistant(s, c, now)) if c.ha_url else None,
        }

    async def _run(self, name: str, fn: Callable[[float], Awaitable[Any]] | None, now: float,
                   timeout: float | None = None) -> dict[str, Any]:
        if fn is None:
            return {"state": "setup", "data": None, "error": None, "at": int(now)}
        timeout = self.cfg.source_timeout_s if timeout is None else timeout
        try:
            data = await asyncio.wait_for(fn(now), timeout)
            return {"state": "ok", "data": data, "error": None, "at": int(now)}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one source never breaks the others
            message = describe(exc, timeout)
            if not isinstance(exc, (SourceError, asyncio.TimeoutError, TimeoutError)):
                log.warning("source %s: unexpected %s", name, type(exc).__name__)
            return {"state": "error", "data": None, "error": message, "at": int(now)}

    # ---- ERAU: stale-while-revalidate -------------------------------------------------------------------
    async def _erau_fetch(self, now: float) -> dict[str, Any]:
        timeout = self.cfg.erau_timeout_s
        result = await self._run("erau", lambda t: erau(self.session, self.cfg, t), now, timeout)
        if result["state"] == "ok":
            result["at"] = int(self.clock())  # when it landed
            self._erau_good = result
            self._erau_last_error = None
        else:
            self._erau_last_error = result["error"]
        return result

    def _erau_start(self, now: float) -> asyncio.Task[dict[str, Any]]:
        task = self._erau_task
        if task is None or task.done():
            task = asyncio.create_task(self._erau_fetch(now), name="erau-fetch")
            self._erau_task = task
        return task

    async def _erau_round(self, now: float) -> dict[str, Any]:
        if not self.cfg.erau_url:
            return {"state": "setup", "data": None, "error": None, "at": int(now)}
        task = self._erau_start(now)
        done, _pending = await asyncio.wait({task}, timeout=self.erau_wait)  # never cancels the task
        if task in done:
            return task.result()  # _run never raises (CancelledError only on shutdown)
        good = self._erau_good
        if good is not None and self.clock() - good["at"] <= self.cfg.erau_max_age_s:
            return {**good, "stale": True, "as_of": good["at"]}
        last = f" (last try: {self._erau_last_error})" if self._erau_last_error else ""
        return {"state": "error", "data": None, "error": f"still loading{last}", "loading": True,
                "last_error": self._erau_last_error, "at": int(now)}

    async def close(self) -> None:
        task, self._erau_task = self._erau_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def collect(self) -> dict[str, Any]:
        now = self.clock()
        fns = self._configured()
        results = await asyncio.gather(*(self._erau_round(now) if n == "erau" else self._run(n, fns[n], now)
                                         for n in SOURCE_NAMES))
        snap = {"at": now, "sources": dict(zip(SOURCE_NAMES, results))}
        for name, r in snap["sources"].items():
            if r["state"] == "error":
                log.info("source %s: %s", name, r["error"])
        self.current = snap
        return snap

    async def snapshot(self, *, max_age: float | None = None) -> dict[str, Any]:
        ttl = self.cfg.cache_ttl_s if max_age is None else max_age
        async with self._lock:
            cur = self.current
            if cur is not None and self.clock() - cur["at"] < ttl:
                return cur
            return await self.collect()
