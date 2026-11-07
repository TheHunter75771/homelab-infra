"""Small shared helpers: untrusted-text cleaning, atomic JSON writes, a file lock, time parsing and formatting."""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import math
import os
import tempfile
import unicodedata
import zoneinfo
from collections.abc import Iterator
from typing import Any

# Removed from text that comes from a source: controls, format chars (zero-width, bidi, tags), private use,
# unassigned, surrogates, line/paragraph separators.
_STRIP = frozenset({"Cc", "Cf", "Co", "Cn", "Cs", "Zl", "Zp"})
_SPACE = frozenset({"Cc", "Zl", "Zp"})  # these become a space (a newline inside a name), the rest vanish
_ELLIPSIS = "…"


def clean(value: Any, limit: int = 120) -> str:
    """Source text made safe to display and log: invisibles removed, whitespace collapsed, capped."""
    if value is None:
        return ""
    out = []
    for c in str(value)[: limit * 4]:
        cat = unicodedata.category(c)
        if cat in _SPACE:
            out.append(" ")
        elif cat not in _STRIP:
            out.append(c)
    s = " ".join("".join(out).split())
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + _ELLIPSIS
    return s


def finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def cents(value: Any) -> int | None:
    """Integer cents, or None. Floats only when integral (Money Hub sends ints)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def money(c: int) -> str:
    sign = "-" if c < 0 else ""
    dollars, rest = divmod(abs(c), 100)
    return f"{sign}${dollars:,}.{rest:02d}"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


# ---- time --------------------------------------------------------------------------------------------------
def parse_ts(value: Any, naive_tz: str = "UTC") -> float | None:
    """ISO 8601 text -> epoch seconds. A naive value is read in `naive_tz` (Money Hub's are naive UTC)."""
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        return None
    try:
        d = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=zoneinfo.ZoneInfo(naive_tz))
    try:
        return d.timestamp()
    except (OverflowError, OSError):
        return None


def local(epoch: float, tz: str) -> dt.datetime:
    return dt.datetime.fromtimestamp(epoch, zoneinfo.ZoneInfo(tz))


def hhmm(d: dt.datetime) -> str:
    return f"{d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def day_label(d: dt.date | dt.datetime, today: dt.date) -> str:
    day = d.date() if isinstance(d, dt.datetime) else d
    if day == today:
        return "Today"
    if day == today + dt.timedelta(days=1):
        return "Tomorrow"
    return f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"


def ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{round(s / 60)} min ago"
    if s < 48 * 3600:
        return f"{round(s / 3600)} h ago"
    return f"{s // 86400} days ago"


# ---- files -------------------------------------------------------------------------------------------------
def read_json(path: str, default: Any) -> Any:
    """The parsed file, or `default` when it is missing. A corrupt file raises ValueError."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError(f"{os.path.basename(path)} is not valid JSON") from None


def write_json_atomic(path: str, data: Any, mode: int = 0o600) -> None:
    """tmp file in the same directory (created with `mode`), fsync, rename over `path`, fsync the directory.
    A failure at any step leaves the old file untouched and removes the tmp file."""
    directory = os.path.dirname(path) or "."
    body = (json.dumps(data, indent=1, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        os.fchmod(fd, mode)
        view = memoryview(body)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


@contextlib.contextmanager
def file_lock(path: str) -> Iterator[None]:
    """An exclusive flock on `path`.lock: every read-modify-write of a state file runs under it, so the server
    and the briefing CLI never interleave their writes."""
    fd = os.open(f"{path}.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock
