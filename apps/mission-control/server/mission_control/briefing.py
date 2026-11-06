"""The morning briefing, composed deterministically from a snapshot (no AI in v1), plus the lock-screen texts.

The same snapshot, alerts and clock always give the same briefing. The in-app briefing may show names and
amounts (it is only served to Noah's login). The lock-screen text is what a push carries; it never holds an
amount, a deadline title or any text from a source:
  none     "Your briefing is ready."
  minimal  "Briefing ready: 3 deadlines, 2 bills, 1 alert"            (default)
  names    minimal + course codes, bill names and alert names, e.g. "3 deadlines (ABCD 101, MATH 111)"
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from . import views
from .alerts import Alert
from .config import Config
from .util import ago, day_label, hhmm, local, money, plural

TITLE = "Mission Control"
STATUS_OF = {"ok": "ok", "stale": "warn", "failed": "down", "unknown": "unknown", "warn": "warn", "crit": "down"}


def _count(n: int | None, word: str) -> str:
    if n is None:
        return f"{word}s unavailable"
    return f"no {word}s" if n == 0 else plural(n, word)


def _school(snap: dict[str, Any], cfg: Config, now: float, today: dt.date) -> tuple[dict[str, Any], int | None, list[str]]:
    src = views.source(snap, "erau")
    hours = f"{cfg.deadline_window_hours:g} h"
    section: dict[str, Any] = {"title": f"School: next {hours}", "items": [], "empty": f"Nothing due in the next {hours}."}
    if src["state"] == "setup":
        section["items"].append({"label": "ERAU dashboard not connected", "value": "", "status": "setup"})
        return section, None, []
    if src.get("loading"):
        last = src.get("last_error")
        section["items"].append({"label": "School data is still loading",
                                 "value": f"last try: {last}" if last else "the ERAU dashboard is slow to answer",
                                 "status": "unknown"})
        return section, None, []
    if src["state"] != "ok":
        section["items"].append({"label": "School data unavailable", "value": src["error"] or "", "status": "unknown"})
        return section, None, []
    d = src["data"]
    items = section["items"]
    if src.get("stale") and now - src["as_of"] > 600:
        items.append({"label": f"School data as of {hhmm(local(src['as_of'], cfg.timezone))}",
                      "value": "the ERAU dashboard is slow to answer", "status": "warn"})
    if d["needs_sign_in"]:
        items.append({"label": "Canvas needs a new access token", "value": "Settings in the ERAU dashboard", "status": "warn"})
    elif d["stale"]:
        items.append({"label": "School data is out of date", "value": "the ERAU dashboard's last sync is old", "status": "warn"})
    if d["no_data"]:
        items.append({"label": "The ERAU dashboard has no Canvas data yet", "value": "", "status": "unknown"})
        return section, None, []
    if d["overdue"]:
        items.append({"label": plural(d["overdue"], "deadline") + " past due", "value": "", "status": "warn"})
    for x in d["upcoming"]:
        when = local(x["at"], cfg.timezone)
        tag = " (recommended)" if x["soft"] else " (early)" if x["early"] else ""
        items.append({"label": f"{day_label(when, today)} {hhmm(when)} · {x['course']} {x['what']}{tag}".strip(),
                      "value": x["title"], "status": None})
    if d["count"] > len(d["upcoming"]):
        items.append({"label": f"+{d['count'] - len(d['upcoming'])} more", "value": "", "status": None})
    return section, d["count"], list(dict.fromkeys(x["course"] for x in d["upcoming"] if x["course"]))


def _bills(snap: dict[str, Any], cfg: Config, today: dt.date) -> tuple[dict[str, Any], int | None, list[str]]:
    src = views.source(snap, "moneyhub")
    section: dict[str, Any] = {"title": "Bills this week", "items": [], "empty": "No bills due this week."}
    if src["state"] == "setup":
        section["items"].append({"label": "Money Hub not connected", "value": "", "status": "setup"})
        return section, None, []
    bills = src["data"]["bills"] if src["state"] == "ok" else {"error": src["error"]}
    if "error" in bills:
        section["items"].append({"label": "Bills unavailable", "value": bills["error"] or "", "status": "unknown"})
        return section, None, []
    for b in bills["due"]:
        amount = money(b["amount_cents"]) if b["amount_cents"] is not None else "amount unknown"
        section["items"].append({"label": f"{day_label(dt.date.fromisoformat(b['due']), today)} · {b['name']}",
                                 "value": amount, "status": None})
    if bills["count"] > len(bills["due"]):
        section["items"].append({"label": f"+{bills['count'] - len(bills['due'])} more", "value": "", "status": None})
    return section, bills["count"], list(dict.fromkeys(b["name"] for b in bills["due"]))


def _backups(snap: dict[str, Any], cfg: Config, now: float) -> tuple[dict[str, Any], int]:
    section: dict[str, Any] = {"title": "Backups", "items": [], "empty": "No backups configured."}
    bad = 0
    for b in views.backups(snap, cfg, now):
        when = ""
        if b.get("last_ok"):
            when = f", {ago(now - b['last_ok'])}" if b["status"] == "ok" else f", last OK {ago(now - b['last_ok'])}"
        detail = f" ({b['detail']})" if b.get("detail") else ""
        word = {"ok": "OK", "stale": "stale", "failed": "failed", "unknown": "unknown"}[b["status"]]
        section["items"].append({"label": b["label"], "value": f"{word}{when}{detail}", "status": STATUS_OF[b["status"]]})
        bad += b["status"] in ("stale", "failed")
    return section, bad


def backup_line(backups: list[dict[str, Any]]) -> str:
    """"Backups: 2 OK, 1 unknown." Never "OK" for the set while one is stale, failed or unknown."""
    counts = {k: sum(b["status"] == k for b in backups) for k in ("ok", "stale", "failed", "unknown")}
    words = {"ok": "OK", "stale": "stale", "failed": "failed", "unknown": "unknown"}
    return "Backups: " + ", ".join(f"{n} {words[k]}" for k, n in counts.items() if n) + "."


def compose(snap: dict[str, Any], alerts: list[Alert], cfg: Config, now: float) -> dict[str, Any]:
    """`alerts`: the raised ones (alerts.briefing_alerts), never the pending ones, so the counts follow the debounce."""
    today = local(now, cfg.timezone).date()
    school, n_deadlines, courses = _school(snap, cfg, now, today)
    bills, n_bills, bill_names = _bills(snap, cfg, today)
    backups, n_bad_backups = _backups(snap, cfg, now)
    alert_section = {
        "title": "Alerts",
        "items": [{"label": a.text, "value": "", "status": STATUS_OF[a.level]} for a in alerts],
        "empty": "No alerts.",
    }
    counts = {"deadlines": n_deadlines, "bills": n_bills, "alerts": len(alerts), "backups_bad": n_bad_backups}
    headline = f"{_count(n_deadlines, 'deadline')}, {_count(n_bills, 'bill')}, {_count(len(alerts), 'alert')}"
    headline = headline[0].upper() + headline[1:]

    summary = []
    if n_deadlines:
        first = views.source(snap, "erau")["data"]["upcoming"][0]
        when = local(first["at"], cfg.timezone)
        summary.append(f"Next deadline: {first['course']} {first['what']}, {day_label(when, today).lower()} at {hhmm(when)}.")
    if n_bills:
        total = views.source(snap, "moneyhub")["data"]["bills"]["total_cents"]
        summary.append(f"{plural(n_bills, 'bill')} due this week, {money(total)} in all.")
    if backups["items"]:
        summary.append(backup_line(views.backups(snap, cfg, now)))

    return {
        "date": today.isoformat(),
        "generated_at": int(now),
        "headline": headline,
        "summary": " ".join(summary),
        "sections": [school, bills, alert_section, backups],
        "counts": counts,
        "lockscreen": lockscreen(counts, cfg.lockscreen_detail, courses, bill_names, alerts),
    }


def _names(names: list[str], limit: int = 3) -> str:
    shown = ", ".join(names[:limit])
    return f" ({shown}{', …' if len(names) > limit else ''})" if names else ""


def lockscreen(counts: dict[str, Any], detail: str, courses: list[str], bills: list[str], alerts: list[Alert]
               ) -> dict[str, str]:
    if detail == "none":
        return {"title": TITLE, "body": "Your briefing is ready."}
    d, b, a = counts["deadlines"], counts["bills"], counts["alerts"]
    if detail == "names":
        parts = [_count(d, "deadline") + _names(courses), _count(b, "bill") + _names(bills),
                 _count(a, "alert") + _names([x.named for x in alerts], 2)]
    else:
        parts = [_count(d, "deadline"), _count(b, "bill"), _count(a, "alert")]
    return {"title": TITLE, "body": "Briefing ready: " + ", ".join(parts)}


def briefing_push(b: dict[str, Any]) -> dict[str, str]:
    return {**b["lockscreen"], "tag": "mc-briefing", "url": "/#card-briefing"}


def alert_push(alerts: list[Alert], detail: str) -> dict[str, str]:
    if detail == "none":
        body = f"{plural(len(alerts), 'new alert')}."
    else:
        lines = [a.named if detail == "names" else a.minimal for a in alerts]
        lines = list(dict.fromkeys(lines))
        body = "; ".join(lines[:3]) + (f"; +{len(lines) - 3} more" if len(lines) > 3 else "")
    return {"title": f"{TITLE} alert", "body": body, "tag": "mc-alerts", "url": "/#card-alerts"}


def as_text(b: dict[str, Any]) -> str:
    """The briefing as plain text (`mission-control briefing --print`)."""
    out = [f"{TITLE} briefing for {b['date']}", b["headline"]]
    if b["summary"]:
        out.append(b["summary"])
    for s in b["sections"]:
        out.append("")
        out.append(s["title"])
        if not s["items"]:
            out.append(f"  {s['empty']}")
        for it in s["items"]:
            out.append(f"  - {it['label']}" + (f": {it['value']}" if it["value"] else ""))
    out += ["", f"Lock screen: {b['lockscreen']['body']}"]
    return "\n".join(out)
