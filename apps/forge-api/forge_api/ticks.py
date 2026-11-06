"""The thin adapter over Builder K3's ``Forge.tick_searches(rid, queries, *, by)`` and
``Forge.tick_sources(rid, urls, *, by)`` (CONTRACTS §12.25.2, row K3).

The forge returns a ``TickResult`` whose ``items`` are ``TickItem(requested, item, outcome,
reason)``, ``outcome`` being ``ticked``, ``already`` or ``not-found``. The forge itself writes the
approval audit line (``approve-chat by=<id>``) into the request's ``state.md`` under its lock, so
this service writes no audit line of its own; it passes the approver's id as ``by``.

``normalise`` checks the answer strictly: every item asked for answered exactly once, nothing
extra, only the three outcomes. Anything else is a ``TickProtocolError`` (an internal error, 500):
the API never claims a tick the forge did not report.
"""
from __future__ import annotations

from typing import Any, Protocol

TICKED = "ticked"
ALREADY = "already"
NOT_FOUND = "not-found"
RESULTS = (TICKED, ALREADY, NOT_FOUND)

Outcome = tuple[str, str, str]   # (item, outcome, reason)


class TickUnavailable(RuntimeError):
    """The installed forge has no tick_* methods (an old forge next to a new API)."""


class TickProtocolError(RuntimeError):
    """The forge answered in a shape this adapter does not understand."""


class Ticker(Protocol):
    def tick_searches(self, rid: str, queries: list[str], by: str) -> list[Outcome]: ...
    def tick_sources(self, rid: str, urls: list[str], by: str) -> list[Outcome]: ...


def normalise(items: list[str], raw: Any) -> list[Outcome]:
    """``raw`` (the forge's TickResult for ``items``) as [(item, outcome, reason)] in input order.
    ``items`` are already in the forge's matched form (checked query, canonical URL)."""
    entries = getattr(raw, "items", None)
    if not isinstance(entries, list):
        raise TickProtocolError(f"unrecognised result of type {type(raw).__name__}")
    by_item: dict[str, tuple[str, str]] = {}
    for entry in entries:
        key, outcome, reason = (getattr(entry, a, None) for a in ("item", "outcome", "reason"))
        if not isinstance(key, str) or key not in items:
            raise TickProtocolError("a result names an item that was not asked for")
        if key in by_item:
            raise TickProtocolError("an item has more than one result")
        if outcome not in RESULTS:
            raise TickProtocolError(f"unknown outcome {outcome!r}")
        by_item[key] = (outcome, reason if isinstance(reason, str) else "")
    missing = [i for i in items if i not in by_item]
    if missing:
        raise TickProtocolError(f"{len(missing)} item(s) have no result")
    return [(i, *by_item[i]) for i in items]


class ForgeTicks:
    """Calls the forge's own tick methods; the only code that knows their names."""

    def __init__(self, forge: Any) -> None:
        self.forge = forge

    def _call(self, name: str, rid: str, items: list[str], by: str) -> list[Outcome]:
        fn = getattr(self.forge, name, None)
        if not callable(fn):
            raise TickUnavailable(f"the installed forge has no {name} (CONTRACTS §12.25 K3)")
        return normalise(items, fn(rid, list(items), by=by))

    def tick_searches(self, rid: str, queries: list[str], by: str) -> list[Outcome]:
        return self._call("tick_searches", rid, queries, by)

    def tick_sources(self, rid: str, urls: list[str], by: str) -> list[Outcome]:
        return self._call("tick_sources", rid, urls, by)
