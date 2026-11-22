"""Alert rules and the dedup/repeat tracker.

Rules (CONTRACTS §12.29): bank sync failing over N h; any backup stale or failed; a configured unit failed or
restarting repeatedly (forge-api included); Open WebUI or the broker down; a disk over the threshold.

Each alert has a stable id. Hysteresis (Reviewer Z, Lead decision 2026-10-28), see `plan`:
- a "down" alert (a service or a unit down) is raised once it was seen in 2 of the last 3 rounds;
- a cleared alert is kept as "clearing" for 30 min, so one that comes back within that time is the same episode;
- within an episode a push repeats only above the highest level already delivered, or after the repeat interval;
- an alert whose source couldn't be read this round is *held* (state unchanged). A host whose disks are missing
  from AI Control's summary (the PC asleep) holds its disk alerts.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from . import views
from .config import Config
from .util import ago, clean, file_lock, local, read_json, write_json_atomic

log = logging.getLogger("mission_control.alerts")
LEVEL_RANK = {"warn": 1, "crit": 2}
PHASES = ("pending", "active", "clearing")
CONFIRM_ROUNDS = 2  # "down" alerts (a service or unit down) are raised when seen in 2 ...
CONFIRM_WINDOW = 3  # ... of the last 3 rounds
CLEAR_S = 30 * 60  # a cleared alert is forgotten after 30 min clean; until then it is "clearing"
DISK_HOSTS = {"aibox": "ai-box", "pc": "gaming PC"}


@dataclass(frozen=True)
class Alert:
    id: str
    level: str  # warn | crit
    text: str  # the in-app line (may name things and quote a source's error text; never secrets)
    minimal: str  # lock screen, lockscreen_detail=minimal: fixed wording from code only
    named: str  # lock screen, lockscreen_detail=names: adds the configured label / unit name, never source text
    confirm: int = 1  # consecutive rounds it must be seen before it is raised


def evaluate(snap: dict[str, Any], cfg: Config, now: float, restarts: dict[str, list[list[float]]] | None = None
             ) -> tuple[list[Alert], set[str]]:
    """(active alerts, held id prefixes). `restarts` is the NRestarts history (RestartStore)."""
    alerts: list[Alert] = []
    held: set[str] = set()

    # Bank sync (Money Hub /api/state).
    mh = views.source(snap, "moneyhub")
    sync = mh["data"]["sync"] if mh["state"] == "ok" else None
    if mh["state"] == "error" or (sync is not None and "error" in sync):
        held.add("bank-sync")
    elif sync is not None:
        last = sync.get("last_ok")
        if sync.get("last_ok_future"):  # a clock problem: the time is unknown, the failing flag still counts
            fire, since = sync.get("failing") is True, "last good sync time unknown (in the future)"
        elif last is None:
            fire, since = True, "no good sync on record"
        else:
            fire = now - last > cfg.bank_sync_alert_hours * 3600
            since = f"last good sync {ago(now - last)} ({local(last, cfg.timezone):%b %d})"
        if fire:
            why = f": {sync['sync_error']}" if sync.get("sync_error") else ""
            alerts.append(Alert("bank-sync", "warn", f"Bank sync failing, {since}{why}", "Bank sync failing",
                                "Bank sync failing"))

    # Backups.
    for b in views.backups(snap, cfg, now):
        aid = f"backup:{b['id']}"
        if b["status"] == "unknown":
            held.add(aid)
        elif b["status"] in ("stale", "failed"):
            when = f", last OK {ago(now - b['last_ok'])}" if b.get("last_ok") else ""
            detail = f" ({b['detail']})" if b.get("detail") else ""
            level = "crit" if b["status"] == "failed" else "warn"
            alerts.append(Alert(aid, level, f"Backup {b['status']}: {b['label']}{detail}{when}",
                                f"Backup {b['status']}", f"Backup {b['status']}: {b['label']}"))

    # Units.
    units = views.units(snap, cfg, restarts)
    if units is None:
        held.add("unit:")
    else:
        for u in units:
            if u["status"] == "down":
                alerts.append(Alert(f"unit:{u['unit']}", "crit", f"{u['unit']} {u['detail']}", "Service failed",
                                    f"Service failed: {u['unit']}", CONFIRM_ROUNDS))
            elif u["status"] == "warn":
                alerts.append(Alert(f"unit:{u['unit']}", "warn", f"{u['unit']} {u['detail']}", "Service restarting",
                                    f"Service restarting: {u['unit']}"))

    # Open WebUI and the broker.
    for sid, name in (("webui", "Open WebUI"), ("broker", "GPU broker")):
        st = views.service_status(snap, cfg, sid)
        if st is None:
            held.add(f"service:{sid}")
        elif st[0] == "down":
            alerts.append(Alert(f"service:{sid}", "crit", f"{name} is down ({clean(st[1], 80)})", f"{name} down",
                                f"{name} down", CONFIRM_ROUNDS))

    # Disks (from AI Control: ai-box and the gaming PC).
    ac = views.ok_data(snap, "aicontrol")
    if ac is None:
        held.add("disk:")
    else:
        crit_at = (100 + cfg.disk_alert_pct) / 2
        for host, disks in ac["disks"].items():
            if not disks:  # not in the summary (the PC asleep or its agent down): unknown, not cleared
                held.add(f"disk:{host}:")
            for d in disks:
                if d["used_pct"] >= cfg.disk_alert_pct:
                    label = f"{DISK_HOSTS.get(host, host)} {d['mount']}"
                    level = "crit" if d["used_pct"] >= crit_at else "warn"
                    alerts.append(Alert(f"disk:{host}:{d['mount']}", level, f"Disk {d['used_pct']:g}% full: {label}",
                                        f"Disk over {cfg.disk_alert_pct:g}%", f"Disk over {cfg.disk_alert_pct:g}%: {label}"))

    alerts.sort(key=lambda a: (-LEVEL_RANK[a.level], a.id))
    return alerts, held


def _held(aid: str, held: set[str]) -> bool:
    return any(aid == h or (h.endswith(":") and aid.startswith(h)) for h in held)


def _entry(a: Alert, **kw: Any) -> dict[str, Any]:
    """A state entry; the alert's texts are kept so the briefing can list a raised alert whose source is
    unreadable at 05:30."""
    return {"level": a.level, "text": a.text, "minimal": a.minimal, "named": a.named, **kw}


def plan(state: dict[str, Any], alerts: list[Alert], held: set[str], now: float, repeat_s: float,
         clear_s: float = CLEAR_S) -> tuple[list[Alert], dict[str, Any]]:
    """(alerts to push now, the next state).

    An alert with `confirm` = 2 stays *pending* until it was seen in 2 of the last 3 rounds (so a source that
    fails every other round is raised too); a pending alert not seen in any of the last 3 rounds is forgotten.
    A raised alert that disappears is kept as *clearing* for `clear_s`; if it comes back meanwhile it is the
    same episode (no new push). Within an episode an alert is pushed again only when its level goes above the
    highest level already delivered, or `repeat_s` after the last delivery. A held alert (its source unreadable
    this round) keeps its state unchanged. `mark_sent` stamps what was delivered."""
    to_send: list[Alert] = []
    nxt: dict[str, Any] = {}
    active = {a.id for a in alerts}
    for a in alerts:
        prev = state.get(a.id)
        if prev is None or prev["phase"] == "pending":
            recent = ((prev["recent"] if prev else []) + [1])[-CONFIRM_WINDOW:]
            raised = sum(recent) >= a.confirm
            nxt[a.id] = _entry(a, phase="active" if raised else "pending", recent=[] if raised else recent,
                               since=prev["since"] if prev else now, last_sent=None, max_sent=None,
                               clear_since=None)
            if raised:
                to_send.append(a)
            continue
        last, top = prev["last_sent"], prev["max_sent"]
        if last is None or top is None or LEVEL_RANK[a.level] > LEVEL_RANK[top] or now - last >= repeat_s:
            to_send.append(a)
        nxt[a.id] = {**prev, **_entry(a, phase="active", clear_since=None)}
    for aid, prev in state.items():
        if aid in active:
            continue
        if _held(aid, held):
            nxt[aid] = prev
        elif prev["phase"] == "pending":
            recent = (prev["recent"] + [0])[-CONFIRM_WINDOW:]
            if any(recent):
                nxt[aid] = {**prev, "recent": recent}
        else:
            since = prev["clear_since"] if prev["clear_since"] is not None else now
            if now - since < clear_s:
                nxt[aid] = {**prev, "phase": "clearing", "clear_since": since}
    return to_send, nxt


def mark_sent(state: dict[str, Any], sent: list[Alert], now: float) -> None:
    for a in sent:
        e = state.get(a.id)
        if e is not None:
            e["last_sent"] = now
            if e["max_sent"] is None or LEVEL_RANK[a.level] > LEVEL_RANK[e["max_sent"]]:
                e["max_sent"] = a.level


def shown(state: dict[str, Any]) -> dict[str, Any]:
    """The raised alerts' entries (for "since" on the card)."""
    return {k: v for k, v in state.items() if v["phase"] == "active"}


