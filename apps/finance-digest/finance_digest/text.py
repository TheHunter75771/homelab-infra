"""Text rules shared by every output: hostile-text cleaning and money formatting.

These mirror apps/finance-tools/moneyhub_readonly.py (CONTRACTS section 12.14). That file is not imported
because it needs pydantic, which is not on the host; the rules are the same and the tests check them.
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata

WARN = "\u26a0"
ELLIPSIS = "\u2026"
TEXT_MAX = 80

# Unicode categories removed from API text: controls, format (zero-width, bidi, tags, soft hyphen),
# private use, unassigned, surrogates, line and paragraph separators.
_STRIP_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cn", "Cs", "Zl", "Zp"})
_SPACE_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})  # these become a space, the rest vanish
_VARIATION_SELECTORS = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))
# Markdown/HTML syntax -> inert lookalikes. "<" also means no HTML comment can be forged, which
# keeps the log file's day markers unforgeable.
_NEUTRALISE = str.maketrans(
    {
        "[": "\uff3b",
        "]": "\uff3d",
        "(": "\uff08",
        ")": "\uff09",
        "<": "\u2039",
        ">": "\u203a",
        "!": "\uff01",
        "`": "\u02cb",
        "|": "\u2223",
        "@": "\uff20",
        "&": "\uff06",
    }
)
_LEADING_HASH_RE = re.compile(r"^#+")
_SCHEME_SEP_RE = re.compile(r":/+")
_WWW_RE = re.compile(r"(?i)www\.")


def _is_invisible(ch: str) -> bool:
    o = ord(ch)
    for lo, hi in _VARIATION_SELECTORS:
        if lo <= o <= hi:
            return True
    return unicodedata.category(ch) in _STRIP_CATEGORIES


def clean(value: object, limit: int = TEXT_MAX) -> str:
    """Untrusted text made inert: invisibles stripped, one line, markdown/HTML neutralised,
    URLs defanged, truncated to ``limit`` characters."""
    if value is None:
        return ""
    raw = str(value)[: limit * 8]
    s = "".join(
        (" " if unicodedata.category(ch) in _SPACE_CATEGORIES else "") if _is_invisible(ch) else ch for ch in raw
    )
    s = " ".join(s.split())
    s = s.translate(_NEUTRALISE)
    s = _LEADING_HASH_RE.sub(lambda m: "\uff03" * len(m.group()), s)
    s = _SCHEME_SEP_RE.sub(":/ /", s)
    s = _WWW_RE.sub(lambda m: m.group()[:3] + " .", s)
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + ELLIPSIS
    return s


def cents(value: object) -> int | None:
    """An integer number of cents, or None. Never a float with a fraction, never a bool."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def money(value: object) -> str:
    c = cents(value)
    if c is None:
        return "n/a"
    sign = "-" if c < 0 else ""
    dollars, rest = divmod(abs(c), 100)
    return f"{sign}${dollars:,}.{rest:02d}"


def total(values) -> tuple[int, int]:
    """(sum in integer cents, number of unreadable amounts left out)."""
    acc = skipped = 0
    for v in values:
        c = cents(v)
        if c is None:
            skipped += 1
        else:
            acc += c
    return acc, skipped


def plain_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def parse_dt(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def month_of(day: dt.date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def next_month(day: dt.date) -> dt.date:
    return dt.date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def valid_month(value: object) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        return False
    return 1 <= int(value[5:]) <= 12
