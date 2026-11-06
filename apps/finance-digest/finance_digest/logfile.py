"""The monthly finance log in the AI Library: ``50-finance/log/YYYY-MM-01_finance-log.md``.

* A new month's file is created once, with the library header (``library.frontmatter``), through
  ``Library.write_new`` (O_EXCL: it never overwrites a file that appeared meanwhile).
* Each day is one block between two marker lines::

      <!-- finance-digest:begin 2030-03-20 -->
      ## 2030-03-20
      ...
      <!-- finance-digest:end 2030-03-20 -->

  A rerun the same day replaces exactly that block; a new day is appended. Every other byte of the
  file (other days, hand-written notes, the header) is kept as it is. The entry text can never hold
  a marker, because every dynamic string has its ``<`` neutralised (``text.clean``).
* Markers that don't pair up (a hand edit, a crash of some other tool) stop the write: it is never
  guessed at.
* Writes are atomic (temp file in the same folder + rename, ``library.safefs``). The path is checked
  by ``Library.abs_path`` (no symlink anywhere from the root down, nothing outside the root), the
  file is read without following a symlink, and it must still be the same file just before the
  rename.

TEMPORARY: the replace-a-dated-section step lives here until module L offers a supported library
API for it (CONTRACTS section 12.18.1, the "L amendment").
"""
from __future__ import annotations

import datetime as dt
import os
import re
import stat

from library import Library, LibraryError
from library import frontmatter as fm
from library.safefs import atomic_write_bytes

LOG_DIR = "50-finance/log"
MAX_LOG_BYTES = 8 * 1024 * 1024
_MARKER_RE = re.compile(r"<!-- finance-digest:(begin|end) ([0-9]{4}-[0-9]{2}-[0-9]{2}) -->")


class LogError(Exception):
    """The log could not be written safely; nothing was changed."""


def log_rel(day: dt.date) -> str:
    """The month's log, named by the library rule: YYYY-MM-01_finance-log.md (Lead, 2026-10-27)."""
    return f"{LOG_DIR}/{day.year:04d}-{day.month:02d}-01_finance-log.md"


def begin_marker(day: dt.date) -> str:
    return f"<!-- finance-digest:begin {day.isoformat()} -->"


def end_marker(day: dt.date) -> str:
    return f"<!-- finance-digest:end {day.isoformat()} -->"


def block(day: dt.date, entry: str) -> str:
    return f"{begin_marker(day)}\n{entry.rstrip(chr(10))}\n{end_marker(day)}\n"


def new_file_text(day: dt.date, entry: str) -> str:
    month = f"{day.year:04d}-{day.month:02d}"
    meta = {
        "title": f"Finance log {month}",
        "source": "finance-digest (read-only Money Hub digest)",
        "added": day.isoformat(),
        "tags": ["finance", "digest"],
        "summary": f"Daily read-only Money Hub digest for {month}: freshness, balances, budget, bills due, debt, next payday.",
        "related": [],
    }
    body = (
        f"# Finance log {month}\n\n"
        "One entry per day, written by finance-digest from Money Hub (read-only). A rerun on the same "
        "day replaces that day's entry; text outside the entries is kept.\n\n"
    )
    return fm.render(meta, body) + block(day, entry)


def _spans(text: str) -> dict[str, tuple[int, int]]:
    """date -> (start, end) offsets of each day block, ``end`` just past the end marker's newline."""
    spans: dict[str, tuple[int, int]] = {}
    open_day = open_at = None
    pos = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        m = _MARKER_RE.fullmatch(stripped)
        if m is None:
            if "finance-digest:" in stripped and "<!--" in stripped:
                raise LogError("the log has a damaged day marker; fix it by hand")
        else:
            kind, day = m.groups()
            if kind == "begin":
                if open_day is not None or day in spans:
                    raise LogError(f"the log's markers for {day} don't pair up; fix them by hand")
                open_day, open_at = day, pos
            else:
                if open_day != day:
                    raise LogError(f"the log's markers for {day} don't pair up; fix them by hand")
                spans[day] = (open_at, pos + len(line))
                open_day = None
        pos += len(line)
    if open_day is not None:
        raise LogError(f"the log's markers for {open_day} don't pair up; fix them by hand")
    return spans


def merge(existing: str, day: dt.date, entry: str) -> str:
    new = block(day, entry)
    spans = _spans(existing)
    key = day.isoformat()
    if key in spans:
        start, end = spans[key]
        return existing[:start] + new + existing[end:]
    if existing and not existing.endswith("\n"):
        existing += "\n"
    return existing + ("\n" if existing and not existing.endswith("\n\n") else "") + new


def _read(path) -> tuple[str, os.stat_result]:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        raise LogError(f"the log file can't be opened ({e.strerror})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise LogError("the log path is not a regular file")
        if st.st_size > MAX_LOG_BYTES:
            raise LogError(f"the log file is larger than {MAX_LOG_BYTES} bytes; refusing to rewrite it")
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_LOG_BYTES:
                raise LogError("the log file grew past the size limit while it was read")
            chunks.append(chunk)
    finally:
        os.close(fd)
    try:
        return b"".join(chunks).decode("utf-8"), st
    except UnicodeDecodeError:
        raise LogError("the log file is not UTF-8; fix it by hand") from None


def write_entry(lib: Library, day: dt.date, entry: str) -> str:
    """Write ``entry`` as ``day``'s block. Returns "created", "replaced" or "appended"."""
    rel = log_rel(day)
    try:
        path = lib.abs_path(rel)  # refuses symlinks from the root down and anything outside it
        lib.ensure_dir(LOG_DIR)
    except LibraryError as e:
        raise LogError(str(e)) from None
    if not os.path.lexists(path):
        try:
            lib.write_new(rel, new_file_text(day, entry))
            return "created"
        except LibraryError as e:
            if not os.path.lexists(path):
                raise LogError(str(e)) from None
            # It appeared meanwhile: fall through and merge into it.
    existing, before = _read(path)
    if not existing:
        # An empty file (e.g. a name reserved by an interrupted write_new) is treated as new: it
        # gets the library header, through the same checked replace as any other write.
        had_day, data = None, new_file_text(day, entry).encode("utf-8")
    else:
        had_day = day.isoformat() in _spans(existing)
        data = merge(existing, day, entry).encode("utf-8")
    try:
        lib.abs_path(rel)  # still no symlink on the way
        now = os.lstat(path)
    except (LibraryError, OSError) as e:
        raise LogError(f"the log file changed while it was being written ({e}); nothing was written") from None
    if (now.st_ino, now.st_size, now.st_mtime_ns) != (before.st_ino, before.st_size, before.st_mtime_ns):
        raise LogError("the log file changed while it was being written; nothing was written, run again")
    atomic_write_bytes(path, data)
    if had_day is None:
        return "created"
    return "replaced" if had_day else "appended"
