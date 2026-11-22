"""GET /api/cards: one card per area, built from a snapshot. The shape is the MC1 <-> MC2 interface (README):

  {generated_at, config: {open_webui_url, vapid_public_key},
   cards: {<id>: {status: ok|warn|down|unknown|setup, headline, items: [{label, value, status|null}],
                  link|null, error|null, updated_at|null}}}

Card ids: ai, ai_control, money, school, home, servers, alerts (the briefing has its own endpoint), plus `apps`
(B1) only while `app_links` isn't empty; its items also carry `url`.
A source that failed gives its card `status: unknown` (down for Open WebUI's health check) and `error`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from . import views
from .alerts import DISK_HOSTS, Alert
from .config import Config
from .util import ago, day_label, hhmm, local, money, plural

RANK = {"ok": 0, "setup": 1, "unknown": 2, "warn": 3, "down": 4}
SERVICE_NAMES = {
    "gate": "GPU gate", "pc-ollama": "Gaming PC Ollama", "cpu-ollama": "ai-box CPU Ollama", "broker": "GPU broker",
    "webui": "Open WebUI", "helper": "ai-box helper", "agent": "Gaming PC host agent",
}
CRITICAL_AI = ("broker", "cpu-ollama", "webui")


def _worst(statuses: list[str], default: str = "ok") -> str:
    return max(statuses, key=lambda s: RANK.get(s, 2)) if statuses else default


def _card(cfg: Config, cid: str, status: str, headline: str, items: list[dict[str, Any]] | None = None,
          error: str | None = None, updated_at: int | None = None) -> dict[str, Any]:
    return {"status": status, "headline": headline, "items": items or [], "link": cfg.link(cid),
            "error": error, "updated_at": updated_at}


def _failed(cfg: Config, cid: str, src: dict[str, Any], what: str, status: str = "unknown") -> dict[str, Any] | None:
    if src["state"] == "setup":
        return _card(cfg, cid, "setup", f"{what} isn't connected yet")
    if src["state"] == "error":
        return _card(cfg, cid, status, f"Couldn't read {what}", error=src["error"], updated_at=src["at"])
    return None


def ai_card(snap: dict[str, Any], cfg: Config) -> dict[str, Any]:
    st = views.service_status(snap, cfg, "webui")
    ac = views.ok_data(snap, "aicontrol")
    if st is None:
        return _card(cfg, "ai", "unknown", "Open WebUI status unknown", error=views.source(snap, "aicontrol")["error"])
    items = []
    if ac and ac["routing_to"]:
        items.append({"label": "Answers on", "value": "GPU (gaming PC)" if ac["routing_to"] == "gpu" else "CPU (ai-box)",
                      "status": None})
    status = st[0] if st[0] in RANK else "unknown"
    headline = {"ok": "Open WebUI is up", "down": "Open WebUI is down"}.get(status, f"Open WebUI: {st[1]}")
    src = views.source(snap, "webui") if cfg.webui_url else views.source(snap, "aicontrol")
    return _card(cfg, "ai", status, headline, items, error=st[1] if status == "down" else None, updated_at=src["at"])


def ai_control_card(snap: dict[str, Any], cfg: Config) -> dict[str, Any]:
    src = views.source(snap, "aicontrol")
    failed = _failed(cfg, "ai_control", src, "AI Control")
    if failed:
        return failed
    d = src["data"]
    items = [{"label": SERVICE_NAMES.get(s["id"], s["id"]), "value": s["detail"], "status": s["status"]} for s in d["services"]]
    statuses = [("down" if s["id"] in CRITICAL_AI else "warn") if s["status"] == "down" else s["status"]
                for s in d["services"]]
    if d["alerts"]:
        items.append({"label": "AI Control alerts", "value": str(len(d["alerts"])), "status": "warn"})
    to = {"gpu": "the GPU (gaming PC)", "cpu": "the CPU (ai-box)"}.get(d["routing_to"] or "", None)
    headline = f"Routing to {to}" + (f" ({d['routing_reason']})" if d["routing_reason"] else "") if to else "Routing unknown"
    status = _worst([s if s != "unknown" else "ok" for s in statuses])
    return _card(cfg, "ai_control", status, headline, items, updated_at=src["at"])


def money_card(snap: dict[str, Any], cfg: Config, now: float) -> dict[str, Any]:
    src = views.source(snap, "moneyhub")
    failed = _failed(cfg, "money", src, "Money Hub")
    if failed:
        return failed
    d = src["data"]
    today = local(now, cfg.timezone).date()
    items: list[dict[str, Any]] = []
    statuses: list[str] = []
    sync = d["sync"]
    if "error" in sync:
        items.append({"label": "Bank sync", "value": f"unknown ({sync['error']})", "status": "unknown"})
        sync_line = None
    else:
        last = sync["last_ok"]
        future = sync.get("last_ok_future") is True
        late = not future and (last is None or now - last > cfg.bank_sync_alert_hours * 3600)
        word = "failing" if sync["failing"] else "late" if late else "OK"
        when = (", last good time unknown (in the future)" if future else
                f", last good {ago(now - last)}" if last else ", no good sync on record")
        s = "warn" if sync["failing"] or late else "ok"
        items.append({"label": "Bank sync", "value": word + when, "status": s})
        if sync["failing"] and sync.get("sync_error"):
            items.append({"label": "Sync error", "value": sync["sync_error"], "status": "warn"})
        statuses.append(s)
        sync_line = "Bank sync failing" if sync["failing"] else "Bank sync late" if late else None
    bills = d["bills"]
    if "error" in bills:
        items.append({"label": "Bills this week", "value": f"unavailable ({bills['error']})", "status": "unknown"})
    else:
        items.append({"label": "Bills this week", "value": f"{bills['count']} · {money(bills['total_cents'])}",
                      "status": None})
        for b in bills["due"][:6]:
            amount = money(b["amount_cents"]) if b["amount_cents"] is not None else "?"
            items.append({"label": f"{day_label(dt.date.fromisoformat(b['due']), today)} · {b['name']}", "value": amount,
                          "status": None})
    budget = d["budget"]
    if "error" in budget:
        items.append({"label": "Budget left", "value": f"unavailable ({budget['error']})", "status": "unknown"})
    else:
        s = "warn" if budget["over"] else "ok"
        statuses.append(s)
        items.append({"label": f"Budget left ({budget['month']})", "value": money(budget["left_cents"]), "status": s})
        if budget["over"] or budget["near"]:
            items.append({"label": "Categories", "value": f"{budget['over']} over, {budget['near']} near the cap",
                          "status": s})
    headline = sync_line or (f"{plural(bills['count'], 'bill')} due this week" if "error" not in bills else "Money Hub")
    return _card(cfg, "money", _worst(statuses), headline, items, updated_at=src["at"])


def school_card(snap: dict[str, Any], cfg: Config, now: float) -> dict[str, Any]:
    src = views.source(snap, "erau")
    if src.get("loading"):
        return _card(cfg, "school", "unknown", "School data is loading", error=src["error"], updated_at=src["at"])
    failed = _failed(cfg, "school", src, "the ERAU dashboard")
    if failed:
        return failed
    d = src["data"]
    today = local(now, cfg.timezone).date()
    items: list[dict[str, Any]] = []
    status = "ok"
    if d["needs_sign_in"]:
        items.append({"label": "Canvas", "value": "needs a new access token", "status": "warn"})
        status = "warn"
    elif d["stale"]:
        items.append({"label": "Canvas sync", "value": "out of date", "status": "warn"})
        status = "warn"
    if d["no_data"]:
        return _card(cfg, "school", "unknown", "No Canvas data yet", items, updated_at=src["at"])
    if d["overdue"]:
        items.append({"label": "Past due", "value": str(d["overdue"]), "status": "warn"})
        status = "warn"
    for x in d["upcoming"]:
        when = local(x["at"], cfg.timezone)
        items.append({"label": f"{day_label(when, today)} {hhmm(when)} · {x['course']} {x['what']}", "value": x["title"],
                      "status": None})
    hours = f"{cfg.deadline_window_hours:g} h"
    headline = f"{plural(d['count'], 'deadline')} in the next {hours}" if d["count"] else f"Nothing due in the next {hours}"
    if src.get("stale"):  # the last good result while a slow fetch runs (stale-while-revalidate)
        headline += f" (as of {hhmm(local(src['as_of'], cfg.timezone))})"
    return _card(cfg, "school", status, headline, items, updated_at=src["at"])


def home_card(snap: dict[str, Any], cfg: Config) -> dict[str, Any]:
    src = views.source(snap, "home")
    if src["state"] == "setup":
        return _card(cfg, "home", "setup", "Connect Home Assistant")
    failed = _failed(cfg, "home", src, "Home Assistant")
    if failed:
        return failed
    d = src["data"]
    if d["connect"]:
        return _card(cfg, "home", "setup", "Connect Home Assistant (no token yet)", updated_at=src["at"])
    items = [{"label": e["name"], "value": (e["state"] or "") + (f" {e['unit']}" if e["unit"] and e["state"] else "")
              if not e["error"] else e["error"], "status": "unknown" if e["error"] else None} for e in d["entities"]]
    return _card(cfg, "home", "ok", plural(len(items), "entity", "entities"), items, updated_at=src["at"])


def servers_card(snap: dict[str, Any], cfg: Config, now: float,
                 restarts: dict[str, list[list[float]]] | None = None) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    statuses: list[str] = []
    units = views.units(snap, cfg, restarts)
    if units is None:
        items.append({"label": "Services", "value": f"unreadable ({views.source(snap, 'units')['error']})", "status": "unknown"})
        statuses.append("unknown")
    for u in units or []:
        items.append({"label": u["unit"], "value": u["detail"], "status": u["status"]})
        statuses.append(u["status"])
    for b in views.backups(snap, cfg, now):
        when = ago(now - b["last_ok"]) if b.get("last_ok") else ""
        when = when if b["status"] == "ok" or not when else f"last OK {when}"
        value = ", ".join(x for x in ({"ok": "OK"}.get(b["status"], b["status"]), when, b.get("detail") or "") if x)
        s = {"ok": "ok", "stale": "warn", "failed": "down"}.get(b["status"], "unknown")
        items.append({"label": f"Backup: {b['label']}", "value": value, "status": s})
        statuses.append(s)
    ac = views.ok_data(snap, "aicontrol")
    if ac is not None:
        for host, disks in ac["disks"].items():
            for disk in disks:
                s = "warn" if disk["used_pct"] >= cfg.disk_alert_pct else "ok"
                items.append({"label": f"Disk {DISK_HOSTS.get(host, host)} {disk['mount']}", "value": f"{disk['used_pct']:g}% used",
                              "status": s})
                statuses.append(s)
    if not items:
        return _card(cfg, "servers", "setup", "No services or backups configured")
    bad = sum(1 for s in statuses if s in ("warn", "down"))
    headline = "All OK" if not bad else f"{plural(bad, 'item')} need{'s' if bad == 1 else ''} attention"
    if bad == 0 and "unknown" in statuses:
        headline = "Some status unknown"
    return _card(cfg, "servers", _worst(statuses), headline, items, updated_at=int(snap["at"]))


def alerts_card(alerts: list[Alert], state: dict[str, Any], cfg: Config, now: float) -> dict[str, Any]:
    items = []
    for a in alerts:
        since = state.get(a.id, {}).get("since")
        items.append({"label": a.text, "value": f"since {ago(now - since)}" if since else "",
                      "status": "down" if a.level == "crit" else "warn"})
    status = "down" if any(a.level == "crit" for a in alerts) else "warn" if alerts else "ok"
    return _card(cfg, "alerts", status, plural(len(alerts), "alert") if alerts else "No alerts", items,
                 updated_at=int(now))


def apps_card(cfg: Config) -> dict[str, Any]:
    """B1: a tap-through link to each homelab UI in `app_links`, in config order. No source behind it."""
    items = [{"label": label, "value": "", "status": None, "url": url} for label, url in cfg.app_links]
    return _card(cfg, "apps", "ok", plural(len(items), "app"), items)


def build(snap: dict[str, Any], alerts: list[Alert], alert_state: dict[str, Any], cfg: Config, now: float,
          vapid_public_key: str | None, restarts: dict[str, list[list[float]]] | None = None) -> dict[str, Any]:
    cards = {
        "ai": ai_card(snap, cfg),
        "ai_control": ai_control_card(snap, cfg),
        "money": money_card(snap, cfg, now),
        "school": school_card(snap, cfg, now),
        "home": home_card(snap, cfg),
        "servers": servers_card(snap, cfg, now, restarts),
        "alerts": alerts_card(alerts, alert_state, cfg, now),
    }
    if cfg.app_links:  # absent when empty, so the page is what it was before B1
        cards["apps"] = apps_card(cfg)
    return {
        "generated_at": int(now),
        "config": {"open_webui_url": cfg.link("ai"), "vapid_public_key": vapid_public_key},
        "cards": cards,
    }
