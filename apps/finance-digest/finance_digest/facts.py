"""Facts: read Money Hub, compute every figure in integer cents, and lay them out as blocks.

``compute`` returns plain data (cents, dates, cleaned names) plus a per-section error map; one failed
read only blanks its own section. ``blocks`` turns the facts into paragraphs and tables. Every
number in an entry is formatted here, never by the model (the AI summary may
contain no numbers at all, see ``guard``). ``headline`` states the key statuses in exact figures.
"""
from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass, field

from .moneyhub import CHECKS, MoneyHubError
from .text import WARN, clean, cents, month_of, money, next_month, parse_dt, plain_int, total

NEAR_CAP_PCT = 85
_ORDINAL_RE = re.compile(r"([0-9]{1,2})(?:st|nd|rd|th)")
_QUARTERLY_RE = re.compile(r"qtrly ([0-9]{1,2})/([0-9]{1,2})")


# ---------------------------------------------------------------- dates


def next_check(day: dt.date) -> tuple[str, str, dt.date]:
    """(check, period, payday) of the next paycheck on or after ``day`` (1st and 15th)."""
    if day.day == 1:
        return "1st", month_of(day), day
    if day.day <= 15:
        return "15th", month_of(day), day.replace(day=15)
    nxt = next_month(day)
    return "1st", month_of(nxt), nxt


def due_dates(rule: object, period: str) -> list[dt.date] | None:
    """The due dates of a bill rule within the month ``period``; None = no fixed day (e.g. biweekly).

    "5th" -> the 5th (clamped to the month's last day); "qtrly 3/15" -> the 15th of months 3, 6, 9
    and 12 (every third month from the one named).
    """
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


# ---------------------------------------------------------------- compute


def _rows(obj, key="rows"):
    rows = obj if isinstance(obj, list) else obj.get(key) if isinstance(obj, dict) else None
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise MoneyHubError("Money Hub returned an unexpected response.")
    return list(rows)


def _check_name(value: object) -> str:
    return value if value in CHECKS else "none"


def _sync(state, day: dt.date) -> dict:
    if not isinstance(state, dict):
        raise MoneyHubError("Money Hub returned an unexpected response for /api/state.")
    last = parse_dt(state.get("sync_last_at"))
    attempt = parse_dt(state.get("sync_last_attempt_at"))
    return {
        "failing": bool(state.get("sync_failing")),
        "last_good": last.strftime("%Y-%m-%d %H:%M") if last else None,
        "last_good_day": last.date() if last else None,
        "days_stale": max(0, (day - last.date()).days) if last else None,  # 0 for a backfilled day
        "last_attempt": attempt.strftime("%Y-%m-%d %H:%M") if attempt else None,
        "stale_accounts": plain_int(state.get("stale_accounts")),
        "error": clean(state.get("sync_error"), 100),
    }


def _is_stale(a: dict) -> bool:
    return bool(a.get("warn")) or clean(a.get("state")).lower().startswith("stale")


def _iso_day(value: object) -> str:
    """A Money Hub date as YYYY-MM-DD, or "unknown" (never raw API text, which could carry digits)."""
    parsed = parse_dt(value)
    return parsed.date().isoformat() if parsed else "unknown"


def _accounts(data, day: dt.date) -> list[dict]:
    out = []
    for a in _rows(data):
        state = clean(a.get("state"), 30)
        if _is_stale(a):
            fresh = f"STALE ({state})" if state else "STALE"
        elif state:
            fresh = state
        elif a.get("simplefin_mapped") is False:
            as_of = parse_dt(a.get("as_of"))
            fresh = f"manual, {(day - as_of.date()).days} days old" if as_of else "manual, age unknown"
        else:
            fresh = "current"
        out.append(
            {
                "name": clean(a.get("name")) or "unnamed account",
                "kind": clean(a.get("kind"), 20) or "other",
                "balance_cents": cents(a.get("balance_cents")),
                "as_of": _iso_day(a.get("as_of")),
                "stale": _is_stale(a),
                "fresh": fresh,
            }
        )
    return out


