"""Visible-character rules, copied from ``forge.checklist`` (apps do not import each other's
private helpers; the library itself is imported). Keep in step with that file."""
from __future__ import annotations

import unicodedata

# Characters the printable test lets through but that render as nothing (or change how
# neighbours render): variation selectors, the combining grapheme joiner, Hangul and Khmer
# fillers, the Mongolian free variation selectors. Bidi controls are Cf, listed for clarity.
_INVISIBLE = frozenset(
    list(range(0xFE00, 0xFE10)) + list(range(0xE0100, 0xE01F0)) + list(range(0x180B, 0x1810))
    + [0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x17B4, 0x17B5, 0x2800]
    + [0x061C, 0x200E, 0x200F] + list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A))
)
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cn", "Cs", "Zl", "Zp"})


def char_problem(text: str) -> str | None:
    """Why ``text`` may hide something from Noah, or None if every character is visible."""
    for ch in text:
        cp = ord(ch)
        if (not ch.isprintable() or cp in _INVISIBLE
                or unicodedata.category(ch) in _REFUSED_CATEGORIES):
            name = unicodedata.name(ch, "unnamed")
            return f"invisible or control character U+{cp:04X} ({name})"
    return None


def visible(text: str) -> str:
    """``text`` with invisible/control characters dropped; tabs and odd spaces become spaces,
    newlines are kept."""
    out = []
    for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
        if ch == "\n":
            out.append(ch)
        elif ch == "\t" or (ch != " " and unicodedata.category(ch) == "Zs"):
            out.append(" ")
        elif char_problem(ch) is None:
            out.append(ch)
    return "".join(out)