def raised(state: dict[str, Any], current: list[Alert], held: set[str]) -> list[Alert]:
    """The alerts the briefing counts: those raised in the state file (not pending, not clearing) that are still
    true. When the current evaluation reports one, its fresh text is used. When its source is unreadable now
    (held), the stored text is used. When its source is readable and the evaluation no longer reports it (it
    recovered, and the alert loop hasn't run since, e.g. the server was down), it is left out (Reviewer Z3-1)."""
    fresh = {a.id: a for a in current}
    out = []
    for aid, e in state.items():
        if e["phase"] != "active":
            continue
        if aid in fresh:
            out.append(fresh[aid])
        elif _held(aid, held):
            out.append(Alert(aid, e["level"], e["text"], e["minimal"], e["named"]))
    out.sort(key=lambda a: (-LEVEL_RANK[a.level], a.id))
    return out


# ---- loading state files: only finite numbers in a sane range (Reviewer Z2-1) --------------------------------
MAX_INT = 2**53
TS_SLACK = 10 * 365 * 86400  # timestamps must lie within 10 years of now


def _num(v: Any, now: float | None = None) -> bool:
    """A finite, non-negative number below 2^53; with `now`, also within 10 years of it."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    if not 0 <= v < MAX_INT:  # also refuses inf and NaN (every comparison with NaN is false)
        return False
    return now is None or abs(v - now) <= TS_SLACK


def _valid_state(raw: Any, now: float | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        if not (isinstance(k, str) and len(k) <= 200 and isinstance(v, dict)):
            continue
        recent = v.get("recent", [])
        texts = [v.get(f, k) for f in ("text", "minimal", "named")]
        if (v.get("phase") in PHASES and v.get("level") in LEVEL_RANK and _num(v.get("since"), now)
                and isinstance(recent, list) and len(recent) <= CONFIRM_WINDOW and all(x in (0, 1) and not isinstance(x, bool) for x in recent)
                and (v.get("last_sent") is None or _num(v.get("last_sent"), now))
                and (v.get("max_sent") is None or v.get("max_sent") in LEVEL_RANK)
                and (v.get("clear_since") is None or _num(v.get("clear_since"), now))
                and all(isinstance(t, str) and len(t) <= 500 for t in texts)):
            out[k] = {"phase": v["phase"], "recent": list(recent), "level": v["level"], "since": v["since"],
                      "last_sent": v.get("last_sent"), "max_sent": v.get("max_sent"),
                      "clear_since": v.get("clear_since"), "text": texts[0], "minimal": texts[1], "named": texts[2]}
    return out


class AlertStore:
    """alerts.json in state_dir. The server is its only writer; writes are atomic and under the file lock.
    `briefing --send` only reads it."""

    def __init__(self, path: str) -> None:
        self.path = path

    def load(self, now: float | None = None) -> dict[str, Any]:
        try:
            return _valid_state(read_json(self.path, {}), now)
        except (ValueError, OverflowError, OSError, TypeError, RecursionError) as exc:
            log.warning("alert state unreadable (%s); starting empty", type(exc).__name__)
            return {}

    def save(self, state: dict[str, Any]) -> None:
        with file_lock(self.path):
            write_json_atomic(self.path, state, 0o600)


def briefing_alerts(snap: dict[str, Any], cfg: Config, now: float) -> list[Alert]:
    """What the briefing (server and `briefing --send` alike) counts: the alerts raised in alerts.json, with the
    restart history loaded read-only (plus this snapshot's sample, in memory) so the evaluation matches the alert
    loop's. Neither file is written."""
    hist = update_restarts(RestartStore(os.path.join(cfg.state_dir, "restarts.json")).load(now), snap, cfg, now)
    current, held = evaluate(snap, cfg, now, hist)
    return raised(AlertStore(os.path.join(cfg.state_dir, "alerts.json")).load(now), current, held)


# ---- unit restart history (Z-3: an increase within a window, not the cumulative NRestarts) ---------------------
def update_restarts(hist: dict[str, list[list[float]]], snap: dict[str, Any], cfg: Config, now: float
                    ) -> dict[str, list[list[float]]]:
    """Append each watched unit's NRestarts sample; drop samples older than the window; a counter that went
    down (the unit was restarted by hand, or the box rebooted) starts its history over."""
    src = views.source(snap, "units")
    if src["state"] != "ok":
        return hist
    out: dict[str, list[list[float]]] = {}
    for unit in cfg.units:
        samples = [s for s in hist.get(unit, []) if now - s[0] <= cfg.unit_restart_window_s]
        n = (src["data"].get(unit) or {}).get("restarts")
        if isinstance(n, int):
            if samples and n < samples[-1][1]:
                samples = []
            samples.append([now, n])
        if samples:
            out[unit] = samples[-200:]
    return out


def _valid_hist(raw: Any, now: float | None = None) -> dict[str, list[list[float]]]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[list[float]]] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or len(k) > 200 or not isinstance(v, list):
            continue
        samples = [[float(x[0]), int(x[1])] for x in v[-200:]
                   if isinstance(x, list) and len(x) == 2 and _num(x[0], now) and isinstance(x[1], int)
                   and not isinstance(x[1], bool) and 0 <= x[1] < MAX_INT]
        if samples:
            out[k] = samples
    return out


class RestartStore:
    """restarts.json in state_dir. The server is its only writer; `briefing --send` only reads it."""

    def __init__(self, path: str) -> None:
        self.path = path

    def load(self, now: float | None = None) -> dict[str, list[list[float]]]:
        try:
            return _valid_hist(read_json(self.path, {}), now)
        except (ValueError, OverflowError, OSError, TypeError, RecursionError) as exc:
            log.warning("restart history unreadable (%s); starting empty", type(exc).__name__)
            return {}

    def save(self, hist: dict[str, list[list[float]]]) -> None:
        with file_lock(self.path):
            write_json_atomic(self.path, hist, 0o600)