def _budget(data, month: str) -> dict:
    if not isinstance(data, dict):
        raise MoneyHubError("Money Hub returned an unexpected response for /api/budget.")
    cats = []
    for c in _rows(data, "categories"):
        spent, cap = cents(c.get("spent_cents")), cents(c.get("cap_cents"))
        row = {"name": clean(c.get("name")) or "unnamed", "spent_cents": spent, "cap_cents": cap,
               "remaining_cents": None, "pct": None, "flag": "no cap", "rank": 3}
        if spent is None:
            row.update(flag="unreadable", rank=4)
        elif cap is not None:
            row["remaining_cents"] = cap - spent
            if cap > 0:
                row["pct"] = (spent * 100) // cap
            if spent > cap:
                row.update(flag="OVER", rank=0)
            elif cap > 0 and spent * 100 >= NEAR_CAP_PCT * cap:
                row.update(flag="NEAR CAP", rank=1)
            else:
                row.update(flag="ok", rank=2)
        cats.append(row)
    cats.sort(key=lambda r: (r["rank"], -(r["pct"] or 0), r["name"].lower()))
    return {
        "month": month,
        "categories": cats,
        "over": sum(1 for r in cats if r["rank"] == 0),
        "near": sum(1 for r in cats if r["rank"] == 1),
    }


def previous_month(day: dt.date) -> str:
    return month_of(day.replace(day=1) - dt.timedelta(days=1))


def _bills(hub, day: dt.date, window_days: int) -> dict:
    """Bills due in the window (this month and, if it reaches, the next), and overdue bills: unpaid
    with a due date before today in the previous calendar month or this one (Lead, round 2: the 1st
    must not hide last month). Paid is per period, as Money Hub reports it."""
    end = day + dt.timedelta(days=window_days - 1)
    prev, this = previous_month(day), month_of(day)
    periods = [this] if month_of(end) == this else [this, month_of(end)]
    due, overdue, undated = [], [], 0
    for period in [prev] + periods:
        for b in _rows(hub.get("/api/bills", {"period": period})):
            if b.get("paid"):
                continue
            dates = due_dates(b.get("due_rule"), period)
            if dates is None:
                undated += period == this  # counted once, from this month's list
                continue
            for d in dates:
                row = {"name": clean(b.get("name")) or "unnamed bill", "due": d,
                       "amount_cents": cents(b.get("amount_cents")), "check": _check_name(b.get("check"))}
                if day <= d <= end:
                    due.append(row)
                elif d < day and period in (prev, this):
                    overdue.append(row)  # unpaid and past due, this month or last (Money Hub's paid flag)
    due.sort(key=lambda r: (r["due"], r["name"].lower()))
    overdue.sort(key=lambda r: (r["due"], r["name"].lower()))
    amount, skipped = total(r["amount_cents"] for r in due)
    o_amount, o_skipped = total(r["amount_cents"] for r in overdue)
    return {"start": day, "end": end, "due": due, "total_cents": amount, "skipped": skipped,
            "undated_unpaid": undated, "period": this, "previous": prev, "overdue": overdue,
            "overdue_cents": o_amount, "overdue_skipped": o_skipped}


def _debt(data) -> dict:
    rows = _rows(data)
    open_rows = [d for d in rows if (clean(d.get("status"), 15).lower() or "open") == "open"]
    amount, skipped = total(d.get("balance_cents") for d in open_rows)
    reported = cents(data.get("total_cents")) if isinstance(data, dict) else None
    return {"open_count": len(open_rows), "total_cents": amount, "skipped": skipped,
            "reported_cents": reported, "mismatch": reported is not None and reported != amount}


def _payday(hub, day: dt.date) -> dict:
    check, period, payday = next_check(day)
    data = hub.get("/api/payday", {"period": period, "check": check})
    if not isinstance(data, dict):
        raise MoneyHubError("Money Hub returned an unexpected response for /api/payday.")
    unpaid = [b for b in _rows(data, "bills") if not b.get("paid")]
    amount, skipped = total(b.get("amount_cents") for b in unpaid)
    reported = cents(data.get("unpaid_cents"))
    return {"check": check, "period": period, "date": payday, "unpaid_count": len(unpaid),
            "unpaid_cents": amount, "skipped": skipped, "reported_cents": reported,
            "mismatch": reported is not None and reported != amount}


