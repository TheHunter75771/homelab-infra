"""The skill drafter's passes (CONTRACTS §12.19.1, module K2).

Every ``poll_interval`` it scans ``35-forge/*/`` and drafts a SKILL.md where one is due:

* **initial**: the request is ``awaiting-source-approval`` with at least one ingested source, no
  ``skill/`` or ``review.md`` yet, and no ticked URL still waiting for the research gate (a URL is
  settled once the gate has ingested it, refused it, or used up its attempts). Drafting earlier
  would move the request out of the states where the gate may fetch, stranding Noah's other ticks.
* **revise**: the request is ``awaiting-skill-redraft`` (Noah set ``decision: revise`` and ran
  ``forge decide`` on his channel) and ``review.md`` carries notes. Without notes it waits.

A draft is the model's reply checked by ``validate`` and re-rendered by code, filed only through
``Forge.draft_skill`` (which keeps the previous draft in ``previous/<n>/`` and writes a fresh
``review.md``). The drafter never calls ``decide`` or ``revive``, never ticks anything and never
writes ``review.md``, ``searches.md``, ``sources.md`` or ``state.md`` itself; the only file it
writes is its own ``drafter-log.md``.

Idempotence: a filed draft moves the request to ``awaiting-skill-review``, which is never due; the
drafter log records each round's outcome, so a finished or failed round is not drafted again.
Each request fails on its own; a broken request never stops the pass.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from forge import Forge, ForgeError, is_request_id
from forge.core import FORGE, LOCK, REVIEW, REVIEW_INTRO, S_REDRAFT, S_REVIEW, S_SOURCE, SKILL_DIR, STATE
from library import Library, LibraryError, naming
from library import frontmatter as fm
from library.cli import CAP_ENV
from library.core import DEFAULT_CAP, SKILLS, SOURCES, Clock
from library.safefs import casefold_names

from . import prompt as promptmod
from .broker import Broker, BrokerError, BrokerUnreachable
from .drafterlog import LINE_RE, DrafterLog, safe_text
from .validate import Invalid, validate

log = logging.getLogger("skill_drafter")

GATE_LOG = "gate-log.md"
GATE_MAX_ATTEMPTS = 3          # research_gate.gatelog.MAX_ATTEMPTS (the gate is not installed here)
GATE_LINE_RE = re.compile(r"^\S+ \| (?P<verb>fetch|refuse) \| `(?P<target>[^`\n]*)` \| `(?P<result>[^`\n]*)`$")
READ_MAX = 8 * 1024 * 1024     # the largest file the drafter reads (the gate stores at most 5 MiB)
TEXT_EXTS = (".txt", ".md")
PROMPT_MARGIN = 256           # a prompt this close to num_ctx was (or may have been) cut by Ollama
SOURCES_HASH_RE = re.compile(r"; sources ([0-9a-f]{12})")
LOCK_TRIES = 20
LOCK_SLEEP = 0.25

T = TypeVar("T")


# ---------------------------------------------------------------------- small readers

def read_text(lib: Library, rel: str, limit: int = READ_MAX) -> str | None:
    """A regular file inside the library as text (no symlinks anywhere), or None if missing."""
    path = lib.abs_path(rel)  # refuses '..' and symlinked components
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ForgeError(f"{rel} is not a regular file")
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise ForgeError(f"{rel} is larger than {limit} bytes")
    return data.decode("utf-8", "replace")


def ingested_entries(lib: Library, base: str) -> list[tuple[str, str]]:
    """(canonical URL, library path) for each ``ingested:`` entry in ``state.md``, in order."""
    text = read_text(lib, f"{base}/{STATE}")
    if text is None:
        raise ForgeError(f"{base}/{STATE} is missing")
    meta, _ = fm.parse(text)
    out = []
    entries = meta.get("ingested")
    for entry in entries if isinstance(entries, list) else []:
        url, sep, item = entry.partition(" -> ")
        if sep and url and item:
            out.append((url, item))
    return out


def gate_settled(lib: Library, base: str) -> set[str]:
    """URLs the research gate is finished with: fetched, refused, or out of attempts. Read-only
    mirror of the gate's ledger rules (CONTRACTS §12.16.3)."""
    text = read_text(lib, f"{base}/{GATE_LOG}") or ""
    attempts: dict[str, int] = {}
    final: set[str] = set()
    for line in text.splitlines():
        m = GATE_LINE_RE.match(line)
        if not m:
            continue
        target, result = m.group("target"), m.group("result")
        if m.group("verb") == "refuse":
            if result.startswith("fetch refused:"):
                final.add(target)
        elif result == "start":
            attempts[target] = attempts.get(target, 0) + 1
        elif result.startswith("ok"):
            final.add(target)
        elif result.startswith("error: not counted: ") and attempts.get(target):
            attempts[target] -= 1
    return final | {t for t, n in attempts.items() if n >= GATE_MAX_ATTEMPTS}


