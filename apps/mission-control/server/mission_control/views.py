"""Derived views of a snapshot, shared by the cards, the alert rules and the briefing (one place decides what
"stale" or "down" means)."""

from __future__ import annotations

from typing import Any

from .config import Config
from .sources import backup_from_file, backup_from_unit, unit_status


def source(snap: dict[str, Any], name: str) -> dict[str, Any]:
    return snap["sources"].get(name) or {"state": "setup", "data": None, "error": None}


def ok_data(snap: dict[str, Any], name: str) -> Any:
    r = source(snap, name)
    return r["data"] if r["state"] == "ok" else None


def backups(snap: dict[str, Any], cfg: Config, now: float) -> list[dict[str, Any]]:
    """[{id, label, status: ok|stale|failed|unknown, detail, last_ok}] in config order."""
    units = source(snap, "units")
    ms = source(snap, "mediaserver")
    out = []
    for b in cfg.backups:
        if b.kind == "file":
            assert b.path is not None
            v = backup_from_file(b.path, b.max_age_hours, now)
        elif b.kind == "unit":
            if units["state"] != "ok":
                v = {"status": "unknown", "detail": f"systemd not readable: {units['error'] or 'not run'}", "last_ok": None}
            else:
                v = backup_from_unit(units["data"].get(b.unit), b.max_age_hours, now,
                                     units["data"].get(b.timer) if b.timer else None, cfg.timezone)
        elif ms["state"] == "ok":
            v = ms["data"]
        elif ms["state"] == "setup":
            v = {"status": "unknown", "detail": "mediaserver dashboard not configured", "last_ok": None}
        else:
            v = {"status": "unknown", "detail": f"mediaserver dashboard: {ms['error']}", "last_ok": None}
        out.append({"id": b.id, "label": b.label, **v})
    return out


def units(snap: dict[str, Any], cfg: Config, restarts: dict[str, list[list[float]]] | None = None
          ) -> list[dict[str, Any]] | None:
    """[{unit, status, detail}] for the configured units, or None when systemd couldn't be read. A unit whose
    NRestarts rose by `unit_restart_alert` or more within `unit_restart_window_s` (per `restarts`, the history
    kept by the alert loop) is "restarting repeatedly"; the cumulative NRestarts alone never is."""
    src = source(snap, "units")
    if not cfg.units:
        return []
    if src["state"] != "ok":
        return None
    out = []
    for u in cfg.units:
        status, detail = unit_status(src["data"].get(u))
        samples = (restarts or {}).get(u) or []
        rise = int(samples[-1][1] - min(s[1] for s in samples)) if samples else 0
        if status == "ok" and rise >= cfg.unit_restart_alert:
            status, detail = "warn", f"restarted {rise} times in the last {cfg.unit_restart_window_s / 60:g} min"
        out.append({"unit": u, "status": status, "detail": detail})
    return out


def service_status(snap: dict[str, Any], cfg: Config, sid: str) -> tuple[str, str] | None:
    """(ok|warn|down|unknown, detail) for an AI service; None when nothing could be read about it. Open WebUI is
    probed directly when webui_url is set; otherwise (and for the broker) AI Control's view is used."""
    if sid == "webui" and cfg.webui_url:
        w = source(snap, "webui")
        return ("ok", "healthy") if w["state"] == "ok" else ("down", w["error"] or "no answer")
    ac = ok_data(snap, "aicontrol")
    if ac is None:
        return None
    for s in ac["services"]:
        if s["id"] == sid:
            return s["status"], s["detail"]
    return "unknown", "not reported"