@dataclass
class Facts:
    day: dt.date
    sync: dict | None = None
    accounts: list | None = None
    budget: dict | None = None
    bills: dict | None = None
    debt: dict | None = None
    payday: dict | None = None
    errors: dict = field(default_factory=dict)  # section -> safe message


SECTIONS = ("sync", "accounts", "budget", "bills", "debt", "payday")


def compute(hub, day: dt.date, window_days: int = 7) -> Facts:
    f = Facts(day=day)
    steps = {
        "sync": lambda: _sync(hub.get("/api/state"), day),
        "accounts": lambda: _accounts(hub.get("/api/accounts"), day),
        "budget": lambda: _budget(hub.get("/api/budget", {"month": month_of(day)}), month_of(day)),
        "bills": lambda: _bills(hub, day, window_days),
        "debt": lambda: _debt(hub.get("/api/debts")),
        "payday": lambda: _payday(hub, day),
    }
    for name in SECTIONS:
        try:
            setattr(f, name, steps[name]())
        except MoneyHubError as e:
            f.errors[name] = str(e)
    return f


# ---------------------------------------------------------------- blocks


def _skip(skipped: int) -> str:
    return f" ({skipped} amount(s) unreadable, not included)" if skipped else ""


def _names(rows: list, limit: int = 8, amount: bool = False) -> str:
    shown = []
    for r in rows[:limit]:
        if amount:
            shown.append(f"{r['name']} {money(r['amount_cents'])} (due {r['due'].isoformat()})")
        else:
            shown.append(r["name"])
    more = f", and {len(rows) - limit} more" if len(rows) > limit else ""
    return "; ".join(shown) + more


def headline(f: Facts) -> str:
    """The code-written headline: exact statuses, so the AI text never has to carry a figure."""
    parts = []
    s = f.sync
    if s is None:
        parts.append("Sync status unavailable.")
    elif s["failing"]:
        days = f", {s['days_stale']} days stale" if s["days_stale"] is not None else ""
        parts.append(f"Bank sync FAILING (last good sync {s['last_good'] or 'unknown'}{days}).")
    else:
        parts.append(f"Bank sync ok (last good sync {s['last_good'] or 'unknown'}).")
    b = f.budget
    if b is None:
        parts.append("Budget unavailable.")
    else:
        over = [c for c in b["categories"] if c["flag"] == "OVER"]
        near = [c for c in b["categories"] if c["flag"] == "NEAR CAP"]
        parts.append(f"Over budget: {len(over)}" + (f" ({_names(over)})." if over else "."))
        parts.append(f"Near the cap: {len(near)}" + (f" ({_names(near)})." if near else "."))
    bl = f.bills
    if bl is None:
        parts.append("Bills unavailable.")
    else:
        parts.append(f"Unpaid bills due in the next {(bl['end'] - bl['start']).days + 1} days: {len(bl['due'])}, "
                     f"{money(bl['total_cents'])}{_skip(bl['skipped'])}"
                     + (f" ({_names(bl['due'], amount=True)})." if bl["due"] else "."))
        parts.append(f"Overdue: {len(bl['overdue'])}, {money(bl['overdue_cents'])}{_skip(bl['overdue_skipped'])}"
                     + (f" ({_names(bl['overdue'], amount=True)})." if bl["overdue"] else "."))
    return "Headline: " + " ".join(parts)


