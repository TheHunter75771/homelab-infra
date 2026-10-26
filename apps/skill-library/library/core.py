"""The AI Library: layout, file, inbox, index, cap, archive and check (CONTRACTS §12.11).

Invariants every operation keeps:

* **Confined.** Every path we write is built from validated names under ``root`` and checked
  component by component with ``lstat`` just before use: a symlink already on the way is
  refused, as are ``..`` and absolute paths outside the root. This is a check, not a lock: a
  local process that swaps a folder for a symlink *between* the check and the write can still
  redirect it (see README "Limits"). The threat model is one writer and no hostile local user.
* **Never deletes.** Items only move (inbox, archive) or are copied in (file). The only
  unlinks are our own temp files and empty name placeholders that never received content.
* **Never overwrites.** New names are claimed with O_EXCL after a case-insensitive check; a
  taken name gets ``-2``, ``-3``, ... (NTFS: ``Foo.md`` and ``foo.md`` are the same file).
* **Atomic.** Content is written to a temp file in the same folder and renamed into place.

The library assumes one writer at a time (the CLI or one Phase 5 feature); O_EXCL keeps even a
race from overwriting, but it does not serialize whole operations. Inbox moves are bracketed in
the inbox log so an interrupted run is finished or rolled back by the next one.
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import os
import re
import secrets
import stat
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from collections.abc import Callable, Iterator

from . import frontmatter as fm
from . import naming
from .readme import README_TEXT
from .safefs import (
    TMP_PREFIX,
    CapExceeded,
    LibraryError,
    PathRefused,
    append_text,
    atomic_copy,
    atomic_write_bytes,
    release,
    reserve,
)

INBOX = "00-inbox"
FORGE = "35-forge"    # the Skill Forge's request folders (module F; not indexed or checked here)
SHELF = "95-shelf"    # denied skills kept by the forge
SOURCES = "10-sources"
NOTES = "20-notes"
SKILLS = "30-skills"
WRITING = "40-writing"
FINANCE = "50-finance"
ARCHIVE = "90-archive"
LAYOUT_DIRS = (
    INBOX, SOURCES, NOTES, SKILLS, WRITING, f"{WRITING}/samples", f"{WRITING}/drafts",
    FINANCE, f"{FINANCE}/log", ARCHIVE, FORGE, SHELF,
)
TOP_DIRS = frozenset(d.split("/")[0] for d in LAYOUT_DIRS)
ROOT_FILES = ("README.md", "INDEX.md")
INBOX_LOG = f"{ARCHIVE}/inbox-log.md"
WRITING_SUBDIRS = frozenset({"samples", "drafts"})
TOPIC_SECTIONS = frozenset({SOURCES, NOTES, SKILLS})  # <section>/<kebab-name>/<item>
FORGE_OWNED = frozenset({FORGE, SHELF})  # the forge keeps its own layout; `forge status` covers them
SKILL_CODE = "code"  # 30-skills/<skill>/code/** holds a skill's code: any names, any depth
# Every *.md page at the top of 30-skills/<skill>/ (SKILL.md, manifest.md, CHANGELOG.md, rules.md,
# examples.md, ...) is named by convention, not dated; each still needs the header (Lead, 2026-10-26).
SKILL_EXEMPT = frozenset({"SKILL.md", "manifest.md", "CHANGELOG.md"})  # kept for callers; see above

DEFAULT_CAP = 500 * 10**9  # 500 GB, decimal like the drive's label
WARN_FRACTION = 0.9
# Files Windows and macOS clients drop on shares; the inbox ignores them silently.
OS_JUNK = frozenset({"desktop.ini", "thumbs.db"})

Clock = Callable[[], dt.datetime]


def system_clock() -> dt.datetime:
    return dt.datetime.now().astimezone()


@dataclass(frozen=True)
class CapStatus:
    usage: int
    cap: int

    @property
    def percent(self) -> float:
        return 100.0 * self.usage / self.cap

    @property
    def status(self) -> str:
        if self.usage >= self.cap:
            return "full"
        if self.usage >= WARN_FRACTION * self.cap:
            return "warn"
        return "ok"

    def describe(self) -> str:
        return (f"{naming.format_size(self.usage)} of {naming.format_size(self.cap)} "
                f"({self.percent:.1f} %)")


@dataclass(frozen=True)
class Violation:
    path: str
    kind: str
    message: str

    def __str__(self) -> str:
        return f"{self.kind}\t{self.path}: {self.message}"


@dataclass
class FileResult:
    item: str                 # library-relative path of the filed item
    sidecar: str | None       # its sidecar, for non-Markdown items
    warning: str | None = None


@dataclass
class InboxResult:
    moved: list[tuple[str, str]] = field(default_factory=list)  # (inbox path, new path)
    left: list[tuple[str, str]] = field(default_factory=list)   # (inbox path, reason)
    warning: str | None = None
    refused: bool = False  # stopped early because the cap was reached
    problems: list[str] = field(default_factory=list)  # log/rollback trouble after a move started
    repaired: list[tuple[str, str]] = field(default_factory=list)  # (inbox path, outcome) from a past run


class _LogBroken(LibraryError):
    """The inbox log could not record a move; nothing more may move."""


class _NeedsHuman(LibraryError):
    """An interrupted move cannot be finished safely; it stays pending until a person resolves it."""


_LOG_ESCAPE_RE = re.compile(r"\\(\\|x[0-9a-f]{2}|u[0-9a-f]{4})")
_MOVING_RE = re.compile(
    r"^- (?P<stamp>\S+) moving \[(?P<id>m-[0-9a-f]{8})\] `(?P<src>(?:[^`\\]|\\.)*)` -> `(?P<dest>(?:[^`\\]|\\.)*)`"
    r"(?: \(with sidecar `(?P<sc>(?:[^`\\]|\\.)*)`\))?(?: \(inode (?P<ino>\d+)\))?$"
)
_RESOLVED_RE = re.compile(r"^- \S+ (?:done|undone) \[(?P<id>m-[0-9a-f]{8})\]")


def _log_quote(text: str) -> str:
    """A path as a backticked log field that cannot break out of its line or its backticks."""
    out = []
    for ch in text:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == "`":
            out.append("\\x60")
        elif o < 0x20 or 0x7F <= o <= 0x9F or ch in "\u2028\u2029" or 0xD800 <= o <= 0xDFFF:
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    return "`" + "".join(out) + "`"


def _log_unquote(field_text: str) -> str:
    def sub(m: re.Match[str]) -> str:
        esc = m.group(1)
        return "\\" if esc == "\\" else chr(int(esc[1:], 16))
    return _LOG_ESCAPE_RE.sub(sub, field_text)


@dataclass(frozen=True)
class _Entry:
    rel: PurePosixPath
    kind: str  # "file" | "dir" | "symlink" | "other"
    size: int


def parse_topic_map(text: str) -> list[tuple[str, str]]:
    """Parse a topic map: one ``<topic> <glob>`` per line, ``#`` comments, first match wins.

    The glob is matched case-insensitively against the inbox file name, e.g.
    ``rocm *rocm*`` or ``papers *.pdf``.
    """
    rules: list[tuple[str, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise LibraryError(f"topic map line {lineno}: expected '<topic> <glob>', got {raw!r}")
        topic, pattern = parts[0], parts[1].strip()
        if not naming.is_kebab_name(topic):
            raise LibraryError(f"topic map line {lineno}: {topic!r} is not a kebab-case topic name")
        rules.append((pattern, topic))
    return rules


class Library:
    """One library root. Use ``Library.init`` to create it and ``Library.open`` to use it."""

    def __init__(self, root: str | os.PathLike[str], *, clock: Clock | None = None,
                 cap_bytes: int = DEFAULT_CAP) -> None:
        given = Path(os.path.abspath(os.fspath(root)))
        if not given.is_dir():
            raise LibraryError(f"library root {given} is not a directory")
        # The root itself may be a symlink (e.g. a stable path to the mount); we resolve it once
        # and then refuse symlinks *inside* it.
        self.root = Path(os.path.realpath(given))
        self._given_root = given
        self.clock: Clock = clock or system_clock
        if cap_bytes <= 0:
            raise LibraryError("the cap must be more than 0 bytes")
        self.cap_bytes = cap_bytes

    # ------------------------------------------------------------------ construction

    @classmethod
    def init(cls, root: str | os.PathLike[str], **kwargs) -> tuple[Library, list[str]]:
        """Create the layout (idempotent). Returns the library and what was created.

        The root's parent must exist (so an unmounted share does not silently get a fresh
        library on the wrong disk), and a non-empty root must already look like a library.
        Existing files, including a hand-edited README.md, are never overwritten.
        """
        path = Path(os.path.abspath(os.fspath(root)))
        created: list[str] = []
        if not os.path.lexists(path):
            if not path.parent.is_dir():
                raise LibraryError(f"{path.parent} does not exist; refusing to create the library there")
            path.mkdir()
            created.append(f"{path}/")
        elif path.is_dir():
            entries = os.listdir(path)
            if entries and not (set(entries) & (TOP_DIRS | set(ROOT_FILES))):
                raise LibraryError(f"{path} is not empty and is not a library; use an empty folder")
        lib = cls(path, **kwargs)
        for rel in LAYOUT_DIRS:
            if not lib._exists(rel):
                lib._ensure_dir(rel)
                created.append(f"{rel}/")
        if not lib._exists("README.md"):
            lib._write_new("README.md", README_TEXT)
            created.append("README.md")
        if not lib._exists(INBOX_LOG):
            lib._write_new(INBOX_LOG, lib._inbox_log_header())
            created.append(INBOX_LOG)
        changed, _ = lib.index()
        if changed:
            created.append("INDEX.md")
        return lib, created

    @classmethod
    def open(cls, root: str | os.PathLike[str], **kwargs) -> Library:
        """Open an initialized library; refuses a folder without the layout (e.g. unmounted)."""
        lib = cls(root, **kwargs)
        missing = []
        for rel in LAYOUT_DIRS:
            try:
                st = os.lstat(lib.root / rel)
            except FileNotFoundError:
                missing.append(rel)
                continue
            if not stat.S_ISDIR(st.st_mode):
                missing.append(rel)
        if missing:
            raise LibraryError(
                f"{lib.root} is not an initialized AI library (missing or not a real folder: "
                f"{', '.join(missing)}); run init"
            )
        return lib

    # ------------------------------------------------------------------ path safety

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def abs_path(self, rel: str | PurePosixPath) -> Path:
        """Library-relative POSIX path -> absolute path, after refusing anything unsafe."""
        parts = PurePosixPath(rel).parts
        if not parts:
            raise PathRefused("empty path")
        for part in parts:
            if part in ("", ".", "..", "/") or "\\" in part or "\0" in part:
                raise PathRefused(f"refusing path {str(rel)!r}: {part!r} is not allowed")
        path = self.root.joinpath(*parts)
        self._refuse_symlinks(path)
        return path

    def _refuse_symlinks(self, path: Path) -> None:
        """Refuse if any existing component from the root down to ``path`` is a symlink."""
        cur = self.root
        for part in path.relative_to(self.root).parts:
            cur = cur / part
            try:
                mode = os.lstat(cur).st_mode
            except FileNotFoundError:
                return
            if stat.S_ISLNK(mode):
                raise PathRefused(f"refusing to follow the symlink {self._rel(cur)}")
        # Defence in depth: the fully resolved path must still be inside the root.
        real = Path(os.path.realpath(path))
        if real != self.root and self.root not in real.parents:
            raise PathRefused(f"{path} resolves outside the library")

    def resolve_user_path(self, raw: str) -> Path:
        """A path typed by a user: absolute (must be inside the root) or relative to the root."""
        p = Path(raw)
        if ".." in p.parts:
            raise PathRefused(f"refusing path {raw!r}: '..' is not allowed")
        if p.is_absolute():
            p = Path(os.path.normpath(p))
            for base in (self.root, self._given_root):
                try:
                    p = p.relative_to(base)
                    break
                except ValueError:
                    continue
            else:
                raise PathRefused(f"{raw} is outside the library {self.root}")
        parts = [x for x in p.parts if x not in ("", ".")]
        if not parts:
            raise PathRefused(f"refusing path {raw!r}: that is the library root")
        return self._abs(PurePosixPath(*parts))

    def _exists(self, rel: str) -> bool:
        return os.path.lexists(self._abs(rel))

    @staticmethod
    def _casefold_clash(directory: Path, name: str) -> str | None:
        for existing in os.listdir(directory):
            if existing != name and existing.casefold() == name.casefold():
                return existing
        return None

    def ensure_dir(self, rel: str) -> Path:
        """mkdir -p inside the library, refusing symlinks and case-insensitive clashes."""
        target = self._abs(rel)
        cur = self.root
        for part in target.relative_to(self.root).parts:
            nxt = cur / part
            try:
                st = os.lstat(nxt)
            except FileNotFoundError:
                clash = self._casefold_clash(cur, part)
                if clash:
                    raise PathRefused(
                        f"{self._rel(nxt)} collides with the existing {self._rel(cur / clash)} "
                        "(names are case-insensitive on NTFS)"
                    ) from None
                os.mkdir(nxt)
            else:
                if stat.S_ISLNK(st.st_mode):
                    raise PathRefused(f"refusing to follow the symlink {self._rel(nxt)}")
                if not stat.S_ISDIR(st.st_mode):
                    raise LibraryError(f"{self._rel(nxt)} exists and is not a folder")
            cur = nxt
        return target

    def write_new(self, rel: str, text: str) -> None:
        """Create a file that must not exist yet (used for README.md and the inbox log)."""
        path = self._abs(rel)
        if not reserve(path.parent, [path.name]):
            raise LibraryError(f"{rel} already exists")
        try:
            atomic_write_bytes(path, text.encode("utf-8"))
        except BaseException:
            release([path])
            raise

    def move_dir(self, src_rel: str, dest_rel: str) -> None:
        """Rename a folder inside the library (creating the destination's parents).

        Refuses symlinks on either path, a source that is not a folder, and a destination that
        exists in any letter case. rename(2) refuses a non-empty target and could only replace an
        empty folder that appeared in the meantime, so nothing can be lost.
        """
        src = self.abs_path(src_rel)
        dest = self.abs_path(dest_rel)
        try:
            mode = os.lstat(src).st_mode
        except FileNotFoundError:
            raise LibraryError(f"{src_rel} does not exist") from None
        if not stat.S_ISDIR(mode):
            raise LibraryError(f"{src_rel} is not a folder")
        parent = self.ensure_dir(PurePosixPath(dest_rel).parent.as_posix())
        if dest.name.casefold() in {n.casefold() for n in os.listdir(parent)}:
            raise PathRefused(f"{dest_rel} already exists (names are case-insensitive on NTFS)")
        os.rename(src, dest)

    # ------------------------------------------------------------------ helpers

    def _today(self) -> str:
        return self.clock().date().isoformat()

    @staticmethod
    def _check_kebab(name: str, what: str) -> None:
        if not naming.is_kebab_name(name):
            raise LibraryError(
                f"{what} {name!r} is not a valid name: use lowercase letters, digits and single "
                f"hyphens, at most {naming.MAX_DIR_NAME} characters (e.g. home-network)"
            )

    @staticmethod
    def _read_text(path: Path, label: str) -> str:
        try:
            return path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            raise LibraryError(f"{label} is not UTF-8 text") from None

    @staticmethod
    def _split(text: str, label: str) -> tuple[fm.Meta, str]:
        """Existing front-matter and body; refuses a malformed header rather than guessing.

        Text without a header comes back as the body with a blank line in front, so the new
        header and the original text are visually separated.
        """
        if text.startswith("\ufeff"):
            text = text[1:]  # a BOM would otherwise end up in the middle of the file
        if not fm.has_header(text):
            return {}, "\n" + text
        try:
            return fm.parse(text)
        except fm.FrontMatterError as exc:
            raise LibraryError(f"{label}: malformed front-matter ({exc}); fix or remove the header") from None

    def _compose(self, existing: fm.Meta, *, title: str, source: str,
                 tags: list[str] | None, summary: str | None) -> fm.Meta:
        """The header for a newly filed item: existing values, overridden by what was given."""
        def as_list(value: object) -> list[str]:
            if isinstance(value, list):
                return [v for v in value if v]
            return [value] if isinstance(value, str) and value else []

        meta: fm.Meta = dict(existing)  # keeps extra keys (e.g. a SKILL.md's name/description)
        meta["title"] = title
        meta["source"] = source
        meta["added"] = self._today()  # the day it entered the library, same as the file name
        meta["tags"] = tags if tags is not None else as_list(existing.get("tags"))
        old_summary = existing.get("summary")
        meta["summary"] = summary if summary is not None else (old_summary if isinstance(old_summary, str) else "")
        meta["related"] = as_list(existing.get("related"))
        return meta

    @staticmethod
    def _existing_str(meta: fm.Meta, key: str) -> str | None:
        value = meta.get(key)
        return value if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _sidecar_text(meta: fm.Meta, item_name: str, body: str | None = None) -> str:
        if body is None:
            body = f"\nSidecar for [{item_name}]({urllib.parse.quote(item_name)}).\n"
        return fm.render(meta, body)

    def _reserve_item(self, directory: Path, date: str, slug: str, ext: str, *, sidecar: bool) -> str:
        for n in range(1, 10_000):
            name = naming.item_name(date, slug, ext, n)
            names = [name, f"{name}.md"] if sidecar else [name]
            if reserve(directory, names):
                return name
        raise LibraryError(f"too many items named {date}_{slug}.{ext}")

    def cap_gate(self, incoming: int, usage: int | None = None) -> str | None:
        """Refuse at 100 %, warn at 90 %, counting the bytes about to be added."""
        usage = self.usage_bytes() if usage is None else usage
        projected = CapStatus(usage + incoming, self.cap_bytes)
        if usage >= self.cap_bytes or projected.usage > self.cap_bytes:
            raise CapExceeded(
                f"refusing: the library would be at {projected.describe()} of its cap. Move items "
                "out of the library by hand or raise the cap (--cap / AI_LIBRARY_CAP)."
            )
        if projected.status != "ok":
            return f"the library is at {projected.describe()} of its cap"
        return None

    # ------------------------------------------------------------------ walking

    def _walk(self, rel: PurePosixPath | None = None) -> Iterator[_Entry]:
        """Every entry under ``rel`` (default: the root) in sorted order, never following symlinks."""
        rel = rel if rel is not None else PurePosixPath()
        directory = self.root.joinpath(*rel.parts)
        with os.scandir(directory) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            child = rel / entry.name
            st = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                yield _Entry(child, "symlink", 0)
            elif stat.S_ISDIR(st.st_mode):
                yield _Entry(child, "dir", 0)
                yield from self._walk(child)
            elif stat.S_ISREG(st.st_mode):
                yield _Entry(child, "file", st.st_size)
            else:
                yield _Entry(child, "other", 0)

    def _is_regular(self, rel: PurePosixPath) -> bool:
        try:
            return stat.S_ISREG(os.lstat(self.root.joinpath(*rel.parts)).st_mode)
        except FileNotFoundError:
            return False

    @staticmethod
    def _sidecar_base(name: str) -> str | None:
        """For "x.pdf.md" return "x.pdf" (a non-Markdown name); otherwise None."""
        if not name.endswith(".md"):
            return None
        base = name[:-3]
        head, dot, ext = base.rpartition(".")
        if not dot or not head or not ext or ext.lower() == "md":
            return None
        return base

    def _is_paired_sidecar(self, rel: PurePosixPath) -> bool:
        base = self._sidecar_base(rel.name)
        return base is not None and self._is_regular(rel.parent / base)

    # ------------------------------------------------------------------ file

    def file(self, src: str | os.PathLike[str], topic: str, *, title: str | None = None,
             source: str | None = None, tags: list[str] | None = None,
             summary: str | None = None) -> FileResult:
        """Copy ``src`` into ``10-sources/<topic>/`` under a dated kebab name, with a header.

        A Markdown file keeps its body and gets (or updates) its header; anything else is copied
        byte for byte and gets a sidecar ``<name>.<ext>.md``. Never overwrites: a taken name gets
        ``-2``, ``-3``, ... The original at ``src`` is not touched.
        """
        self._check_kebab(topic, "topic")
        src_path = Path(src)
        try:
            st = os.stat(src_path)
        except OSError as exc:
            raise LibraryError(f"cannot read {src}: {exc.strerror}") from None
        if not stat.S_ISREG(st.st_mode):
            raise LibraryError(f"{src} is not a regular file")
        ext = naming.normalize_ext(src_path.name)
        existing: fm.Meta = {}
        body = ""
        if ext == "md":
            existing, body = self._split(self._read_text(src_path, str(src)), str(src))
        meta = self._compose(
            existing,
            title=title or self._existing_str(existing, "title") or naming.title_from_filename(src_path.name),
            source=source if source is not None else (
                self._existing_str(existing, "source")
                or src_path.name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")),
            tags=tags, summary=summary,
        )
        date, slug = self._today(), naming.slugify(meta["title"])  # type: ignore[arg-type]
        if ext == "md":
            content = fm.render(meta, body).encode("utf-8")
            incoming = len(content)
        else:
            sidecar_bytes = len(self._sidecar_text(meta, naming.item_name(date, slug, ext, 99)).encode("utf-8"))
            incoming = st.st_size + sidecar_bytes
        warning = self._cap_gate(incoming)

        directory = self._ensure_dir(f"{SOURCES}/{topic}")
        name = self._reserve_item(directory, date, slug, ext, sidecar=ext != "md")
        dest = directory / name
        pending = [dest] if ext == "md" else [dest, directory / f"{name}.md"]
        try:
            if ext == "md":
                atomic_write_bytes(dest, content)
                pending.remove(dest)
            else:
                atomic_copy(src_path, dest, max_bytes=st.st_size)  # the size the cap gate counted
                pending.remove(dest)
                atomic_write_bytes(pending[0], self._sidecar_text(meta, name).encode("utf-8"))
                pending.clear()
        except BaseException:
            release(pending)
            raise
        return FileResult(
            item=self._rel(dest),
            sidecar=None if ext == "md" else self._rel(directory / f"{name}.md"),
            warning=warning,
        )

    # ------------------------------------------------------------------ inbox
    #
    # Each move is bracketed in 90-archive/inbox-log.md:
    #
    #   - <time> moving [m-1a2b3c4d] `00-inbox/x.pdf` -> `10-sources/t/<date>_x.pdf` (with sidecar `00-inbox/x.pdf.md`)
    #   - <time> done [m-1a2b3c4d] `00-inbox/x.pdf` -> `10-sources/t/<date>_x.pdf`
    #
    # or "undone [id] <reason>" when it was rolled back. A "moving" line with no "done"/"undone"
    # means the tool was interrupted; the next `inbox` run finishes or rolls back that move first.

    def _inbox_log_header(self) -> str:
        meta: fm.Meta = {
            "title": "Inbox log",
            "source": "library tool",
            "added": self._today(),
            "tags": ["log"],
            "summary": "Every move out of 00-inbox, oldest first. Written by the library tool.",
            "related": [],
        }
        return fm.render(meta, "\n")

    def _inbox_log_text(self) -> str:
        """Pre-flight for the log: it must be a regular UTF-8 file we can append to.

        Called before anything moves, so a broken log (e.g. re-saved as UTF-16) refuses the whole
        run instead of leaving moves unrecorded.
        """
        path = self._abs(INBOX_LOG)
        if not os.path.lexists(path):
            self._write_new(INBOX_LOG, self._inbox_log_header())
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise LibraryError(f"{INBOX_LOG} is not a regular file; fix it before running inbox")
        try:
            text = self._read_text(path, INBOX_LOG)
        except LibraryError:
            raise LibraryError(f"{INBOX_LOG} is not UTF-8 text (re-save it as UTF-8); nothing was moved") from None
        if not os.access(path, os.W_OK):
            raise LibraryError(f"{INBOX_LOG} is not writable; nothing was moved")
        return text

    def _append_inbox_log(self, lines: list[str]) -> None:
        append_text(self._abs(INBOX_LOG), "".join(lines))

    def _log_event(self, event: str, move_id: str, detail: str) -> str:
        line = f"- {self.clock().isoformat(timespec='seconds')} {event} [{move_id}] {detail}\n"
        self._append_inbox_log([line])
        return line

    @staticmethod
    def _match_topic(name: str, topic_map: list[tuple[str, str]]) -> str | None:
        for pattern, topic in topic_map:
            if fnmatch.fnmatchcase(name.casefold(), pattern.casefold()):
                return topic
        return None

    def _inbox_meta(self, text: str, label: str, name: str, date: str) -> tuple[fm.Meta, str]:
        """The header an inbox item will carry, or LibraryError if it cannot be carried losslessly.

        Missing fields are added; nothing present is changed. A header with comments, non-text
        title/source/summary, non-list tags/related, or an ``added`` other than ``date`` is
        refused: the inbox moves (it does not copy), so a rewrite must never lose anything.
        ``file`` copies instead and may merge.
        """
        if text.startswith("﻿"):
            text = text[1:]
        if not fm.has_header(text):
            existing: fm.Meta = {}
            body = "\n" + text
        else:
            try:
                existing, body, comments = fm.parse_detailed(text)
            except fm.FrontMatterError as exc:
                raise LibraryError(f"malformed front-matter in {label} ({exc}); fix the header") from None
            if comments:
                raise LibraryError(
                    f"the header of {label} has comments (line {comments[0]}) that a rewrite would drop; "
                    "remove them, or use `file` (which copies)")
            for key in ("title", "source", "summary"):
                if key in existing and not isinstance(existing[key], str):
                    raise LibraryError(
                        f"'{key}' in {label} is a list; filing would lose part of it. Make it one text "
                        "value, or use `file` (which copies)")
            for key in ("tags", "related"):
                if key in existing and not isinstance(existing[key], list):
                    raise LibraryError(f"'{key}' in {label} must be a list like [a, b]; fix the header")
            if "added" in existing and existing["added"] != date:
                raise LibraryError(
                    f"{label} says added: {existing['added']}, but the inbox files items as added {date}; "
                    "remove the line, or use `file` (which copies)")
        meta: fm.Meta = dict(existing)
        title = existing.get("title")
        if not (isinstance(title, str) and title.strip()):
            meta["title"] = naming.title_from_filename(name)
        meta.setdefault("source", f"inbox: {name}")
        meta["added"] = date
        meta.setdefault("tags", [])
        meta.setdefault("summary", "")
        meta.setdefault("related", [])
        return meta, body

    def _complete_header(self, target: Path, inbox_name: str, *, generated_sidecar: bool) -> None:
        """Give a moved Markdown file (or sidecar) its complete header, atomically and losslessly.

        Idempotent, so an interrupted run can call it again. An empty ``generated_sidecar`` file
        (our placeholder) becomes a fresh sidecar.
        """
        date = target.name[:10]
        label = self._rel(target)
        try:
            text = self._read_text(target, label)
        except FileNotFoundError:
            text = ""
        if generated_sidecar and not text:
            meta, _ = self._inbox_meta("", label, inbox_name, date)
            new = self._sidecar_text(meta, target.name[:-3])
        else:
            meta, body = self._inbox_meta(text, label, inbox_name, date)
            new = fm.render(meta, body)
        if new != text:
            atomic_write_bytes(target, new.encode("utf-8"))

    def inbox(self, *, topic_map: list[tuple[str, str]] | None = None,
              default_topic: str | None = None) -> InboxResult:
        """Move every top-level file out of ``00-inbox`` into ``10-sources/<topic>/``.

        The topic is the first ``topic_map`` match, else ``default_topic``; with neither, the
        file stays in the inbox and is reported. A dropped ``x.pdf.md`` next to ``x.pdf`` is
        its sidecar and moves with it. Each move is bracketed in ``90-archive/inbox-log.md``
        (see above); interrupted moves from an earlier run are finished or rolled back first.
        Anything that cannot be filed without loss (symlinks, folders, bad names, headers that a
        rewrite would change) stays in the inbox with a reason in ``left``.
        """
        topic_map = topic_map or []
        if default_topic is not None:
            self._check_kebab(default_topic, "default topic")
        for _, topic in topic_map:
            self._check_kebab(topic, "topic")
        result = InboxResult()
        log_text = self._inbox_log_text()  # refuse before anything moves
        blocked = self._recover_inbox(log_text, result)
        usage = self.usage_bytes()
        result.warning = self._cap_gate(0, usage)
        inbox_dir = self._abs(INBOX)
        names = sorted(os.listdir(inbox_dir))
        present = set(names)
        for name in names:
            if name.startswith(".") or name.casefold() in OS_JUNK:
                continue
            rel = f"{INBOX}/{name}"
            if not naming.is_valid_utf8(name):
                result.left.append((naming.display(rel), "the file name is not valid UTF-8; rename it"))
                continue
            if name in blocked:
                result.left.append((rel, "an interrupted earlier move of this name needs checking by hand "
                                         "(see the problems above)"))
                continue
            try:
                mode = os.lstat(inbox_dir / name).st_mode
            except FileNotFoundError:
                continue  # moved by recovery or someone else since we listed the folder
            base = self._sidecar_base(name)
            if base is not None and stat.S_ISREG(mode):
                if base in present:
                    try:
                        base_mode = os.lstat(inbox_dir / base).st_mode
                    except FileNotFoundError:
                        base_mode = 0
                    if stat.S_ISREG(base_mode):
                        continue  # travels with its item
                    result.left.append((rel, f"sidecar of {INBOX}/{base}, which is not a regular file"))
                else:
                    result.left.append((rel, f"sidecar without its item {base}; put the item back next to it, "
                                             "or rename this note"))
                continue
            if stat.S_ISLNK(mode):
                result.left.append((rel, "symlinks are refused; copy the real file into the inbox"))
                continue
            if stat.S_ISDIR(mode):
                result.left.append((rel, "folders are not filed; put the files themselves in the inbox"))
                continue
            if not stat.S_ISREG(mode):
                result.left.append((rel, "not a regular file"))
                continue
            topic = self._match_topic(name, topic_map) or default_topic
            if topic is None:
                result.left.append((rel, "no topic: nothing in --topic-map matched and no --default-topic"))
                continue
            paired = self._sidecar_base(f"{name}.md") == name  # only non-Markdown items have sidecars
            sidecar = f"{name}.md" if paired and f"{name}.md" in present else None
            if sidecar is not None and not stat.S_ISREG(os.lstat(inbox_dir / sidecar).st_mode):
                result.left.append((rel, f"its sidecar {sidecar} is not a regular file"))
                continue
            try:
                dest, added, warning = self._inbox_one(inbox_dir, name, sidecar, topic, usage, result)
            except CapExceeded as exc:
                result.left.append((rel, str(exc)))
                result.refused = True
                break  # stop filing; what was already moved stays moved and logged
            except _LogBroken as exc:
                result.left.append((rel, str(exc)))
                result.problems.append(str(exc))
                break  # without a working log nothing else may move
            except (LibraryError, OSError) as exc:
                reason = str(exc) if isinstance(exc, LibraryError) else f"{exc.strerror or exc}; nothing was moved"
                result.left.append((rel, reason))
                continue
            if dest is not None:
                result.warning = warning or result.warning
                result.moved.append((rel, dest))
                usage += added
        return result

    def _inbox_one(self, inbox_dir: Path, name: str, sidecar: str | None, topic: str,
                   usage: int, result: InboxResult) -> tuple[str | None, int, str | None]:
        """Move one inbox file (and its sidecar). Returns (new path or None, bytes added, warning).

        Returns None as the path when the move was rolled back (the reason is in ``result.left``)
        or when only the log's "done" line failed (the move stands; see ``result.problems``).
        """
        ext = naming.normalize_ext(name)
        rel = f"{INBOX}/{name}"
        date = self._today()
        if ext == "md":
            meta, _ = self._inbox_meta(self._read_text(inbox_dir / name, rel), rel, name, date)
        elif sidecar is not None:
            srel = f"{INBOX}/{sidecar}"
            meta, _ = self._inbox_meta(self._read_text(inbox_dir / sidecar, srel), srel, name, date)
        else:
            meta, _ = self._inbox_meta("", rel, name, date)
        # A move adds no bytes; the header (or a new sidecar) and two log lines do. An estimate.
        slug = naming.slugify(meta["title"])  # type: ignore[arg-type]
        header = fm.dump(meta) if ext == "md" else self._sidecar_text(meta, f"{date}_{slug}-99.{ext}")
        log_line = 60 + len(_log_quote(rel)) + len(f"{SOURCES}/{topic}/{date}_{slug}-99.{ext}") + 4
        added = len(header.encode("utf-8")) + 2 * log_line + (len(sidecar) + 40 if sidecar else 0)
        warning = self._cap_gate(added, usage)

        directory = self._ensure_dir(f"{SOURCES}/{topic}")
        new_name = self._reserve_item(directory, date, slug, ext, sidecar=ext != "md")
        dest = directory / new_name
        dest_sc = directory / f"{new_name}.md"
        placeholders = [dest] if ext == "md" else [dest, dest_sc]
        dest_rel = self._rel(dest)
        move_id = f"m-{secrets.token_hex(4)}"
        with_sc = f" (with sidecar {_log_quote(f'{INBOX}/{sidecar}')})" if sidecar else ""
        try:
            # The inode identifies the file after the rename, so recovery only ever finishes a
            # move onto the very file this entry moved (rename keeps the inode; ntfs3 too).
            ino = os.lstat(inbox_dir / name).st_ino
            self._log_event("moving", move_id,
                            f"{_log_quote(rel)} -> {_log_quote(dest_rel)}{with_sc} (inode {ino})")
        except (OSError, LibraryError) as exc:
            release(placeholders)
            raise _LogBroken(f"cannot write {INBOX_LOG} ({exc}); stopped before moving anything else") from None

        def undo(reason: str) -> None:
            release(placeholders)
            try:
                self._log_event("undone", move_id, f"{_log_quote(rel)}: {reason}")
            except (OSError, LibraryError) as exc:
                result.problems.append(f"{rel}: rolled back, but the log could not record it ({exc})")

        try:
            os.replace(inbox_dir / name, dest)  # a move on the same filesystem: atomic
        except OSError as exc:
            undo(f"move failed ({exc.strerror or exc}); nothing moved")
            raise
        if sidecar is not None:
            try:
                os.replace(inbox_dir / sidecar, dest_sc)
            except OSError as exc:
                try:
                    os.replace(dest, inbox_dir / name)  # put the item back: the pair stays together
                except OSError as exc2:
                    result.problems.append(
                        f"{rel}: moved to {dest_rel}, but its sidecar could not follow ({exc.strerror or exc}) "
                        f"and moving it back failed ({exc2.strerror or exc2}); the next inbox run repairs this")
                    return None, added, warning
                undo(f"sidecar move failed ({exc.strerror or exc}); both left in the inbox")
                result.left.append((rel, f"its sidecar could not be moved ({exc.strerror or exc}); nothing moved"))
                return None, added, warning
        target = dest if ext == "md" else dest_sc
        try:
            self._complete_header(target, name, generated_sidecar=ext != "md" and sidecar is None)
            self._log_event("done", move_id, f"{_log_quote(rel)} -> {_log_quote(dest_rel)}")
        except (OSError, LibraryError) as exc:
            result.problems.append(f"{rel}: moved to {dest_rel}, but finishing it failed ({exc}); "
                                   "the next inbox run finishes it")
        return dest_rel, added, warning

    def _recover_inbox(self, log_text: str, result: InboxResult) -> set[str]:
        """Finish moves an earlier run started but never marked done/undone.

        Recovery never unlinks anything: an empty placeholder an interrupted run left behind
        stays for ``check`` to report. Returns inbox names this run must not file, because an
        unresolved earlier move of the same name needs a human first.
        """
        pending: dict[str, tuple[str, str, str, str | None, int | None]] = {}
        for line in log_text.splitlines():
            m = _MOVING_RE.match(line)
            if m:
                pending[m.group("id")] = (
                    m.group("stamp"), _log_unquote(m.group("src")), _log_unquote(m.group("dest")),
                    _log_unquote(m.group("sc")) if m.group("sc") else None,
                    int(m.group("ino")) if m.group("ino") else None)
                continue
            m = _RESOLVED_RE.match(line)
            if m:
                pending.pop(m.group("id"), None)
        blocked: set[str] = set()
        for move_id, (stamp, src, dest, sc, ino) in pending.items():
            try:
                outcome = self._recover_one(move_id, stamp, src, dest, sc, ino)
            except _NeedsHuman as exc:
                result.problems.append(str(exc))
                blocked.update(PurePosixPath(p).name for p in (src, sc) if p)
                continue
            except (OSError, LibraryError) as exc:
                result.problems.append(f"interrupted move {move_id} of {src}: could not repair ({exc})")
                continue
            result.repaired.append((src, outcome))
        return blocked

    def _recover_one(self, move_id: str, stamp: str, src: str, dest: str, sc: str | None,
                     ino: int | None) -> str:
        src_p, dest_p = PurePosixPath(src), PurePosixPath(dest)
        valid = (
            len(src_p.parts) == 2 and src_p.parts[0] == INBOX
            and len(dest_p.parts) == 3 and dest_p.parts[0] == SOURCES and naming.is_kebab_name(dest_p.parts[1])
            and naming.parse_item_name(dest_p.name) is not None
            and dest_p.name[:10] == stamp[:10]  # we only ever reserve names dated the day we log them
            and (sc is None or sc == f"{src}.md")
        )
        if not valid:
            self._log_event("undone", move_id, "the log entry is not a valid inbox move; nothing changed")
            raise LibraryError(f"log entry {move_id} is not a valid inbox move; marked undone, nothing changed")
        src_abs, dest_abs = self._abs(src), self._abs(dest)
        is_md = dest_p.suffix == ".md"
        dest_sc = dest_abs.with_name(dest_abs.name + ".md")

        def state(path: Path) -> os.stat_result | None:
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                return None
            if not stat.S_ISREG(st.st_mode):
                raise LibraryError(f"{self._rel(path)} is not a regular file; check by hand")
            return st

        src_st, dest_st = state(src_abs), state(dest_abs)
        # "Ours" = the very file this entry moved (same inode as recorded before the rename).
        dest_is_moved_item = dest_st is not None and ino is not None and dest_st.st_ino == ino
        if src_st is not None:
            if dest_st is not None and (dest_st.st_size > 0 or dest_is_moved_item):
                raise _NeedsHuman(
                    f"{src} and {dest} both exist: an earlier run was interrupted after moving {src}, and a "
                    f"file of the same name was dropped again. Check by hand: rename or move the new {src}, "
                    "then run inbox again to finish the earlier move.")
            # The item never moved. Whatever empty placeholders that run reserved stay (check
            # reports them); the inbox file is filed normally below, under a fresh name.
            self._log_event("undone", move_id, f"{_log_quote(src)}: interrupted before the move; nothing moved, "
                                               "nothing removed")
            return "interrupted before the move; filed again below"
        if dest_st is None:
            self._log_event("undone", move_id, f"{_log_quote(src)}: found in neither place; nothing changed")
            raise LibraryError(f"{src} is in neither {INBOX} nor {dest}; check by hand")
        if not dest_is_moved_item:
            # Not provably the file this entry moved: a forged/edited line, or a Markdown item whose
            # header was rewritten (new inode) just before a crash. Never guess, never log "undone".
            raise _NeedsHuman(
                f"{dest} is not provably the file move {move_id} moved "
                f"({'no inode recorded' if ino is None else 'its inode differs'}); refused, nothing changed. "
                f"Check {dest} by hand, then append the line '- <time> done [{move_id}] checked by hand' "
                f"to {INBOX_LOG}.")
        if sc is not None:
            sc_abs = self._abs(sc)
            if state(sc_abs) is not None:
                sc_dest_st = state(dest_sc)
                if sc_dest_st is not None and sc_dest_st.st_size > 0:
                    raise _NeedsHuman(f"{sc} and {self._rel(dest_sc)} both exist; merge them by hand, then run "
                                      "inbox again")
                os.replace(sc_abs, dest_sc)  # onto our own empty placeholder for this move
        self._complete_header(dest_abs if is_md else dest_sc, src_p.name,
                              generated_sidecar=not is_md and sc is None)
        self._log_event("done", move_id, f"{_log_quote(src)} -> {_log_quote(dest)} (finished after an interruption)")
        return dest

    # ------------------------------------------------------------------ index

    @staticmethod
    def _md_text(text: str) -> str:
        """Single-line text that is safe inside a Markdown list item / link label."""
        text = " ".join(text.split())
        for ch in "\\[]":
            text = text.replace(ch, "\\" + ch)
        return text

    def _read_meta(self, rel: PurePosixPath) -> fm.Meta | None:
        try:
            text = self.root.joinpath(*rel.parts).read_bytes().decode("utf-8")
            return fm.parse(text)[0]
        except (OSError, UnicodeDecodeError, fm.FrontMatterError):
            return None

    def _index_line(self, rel: PurePosixPath) -> str:
        if rel.name.endswith(".md"):
            meta = self._read_meta(rel)
        else:
            sidecar = rel.parent / f"{rel.name}.md"
            meta = self._read_meta(sidecar) if self._is_regular(sidecar) else None
        title = meta.get("title") if meta else None
        label = title if isinstance(title, str) and title.strip() else rel.name
        line = f"- [{self._md_text(label)}]({urllib.parse.quote(rel.as_posix())})"
        if rel.parts[0] == INBOX:
            return line + " — unfiled"
        if meta is None:
            return line + " — (no valid front-matter; run check)"
        added, summary, tags = meta.get("added"), meta.get("summary"), meta.get("tags")
        if isinstance(added, str) and added:
            line += f" — {self._md_text(added)}"
        if isinstance(summary, str) and summary.strip():
            line += f" — {self._md_text(summary)}"
        if isinstance(tags, list) and tags:
            line += f" — tags: {self._md_text(', '.join(tags))}"
        return line

    def render_index(self) -> tuple[str, int]:
        """INDEX.md content and item count. Depends only on the files, never on the clock."""
        groups: dict[str, list[str]] = {}
        for entry in self._walk():
            if entry.kind != "file" or len(entry.rel.parts) == 1:
                continue
            if any(part.startswith(".") for part in entry.rel.parts):
                continue  # hidden files and anything in hidden folders (.git, .recycle, ...)
            if entry.rel.parts[0] in FORGE_OWNED:
                continue  # forge requests and the shelf: `forge status` lists them
            if not naming.is_valid_utf8(entry.rel.as_posix()):
                continue  # an undecodable name cannot be linked; `check` reports it
            if self._is_paired_sidecar(entry.rel):
                continue
            groups.setdefault(entry.rel.parent.as_posix(), []).append(self._index_line(entry.rel))
        count = sum(len(lines) for lines in groups.values())
        out = [
            "# AI Library index",
            "",
            "Generated by `python3 -m library index`; do not edit by hand. One line per item, grouped by",
            "folder: title, date added, summary, tags. Read `README.md` for the rules.",
            "",
            f"{count} item{'s' if count != 1 else ''}.",
        ]
        for folder in sorted(groups):
            out += ["", f"## {folder}", ""]
            out += sorted(groups[folder])
        return "\n".join(out) + "\n", count

    def index(self) -> tuple[bool, int]:
        """Rebuild INDEX.md. Returns (changed, item count); an unchanged library is not rewritten."""
        text, count = self.render_index()
        data = text.encode("utf-8")
        path = self._abs("INDEX.md")
        try:
            if path.read_bytes() == data:
                return False, count
        except FileNotFoundError:
            pass
        atomic_write_bytes(path, data)
        return True, count

    # ------------------------------------------------------------------ cap

    def usage_bytes(self) -> int:
        """Total size of the regular files under the root (symlinks are not followed)."""
        return sum(e.size for e in self._walk() if e.kind == "file")

    def cap_status(self) -> CapStatus:
        return CapStatus(self.usage_bytes(), self.cap_bytes)

    # ------------------------------------------------------------------ archive

    def archive(self, raw: str) -> list[tuple[str, str]]:
        """Move an item (with its sidecar) or a folder to ``90-archive/<same path>``.

        ``raw`` is relative to the library root, or absolute inside it. Archiving a sidecar
        archives its item too. A taken destination name gets ``-2``, ``-3``, ...
        """
        target = self.resolve_user_path(raw)
        rel = PurePosixPath(self._rel(target))
        if not os.path.lexists(target):
            raise LibraryError(f"{rel} does not exist")
        if rel.parts[0] == ARCHIVE:
            raise LibraryError(f"{rel} is already archived")
        if len(rel.parts) == 1 or rel.as_posix() in LAYOUT_DIRS:
            raise LibraryError(f"{rel} is part of the library layout and cannot be archived")
        mode = os.lstat(target).st_mode
        is_dir = stat.S_ISDIR(mode)
        if not is_dir and not stat.S_ISREG(mode):
            raise LibraryError(f"{rel} is not a file or folder")

        moves: list[PurePosixPath] = [rel]
        if not is_dir:
            base = self._sidecar_base(rel.name)
            if base is not None and self._is_regular(rel.parent / base):
                moves = [rel.parent / base, rel]  # archive the item with its sidecar
            elif self._is_regular(rel.parent / f"{rel.name}.md") and not rel.name.endswith(".md"):
                moves = [rel, rel.parent / f"{rel.name}.md"]
        dest_dir = self._ensure_dir(f"{ARCHIVE}/{rel.parent.as_posix()}")
        primary = moves[0].name

        if is_dir:
            taken = {name.casefold() for name in os.listdir(dest_dir)}
            for n in range(1, 10_000):
                candidate = naming.numbered(primary, n, is_dir=True)
                if candidate.casefold() not in taken:
                    break
            else:
                raise LibraryError(f"too many archived folders named {primary}")
            # No O_EXCL for folders. rename(2) fails on a non-empty folder that appeared in the
            # meantime and could only replace an empty one, so nothing can be lost.
            os.rename(target, dest_dir / candidate)
            return [(rel.as_posix(), self._rel(dest_dir / candidate))]

        for n in range(1, 10_000):
            new_primary = naming.numbered(primary, n, is_dir=False)
            names = [new_primary] + ([f"{new_primary}.md"] if len(moves) == 2 else [])
            if reserve(dest_dir, names):
                break
        else:
            raise LibraryError(f"too many archived items named {primary}")
        done: list[tuple[str, str]] = []
        pending = [dest_dir / n for n in names]
        try:
            for src_rel, new_name in zip(moves, names, strict=True):
                os.replace(self.root.joinpath(*src_rel.parts), dest_dir / new_name)
                pending.remove(dest_dir / new_name)
                done.append((src_rel.as_posix(), self._rel(dest_dir / new_name)))
        except BaseException:
            release(pending)
            raise
        return done

    # ------------------------------------------------------------------ check

    def check(self) -> list[Violation]:
        """Validate layout, naming and front-matter of everything. Reports; never changes anything."""
        found: list[Violation] = []

        def report(path: str, kind: str, message: str) -> None:
            # Printable on one line whatever bytes the name holds (no forged output lines).
            found.append(Violation(naming.display(path), kind, naming.display(message)))

        for rel in LAYOUT_DIRS:
            try:
                mode = os.lstat(self.root / rel).st_mode
            except FileNotFoundError:
                report(rel, "missing-layout", "required folder is missing (run init)")
                continue
            if not stat.S_ISDIR(mode):
                report(rel, "missing-layout", "must be a folder")
        for name in ROOT_FILES:
            if not os.path.lexists(self.root / name):
                report(name, "missing-layout", "missing (run init / index)")

        by_dir: dict[PurePosixPath, list[str]] = {}
        for entry in self._walk():
            rel, parts = entry.rel, entry.rel.parts
            by_dir.setdefault(rel.parent, []).append(rel.name)
            path, name, top = rel.as_posix(), rel.name, parts[0]
            if len(parts) > 1 and top not in TOP_DIRS:
                continue  # inside an unknown top-level entry, which is reported once
            if any(part.startswith(".") for part in parts[:-1]):
                continue  # inside a hidden folder, which is reported once
            if entry.kind == "symlink":
                report(path, "symlink", "symlinks are not allowed in the library (not followed)")
                continue
            if name.startswith(TMP_PREFIX):
                report(path, "temp-file", "leftover temporary file from an interrupted write")
                continue
            if (top == INBOX and len(parts) > 1) or (top == ARCHIVE and len(parts) > 2 and parts[1] == INBOX):
                continue  # unfiled (or archived while unfiled); no naming rules yet
            if top in FORGE_OWNED and len(parts) > 1:
                continue  # the forge's own layout; `forge status` / `forge verify` check it
            if top == SKILLS and len(parts) > 3 and parts[2] == SKILL_CODE:
                continue  # a skill's code/: any names, any depth (the forge pins it by hash)
            if name.startswith("."):
                report(path, "hidden-file", "hidden files do not belong in the library")
                continue
            if not naming.is_valid_utf8(name):
                report(path, "bad-name", "the name is not valid UTF-8; rename it")
                continue
            if entry.kind == "other":
                report(path, "misplaced", "not a regular file or folder")
            elif entry.kind == "dir":
                self._check_dir(parts, report)
            else:
                self._check_file(rel, report)

        for parent, names in by_dir.items():
            seen: dict[str, list[str]] = {}
            for name in names:
                seen.setdefault(name.casefold(), []).append(name)
            for group in seen.values():
                if len(group) > 1:
                    where = (parent / group[0]).as_posix()
                    report(where, "case-collision",
                           f"{', '.join(sorted(group))} differ only in case (one file on NTFS/Windows)")
        return sorted(found, key=lambda v: (v.path, v.kind, v.message))

    @staticmethod
    def _check_dir(parts: tuple[str, ...], report) -> None:
        path, name, top, depth = "/".join(parts), parts[-1], parts[0], len(parts)
        if depth == 1:
            if top not in TOP_DIRS:
                report(path, "unknown-entry", "not part of the library layout")
            return
        if top in TOPIC_SECTIONS:
            if top == SKILLS and depth == 3 and name == SKILL_CODE:
                return  # 30-skills/<skill>/code/
            if depth > 2:
                report(path, "misplaced", "no folders inside a topic or skill folder (a skill may have code/)")
            elif not naming.is_kebab_name(name):
                report(path, "bad-dir-name", "topic/skill names are kebab-case, 1-48 characters")
        elif top == WRITING:
            if depth > 2 or name not in WRITING_SUBDIRS:
                report(path, "misplaced", "40-writing holds only samples/ and drafts/")
        elif top == FINANCE:
            if depth > 2 or name != "log":
                report(path, "misplaced", "50-finance holds only log/")

    def _check_file(self, rel: PurePosixPath, report) -> None:
        parts, path, name, top, depth = rel.parts, rel.as_posix(), rel.name, rel.parts[0], len(rel.parts)
        if depth == 1:
            if name not in ROOT_FILES:
                report(path, "unknown-entry" if top not in TOP_DIRS else "misplaced",
                       "loose file at the library root")
            return  # README.md and INDEX.md are tool files without a header
        if top in TOPIC_SECTIONS:
            placed = depth == 3
        elif top == WRITING:
            placed = depth == 3 and parts[1] in WRITING_SUBDIRS
        elif top == FINANCE:
            placed = depth == 3 and parts[1] == "log"
        else:  # 90-archive keeps whatever path the item had
            placed = True
        if not placed:
            report(path, "misplaced", "items live one folder deep inside their section")

        exempt = path == INBOX_LOG or (top == SKILLS and depth == 3 and name.endswith(".md"))
        base = self._sidecar_base(name)
        size = self._size(rel)
        if base is not None and naming.parse_item_name(base):
            if size == 0 and self._size(rel.parent / base) == 0:
                report(path, "empty-placeholder", f"empty sidecar of the empty {base}: both were reserved by "
                       "an interrupted run and never filled; archive them or fill them by hand")
                return
            if not self._is_regular(rel.parent / base):
                report(path, "orphan-sidecar", f"sidecar without its item {base}")
            self._check_header(rel, report)
        elif exempt or naming.parse_item_name(name):
            if not exempt and size == 0 and (name.endswith(".md") or not self._size(rel.parent / f"{name}.md")):
                report(path, "empty-placeholder", "empty file with no header or sidecar: a name reserved by an "
                       "interrupted run and never filled; archive it or fill it by hand")
                return
            if name.endswith(".md"):
                self._check_header(rel, report)
            elif not self._is_regular(rel.parent / f"{name}.md"):
                report(path, "missing-sidecar", f"non-Markdown item needs a sidecar {name}.md")
        else:
            report(path, "bad-name", f"names follow {naming.ITEM_PATTERN} with a real date")
            if name.endswith(".md"):
                self._check_header(rel, report)

    def _size(self, rel: PurePosixPath) -> int | None:
        """Size of a regular file, or None if it is missing or not a regular file."""
        try:
            st = os.lstat(self.root.joinpath(*rel.parts))
        except FileNotFoundError:
            return None
        return st.st_size if stat.S_ISREG(st.st_mode) else None

    def _check_header(self, rel: PurePosixPath, report) -> None:
        path = rel.as_posix()
        try:
            text = self.root.joinpath(*rel.parts).read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            report(path, "bad-encoding", "Markdown files must be UTF-8")
            return
        if not fm.has_header(text):
            report(path, "missing-front-matter", "must start with a '---' header")
            return
        try:
            meta, _ = fm.parse(text)
        except fm.FrontMatterError as exc:
            report(path, "bad-front-matter", str(exc))
            return
        for kind, message in fm.validate(meta):
            report(path, kind, message)

    # Private names kept as aliases for existing callers (forge).
    _abs = abs_path
    _ensure_dir = ensure_dir
    _write_new = write_new
    _cap_gate = cap_gate
