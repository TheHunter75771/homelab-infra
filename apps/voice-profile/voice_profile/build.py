"""One voice-profile build (CONTRACTS §12.21.2): extract, clean, stats, draft.

Reads ``40-writing/samples/*.docx`` (and the optional ``40-writing/include.txt``) and writes,
inside ``40-writing/``:

* ``samples-text/<name>.md``: one cleaned text per kept sample, with a library header whose
  ``source:`` is the original file name, plus ``samples-text/MANIFEST.md`` (kept, merged,
  excluded and why, archived outputs);
* ``style-stats.md``: the code-computed statistics;
* ``voice-profile.md`` (a draft), or ``voice-profile.next.md`` when the existing profile is not a
  draft. **An approved profile is never touched:** the main file is replaced only when its header
  parses and says ``status: draft``; a missing, approved, edited-beyond-parsing or otherwise
  non-draft profile is left alone. The check is repeated just before the write.

Writes go through the library's helpers: ``Library.abs_path`` (no ``..``, no symlink on the way or
at the target), ``Library.ensure_dir``, ``safefs.atomic_write_bytes`` (temp file + rename) and
``Library.archive`` through ``archive.archive`` (an output whose sample is no longer kept, or a
draft profile about to be replaced, is archived, never deleted; across the unit's separate
``40-writing`` and ``90-archive/40-writing`` mounts it falls back to copy, fsync, rename, unlink). A file
is rewritten only if its bytes change, and only if it carries this tool's ``generator:`` key, so a
hand-made file of the same name is never replaced. A name that differs only in case from an
existing one is refused (NTFS). The library cap is checked before writing.

``--dry-run`` does everything (including the model call unless ``--no-ai``) and writes nothing.
"""
from __future__ import annotations

import logging
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, TextIO

from library import SENTINEL_NAME, Library, LibraryError, PathRefused, naming
from library import frontmatter as fm
from library.core import DEFAULT_CAP, WRITING, Clock
from library.safefs import atomic_write_bytes

from . import prompt as promptmod
from . import stats as statsmod
from .archive import Identity, archive, copy_archive, identity, write_new
from .broker import Broker, BrokerError, BrokerUnreachable
from .chars import visible
from .clean import Cleaned, Outcome, Sample, clean_all, parse_include
from .docx import Caps, DocxRefused, read_docx
from .validate import Invalid, validate

log = logging.getLogger("voice_profile")

SAMPLES = f"{WRITING}/samples"
TEXTS = f"{WRITING}/samples-text"
MANIFEST = f"{TEXTS}/MANIFEST.md"
STATS = f"{WRITING}/style-stats.md"
PROFILE = f"{WRITING}/voice-profile.md"
NEXT = f"{WRITING}/voice-profile.next.md"
INCLUDE = f"{WRITING}/include.txt"
GENERATOR = "voice-profile"
INCLUDE_MAX = 64 * 1024
READ_MAX = 1024 * 1024
PROMPT_MARGIN = 256          # a prompt this close to num_ctx was (or may have been) cut by Ollama
MIN_EXCERPTS = 4
CAP_ENV = "AI_LIBRARY_CAP"
SOURCE_MAX = 200             # header source:/title values (Lead, §12.21 V question 8)
MAX_CORPUS_CHARS = 2_000_000  # all samples read, together (memory: the unit has MemoryMax=256M)
# Line breaks other than LF/CRLF: a header holding one is unreadable, so a profile is protected.
_ODD_BREAK_RE = re.compile(r"[\x0b\x0c\x85\u2028\u2029]|\r(?!\n)")


class BuildError(LibraryError):
    """The build cannot run (library unmounted, unsafe path, cap); the message is for a human."""


@dataclass
class Report:
    kept: list[tuple[str, str, int]] = field(default_factory=list)        # (sample, output, words)
    merged: list[tuple[str, str, float]] = field(default_factory=list)
    excluded: list[tuple[str, str]] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    archived: list[tuple[str, str]] = field(default_factory=list)
    profile: str = ""            # what happened to the profile, one line
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    def summary(self) -> str:
        verb = "would write" if self.dry_run else "wrote"
        return (f"kept {len(self.kept)}, merged {len(self.merged)}, excluded {len(self.excluded)}; "
                f"{verb} {len(self.written)}, unchanged {len(self.unchanged)}, archived {len(self.archived)}; "
                f"profile: {self.profile or 'not drafted'}; errors {len(self.errors)}")


