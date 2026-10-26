"""The Skill Forge core (CONTRACTS §12.12, module F): offline, stdlib only, built on ``library``.

A feature request becomes a skill only through steps Noah approves in plain files:

    new ──> searches.md ticked ──> sources.md ticked ──> ingest ──> skill/ + review.md ──> decide
    awaiting-search-approval → awaiting-source-approval → awaiting-skill-review
                                                           │ approve → approved   (30-skills/<skill>/)
                                                           │ deny    → shelved    (95-shelf/<skill>/)
                                                           │ revise  → awaiting-skill-redraft → draft again
    revive: 95-shelf/<skill>/ → 35-forge/<id>/, awaiting-skill-review (no new searches; sources kept)

Invariants:

* **Files are the truth.** ``state.md`` holds ``status:`` and an append-only log (one timestamped
  line per change); Noah's ticks and ``decision:`` are read from the files every time.
* **Never deletes, never overwrites Noah's work.** Proposals are appended (existing lines, ticked or
  not, are left alone). A redraft moves the old ``skill/`` and ``review.md`` to
  ``previous/<n>/``. Moves are renames inside the library. The only files the forge removes are
  its own lock file and the library's own temp files.
* **Confined.** Every path goes through the library's confinement checks (no symlinks, no ``..``,
  nothing outside the root). A skill draft must be plain files: symlinks anywhere are refused.
* **One forge writer at a time.** Every mutating command holds ``35-forge/.forge-lock`` (O_EXCL)
  and re-reads the state inside it, so a second ``decide`` is refused by the lock while the first
  runs, and by the state check after it finished.
* **Approval is pinned to content.** ``review.md`` records the sha256 of ``skill/`` when drafted.
  On ``approve`` the forge recomputes it. If Noah edited files first, his edited content is what
  is approved: the new hash is recorded as approved (and the changed files are named in the log
  and CHANGELOG). If that content has code (any non-``.md`` file, now or in the draft), Noah must confirm it
  with ``confirm_hash: <first 8 hex digits of the new hash>`` in ``review.md``; until he does,
  ``decide`` stops with NotDecided and prints the hash. The hash is computed again after the move
  into ``30-skills/``; a difference rolls the move back. After approval, any change to the skill
  needs a new decision; ``verify`` checks an installed skill against its CHANGELOG **and** the
  approving request's ``state.md`` and ``review.md``.
* **Noah's concurrent edits survive.** Proposals are appended with one ``O_APPEND`` write (never a
  rewrite of a copy read earlier); ``review.md`` is rewritten only if its inode, size and mtime are
  unchanged since it was read.

Limits (by design, like the library):

* Paths are checked component by component (no symlinks) and then used by path. Someone who can
  swap a folder for a symlink on the share between the check and the use (a TOCTOU race) is not
  stopped; the forge assumes it is the only writer of structure under the root, and its lock
  serializes forge commands only, not other tools or people.
* A crash in the middle of ``decide`` can leave work for a human: the approval steps after the
  CHANGELOG write (``review.md``, ``state.md``) are not rolled back; ``verify`` then reports the
  missing records, so the skill is not trusted. A crash leaves ``35-forge/.forge-lock`` behind;
  remove it by hand once nothing is running. An interrupted ``draft-skill`` can leave a
  ``.forge-staging-*`` folder (never deleted automatically).
* Approval files are plain files on a share. The forge cannot tell Noah's edit from anyone
  else's; whoever can write ``searches.md``/``sources.md``/``review.md`` can approve. The AI must
  reach the forge only through these commands.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import re
import secrets
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from library import Library, LibraryError, PathRefused
from library import frontmatter as fm
from library import naming
from library.core import ARCHIVE, SKILLS, SOURCES
from library.safefs import atomic_copy, atomic_write_bytes, casefold_names

from . import checklist
from .hashing import CHANGELOG, file_digests, skill_hash, tree_files

FORGE = "35-forge"
SHELF = "95-shelf"
LOCK = ".forge-lock"

REQUEST = "request.md"
STATE = "state.md"
SEARCHES = "searches.md"
SOURCES_FILE = "sources.md"
REVIEW = "review.md"
SKILL_DIR = "skill"
PREVIOUS = "previous"

S_SEARCH = "awaiting-search-approval"
S_SOURCE = "awaiting-source-approval"
S_REVIEW = "awaiting-skill-review"
S_REDRAFT = "awaiting-skill-redraft"
S_APPROVED = "approved"
S_SHELVED = "shelved"
STATUSES = (S_SEARCH, S_SOURCE, S_REVIEW, S_REDRAFT, S_APPROVED, S_SHELVED)
DECISIONS = ("pending", "approve", "deny", "revise")

# Where each gate check may say yes. Searches stop once a draft exists ("no new searches"
# after a revive); approved URLs may still be fetched while a redraft is pending.
QUERY_STATES = frozenset({S_SEARCH, S_SOURCE})
URL_STATES = frozenset({S_SOURCE, S_REDRAFT})
PROPOSE_STATES = frozenset({S_SEARCH, S_SOURCE})
DRAFT_STATES = frozenset({S_SOURCE, S_REDRAFT})

MAX_ID_SLUG = 48
_ID_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})_(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)")

NEXT_STEP = {
    S_SEARCH: "Noah: tick the searches you approve in searches.md (edit a query first if you like)",
    S_SOURCE: "Noah: tick the sources to fetch in sources.md; then ingest and draft-skill",
    S_REVIEW: "Noah: read skill/, set decision: approve | deny | revise in review.md, then run decide",
    S_REDRAFT: "AI: redraft the skill (draft-skill); Noah reviews again",
    S_APPROVED: "done: the skill is in 30-skills/",
    S_SHELVED: "shelved: revive it to review the draft again",
}


class ForgeError(LibraryError):
    """A forge step was refused; the message says why and what to do."""


class NotDecided(ForgeError):
    """``review.md`` still says ``decision: pending``."""


def is_request_id(rid: str) -> bool:
    m = _ID_RE.fullmatch(rid) if isinstance(rid, str) else None
    if not m or len(m.group("slug")) > MAX_ID_SLUG + 6:  # room for a "-NNNN" collision suffix
        return False
    try:
        dt.date.fromisoformat(m.group("date"))
    except ValueError:
        return False
    return True


@dataclass
class DecideResult:
    decision: str
    skill: str
    location: str                       # library-relative folder afterwards
    hash: str | None = None             # the approved hash (approve only)
    edited: list[str] = field(default_factory=list)  # files changed since the draft (approve only)


@dataclass
class VerifyResult:
    ok: bool
    approved: str        # the hash in the skill's CHANGELOG.md
    current: str         # the hash of the files now
    problems: list[str] = field(default_factory=list)


@dataclass
class RequestInfo:
    id: str
    folder: str
    status: str
    title: str
    skill: str
    queries: tuple[int, int] = (0, 0)   # (approved, listed)
    sources: tuple[int, int] = (0, 0)
    ingested: int = 0
    decision: str = ""
    warnings: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    listed_queries: list[tuple[bool, str]] = field(default_factory=list)  # (ticked, exact query)
    listed_urls: list[tuple[bool, str]] = field(default_factory=list)     # (ticked, canonical URL)

    def raw_listing(self) -> str:
        """Every parsed query and URL as Python repr with its tick state: the authoritative view
        (CONTRACTS §12.9 round 3). A rendered Markdown view can hide or restyle text; this cannot."""
        lines = ["  searches (exact text, [x] = approved):"]
        lines += [f"    [{'x' if t else ' '}] {q!r}" for t, q in self.listed_queries] or ["    (none)"]
        lines.append("  sources (exact URL, [x] = approved):")
        lines += [f"    [{'x' if t else ' '}] {u!r}" for t, u in self.listed_urls] or ["    (none)"]
        return "\n".join(lines)

    def summary(self) -> str:
        lines = [f"{self.folder}  [{self.status}]  {self.title!r}"]
        detail = (f"searches {self.queries[0]}/{self.queries[1]} approved; "
                  f"sources {self.sources[0]}/{self.sources[1]} approved; ingested {self.ingested}")
        if self.skill:
            detail += f"; skill {self.skill}"
        if self.decision:
            detail += f"; decision {self.decision}"
        lines.append("  " + detail)
        lines.append("  next: " + NEXT_STEP.get(self.status, "?"))
        lines += [f"  warning: {w}" for w in self.warnings]
        return "\n".join(lines)


class Forge:
    """The forge inside one library. ``Forge.open(root)`` or ``Forge(Library.open(root))``."""

    def __init__(self, lib: Library) -> None:
        self.lib = lib

    @classmethod
    def open(cls, root: str | os.PathLike[str], **kwargs) -> Forge:
        return cls(Library.open(root, **kwargs))

    # ------------------------------------------------------------------ library primitives
    # The library's confinement and safe-write helpers are used through these four methods
    # only, so a later public API in `library` is a one-place change.

    def _abs(self, rel: str) -> Path:
        return self.lib._abs(rel)  # noqa: SLF001 - see Lead item: make public

    def _ensure_dir(self, rel: str) -> Path:
        return self.lib._ensure_dir(rel)  # noqa: SLF001

    def _write_new(self, rel: str, text: str) -> None:
        self.lib._write_new(rel, text)  # noqa: SLF001

    def _replace(self, rel: str, text: str) -> None:
        """Atomically replace an existing regular file inside the library."""
        path = self._abs(rel)
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise PathRefused(f"{rel} is not a regular file")
        atomic_write_bytes(path, text.encode("utf-8"))

    def _append_lines(self, rel: str, lines: list[str]) -> None:
        """Append to an existing file with one O_APPEND write, so an edit Noah saved in the
        meantime (a tick, an edited query) is never overwritten by a stale copy."""
        path = self._abs(rel)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise PathRefused(f"{rel} is not a regular file")
            data = "".join(lines)
            size = os.fstat(fd).st_size
            if size:
                with open(path, "rb") as fh:  # same file: we hold it open and refused links
                    fh.seek(size - 1)
                    if fh.read(1) != b"\n":
                        data = "\n" + data
            os.write(fd, data.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def _read_guarded(self, rel: str) -> tuple[str, tuple[int, int, int]]:
        """Text plus a fingerprint (inode, size, mtime) for ``_replace_unchanged``."""
        path = self._abs(rel)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            raise ForgeError(f"{rel} is missing") from None
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise PathRefused(f"{rel} is not a regular file")
            data = fh.read()
        try:
            return data.decode("utf-8"), (st.st_ino, st.st_size, st.st_mtime_ns)
        except UnicodeDecodeError:
            raise ForgeError(f"{rel} is not UTF-8 text") from None

    def _replace_unchanged(self, rel: str, fingerprint: tuple[int, int, int], text: str) -> None:
        """Replace a file Noah may edit, only if it is unchanged since we read it."""
        st = os.lstat(self._abs(rel))
        if (st.st_ino, st.st_size, st.st_mtime_ns) != fingerprint:
            raise ForgeError(f"{rel} changed while the forge was working (an edit by Noah?); "
                             "nothing was written to it; run the command again")
        self._replace(rel, text)

    def _read(self, rel: str) -> str:
        path = self._abs(rel)
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            raise ForgeError(f"{rel} is missing") from None
        if not stat.S_ISREG(mode):
            raise PathRefused(f"{rel} is not a regular file")
        return Library._read_text(path, rel)  # noqa: SLF001

    def _is_dir(self, rel: str) -> bool:
        try:
            return stat.S_ISDIR(os.lstat(self._abs(rel)).st_mode)
        except FileNotFoundError:
            return False

    def _move_dir(self, src_rel: str, dest_rel: str) -> None:
        """Rename a folder inside the library; refuses an existing or case-colliding target."""
        src = self._abs(src_rel)
        dest = self._abs(dest_rel)
        self._ensure_dir(str(Path(dest_rel).parent.as_posix()))
        if dest.name.casefold() in casefold_names(dest.parent):
            raise ForgeError(f"{dest_rel} already exists (names are case-insensitive)")
        # rename(2) refuses a non-empty target and can only replace an empty folder that
        # appeared in the meantime, so nothing can be lost.
        os.rename(src, dest)

    # ------------------------------------------------------------------ small helpers

    def _stamp(self) -> str:
        return self.lib.clock().isoformat(timespec="seconds")

    def _today(self) -> str:
        return self.lib.clock().date().isoformat()

    def _header(self, title: str, summary: str, **extra: str | list[str]) -> fm.Meta:
        meta: fm.Meta = {"title": title, "source": "skill forge", "added": self._today(),
                         "tags": ["forge"], "summary": summary, "related": []}
        meta.update(extra)
        return meta

    @staticmethod
    def _check_id(rid: str) -> str:
        if not is_request_id(rid):
            raise ForgeError(f"{rid!r} is not a request id (YYYY-MM-DD_kebab-title)")
        return rid

    def _base(self, rid: str) -> str:
        base = f"{FORGE}/{self._check_id(rid)}"
        if not self._is_dir(base):
            raise ForgeError(f"no request {rid} in {FORGE}/")
        return base

    def _parse(self, rel: str) -> tuple[fm.Meta, str]:
        try:
            return fm.parse(self._read(rel))
        except fm.FrontMatterError as exc:
            raise ForgeError(f"{rel}: malformed front-matter ({exc})") from None

    def _state(self, base: str) -> tuple[fm.Meta, str]:
        meta, body = self._parse(f"{base}/{STATE}")
        if meta.get("status") not in STATUSES:
            raise ForgeError(f"{base}/{STATE}: unknown status {meta.get('status')!r}")
        return meta, body

    def _set_state(self, base: str, meta: fm.Meta, body: str, message: str,
                   status: str | None = None, **extra: str | list[str]) -> None:
        """Rewrite the header and append exactly one log line; earlier log lines are kept as is."""
        meta = dict(meta)
        if status is not None:
            meta["status"] = status
        meta.update(extra)
        if body and not body.endswith("\n"):
            body += "\n"
        note = f" (status {status})" if status is not None else ""
        self._replace(f"{base}/{STATE}", _render(meta, body + f"- {self._stamp()} {message}{note}\n"))

    @staticmethod
    def _require(status: str, allowed: frozenset[str] | set[str], what: str) -> None:
        if status not in allowed:
            raise ForgeError(f"{what} is not allowed while the request is {status} "
                             f"(allowed: {', '.join(sorted(allowed))})")

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        forge_dir = self._ensure_dir(FORGE)
        path = forge_dir / LOCK
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            raise ForgeError(f"another forge command is running ({FORGE}/{LOCK} exists); "
                             "if nothing is running, remove that file and retry") from None
        try:
            os.write(fd, f"pid {os.getpid()} since {self._stamp()}\n".encode("ascii"))
        finally:
            os.close(fd)
        try:
            yield
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)  # our own lock file, never library content

    # ------------------------------------------------------------------ new

    def _shelved(self) -> list[tuple[str, fm.Meta]]:
        """(shelf folder name, state header) for every readable shelved request."""
        if not self._is_dir(SHELF):
            return []
        out = []
        for name in sorted(os.listdir(self._abs(SHELF))):
            try:
                meta, _ = self._state(f"{SHELF}/{name}")
            except (LibraryError, OSError):
                continue
            out.append((name, meta))
        return out

    def new(self, title: str, request_text: str) -> str:
        """Scaffold ``35-forge/<YYYY-MM-DD_kebab>/``. Returns the request id."""
        try:
            title = checklist.check_field(title, "title")
        except ValueError as exc:
            raise ForgeError(str(exc)) from None
        if not request_text.strip():
            raise ForgeError("the request is empty")
        stem = f"{self._today()}_{naming.slugify(title, MAX_ID_SLUG)}"
        with self._locked():
            forge_dir = self._ensure_dir(FORGE)
            taken = casefold_names(forge_dir)
            taken |= {str(m.get("request", "")).casefold() for _, m in self._shelved()}
            for n in range(1, 10_000):
                rid = stem if n == 1 else f"{stem}-{n}"
                if rid.casefold() in taken:
                    continue
                try:
                    os.mkdir(forge_dir / rid)
                except FileExistsError:
                    continue
                break
            else:
                raise ForgeError(f"too many requests named {stem}")
            base = f"{FORGE}/{rid}"
            # Noah's words byte for byte after the header (the header is the library's rule for
            # every .md file; `frontmatter.parse` gives the body back unchanged).
            self._write_new(f"{base}/{REQUEST}", _render(
                self._header(title, "Noah's feature request, verbatim.", request=rid), request_text))
            self._write_new(f"{base}/{SEARCHES}", _render(
                self._header(f"Searches: {title}", "Proposed web searches; Noah ticks the ones to run.",
                             request=rid), SEARCHES_INTRO))
            self._write_new(f"{base}/{STATE}", _render(
                self._header(f"State: {title}", "Forge status and append-only log.",
                             request=rid, status=S_SEARCH, ingested=[]),
                f"\n# Log\n\n- {self._stamp()} created (status {S_SEARCH})\n"))
        return rid

    # ------------------------------------------------------------------ searches

    def _searches(self, base: str) -> tuple[list[checklist.Query], list[checklist.Malformed]]:
        return checklist.parse_searches(self._read(f"{base}/{SEARCHES}"))

    def approved_queries(self, rid: str) -> list[str]:
        """The exact query strings on ``- [x]`` lines (Noah's edited text, if he edited)."""
        queries, _ = self._searches(self._base(rid))
        return [q.query for q in queries if q.checked]

    def is_query_approved(self, rid: str, query: str) -> bool:
        """Gate check: may this exact query run now? Fails closed (False) on any problem."""
        try:
            if not isinstance(query, str) or not query:
                return False
            base = self._base(rid)
            meta, _ = self._state(base)
            if meta["status"] not in QUERY_STATES:
                return False
            return query in self.approved_queries(rid)
        except Exception:  # noqa: BLE001 - a gate check must never raise its way to "yes"
            return False

    def propose_searches(self, rid: str, proposals: list[tuple[str, str]]) -> int:
        """Append ``- [ ] <query> — why: <why>`` lines. Returns how many were new."""
        clean: list[tuple[str, str]] = []
        for i, (query, why) in enumerate(proposals, start=1):
            try:
                clean.append((checklist.check_query(query),
                              checklist.check_why(why)))
            except ValueError as exc:
                raise ForgeError(f"proposal {i}: {exc}") from None
        if not clean:
            raise ForgeError("no searches proposed")
        with self._locked():
            base = self._base(rid)
            meta, body = self._state(base)
            self._require(meta["status"], PROPOSE_STATES, "proposing searches")
            text = self._read(f"{base}/{SEARCHES}")
            listed = {q.query for q in checklist.parse_searches(text)[0]}
            lines = []
            for query, why in clean:
                if query in listed:
                    continue
                listed.add(query)
                lines.append(f"- [ ] {query} — why: {why}\n")
            if lines:
                self._append_lines(f"{base}/{SEARCHES}", lines)
            self._set_state(base, meta, body,
                            f"proposed {len(lines)} search(es) ({len(clean) - len(lines)} already listed)")
        return len(lines)

    # ------------------------------------------------------------------ sources

    def _sources(self, base: str) -> tuple[list[checklist.Source], list[checklist.Malformed]]:
        try:
            text = self._read(f"{base}/{SOURCES_FILE}")
        except ForgeError:
            if not os.path.lexists(self._abs(f"{base}/{SOURCES_FILE}")):
                return [], []
            raise
        return checklist.parse_sources(text)

    def approved_urls(self, rid: str) -> list[str]:
        """Canonical forms of the URLs on ``- [x]`` lines of ``sources.md``."""
        sources, _ = self._sources(self._base(rid))
        return [s.url for s in sources if s.checked]

    def is_url_approved(self, rid: str, url: str) -> bool:
        """Gate check: may this URL be fetched now? Exact match after scheme/host lowercasing."""
        try:
            canon = checklist.canonical_url(url)
            base = self._base(rid)
            meta, _ = self._state(base)
            if meta["status"] not in URL_STATES:
                return False
            return canon in self.approved_urls(rid)
        except Exception:  # noqa: BLE001 - fail closed
            return False

    def propose_sources(self, rid: str, rows: list[tuple[str, str, str, str]]) -> int:
        """Append candidates (url, title, why, take). Needs at least one approved search."""
        clean: list[tuple[str, str, str, str]] = []
        for i, row in enumerate(rows, start=1):
            if len(row) != 4:
                raise ForgeError(f"source {i}: expected url, title, why, what-will-be-taken")
            try:
                url = checklist.canonical_url(row[0].strip())
                title, why, take = (checklist.check_proposal_text(v, w) for v, w in
                                    zip(row[1:], ("title", "why", "what will be taken"), strict=True))
            except ValueError as exc:
                raise ForgeError(f"source {i}: {exc}") from None
            clean.append((url, title, why, take))
        if not clean:
            raise ForgeError("no sources proposed")
        with self._locked():
            base = self._base(rid)
            meta, body = self._state(base)
            self._require(meta["status"], PROPOSE_STATES, "proposing sources")
            if not self.approved_queries(rid):
                raise ForgeError("no search is approved yet: Noah ticks searches.md first")
            rel = f"{base}/{SOURCES_FILE}"
            if os.path.lexists(self._abs(rel)):
                text = self._read(rel)
            else:
                text = _render(self._header(f"Sources: {_title(meta)}",
                                              "Candidate sources; Noah ticks the ones to fetch.",
                                              request=rid), SOURCES_INTRO)
                self._write_new(rel, text)
            listed = {s.url for s in checklist.parse_sources(text)[0]}
            lines = []
            for url, title, why, take in clean:
                if url in listed:
                    continue
                listed.add(url)
                idn = checklist.idn_note(url)
                note = f" — note: the host is an IDN that displays as \"{idn}\"; check it is the site you expect" if idn else ""
                lines.append(f"- [ ] {checklist.url_span(url)} — title: {title} — why: {why} — take: {take}{note}\n")
            if lines:
                self._append_lines(rel, lines)
            self._set_state(base, meta, body,
                            f"proposed {len(lines)} source(s) ({len(clean) - len(lines)} already listed)",
                            status=S_SOURCE)
        return len(lines)

    def record_ingest(self, rid: str, url: str, item: str) -> str:
        """Log that an approved URL was filed at ``item`` (a 10-sources/<topic>/ item whose
        header has ``source: <url>`` and ``forge: <rid>``). Returns the item's library path."""
        try:
            canon = checklist.canonical_url(url)
        except ValueError as exc:
            raise ForgeError(f"refused: {exc}") from None
        with self._locked():
            base = self._base(rid)
            meta, body = self._state(base)
            self._require(meta["status"], URL_STATES, "recording an ingest")
            if canon not in self.approved_urls(rid):
                raise ForgeError(f"refused: {canon} is not ticked in {base}/{SOURCES_FILE}")
            path = self.lib.resolve_user_path(item)
            rel = path.relative_to(self.lib.root).as_posix()
            parts = rel.split("/")
            if len(parts) != 3 or parts[0] != SOURCES:
                raise ForgeError(f"{rel} is not an item in {SOURCES}/<topic>/")
            if not os.path.lexists(path) or not stat.S_ISREG(os.lstat(path).st_mode):
                raise ForgeError(f"{rel} is not a regular file")
            header_rel = rel if rel.endswith(".md") else f"{rel}.md"
            item_meta, _ = self._parse(header_rel)
            try:
                filed_source = checklist.canonical_url(str(item_meta.get("source", "")))
            except ValueError:
                filed_source = None
            if filed_source != canon:
                raise ForgeError(f"{header_rel}: source: is {item_meta.get('source')!r}, not {canon}")
            if item_meta.get("forge") != rid:
                raise ForgeError(f"{header_rel}: forge: must be {rid!r}")
            previous = meta.get("ingested")
            ingested = list(previous) if isinstance(previous, list) else []
            entry = f"{canon} -> {rel}"
            if entry not in ingested:
                ingested.append(entry)
            self._set_state(base, meta, body, f"ingested {canon} -> {rel}", ingested=ingested)
        return rel

    # ------------------------------------------------------------------ draft

    def draft_skill(self, rid: str, src: str | os.PathLike[str], *, name: str | None = None) -> tuple[str, str, str | None]:
        """Copy a draft into ``<id>/skill/`` and write ``review.md``. Returns (skill, hash, cap warning)."""
        src_path = Path(os.path.abspath(os.fspath(src)))
        try:
            files = tree_files(src_path)
        except FileNotFoundError:
            raise ForgeError(f"{src} does not exist") from None
        rels = {rel for rel, _ in files}
        if "SKILL.md" not in rels:
            raise ForgeError(f"{src} has no SKILL.md at its top level")
        if any(rel.casefold() == CHANGELOG.casefold() for rel in rels):
            raise ForgeError(f"a draft may not contain {CHANGELOG} (in any case); the forge writes it on approval")
        folded: dict[str, str] = {}
        for rel in sorted(rels):
            if rel.casefold() in folded:
                raise ForgeError(f"{folded[rel.casefold()]} and {rel} differ only in case (one file on NTFS)")
            folded[rel.casefold()] = rel
        if any(_is_code(rel) for rel in rels) and MANIFEST not in {r.casefold() for r in rels}:
            raise ForgeError("a draft with code (any file that is not *.md) needs manifest.md "
                             "(inputs, outputs, paths it may read)")
        skill = name or _skill_name_from(src_path / "SKILL.md")
        if skill is None:
            skill = _ID_RE.fullmatch(self._check_id(rid)).group("slug")[:naming.MAX_DIR_NAME].strip("-")
        if not naming.is_kebab_name(skill):
            raise ForgeError(f"skill name {skill!r} is not kebab-case (1-48 characters)")
        incoming = sum(os.lstat(p).st_size for _, p in files)

        with self._locked():
            base = self._base(rid)
            meta, body = self._state(base)
            self._require(meta["status"], DRAFT_STATES, "drafting a skill")
            warning = self.lib._cap_gate(incoming)  # noqa: SLF001
            kept = self._keep_previous_draft(base)
            staging = f"{base}/.forge-staging-{secrets.token_hex(4)}"
            self._ensure_dir(staging)
            for rel, path in files:
                dest_rel = f"{staging}/{rel}"
                self._ensure_dir(str(Path(dest_rel).parent.as_posix()))
                atomic_copy(path, self._abs(dest_rel))
            self._move_dir(staging, f"{base}/{SKILL_DIR}")
            skill_dir = self._abs(f"{base}/{SKILL_DIR}")
            digest = skill_hash(skill_dir)
            listing = [f"{h}  {rel}" for rel, h in file_digests(skill_dir)]
            self._write_new(f"{base}/{REVIEW}", _render(
                self._header(f"Review: {skill}", "Noah's decision on the drafted skill.",
                             request=rid, skill=skill, decision="pending", hash=digest, files=listing),
                REVIEW_INTRO.format(skill=skill, rid=rid)))
            note = f"; previous draft kept in {kept}" if kept else ""
            self._set_state(base, meta, body, f"drafted skill {skill} ({digest}){note}",
                            status=S_REVIEW, skill=skill)
        return skill, digest, warning

    def _keep_previous_draft(self, base: str) -> str | None:
        """Move an existing ``skill/`` and ``review.md`` to ``previous/<n>/`` (never overwritten)."""
        has_skill = os.path.lexists(self._abs(f"{base}/{SKILL_DIR}"))
        has_review = os.path.lexists(self._abs(f"{base}/{REVIEW}"))
        if not (has_skill or has_review):
            return None
        prev_dir = self._ensure_dir(f"{base}/{PREVIOUS}")
        for n in range(1, 10_000):
            if str(n) in casefold_names(prev_dir):
                continue
            try:
                os.mkdir(prev_dir / str(n))
            except FileExistsError:
                continue
            break
        else:
            raise ForgeError("too many previous drafts")
        dest = f"{base}/{PREVIOUS}/{n}"
        if has_skill:
            self._move_dir(f"{base}/{SKILL_DIR}", f"{dest}/{SKILL_DIR}")
        if has_review:
            os.replace(self._abs(f"{base}/{REVIEW}"), self._abs(f"{dest}/{REVIEW}"))  # into a fresh folder
        return dest

    # ------------------------------------------------------------------ decide

    def decide(self, rid: str) -> DecideResult:
        """Apply Noah's ``decision:`` from ``review.md`` (approve | deny | revise)."""
        with self._locked():
            base = self._base(rid)
            meta, body = self._state(base)
            self._require(meta["status"], {S_REVIEW}, "decide")
            review_text, fingerprint = self._read_guarded(f"{base}/{REVIEW}")
            try:
                review_meta, review_body = fm.parse(review_text)
            except fm.FrontMatterError as exc:
                raise ForgeError(f"{base}/{REVIEW}: malformed front-matter ({exc})") from None
            decision = str(review_meta.get("decision", "")).strip().lower()
            if decision not in DECISIONS:
                raise ForgeError(f"{base}/{REVIEW}: decision must be one of approve, deny, revise "
                                 f"(got {review_meta.get('decision')!r})")
            if decision == "pending":
                raise NotDecided(f"{base}/{REVIEW} still says decision: pending")
            skill = str(review_meta.get("skill", ""))
            if not naming.is_kebab_name(skill):
                raise ForgeError(f"{base}/{REVIEW}: skill {skill!r} is not kebab-case (1-48 characters)")
            if decision == "approve":
                return self._approve(rid, base, meta, body, review_meta, review_body, fingerprint, skill)
            if decision == "deny":
                return self._deny(rid, base, meta, body, skill)
            self._set_state(base, meta, body, "Noah asked for a revision", status=S_REDRAFT)
            return DecideResult("revise", skill, base)

    def _approve(self, rid: str, base: str, meta: fm.Meta, body: str, review_meta: fm.Meta,
                 review_body: str, fingerprint: tuple[int, int, int], skill: str) -> DecideResult:
        # ---- preflight: every check runs before the first move.
        skill_rel = f"{base}/{SKILL_DIR}"
        if not self._is_dir(skill_rel):
            raise ForgeError(f"{skill_rel}/ is missing; nothing to approve")
        skill_dir = self._abs(skill_rel)
        if CHANGELOG.casefold() in casefold_names(skill_dir):
            raise ForgeError(f"{skill_rel}/ has a file named like {CHANGELOG} (any case); the forge "
                             "writes that file on approval, so rename or remove it first")
        current = skill_hash(skill_dir)  # also refuses symlinks and special files
        digests = file_digests(skill_dir)
        drafted = str(review_meta.get("hash", ""))
        edited: list[str] = []
        if current != drafted:
            edited = _changed_files(review_meta.get("files"), digests)
            listed = review_meta.get("files") if isinstance(review_meta.get("files"), list) else []
            drafted_code = any(isinstance(x, str) and "  " in x and _is_code(x.split("  ", 1)[1]) for x in listed)
            if drafted_code or any(_is_code(rel) for rel, _ in digests):
                _require_confirm(review_meta, current, base, edited)

        dest_rel = f"{SKILLS}/{skill}"
        skills_dir = self._abs(SKILLS)
        clash = [n for n in os.listdir(skills_dir) if n.casefold() == skill.casefold()]
        if clash and clash[0] != skill:
            raise ForgeError(f"{SKILLS}/{clash[0]} differs from {skill} only in case")
        updating = bool(clash)
        old_entries = ""
        if updating:
            if not self._is_dir(dest_rel):
                raise ForgeError(f"{dest_rel} exists and is not a folder")
            try:
                _, old_entries = self._parse(f"{dest_rel}/{CHANGELOG}")
            except ForgeError:
                old_entries = ""
            self._ensure_dir(f"{ARCHIVE}/{SKILLS}")  # the archive target must be usable (no links)
        self._abs(f"{base}/{STATE}")
        st = os.lstat(self._abs(f"{base}/{REVIEW}"))
        if (st.st_ino, st.st_size, st.st_mtime_ns) != fingerprint:
            raise ForgeError(f"{base}/{REVIEW} changed while the forge was working; nothing was moved; "
                             "run decide again")

        # ---- moves, with a rollback if anything fails before the approval is recorded.
        archived: str | None = None
        moved = False
        try:
            if updating:
                archived = self.lib.archive(dest_rel)[0][1]
            self._move_dir(skill_rel, dest_rel)
            moved = True
            installed = skill_hash(self._abs(dest_rel))
            if installed != current:
                raise ForgeError(f"the skill changed while it was being approved ({current} reviewed, "
                                 f"{installed} installed); nothing was approved, decide again")
            stamp = self._stamp()
            how = ("as drafted" if not edited else
                   f"with Noah's edits since the draft (draft {drafted}; changed: {', '.join(edited)})")
            entry = f"- {stamp} approved from forge request {rid}, {how}; content {installed}\n"
            history = old_entries.split("# Changelog", 1)[-1].lstrip("\n") if old_entries else ""
            self._write_new(f"{dest_rel}/{CHANGELOG}", _render(
                self._header(f"Changelog: {skill}", "Every approval of this skill and its content hash.",
                             skill=skill, approved_hash=installed, request=rid),
                "\n# Changelog\n\n" + entry + history))
        except BaseException:
            # Undo in reverse order; the archived previous version comes back even when the
            # new skill never moved (e.g. the rename itself failed).
            if moved and not os.path.lexists(self._abs(f"{dest_rel}/{CHANGELOG}")):
                self._move_dir(dest_rel, skill_rel)
                moved = False
            if archived and not moved:
                self._move_dir(archived, dest_rel)
            raise
        # ---- record the approval (the skill is installed; verify needs these records).
        # state.md first (the forge's own record), then review.md. If review.md was edited in
        # this instant the write is refused and `verify` reports the missing record.
        self._set_state(base, meta, body, f"Noah approved skill {skill} -> {dest_rel}/ ({installed}"
                        + (f"; edited after the draft: {', '.join(edited)}" if edited else "") + ")",
                        status=S_APPROVED, approved_hash=installed)
        review_meta = dict(review_meta)
        review_meta.update(approved_hash=installed, decided=stamp)
        self._replace_unchanged(f"{base}/{REVIEW}", fingerprint, _render(review_meta, review_body))
        return DecideResult("approve", skill, dest_rel, installed, edited)

    def _deny(self, rid: str, base: str, meta: fm.Meta, body: str, skill: str) -> DecideResult:
        shelf_dir = self._ensure_dir(SHELF)
        taken = casefold_names(shelf_dir)
        for n in range(1, 10_000):
            name = naming.numbered(skill, n, is_dir=True)
            if name.casefold() not in taken and len(name) <= naming.MAX_DIR_NAME:
                break
        else:
            raise ForgeError(f"too many shelved skills named {skill}")
        dest = f"{SHELF}/{name}"
        self._move_dir(base, dest)
        self._set_state(dest, meta, body, f"Noah denied skill {skill}; shelved as {dest}/ "
                        "with its draft and sources", status=S_SHELVED, shelf=name)
        return DecideResult("deny", skill, dest)

    # ------------------------------------------------------------------ revive

    def revive(self, name: str) -> str:
        """Move ``95-shelf/<name>/`` back to ``35-forge/<id>/`` for review. Returns the id."""
        if not naming.is_kebab_name(name):
            raise ForgeError(f"{name!r} is not a shelved skill name")
        with self._locked():
            src = f"{SHELF}/{name}"
            if not self._is_dir(src):
                raise ForgeError(f"nothing shelved as {src}/")
            meta, body = self._state(src)
            self._require(meta["status"], {S_SHELVED}, "revive")
            rid = self._check_id(str(meta.get("request", "")))
            if not self._is_dir(f"{src}/{SKILL_DIR}"):
                raise ForgeError(f"{src}/{SKILL_DIR}/ is missing; nothing to review")
            # Reset the decision first, in place: if review.md is being edited, nothing moves.
            review_text, fingerprint = self._read_guarded(f"{src}/{REVIEW}")
            try:
                review_meta, review_body = fm.parse(review_text)
            except fm.FrontMatterError as exc:
                raise ForgeError(f"{src}/{REVIEW}: malformed front-matter ({exc})") from None
            review_meta = {k: v for k, v in review_meta.items() if k != "confirm_hash"}
            review_meta["decision"] = "pending"
            self._ensure_dir(FORGE)
            dest = f"{FORGE}/{rid}"
            if dest.split("/")[-1].casefold() in casefold_names(self._abs(FORGE)):
                raise ForgeError(f"{dest} already exists; nothing was moved")
            self._replace_unchanged(f"{src}/{REVIEW}", fingerprint, _render(review_meta, review_body))
            self._move_dir(src, dest)
            meta = {k: v for k, v in meta.items() if k != "shelf"}
            self._set_state(dest, meta, body, f"revived from {src}/ (sources kept, no new searches; "
                            "review.md decision reset to pending)", status=S_REVIEW)
        return rid

    # ------------------------------------------------------------------ status / verify

    def info(self, folder: str) -> RequestInfo:
        meta, body = self._state(folder)
        info = RequestInfo(id=str(meta.get("request", "")), folder=folder, status=str(meta["status"]),
                           title=_title(meta),
                           skill=str(meta.get("skill", "")))
        try:
            queries, bad = checklist.parse_searches(self._read(f"{folder}/{SEARCHES}"))
            info.queries = (sum(q.checked for q in queries), len(queries))
            info.listed_queries = [(q.checked, q.query) for q in queries]
            info.warnings += [f"{SEARCHES} {b}" for b in bad]
        except LibraryError as exc:
            info.warnings.append(str(exc))
        try:
            sources, bad = self._sources(folder)
            info.sources = (sum(s.checked for s in sources), len(sources))
            info.listed_urls = [(s.checked, s.url) for s in sources]
            info.warnings += [f"{SOURCES_FILE} {b}" for b in bad]
        except LibraryError as exc:
            info.warnings.append(str(exc))
        ingested = meta.get("ingested")
        info.ingested = len(ingested) if isinstance(ingested, list) else 0
        if os.path.lexists(self._abs(f"{folder}/{REVIEW}")):
            try:
                info.decision = str(self._parse(f"{folder}/{REVIEW}")[0].get("decision", ""))
            except LibraryError as exc:
                info.warnings.append(str(exc))
        info.log = [line for line in body.splitlines() if line.startswith("- ")]
        return info

    def all_requests(self) -> tuple[list[RequestInfo], list[str]]:
        """Every request in 35-forge/ and 95-shelf/, plus problems reading any of them."""
        infos: list[RequestInfo] = []
        problems: list[str] = []
        for section in (FORGE, SHELF):
            if not self._is_dir(section):
                continue
            for name in sorted(os.listdir(self._abs(section))):
                if name.startswith("."):
                    continue
                try:
                    infos.append(self.info(f"{section}/{name}"))
                except (LibraryError, OSError) as exc:
                    problems.append(f"{section}/{name}: {exc}")
        return infos, problems

    def request_info(self, rid: str) -> RequestInfo:
        """One request by id, wherever it is (35-forge, or shelved)."""
        self._check_id(rid)
        if self._is_dir(f"{FORGE}/{rid}"):
            return self.info(f"{FORGE}/{rid}")
        for name, meta in self._shelved():
            if meta.get("request") == rid:
                return self.info(f"{SHELF}/{name}")
        raise ForgeError(f"no request {rid} in {FORGE}/ or {SHELF}/")

    def verify(self, skill: str) -> VerifyResult:
        """Check an installed skill against every approval record.

        The current content hash of ``30-skills/<skill>/`` must equal the ``approved_hash`` in its
        ``CHANGELOG.md`` **and** in the approving request's ``35-forge/<id>/state.md`` and
        ``review.md``; any disagreement (including between the records) is a mismatch.
        """
        if not naming.is_kebab_name(skill):
            raise ForgeError(f"{skill!r} is not a skill name")
        rel = f"{SKILLS}/{skill}"
        if not self._is_dir(rel):
            raise ForgeError(f"no skill {rel}/")
        current = skill_hash(self._abs(rel))
        problems: list[str] = []
        records: dict[str, str] = {}
        try:
            meta, _ = self._parse(f"{rel}/{CHANGELOG}")
            records[f"{rel}/{CHANGELOG}"] = str(meta.get("approved_hash", ""))
            rid = str(meta.get("request", ""))
        except LibraryError as exc:
            problems.append(str(exc))
            rid = ""
        if rid:
            if not is_request_id(rid) or not self._is_dir(f"{FORGE}/{rid}"):
                problems.append(f"the approving request {rid!r} is not in {FORGE}/")
            else:
                for name in (STATE, REVIEW):
                    try:
                        records[f"{FORGE}/{rid}/{name}"] = str(self._parse(f"{FORGE}/{rid}/{name}")[0]
                                                               .get("approved_hash", ""))
                    except LibraryError as exc:
                        problems.append(str(exc))
                try:
                    status = self._state(f"{FORGE}/{rid}")[0]["status"]
                    if status != S_APPROVED:
                        problems.append(f"{FORGE}/{rid} is {status}, not approved")
                except LibraryError:
                    pass
        elif not problems:
            problems.append(f"{rel}/{CHANGELOG} names no approving request")
        for where, value in records.items():
            if value != current:
                problems.append(f"{where} records {value or '(nothing)'}")
        approved = records.get(f"{rel}/{CHANGELOG}", "")
        return VerifyResult(not problems and len(records) == 3, approved, current, problems)


# ---------------------------------------------------------------------- module helpers

_TOKEN_LINE_RE = re.compile(r'^(status|decision): "([a-z]+(?:-[a-z]+)*)"$', re.MULTILINE)


def _render(meta: fm.Meta, body: str) -> str:
    """``frontmatter.render``, but ``status:``/``decision:`` tokens unquoted so Noah can edit them
    as ``decision: approve`` (both forms parse the same)."""
    head = fm.dump(meta)
    return _TOKEN_LINE_RE.sub(r"\1: \2", head) + body


MANIFEST = "manifest.md"


def _is_code(rel: str) -> bool:
    """CONTRACTS §12.9 round 2: every file that is not Markdown counts as code (any case)."""
    return not rel.casefold().endswith(".md")


def _require_confirm(review_meta: fm.Meta, current: str, base: str, edited: list[str]) -> None:
    """Code changed since the draft: Noah must confirm the exact new content by its hash."""
    want = current.removeprefix("sha256:")[:8]
    given = str(review_meta.get("confirm_hash", "")).strip().lower().removeprefix("sha256:")
    if len(given) >= 8 and all(c in "0123456789abcdef" for c in given) and current[7:].startswith(given):
        return
    raise NotDecided(
        f"the skill's content changed since the draft ({', '.join(edited)}) and it has code (non-.md files): to approve "
        f"exactly this content add the line 'confirm_hash: {want}' to {base}/{REVIEW} "
        f"(content {current}), then run decide again"
        + (f"; the confirm_hash given ({given!r}) does not match" if given else ""))


def _title(state_meta: fm.Meta) -> str:
    return str(state_meta.get("title", "")).removeprefix("State: ")


def _append(text: str, lines: list[str]) -> str:
    if text and not text.endswith("\n"):
        text += "\n"
    return text + "".join(lines)


def _skill_name_from(skill_md: Path) -> str | None:
    """``name:`` from the draft's SKILL.md header, if it has a usable one."""
    try:
        meta, _ = fm.parse(skill_md.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, fm.FrontMatterError):
        return None
    name = meta.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None


def _changed_files(recorded: object, current: list[tuple[str, str]]) -> list[str]:
    """Names of files added, removed or changed since the draft's per-file listing."""
    before: dict[str, str] = {}
    if isinstance(recorded, list):
        for line in recorded:
            if isinstance(line, str) and "  " in line:
                digest, rel = line.split("  ", 1)
                before[rel] = digest
    after = dict((rel, h) for rel, h in current)
    changed = [f"{rel} (changed)" for rel in sorted(before.keys() & after.keys()) if before[rel] != after[rel]]
    changed += [f"{rel} (added)" for rel in sorted(after.keys() - before.keys())]
    changed += [f"{rel} (removed)" for rel in sorted(before.keys() - after.keys())]
    return changed or ["(the file listing in review.md was edited)"]


SEARCHES_INTRO = """\

# Proposed searches

Nothing goes to the internet until you approve it here. To approve a search, change its `[ ]` to
`[x]`. You may edit the query text first: the edited text is exactly what will run. Leave a box
empty to refuse it. Only lines of the form `- [x] <query> — why: <reason>` at the start of a line
count; anything else is ignored.

"""

SOURCES_INTRO = """\

# Candidate sources

Only the search results' snippets were seen; no page has been fetched. To approve fetching a page,
change its `[ ]` to `[x]`. The URL (between the backticks) is what gets fetched, exactly:
the gate will not fetch any other address, not even with a trailing slash added. You may edit the
URL, but keep it between its two backticks. "take" says what will be kept from the page.

"""

REVIEW_INTRO = """\

# Review: {skill}

Read the draft in `skill/`. You may edit any file there first. Then set `decision:` above to:

- `approve`: the files in `skill/` become `30-skills/{skill}/`. If you edited them, your edited
  files are what is approved: the content hash is recomputed and recorded as approved. Any change
  after approval needs a new decision.
- `deny`: the whole request (draft and approved sources) is kept in `95-shelf/{skill}/`, so it
  can be revived later without new research.
- `revise`: the AI redrafts; nothing moves.

If the content has code (any file that is not .md) and anyone changed a file after the draft, approving also
needs the line `confirm_hash: <first 8 hex digits>` in the header above; `decide` prints the
exact value to confirm.

Then run `python3 -m forge decide {rid}` (or use the Open WebUI button).

`hash` is the content hash of `skill/` when it was drafted; `files` lists each file's sha256.
"""
