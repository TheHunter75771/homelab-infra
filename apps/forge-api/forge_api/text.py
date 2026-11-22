"""Text clean-up for what the API returns.

Everything returned to the Open WebUI tool is data, and the tool frames it as untrusted. This
module only makes sure no control or invisible character rides along, and that sizes are capped.
"""
from __future__ import annotations

import unicodedata

ITEM_MAX_BYTES = 20 * 1024       # GET /items: the text is clipped to 20 KB (UTF-8)
MESSAGE_MAX = 400                 # error messages
FIELD_MAX = 300                   # titles, why's and other short fields in listings

# Removed: C0/C1 controls except \n and \t, format characters (bidi controls, zero-width
# joiners, soft hyphen, BOM, tag characters), surrogates, private use, unassigned.
_DROP = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_BREAKS = {"\r\n": "\n", "\r": "\n", "\u2028": "\n", "\u2029": "\n", "\x85": "\n"}


def strip_controls(text: str, *, keep_newlines: bool = True) -> str:
    for old, new in _BREAKS.items():
        text = text.replace(old, new)
    out = []
    for ch in text:
        if ch == "\n" or ch == "\t":
            out.append(ch if keep_newlines else " ")
        elif unicodedata.category(ch) in _DROP:
            continue
        else:
            out.append(ch)
    return "".join(out)


def clip_utf8(text: str, limit: int) -> tuple[str, bool]:
    """(text cut to at most ``limit`` UTF-8 bytes at a character boundary, whether it was cut)."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    return data[:limit].decode("utf-8", "ignore"), True


def short(text: object, limit: int = FIELD_MAX) -> str:
    """One line, no control characters, at most ``limit`` characters."""
    text = " ".join(strip_controls(str(text), keep_newlines=False).split())
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def message(exc: BaseException | str) -> str:
    """A refusal's text for a JSON error body."""
    return short(exc, MESSAGE_MAX)