def _safe(text: str) -> str:
    """A file name as one printable line without backticks (for headers and the manifest)."""
    return naming.display(visible(text)).replace("`", "'")


def _capped(text: str, limit: int = SOURCE_MAX) -> str:
    """``text`` cut to ``limit`` characters (the last one an ellipsis) for a header value."""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def sentinel_problem(lib: Library) -> str | None:
    """None if the mount sentinel is a regular file at the library root (not a symlink)."""
    try:
        st = os.lstat(lib.root / SENTINEL_NAME)
    except FileNotFoundError:
        return f"{SENTINEL_NAME} is missing (is the library share mounted?)"
    except OSError as e:
        return f"{SENTINEL_NAME} can't be checked ({e.strerror})"
    if not stat.S_ISREG(st.st_mode):
        return f"{SENTINEL_NAME} is not a regular file"
    return None


def read_text(lib: Library, rel: str, limit: int = READ_MAX) -> str | None:
    """A regular file inside the library as text (no symlinks anywhere), or None if missing."""
    return read_with_identity(lib, rel, limit)[0]


def read_with_identity(lib: Library, rel: str, limit: int = READ_MAX) -> tuple[str | None, Identity | None]:
    """(text, identity of the very file read) or (None, None) if missing."""
    path = lib.abs_path(rel)  # refuses '..' and symlinked components, the last one included
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise BuildError(f"{rel} cannot be read ({exc.strerror or type(exc).__name__})") from None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise BuildError(f"{rel} is not a regular file")
        data = fh.read(limit + 1)
        after = os.fstat(fh.fileno())
    if len(data) > limit:
        raise BuildError(f"{rel} is larger than {limit} bytes")
    if identity(after) != identity(st):
        raise BuildError(f"{rel} changed while it was read; run again")
    return data.decode("utf-8", "replace"), identity(st)


def header_of(text: str | None) -> fm.Meta | None:
    """The front-matter, or None if there is none, it does not parse, or it holds a line break
    other than LF/CRLF (FF, VT, NEL, U+2028/2029, a lone CR): a header that tools may split into
    lines differently is never trusted."""
    if text is None or not fm.has_header(text):
        return None
    close = text.find("\n---", 3)
    # Up to and including the LF before the closing ---, so a CRLF header's last CR is not "lone".
    if _ODD_BREAK_RE.search(text[:close + 1 if close >= 0 else len(text)]):
        return None
    try:
        return fm.parse(text)[0]
    except fm.FrontMatterError:
        return None


def profile_status(lib: Library, rel: str) -> str | None:
    """None if missing; else the header's ``status`` (casefolded), or ``"unreadable"``."""
    return profile_state(lib, rel)[0]


def profile_state(lib: Library, rel: str) -> tuple[str | None, Identity | None]:
    """(status as in ``profile_status``, identity of the file that status was read from)."""
    text, ident = read_with_identity(lib, rel)
    meta = header_of(text)
    if meta is None:
        return (None if text is None and not os.path.lexists(lib.abs_path(rel)) else "unreadable"), ident
    status = meta.get("status")
    return (status.strip().casefold() if isinstance(status, str) else "unreadable"), ident


def profile_target(lib: Library) -> str:
    """Where a new draft goes: the main profile only if it is missing or a draft; else the .next
    file if that is missing or a draft; else nowhere (BuildError)."""
    for rel in (PROFILE, NEXT):
        if profile_status(lib, rel) in (None, "draft"):
            return rel
    raise BuildError(f"{PROFILE} and {NEXT} are both not drafts (approved or hand-edited); "
                     "nothing is overwritten. Move one aside to draft again.")


