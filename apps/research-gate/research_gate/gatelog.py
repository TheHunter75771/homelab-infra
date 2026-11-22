"""``35-forge/<id>/gate-log.md``: the gate's append-only, plain-text record (CONTRACTS §12.16.3).

One line per action, written before and after each network call::

    2026-10-27T10:00:00+00:00 | search | `rocm install guide` | `start`
    2026-10-27T10:00:01+00:00 | search | `rocm install guide` | `ok: 8 results, 10423 bytes, http 200`
    2026-10-27T10:05:00+00:00 | fetch | `https://example.com/a` | `start`
    2026-10-27T10:05:02+00:00 | fetch | `https://example.com/a` | `ok: filed 10-sources/t/2026-10-27_a.txt, 5120 bytes, http 200, text/html`
    2026-10-27T10:06:00+00:00 | refuse | `https://example.com/b` | `fetch refused: ... resolves to a refused address 10.0.0.5 (private)`

Both the target (a query or a canonical URL, neither of which can hold a backtick) and the result
are code spans, so a Markdown viewer shows them as literal text: no link, autolinked bare URL,
image, entity or HTML can come out of either. The result is written by the gate (never a server's
own words: error texts name only the local error type, media types are a plain ``type/subtype``
token) and is reduced to printable ASCII without backticks, one line, at most 300 characters.
Outside the two spans a line holds only the timestamp, the verb and `` | `` separators.

The log is also the record of what already ran (see ``Ledger``):

* ``start`` lines count attempts;
* ``ok: ...`` and ``... refused: ...`` lines are final: the item never runs again;
* ``error: ...`` lines are retryable, up to ``MAX_ATTEMPTS`` starts in total; an
  ``error: not counted: ...`` line (the host's own interface tables were unreadable, so nothing
  was sent) gives its attempt back;
* ``... not approved at call time; skipped`` lines record a re-check that said no (not an attempt).
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from library import Library
from library import frontmatter as fm
from library.safefs import append_text

LOG_NAME = "gate-log.md"
MAX_ATTEMPTS = 3
MAX_RESULT = 300
VERBS = ("search", "fetch", "refuse")
LINE_RE = re.compile(r"^(?P<ts>\S+) \| (?P<verb>search|fetch|refuse) \| `(?P<target>[^`\n]*)` \| `(?P<result>[^`\n]*)`$")


def safe_text(text: str, limit: int = MAX_RESULT) -> str:
    """Text that can sit inside a code span: printable ASCII without backticks (anything else
    becomes '?'), one line, truncated."""
    text = " ".join(str(text).split())
    out = "".join(c if " " <= c <= "~" and c != "`" else "?" for c in text)
    if len(out) > limit:
        out = out[: limit - 3] + "..."
    return out


def span(target: str) -> str:
    """A target as a code span. Queries and canonical URLs never contain backticks or line
    breaks; anything else is reduced to the allowlist first (it is then only a label)."""
    if "`" in target or "\n" in target or "\r" in target:
        target = safe_text(target)
    return f"`{target}`"


@dataclass
class ItemState:
    attempts: int = 0
    final: str | None = None        # the result of the ok / refused line that ended it
    last_error: str | None = None


@dataclass
class Ledger:
    searches: dict[str, ItemState] = field(default_factory=lambda: defaultdict(ItemState))
    fetches: dict[str, ItemState] = field(default_factory=lambda: defaultdict(ItemState))

    def item(self, kind: str, target: str) -> ItemState:
        table = self.searches if kind == "search" else self.fetches
        return table[target]

    def pending(self, kind: str, target: str) -> bool:
        table = self.searches if kind == "search" else self.fetches
        st = table.get(target)
        return st is None or (st.final is None and st.attempts < MAX_ATTEMPTS)


def parse(text: str) -> Ledger:
    ledger = Ledger()
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        verb, target, result = m.group("verb"), m.group("target"), m.group("result")
        if verb == "refuse":
            kind = result.split(" ", 1)[0]
            if kind not in ("search", "fetch"):
                continue
            st = ledger.item(kind, target)
            if result.startswith(f"{kind} refused:"):
                st.final = st.final or result
            continue
        st = ledger.item(verb, target)
        if result == "start":
            st.attempts += 1
        elif result.startswith("ok"):
            st.final = st.final or result
        elif result.startswith("error"):
            st.last_error = result
            if result.startswith("error: not counted: ") and st.attempts:
                st.attempts -= 1
    return ledger


class GateLog:
    """Append-only writer for one request's ``gate-log.md`` (created with a library header)."""

    def __init__(self, lib: Library, base: str, rid: str) -> None:
        self.lib = lib
        self.rel = f"{base}/{LOG_NAME}"
        self.rid = rid

    def _path(self) -> Path:
        return self.lib.abs_path(self.rel)

    def read(self) -> str:
        path = self._path()
        try:
            return path.read_bytes().decode("utf-8", "replace")
        except FileNotFoundError:
            return ""

    def ledger(self) -> Ledger:
        return parse(self.read())

    def _ensure(self) -> None:
        if os.path.lexists(self._path()):
            return
        meta: fm.Meta = {
            "title": f"Gate log: {self.rid}", "source": "research gate",
            "added": self.lib.clock().date().isoformat(), "tags": ["forge", "research-gate"],
            "summary": "Every search and fetch the research gate made for this request. Plain text; "
                       "written only by the gate, never edited.",
            "related": [], "request": self.rid,
        }
        try:
            self.lib.write_new(self.rel, fm.render(meta, "\n"))
        except Exception:  # noqa: BLE001 - created meanwhile; the append below decides
            if not os.path.lexists(self._path()):
                raise

    def write(self, verb: str, target: str, result: str) -> str:
        if verb not in VERBS:
            raise ValueError(f"bad verb {verb!r}")
        self._ensure()
        stamp = self.lib.clock().isoformat(timespec="seconds")
        line = f"{stamp} | {verb} | {span(target)} | `{safe_text(result)}`\n"
        append_text(self._path(), line)
        return line