_NOTES_HEADING_RE = re.compile(r"^#{1,6}[ \t]+notes?[ \t]*:?[ \t]*#*[ \t]*$", re.IGNORECASE)
_HEADING_RE = re.compile(r"^#{1,6}[ \t]")


def revise_notes(review_meta: fm.Meta, review_body: str, skill: str, rid: str) -> str | None:
    """Noah's revise notes from ``review.md`` (read-only; the forge has no API for them yet):

    1. a ``notes:`` header value (text, or a list of lines);
    2. else the text under a ``# Notes`` / ``## Notes`` heading in the body, up to the next heading;
    3. else whatever Noah added to the body around the forge's own review instructions.
    """
    value = review_meta.get("notes")
    if isinstance(value, list):
        value = "\n".join(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    lines = review_body.replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        if _NOTES_HEADING_RE.match(line):
            out = []
            for later in lines[i + 1:]:
                if _HEADING_RE.match(later):
                    break
                out.append(later)
            text = "\n".join(out).strip()
            return text or None
    body = review_body.replace("\r\n", "\n")
    intro = REVIEW_INTRO.format(skill=skill, rid=rid)
    rest = body.replace(intro, "\n", 1) if intro in body else body
    rest = rest.strip()
    return rest if rest and intro in body else None


# ---------------------------------------------------------------------- plans

@dataclass
class Plan:
    rid: str
    base: str
    status: str
    phase: str = "idle"          # idle | waiting | due | done | failed
    reason: str = ""
    kind: str = ""               # initial | revise
    target: str = ""             # the round, as written in drafter-log.md
    attempts: int = 0
    sources: list[tuple[str, str]] = field(default_factory=list)
    notes: str | None = None
    previous_draft: str | None = None
    previous_hash: str = ""
    skill_name: str | None = None
    title: str = ""
    note: tuple[str, str] | None = None   # (round, text): a line to log once, e.g. sources changed


@dataclass
class PassReport:
    drafted: list[str] = field(default_factory=list)
    attempts: int = 0
    invalid: int = 0
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"attempts {self.attempts}, drafted {len(self.drafted)}, invalid {self.invalid}, "
                f"failed {len(self.failed)}, errors {len(self.errors)}")


