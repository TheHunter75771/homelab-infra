"""``35-forge/<id>/drafter-log.md``: the drafter's append-only, plain-text record.

Its own file, not ``gate-log.md``: that file's header says it is written only by the research
gate, the gate's ledger parses it, and two services appending to one file would make neither the
single writer. The line format mirrors the gate's (CONTRACTS §12.16.3)::

    2026-10-27T10:00:00+00:00 | draft | `initial 3f2a9c1b04de` | `start`
    2026-10-27T10:04:10+00:00 | draft | `initial 3f2a9c1b04de` | `invalid: missing sections: Examples`
    2026-10-27T10:06:00+00:00 | draft | `initial 3f2a9c1b04de` | `start`
    2026-10-27T10:09:30+00:00 | draft | `initial 3f2a9c1b04de` | `ok: filed skill apa7 sha256:..., 5120 bytes`

The target is the drafting *round*: ``initial`` or ``revise``, plus a short hash of the round's
inputs (the ingested sources, the previous draft's hash and Noah's notes). New inputs (another
ingested source, edited notes, a new revise) start a new round with fresh attempts, so a
"draft failed" round never blocks a later one.

Results (both fields are code spans, reduced to printable ASCII without backticks, one line, at
most 300 characters, like the gate's):

* ``start`` counts an attempt;
* ``ok: ...`` ends the round (filed); ``failed: ...`` ends it after ``max_attempts`` (the
  "draft failed" mark); ``skipped: ...`` ends it without an attempt (nothing to draft from);
* ``invalid: ...`` (the reply broke the template) and ``error: ...`` follow a ``start``;
* ``error: not counted: ...`` gives its attempt back (the broker was unreachable, so nothing was
  generated, or the inputs changed while drafting).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from library import Library
from library import frontmatter as fm
from library.safefs import append_text

LOG_NAME = "drafter-log.md"
MAX_RESULT = 300
LINE_RE = re.compile(r"^(?P<ts>\S+) \| draft \| `(?P<target>[^`\n]*)` \| `(?P<result>[^`\n]*)`$")
FINAL_PREFIXES = ("ok", "failed:", "skipped:")


def safe_text(text: object, limit: int = MAX_RESULT) -> str:
    text = " ".join(str(text).split())
    out = "".join(c if " " <= c <= "~" and c != "`" else "?" for c in text)
    if len(out) > limit:
        out = out[: limit - 3] + "..."
    return out


@dataclass
class Round:
    attempts: int = 0
    final: str | None = None
    last: str | None = None          # the last result line
    results: list[str] = field(default_factory=list)


def parse(text: str) -> dict[str, Round]:
    rounds: dict[str, Round] = {}
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        rnd = rounds.setdefault(m.group("target"), Round())
        result = m.group("result")
        rnd.last = result
        rnd.results.append(result)
        if result == "start":
            rnd.attempts += 1
        elif result.startswith(FINAL_PREFIXES):
            rnd.final = rnd.final or result
        elif result.startswith("error: not counted:") and rnd.attempts:
            rnd.attempts -= 1
    return rounds


class DrafterLog:
    def __init__(self, lib: Library, base: str, rid: str) -> None:
        self.lib = lib
        self.rel = f"{base}/{LOG_NAME}"
        self.rid = rid

    def _path(self) -> Path:
        return self.lib.abs_path(self.rel)

    def read(self) -> str:
        path = self._path()
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return ""
        with os.fdopen(fd, "rb") as fh:
            return fh.read().decode("utf-8", "replace")

    def rounds(self) -> dict[str, Round]:
        return parse(self.read())

    def _ensure(self) -> None:
        if os.path.lexists(self._path()):
            return
        meta: fm.Meta = {
            "title": f"Drafter log: {self.rid}", "source": "skill drafter",
            "added": self.lib.clock().date().isoformat(), "tags": ["forge", "skill-drafter"],
            "summary": "Every drafting attempt the skill drafter made for this request. Plain text; "
                       "written only by the drafter, never edited.",
            "related": [], "request": self.rid,
        }
        try:
            self.lib.write_new(self.rel, fm.render(meta, "\n"))
        except Exception:  # noqa: BLE001 - created meanwhile; the append below decides
            if not os.path.lexists(self._path()):
                raise

    def write(self, target: str, result: str) -> str:
        self._ensure()
        stamp = self.lib.clock().isoformat(timespec="seconds")
        line = f"{stamp} | draft | `{safe_text(target, 80)}` | `{safe_text(result)}`\n"
        append_text(self._path(), line)
        return line