class Builder:
    def __init__(self, cfg: dict[str, Any], *, broker: Broker | None = None, clock: Clock | None = None,
                 cap_bytes: int | None = None) -> None:
        self.cfg = cfg
        self.clock = clock
        if cap_bytes is None:
            cap_text = os.environ.get(CAP_ENV)
            cap_bytes = naming.parse_size(cap_text) if cap_text else DEFAULT_CAP
        self.cap_bytes = cap_bytes
        self.caps = Caps.from_config(cfg)
        self.broker = broker or Broker(cfg["broker_url"], cfg["model"], timeout=cfg["timeout"],
                                       num_ctx=cfg["num_ctx"])

    # ------------------------------------------------------------------ plumbing

    def open(self) -> Library:
        kwargs: dict[str, Any] = {"cap_bytes": self.cap_bytes}
        if self.clock is not None:
            kwargs["clock"] = self.clock
        lib = Library.open(self.cfg["library_root"], **kwargs)
        problem = sentinel_problem(lib)
        if problem:
            raise BuildError(f"the AI Library can't be used: {problem}; nothing read or written")
        return lib

    # ------------------------------------------------------------------ step 1: extract

    def read_samples(self, lib: Library) -> tuple[list[Sample], list[tuple[str, str]]]:
        folder = lib.abs_path(SAMPLES)
        st = os.lstat(folder)
        if not stat.S_ISDIR(st.st_mode):
            raise BuildError(f"{SAMPLES} is not a folder")
        samples, excluded = [], []
        names = sorted(os.listdir(folder))
        count = chars = 0
        for name in names:
            if name.startswith((".", "~$")):
                continue  # hidden files and Word's lock files
            shown = _safe(name)
            try:
                entry = os.lstat(folder / name)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(entry.st_mode):
                excluded.append((shown, "a symlink; not followed"))
                continue
            if not stat.S_ISREG(entry.st_mode):
                continue
            if not name.casefold().endswith(".docx"):
                excluded.append((shown, "not a .docx file (only .docx is read)"))
                continue
            count += 1
            if count > self.cfg["max_samples"]:
                excluded.append((shown, f"over max_samples ({self.cfg['max_samples']}); not read"))
                continue
            try:
                paragraphs = read_docx(folder / name, self.caps)
            except DocxRefused as exc:
                excluded.append((shown, f"refused: {exc}"))
                continue
            size = sum(len(p.text) for p in paragraphs)
            if chars + size > MAX_CORPUS_CHARS:
                excluded.append((shown, (f"not read: the samples together hold over {MAX_CORPUS_CHARS} "
                                        "characters of text")))
                continue
            chars += size
            samples.append(Sample(shown, entry.st_mtime, paragraphs))
        return samples, excluded

    # ------------------------------------------------------------------ step 2: outputs

    @staticmethod
    def output_names(kept: list[Cleaned]) -> dict[str, str]:
        """sample name -> ``samples-text/<slug>.md``: kebab slugs, unique ignoring case, never
        MANIFEST and never a Windows device name."""
        taken = {"manifest"}
        out = {}
        for doc in sorted(kept, key=lambda d: d.name):
            stem = doc.name.rsplit(".", 1)[0]
            base = naming.slugify(stem, naming.MAX_DIR_NAME)
            if not naming.is_kebab_name(base):  # a Windows device name (con, aux, ...)
                base = f"sample-{base}"
            n, slug = 1, base
            while slug.casefold() in taken:
                n += 1
                slug = f"{base}-{n}"
            taken.add(slug.casefold())
            out[doc.name] = f"{TEXTS}/{slug}.md"
        return out

    def _header(self, *, title: str, source: str, summary: str, tags: list[str], related: list[str],
                today: str) -> fm.Meta:
        return {"title": title, "source": source, "added": today, "tags": tags, "summary": summary,
                "related": related, "generator": GENERATOR}

    def sample_text(self, doc: Cleaned, today: str) -> str:
        meta = self._header(
            title=_capped(f"Writing sample: {doc.name.rsplit('.', 1)[0]}"), source=_capped(doc.name), today=today,
            summary=f"Noah's own writing ({doc.words} words), cleaned for style reference only; "
                    "not facts or content to reuse.",
            tags=["writing-sample", "voice-profile"], related=[MANIFEST])
        return fm.render(meta, doc.body() + "\n")

    def manifest_text(self, outcome: Outcome, names: dict[str, str], stale: list[str],
                      foreign: list[str], extra_excluded: list[tuple[str, str]], today: str) -> str:
        meta = self._header(
            title="Writing samples manifest", source=f"voice-profile from {SAMPLES}/", today=today,
            summary="Which samples were kept, merged as near-duplicates or excluded, and why.",
            tags=["writing-sample", "voice-profile"], related=[STATS])
        excluded = sorted(extra_excluded + outcome.excluded)
        lines = ["# Writing samples manifest", "",
                 (f"Built by voice-profile from {SAMPLES}/. A kept sample is Noah's own writing with the cover "
                 "page, reference list, block quotes and long quotations removed, for style reference only."),
                 "", f"## Kept ({len(outcome.kept)})"]
        for doc in sorted(outcome.kept, key=lambda d: d.name):
            lines.append(f"- `{doc.name}` -> {PurePosixPath(names[doc.name]).name}: {doc.words} words; "
                         f"cover lines {doc.cover_lines}, reference paragraphs {doc.reference_paras}, "
                         f"block quotes {doc.block_quotes}, quotations omitted {doc.quotes_removed}, "
                         f"repeated paragraphs dropped {doc.duplicate_paras}")
        lines += ["", f"## Merged near-duplicates ({len(outcome.merged)})"]
        for dropped, kept, score in sorted(outcome.merged):
            lines.append(f"- `{dropped}` merged into `{kept}` (5-word shingle Jaccard {score})")
        lines += ["", f"## Excluded ({len(excluded)})"]
        for name, why in excluded:
            lines.append(f"- `{name}`: {why}")
        lines += ["", f"## Archived outputs ({len(stale)})"]
        for rel in stale:
            lines.append(f"- {PurePosixPath(rel).name}: its sample is no longer kept; moved to 90-archive/")
        if foreign:
            lines += ["", f"## Left alone ({len(foreign)})"]
            for rel in foreign:
                lines.append(f"- {PurePosixPath(rel).name}: not written by voice-profile; not changed")
        return fm.render(meta, "\n".join(lines) + "\n")

    def stats_text(self, body: str, samples: int, today: str) -> str:
        meta = self._header(
            title="Style statistics", source=f"voice-profile (computed in code from {samples} cleaned sample(s))",
            today=today, summary="Sentence, paragraph, vocabulary, punctuation, person, contraction, "
                                 "passive and citation figures for Noah's writing.",
            tags=["writing", "voice-profile"], related=[MANIFEST])
        return fm.render(meta, body)

    # ------------------------------------------------------------------ writes

    def _existing(self, lib: Library, rel: str) -> tuple[str | None, fm.Meta | None]:
        path = lib.abs_path(rel)
        parent = path.parent
        if os.path.isdir(parent):
            for name in os.listdir(parent):
                if name != path.name and name.casefold() == path.name.casefold():
                    raise PathRefused(f"{rel} collides with the existing {name} (names are case-insensitive on NTFS)")
        text = read_text(lib, rel)
        return text, header_of(text)

    def _keep_added(self, text: str, existing: fm.Meta | None) -> str:
        """Keep the existing file's ``added`` date, so an unchanged rebuild gives identical bytes.
        Only the header's ``added:`` line changes; the rest of the text is kept byte for byte."""
        if existing and existing.get("generator") == GENERATOR and fm.is_iso_date(existing.get("added")):
            head, sep, body = text.partition("\n---\n")
            head = re.sub(r"(?m)^added: \d{4}-\d{2}-\d{2}$", f"added: {existing['added']}", head, count=1)
            return head + sep + body
        return text

    def plan_write(self, lib: Library, rel: str, text: str, *, managed: bool = True) -> tuple[str, bytes] | None:
        """(rel, bytes) to write, None if unchanged. ``managed`` files must carry our generator key."""
        current, meta = self._existing(lib, rel)
        if current is not None and managed and (meta is None or meta.get("generator") != GENERATOR):
            raise BuildError(f"{rel} exists and was not written by voice-profile; not replacing it")
        text = self._keep_added(text, meta)
        data = text.encode("utf-8")
        if current is not None and current.encode("utf-8") == data:
            return None
        return rel, data

    @staticmethod
    def _stale(lib: Library, keep: set[str]) -> tuple[list[str], list[str]]:
        """(our outputs to archive, files not ours that are left alone) in samples-text/."""
        try:
            folder = lib.abs_path(TEXTS)
            names = sorted(os.listdir(folder))
        except FileNotFoundError:
            return [], []
        stale, foreign = [], []
        for name in names:
            rel = f"{TEXTS}/{name}"
            if rel in keep or rel.casefold() == MANIFEST.casefold() or name.startswith("."):
                continue
            entry = os.lstat(folder / name)
            meta = None
            if stat.S_ISREG(entry.st_mode) and name.endswith(".md"):
                meta = header_of(read_text(lib, rel))
            if meta is not None and meta.get("generator") == GENERATOR:
                stale.append(rel)
            else:
                foreign.append(rel)
        return stale, foreign

    # ------------------------------------------------------------------ the build

    def build(self, *, dry_run: bool = False, no_ai: bool = False, out: TextIO | None = None) -> Report:
        lib = self.open()
        report = Report(dry_run=dry_run)
        today = lib.clock().date().isoformat()

        samples, extra_excluded = self.read_samples(lib)
        include = parse_include(read_text(lib, INCLUDE, INCLUDE_MAX) or "")
        outcome = clean_all(samples, include=include, threshold=self.cfg["dedupe_jaccard"],
                            min_words=self.cfg["min_words"])
        names = self.output_names(outcome.kept)
        report.kept = [(d.name, names[d.name], d.words) for d in sorted(outcome.kept, key=lambda d: d.name)]
        report.merged = list(outcome.merged)
        report.excluded = sorted(extra_excluded + outcome.excluded)

        stale, foreign = self._stale(lib, set(names.values()))
        st = statsmod.compute([d.paragraphs for d in outcome.kept], [d.reference_heading for d in outcome.kept])
        stats_body = statsmod.render(st)

        files = [(names[d.name], self.sample_text(d, today)) for d in sorted(outcome.kept, key=lambda d: d.name)]
        files.append((MANIFEST, self.manifest_text(outcome, names, stale, foreign, extra_excluded, today)))
        files.append((STATS, self.stats_text(stats_body, len(outcome.kept), today)))
        planned = []
        for rel, text in files:
            item = self.plan_write(lib, rel, text)
            if item is None:
                report.unchanged.append(rel)
            else:
                planned.append(item)
                report.written.append(rel)
        if planned:
            warning = lib.cap_gate(sum(len(data) for _, data in planned))
            if warning:
                log.warning("%s", warning)
        if not dry_run:
            # Archive first: a stale text must never stay beside a manifest that says it is gone
            # (the writing tool reads samples-text/). If archiving fails, nothing new is written.
            for rel in stale:
                try:
                    report.archived.extend(archive(lib, rel))
                except OSError as exc:
                    raise BuildError(f"cannot archive {rel} ({exc.strerror or type(exc).__name__}); "
                                     "nothing new written") from None
            lib.ensure_dir(TEXTS)
            for rel, data in planned:
                atomic_write_bytes(lib.abs_path(rel), data)
        else:
            report.archived = [(rel, "(would be archived)") for rel in stale]

        if no_ai:
            report.profile = "skipped (--no-ai)"
        else:
            self.draft(lib, outcome, st, stats_body, report, dry_run=dry_run, out=out)
        return report

    # ------------------------------------------------------------------ step 4: draft

    def draft(self, lib: Library, outcome: Outcome, st: statsmod.Stats, stats_body: str, report: Report,
              *, dry_run: bool, out: TextIO | None) -> None:
        try:
            target = profile_target(lib)
        except BuildError as exc:  # both files are protected: nothing to ask the model for
            report.profile = f"not drafted: {exc}"
            report.errors.append(report.profile)
            return
        excerpts = promptmod.pick_excerpts(outcome.kept, self.cfg["excerpts"], self.cfg["excerpt_chars"],
                                           st.sentence_words.median)
        if len(excerpts) < MIN_EXCERPTS:
            report.profile = (f"not drafted: only {len(excerpts)} usable excerpt(s) in the kept samples "
                              f"(at least {MIN_EXCERPTS} are needed)")
            report.errors.append(report.profile)
            return
        built = promptmod.build(stats_text=stats_body, excerpts=excerpts)
        try:
            answer = self.broker.chat(built.system, built.user)
        except BrokerUnreachable as exc:
            report.profile = f"not drafted: {exc}"
            report.errors.append(report.profile)
            return
        except BrokerError as exc:
            report.profile = f"not drafted: {exc}"
            report.errors.append(report.profile)
            return
        limit = self.cfg["num_ctx"] - PROMPT_MARGIN
        if answer.prompt_eval_count is not None and answer.prompt_eval_count >= limit:
            report.profile = (f"not drafted: prompt truncated: the model read {answer.prompt_eval_count} prompt "
                              f"tokens (num_ctx {self.cfg['num_ctx']}, limit {limit}); lower excerpt_chars "
                              "or raise num_ctx")
            report.errors.append(report.profile)
            return
        try:
            profile = validate(answer.content, today=lib.clock().date().isoformat(), model=self.cfg["model"],
                               samples=len(outcome.kept))
        except Invalid as exc:
            report.profile = f"not drafted: invalid: {exc}"
            report.errors.append(report.profile)
            return
        if dry_run:
            report.profile = f"valid draft ({len(profile.text.encode('utf-8'))} bytes); would write {target}"
            if out is not None:
                out.write(profile.text)
            return
        # The model can take many minutes: Noah may have approved a profile meanwhile. Decide again.
        target = profile_target(lib)
        item = self.plan_write(lib, target, profile.text, managed=False)
        if item is None:
            report.profile = f"unchanged: {target}"
            return
        lib.cap_gate(len(item[1]))
        if profile_target(lib) != target:  # defence in depth: the status changed while we planned
            raise BuildError(f"{target} changed while writing; nothing written; run again")
        status, ident = profile_state(lib, target)
        if status not in (None, "draft"):
            raise BuildError(f"{target} changed while writing; nothing written; run again")
        if ident is not None:
            # A draft Noah may have edited: keep it in 90-archive/ rather than replacing it. Copied,
            # never renamed, and removed only if it is still the very file just read as a draft
            # (Noah may save it as approved at any moment; then it stays and nothing is written).
            try:
                report.archived.extend(copy_archive(lib, target, expect=ident))
            except (OSError, LibraryError) as exc:
                why = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc) or type(exc).__name__
                raise BuildError(f"cannot archive the previous {target} ({why}); nothing written") from None
        try:
            write_new(lib, target, item[1])      # only if the name is still absent: never over a new file
        except FileExistsError:
            raise BuildError(f"{target} appeared while writing (Noah saved one?); it was kept and "
                             "nothing was written; run again") from None
        report.written.append(target)
        report.profile = f"wrote {target} (status: draft)"

    # ------------------------------------------------------------------ status

    def status(self) -> list[str]:
        lib = self.open()
        lines = [f"library: {lib.root} (sentinel present)"]
        try:
            folder = lib.abs_path(SAMPLES)
            docx = [n for n in os.listdir(folder) if n.casefold().endswith(".docx") and not n.startswith((".", "~$"))]
            lines.append(f"samples: {len(docx)} .docx file(s) in {SAMPLES}/")
        except FileNotFoundError:
            lines.append(f"samples: {SAMPLES}/ is missing")
        try:
            texts = [n for n in os.listdir(lib.abs_path(TEXTS)) if n.endswith(".md") and n != "MANIFEST.md"]
            lines.append(f"samples-text: {len(texts)} cleaned text(s)"
                         + ("" if os.path.lexists(lib.abs_path(MANIFEST)) else "; MANIFEST.md missing"))
        except FileNotFoundError:
            lines.append("samples-text: not built yet")
        meta = header_of(read_text(lib, STATS))
        lines.append(f"style-stats.md: {'added ' + str(meta.get('added')) if meta else 'missing'}")
        for rel in (PROFILE, NEXT):
            status = profile_status(lib, rel)
            lines.append(f"{PurePosixPath(rel).name}: {'missing' if status is None else 'status ' + status}")
        try:
            lines.append(f"next draft goes to: {PurePosixPath(profile_target(lib)).name}")
        except BuildError as exc:
            lines.append(f"next draft: none ({exc})")
        lines.append(f"broker: {'reachable' if self.broker.reachable() else 'unreachable'}")
        return lines