class Drafter:
    def __init__(self, cfg: dict[str, Any], *, broker: Broker | None = None, clock: Clock | None = None,
                 cap_bytes: int | None = None) -> None:
        self.cfg = cfg
        self.clock = clock
        if cap_bytes is None:
            cap_text = os.environ.get(CAP_ENV)
            cap_bytes = naming.parse_size(cap_text) if cap_text else DEFAULT_CAP
        self.cap_bytes = cap_bytes
        self.broker = broker or Broker(cfg["broker_url"], cfg["model"], timeout=cfg["timeout"],
                                       num_ctx=cfg["num_ctx"])

    # ------------------------------------------------------------------ plumbing

    def open(self) -> tuple[Library, Forge]:
        kwargs: dict[str, Any] = {"cap_bytes": self.cap_bytes}
        if self.clock is not None:
            kwargs["clock"] = self.clock
        lib = Library.open(self.cfg["library_root"], **kwargs)
        return lib, Forge(lib)

    @staticmethod
    def _forge_call(lib: Library, fn: Callable[[], T]) -> T:
        """A mutating forge call, waiting briefly while another forge command holds the lock."""
        lock = lib.root / FORGE / LOCK
        for attempt in range(LOCK_TRIES):
            try:
                return fn()
            except ForgeError:
                if attempt + 1 < LOCK_TRIES and os.path.lexists(lock):
                    time.sleep(LOCK_SLEEP)
                    continue
                raise
        raise AssertionError("unreachable")

    @staticmethod
    def _request_dirs(lib: Library) -> list[str]:
        return [n for n in sorted(os.listdir(lib.abs_path(FORGE))) if not n.startswith(".")]

    # ------------------------------------------------------------------ what is due

    def assess(self, lib: Library, forge: Forge, name: str) -> Plan:
        if not is_request_id(name):
            raise ForgeError(f"{FORGE}/{name} is not named like a request id")
        base = f"{FORGE}/{name}"
        info = forge.info(base)
        if info.id != name:
            raise ForgeError(f"{base}/{STATE} names request {info.id!r}, not {name!r}")
        plan = Plan(name, base, info.status, title=info.title)
        if info.status == S_SOURCE:
            self._assess_initial(lib, info, plan)
        elif info.status == S_REDRAFT:
            self._assess_revise(lib, info, plan)
        elif info.status == S_REVIEW:
            plan.reason = ("decision: revise is set; waiting for 'forge decide' on Noah's channel"
                           if info.decision.strip().lower() == "revise" else "draft filed; awaiting Noah's review")
            self._sources_changed(lib, plan)
        else:
            plan.reason = "nothing to draft in this state"
        if plan.kind:
            self._round(lib, plan)
        return plan

    def _assess_initial(self, lib: Library, info, plan: Plan) -> None:
        if os.path.lexists(lib.abs_path(f"{plan.base}/{SKILL_DIR}")) or \
                os.path.lexists(lib.abs_path(f"{plan.base}/{REVIEW}")):
            plan.phase, plan.reason = "waiting", "a draft or review.md already exists; not redrafting"
            return
        entries = ingested_entries(lib, plan.base)
        if not entries:
            plan.reason = "no ingested source yet"
            return
        if self._gate_pending(lib, info, plan, entries):
            return
        plan.kind, plan.sources = "initial", entries

    @staticmethod
    def _gate_pending(lib: Library, info, plan: Plan, entries: list[tuple[str, str]]) -> bool:
        """Wait (True) while a ticked URL is still neither ingested nor settled by the gate."""
        done = {url for url, _ in entries} | gate_settled(lib, plan.base)
        pending = [u for ticked, u in info.listed_urls if ticked and u not in done]
        if pending:
            plan.phase = "waiting"
            plan.reason = f"waiting for the research gate: {len(pending)} ticked source(s) not fetched yet"
        return bool(pending)

    @staticmethod
    def sources_hash(entries: list[tuple[str, str]]) -> str:
        key = json.dumps(sorted(f"{u} -> {i}" for u, i in entries))
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]

    def _sources_changed(self, lib: Library, plan: Plan) -> None:
        """Known limit (Lead, 2026-10-27): a source ingested after the draft was filed is not in it.
        Note it once in the log, so Noah knows to revise."""
        text = DrafterLog(lib, plan.base, plan.rid).read()
        filed = [(m.group("target"), SOURCES_HASH_RE.search(m.group("result")))
                 for m in (LINE_RE.match(line) for line in text.splitlines())
                 if m and m.group("result").startswith("ok: filed")]
        if not filed or filed[-1][1] is None:
            return
        target, used = filed[-1][0], filed[-1][1].group(1)
        entries = ingested_entries(lib, plan.base)
        now = self.sources_hash(entries)
        if now == used:
            return
        line = (f"note: sources changed after this draft ({len(entries)} ingested now, sources {now}); "
                "revise to include them")
        if f"sources {now});" not in text:
            plan.note = (target, line)
            plan.reason += "; sources changed after this draft (revise to include them)"

    def _assess_revise(self, lib: Library, info, plan: Plan) -> None:
        text = read_text(lib, f"{plan.base}/{REVIEW}", 256 * 1024)
        if text is None:
            plan.phase, plan.reason = "waiting", "review.md is missing"
            return
        meta, body = fm.parse(text)
        if str(meta.get("decision", "")).strip().lower() != "revise":
            plan.phase, plan.reason = "waiting", "review.md no longer says decision: revise"
            return
        skill = str(meta.get("skill", "")).strip()
        notes = revise_notes(meta, body, skill, plan.rid)
        if not notes:
            plan.phase = "waiting"
            plan.reason = "decision: revise has no notes; add 'notes:' (or a '## Notes' section) to review.md"
            return
        entries = ingested_entries(lib, plan.base)
        if self._gate_pending(lib, info, plan, entries):
            return
        plan.kind, plan.notes, plan.sources = "revise", notes, entries
        plan.skill_name = skill if naming.is_kebab_name(skill) else None
        plan.previous_draft = read_text(lib, f"{plan.base}/{SKILL_DIR}/SKILL.md", 64 * 1024)
        plan.previous_hash = str(meta.get("hash", ""))

    def _round(self, lib: Library, plan: Plan) -> None:
        key = json.dumps([plan.kind, sorted(f"{u} -> {i}" for u, i in plan.sources),
                          plan.previous_hash, plan.notes or ""])
        plan.target = f"{plan.kind} {hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]}"
        rnd = DrafterLog(lib, plan.base, plan.rid).rounds().get(plan.target)
        plan.attempts = rnd.attempts if rnd else 0
        if rnd and rnd.final:
            plan.phase = "failed" if rnd.final.startswith("failed:") else "done"
            plan.reason = rnd.final
        elif plan.attempts >= self.cfg["max_attempts"]:
            plan.phase, plan.reason = "failed", f"{plan.attempts} attempts used"
        else:
            plan.phase = "due"
            plan.reason = f"{plan.kind} draft due (attempt {plan.attempts + 1} of {self.cfg['max_attempts']})"

    # ------------------------------------------------------------------ one pass

    def run_once(self) -> PassReport:
        lib, forge = self.open()
        report = PassReport()
        for name in self._request_dirs(lib):
            try:
                self._request(lib, forge, name, report)
            except Exception as exc:  # noqa: BLE001 - one broken request never stops the pass
                log.exception("request %s failed", name)
                report.errors.append(f"{name}: {exc}")
        log.info("pass done: %s", report.summary())
        return report

    def _request(self, lib: Library, forge: Forge, name: str, report: PassReport) -> None:
        plan = self.assess(lib, forge, name)
        dlog = DrafterLog(lib, plan.base, plan.rid)
        if plan.note:
            dlog.write(*plan.note)
            return
        if plan.phase == "failed" and not plan.reason.startswith("failed:"):
            self._mark_failed(dlog, plan, report, "")  # e.g. a crash after the last attempt
            return
        if plan.phase != "due":
            return
        sources = self._load_sources(lib, plan)
        if not any(s.text and s.text.strip() for s in sources):
            dlog.write(plan.target, "skipped: no ingested source has text to draft from "
                                    "(PDFs are not extracted yet); new sources start a new round")
            return
        self._attempt(lib, forge, plan, sources, dlog, report)

    def _load_sources(self, lib: Library, plan: Plan) -> list[promptmod.SourceText]:
        out = []
        for url, item in plan.sources:
            parts = item.split("/")
            text = None
            if len(parts) == 3 and parts[0] == SOURCES and item.lower().endswith(TEXT_EXTS):
                text = read_text(lib, item)
                if text is not None:
                    text = text[: self.cfg["max_source_chars"] * 2]  # the rest can never fit
            out.append(promptmod.SourceText(url, item, text))
        return out

    def _attempt(self, lib: Library, forge: Forge, plan: Plan, sources: list[promptmod.SourceText],
                 dlog: DrafterLog, report: PassReport) -> None:
        built = promptmod.build(title=plan.title, request_text=self._request_text(lib, plan),
                                sources=sources, max_source_chars=self.cfg["max_source_chars"],
                                skill_name=plan.skill_name, notes=plan.notes,
                                previous_draft=plan.previous_draft)
        if not self.broker.reachable():
            # Nothing logged against the request: an outage must not fill its log or use attempts.
            report.errors.append(f"{plan.rid}: broker unreachable; will retry next poll")
            log.warning("draft for %s: broker unreachable; will retry next poll", plan.rid)
            return
        dlog.write(plan.target, "start")
        report.attempts += 1
        try:
            answer = self.broker.chat(built.system, built.user)
        except BrokerUnreachable as exc:
            dlog.write(plan.target, f"error: not counted: {exc}")
            report.errors.append(f"{plan.rid}: {exc}")
            log.warning("draft for %s: %s", plan.rid, exc)
            return
        except BrokerError as exc:
            self._failed_attempt(dlog, plan, report, f"error: {exc}")
            return
        limit = self.cfg["num_ctx"] - PROMPT_MARGIN
        if answer.prompt_eval_count is not None and answer.prompt_eval_count >= limit:
            report.invalid += 1
            self._failed_attempt(dlog, plan, report, f"invalid: prompt truncated: the model read "
                                 f"{answer.prompt_eval_count} prompt tokens (num_ctx {self.cfg['num_ctx']}, "
                                 f"limit {limit}); lower max_source_chars or raise num_ctx")
            return
        today = lib.clock().date().isoformat()
        urls = [u for u, _ in plan.sources]
        try:
            draft = validate(answer.content, rid=plan.rid, today=today, urls=urls,
                             expected_name=plan.skill_name)
            renamed = ""
            if plan.kind != "revise":
                free = self._free_name(lib, draft.name)
                if free != draft.name:
                    renamed = f" ({draft.name} is taken in {SKILLS}/, so named {free})"
                    draft = validate(answer.content, rid=plan.rid, today=today, urls=urls, expected_name=free)
        except Invalid as exc:
            report.invalid += 1
            self._failed_attempt(dlog, plan, report, f"invalid: {exc}")
            return
        # The model can take minutes: file only if the request still needs this very round.
        again = self.assess(lib, forge, plan.rid)
        if again.target != plan.target:
            dlog.write(plan.target, "error: not counted: the request changed while drafting "
                                    f"(now {again.status}); not filed")
            return
        try:
            with tempfile.TemporaryDirectory(prefix="sdraft-") as tmp:
                Path(tmp, "SKILL.md").write_text(draft.text, encoding="utf-8")
                skill, digest, warning = self._forge_call(
                    lib, lambda: forge.draft_skill(plan.rid, tmp, name=draft.name))
        except (LibraryError, OSError) as exc:  # ForgeError, CapExceeded, PathRefused, disk errors
            self._failed_attempt(dlog, plan, report, f"error: not filed: {exc}")
            return
        truncated = f", {len(built.truncated)} cut to fit" if built.truncated else ""
        cap = f"; {warning}" if warning else ""
        dlog.write(plan.target, f"ok: filed skill {skill}{renamed} {digest}, {len(draft.text.encode('utf-8'))} "
                                f"bytes; prompt {built.chars} chars, {built.included} source(s){truncated}{cap}"
                                f"; sources {self.sources_hash(plan.sources)}")
        report.drafted.append(f"{plan.rid}: {skill}")
        log.info("drafted %s for %s (%s)", skill, plan.rid, digest)

    @staticmethod
    def _free_name(lib: Library, name: str) -> str:
        """``name``, or ``name-2``, ``name-3``... if ``30-skills/`` already has it (any case)."""
        taken = casefold_names(lib.abs_path(SKILLS))
        for n in range(1, 1000):
            suffix = "" if n == 1 else f"-{n}"
            candidate = name[:naming.MAX_DIR_NAME - len(suffix)].rstrip("-") + suffix
            if candidate.casefold() not in taken and naming.is_kebab_name(candidate):
                return candidate
        raise Invalid(f"no free skill name near {name!r} in {SKILLS}/")

    def _request_text(self, lib: Library, plan: Plan) -> str:
        text = read_text(lib, f"{plan.base}/request.md", 256 * 1024) or ""
        try:
            return fm.parse(text)[1]
        except fm.FrontMatterError:
            return text

    def _failed_attempt(self, dlog: DrafterLog, plan: Plan, report: PassReport, result: str) -> None:
        dlog.write(plan.target, result)
        report.errors.append(f"{plan.rid}: {result}")
        log.warning("draft for %s: %s", plan.rid, result)
        rnd = dlog.rounds().get(plan.target)
        if rnd and rnd.attempts >= self.cfg["max_attempts"] and not rnd.final:
            self._mark_failed(dlog, plan, report, result)

    def _mark_failed(self, dlog: DrafterLog, plan: Plan, report: PassReport, last: str) -> None:
        dlog.write(plan.target, f"failed: draft failed after {self.cfg['max_attempts']} attempts"
                                + (f" (last: {safe_text(last, 120)})" if last else "")
                                + "; nothing is blocked: new sources or new revise notes start a new round")
        report.failed.append(plan.rid)

    # ------------------------------------------------------------------ status

    def status(self) -> list[str]:
        lib, forge = self.open()
        lines = []
        for name in self._request_dirs(lib):
            try:
                plan = self.assess(lib, forge, name)
            except Exception as exc:  # noqa: BLE001
                lines.append(f"{FORGE}/{name}: unreadable ({exc})")
                continue
            round_ = f" round {plan.target!r}" if plan.target else ""
            lines.append(f"{plan.base}  [{plan.status}]  {plan.phase}{round_}: {plan.reason}")
        return lines