def blocks(f: Facts) -> list[tuple]:
    """A block is ("para", text) or ("table", caption, header, rows). Every figure is formatted here."""
    out: list[tuple] = []

    def unavailable(name: str, key: str) -> tuple:
        return ("para", f"{WARN} {name}: unavailable ({clean(f.errors.get(key), 120)})")

    s = f.sync
    if s is None:
        out.append(("para", f"{WARN} Money Hub sync status is unavailable "
                            f"({clean(f.errors.get('sync'), 120)}); every figure below may be out of date."))
    else:
        days = f", {s['days_stale']} days stale" if s["days_stale"] is not None else ""
        last = s["last_good"] or "unknown"
        if s["failing"]:
            out.append(("para", f"{WARN} BANK SYNC IS FAILING. Last good sync: {last}{days}. "
                                f"Error: {s['error'] or 'no error text'}. Balances may be out of date."))
        stale = f"{s['stale_accounts']} stale account(s)" if s["stale_accounts"] is not None else "stale count unknown"
        out.append(("para", f"Data freshness: last good sync {last}{days}; last attempt {s['last_attempt'] or 'unknown'}; "
                            f"sync {'FAILING' if s['failing'] else 'ok'}; {stale}; checked {f.day.isoformat()}."))
    out.append(("para", headline(f)))

    if f.accounts is None:
        out.append(unavailable("Accounts", "accounts"))
    else:
        rows = [[a["name"], a["kind"], money(a["balance_cents"]), a["as_of"], a["fresh"]] for a in f.accounts]
        out.append(("table", f"Balances ({len(f.accounts)} accounts):",
                    ["Account", "Kind", "Balance", "As of", "Freshness"], rows or [["none", "", "", "", ""]]))

    b = f.budget
    if b is None:
        out.append(unavailable("Budget", "budget"))
    else:
        rows = []
        for c in b["categories"]:
            cap = money(c["cap_cents"]) if c["cap_cents"] is not None else "no cap"
            rem = money(c["remaining_cents"]) if c["remaining_cents"] is not None else ""
            pct = f"{c['pct']}%" if c["pct"] is not None else ""
            rows.append([c["name"], money(c["spent_cents"]), cap, rem, pct, c["flag"]])
        caption = f"Budget {b['month']}: {b['over']} over, {b['near']} near the cap (at or above {NEAR_CAP_PCT}%):"
        out.append(("table", caption, ["Category", "Spent", "Cap", "Remaining", "Used", "Flag"],
                    rows or [["none", "", "", "", "", ""]]))

    bl = f.bills
    if bl is None:
        out.append(unavailable("Bills", "bills"))
    else:
        rows = [[r["name"], r["due"].isoformat(), money(r["amount_cents"]), r["check"]] for r in bl["due"]]
        rows.append(["Total", "", money(bl["total_cents"]), ""])
        caption = (f"Unpaid bills due {bl['start'].isoformat()} to {bl['end'].isoformat()} "
                   f"({len(bl['due'])}){_skip(bl['skipped'])}:")
        out.append(("table", caption, ["Bill", "Due", "Amount", "Check"], rows))
        rows = [[r["name"], r["due"].isoformat(), money(r["amount_cents"]), r["check"]] for r in bl["overdue"]]
        rows.append(["Total", "", money(bl["overdue_cents"]), ""])
        out.append(("table", f"Overdue (unpaid, past due in {bl['previous']} or {bl['period']}) "
                             f"({len(bl['overdue'])}){_skip(bl['overdue_skipped'])}:",
                    ["Bill", "Due", "Amount", "Check"], rows))
        if bl["undated_unpaid"]:
            out.append(("para", f"{bl['undated_unpaid']} unpaid bill(s) have no fixed due day and are not listed."))

    d = f.debt
    if d is None:
        out.append(unavailable("Debt", "debt"))
    else:
        out.append(("table", "Debt:", ["Open debts", "Total open debt"],
                    [[str(d["open_count"]), money(d["total_cents"]) + _skip(d["skipped"])]]))
        if d["mismatch"]:
            out.append(("para", f"{WARN} Money Hub's own debt total is {money(d['reported_cents'])}, "
                                "which differs from the computed sum of open debts."))

    p = f.payday
    if p is None:
        out.append(unavailable("Payday", "payday"))
    else:
        out.append(("table", "Next payday:", ["Check", "Date", "Unpaid bills", "Unpaid total"],
                    [[p["check"], p["date"].isoformat(), str(p["unpaid_count"]),
                      money(p["unpaid_cents"]) + _skip(p["skipped"])]]))
        if p["mismatch"]:
            out.append(("para", f"{WARN} Money Hub's own unpaid total for this check is "
                                f"{money(p['reported_cents'])}, which differs from the computed sum."))
    return out
