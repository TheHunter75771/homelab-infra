"""Front-matter: the small YAML subset every library ``.md`` file starts with.

We write and parse it ourselves (stdlib only, no PyYAML). The subset:

    ---
    title: "Any text: quotes \\" and colons are fine"
    source: 'single quotes work too'
    added: 2026-10-26
    tags:
      - "gpu"
      - rocm
    summary: plain text also works
    related: [a, "b"]
    ---

* one ``key: value`` per line, keys at column 0, no nesting;
* a value is a scalar (double-quoted with YAML escapes, single-quoted with ``''``, or plain)
  or a list (block ``- item`` lines, or a one-line flow ``[a, b]``);
* ``#`` comments and blank lines are ignored; ``---`` (or ``...``) closes the header;
* everything else (anchors, block scalars ``|``/``>``, nested maps, multi-line quotes) is
  rejected with a line number, so ``check`` can report it instead of guessing.

Every value is a ``str`` or a ``list[str]``: ``added: 2026-10-26`` parses to the string
``"2026-10-26"``. The writer always double-quotes strings (except a valid ``added`` date) so
that anything a user types round-trips.
"""
from __future__ import annotations

import datetime as dt
import re

REQUIRED = ("title", "source", "added", "tags", "summary", "related")
LIST_KEYS = frozenset({"tags", "related"})

Meta = dict[str, "str | list[str]"]

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_KEY_LINE_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]+(?P<rest>.*))?")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# YAML double-quoted escapes we accept when parsing.
_SIMPLE_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f",
    "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85",
    "_": "\xa0", "L": " ", "P": " ",
}
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}
# Characters that cannot start a YAML plain scalar (they mean something else).
_PLAIN_FORBIDDEN_START = set("[]{},&*!|>%@`\"'")


class FrontMatterError(ValueError):
    """The header is not in the supported subset. ``line`` is 1-based when known."""

    def __init__(self, message: str, line: int | None = None) -> None:
        self.line = line
        super().__init__(f"line {line}: {message}" if line else message)


def is_iso_date(value: object) -> bool:
    """True for a real calendar date written as YYYY-MM-DD."""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------- writing

def _needs_escape(ch: str) -> bool:
    # Outside YAML's printable set, or a character some parsers treat as a line break / BOM.
    o = ord(ch)
    return o < 0x20 or 0x7F <= o <= 0x9F or ch in "  ﻿￾￿"


def quote(value: str) -> str:
    """Render ``value`` as a YAML double-quoted scalar that parses back to exactly ``value``."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:  # lone surrogates (e.g. undecodable argv bytes)
        raise FrontMatterError(f"text is not valid Unicode: {value!r}") from exc
    out = ['"']
    for ch in value:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif _needs_escape(ch):
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def dump(meta: Meta) -> str:
    """Render the header (including both ``---`` lines and a trailing newline).

    Required keys come first in the contract's order; any extra keys follow, sorted, so the
    output is deterministic.
    """
    keys = [k for k in REQUIRED if k in meta] + sorted(k for k in meta if k not in REQUIRED)
    lines = ["---"]
    for key in keys:
        if not _KEY_RE.fullmatch(key):
            raise FrontMatterError(f"invalid key {key!r}")
        value = meta[key]
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                for item in value:
                    if not isinstance(item, str):
                        raise FrontMatterError(f"{key}: list items must be text, got {item!r}")
                    lines.append(f"  - {quote(item)}")
        elif isinstance(value, str):
            if key == "added" and is_iso_date(value):
                lines.append(f"{key}: {value}")
            else:
                lines.append(f"{key}: {quote(value)}")
        else:
            raise FrontMatterError(f"{key}: values must be text or a list of text, got {value!r}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def render(meta: Meta, body: str) -> str:
    """Header followed by ``body`` (which is kept byte-for-byte)."""
    return dump(meta) + body


# --------------------------------------------------------------------------- parsing

def has_header(text: str) -> bool:
    """True if ``text`` opens with a ``---`` line, i.e. it claims to have front-matter."""
    first = text.lstrip("\ufeff").split("\n", 1)[0].rstrip("\r")
    return first == "---"


def _rest_is_blank_or_comment(rest: str, line: int, comments: list[int]) -> None:
    stripped = rest.strip()
    if stripped and not stripped.startswith("#"):
        raise FrontMatterError(f"unexpected text after the closing quote: {stripped!r}", line)
    if stripped:
        comments.append(line)


def _double_quoted(s: str, start: int, line: int) -> tuple[str, int]:
    """Parse a double-quoted scalar starting at ``s[start] == '"'``; return (value, index after)."""
    out: list[str] = []
    i = start + 1
    while i < len(s):
        ch = s[i]
        if ch == '"':
            return "".join(out), i + 1
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(s):
            break
        esc = s[i]
        if esc in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[esc])
            i += 1
        elif esc in _HEX_ESCAPES:
            n = _HEX_ESCAPES[esc]
            digits = s[i + 1:i + 1 + n]
            if len(digits) != n or not all(c in "0123456789abcdefABCDEF" for c in digits):
                raise FrontMatterError(f"bad \\{esc} escape", line)
            code = int(digits, 16)
            if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                raise FrontMatterError(f"escape \\{esc}{digits} is not a valid character", line)
            out.append(chr(code))
            i += 1 + n
        else:
            raise FrontMatterError(f"unknown escape \\{esc}", line)
    raise FrontMatterError("unterminated double-quoted string (quotes must close on the same line)", line)


def _single_quoted(s: str, start: int, line: int) -> tuple[str, int]:
    out: list[str] = []
    i = start + 1
    while i < len(s):
        if s[i] == "'":
            if s[i + 1:i + 2] == "'":  # '' is an escaped quote
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        out.append(s[i])
        i += 1
    raise FrontMatterError("unterminated single-quoted string (quotes must close on the same line)", line)


def _plain(text: str, line: int, comments: list[int], *, in_flow: bool = False) -> str:
    """A plain (unquoted) scalar, with any trailing `` #comment`` removed (and recorded)."""
    cut = re.search(r"(^|[ \t])#", text)
    if cut:
        if in_flow:
            # YAML would end the line here and then fail on the unclosed list.
            raise FrontMatterError("a ' #' comment inside [...] is not allowed; quote the item", line)
        comments.append(line)
        text = text[:cut.start()]
    text = text.strip()
    if not text:
        return ""
    if text[0] in _PLAIN_FORBIDDEN_START or text.startswith(("- ", "? ")) or text in ("-", "?"):
        raise FrontMatterError(f"unsupported value {text!r}; put it in double quotes", line)
    if ": " in text or text.endswith(":"):
        raise FrontMatterError(f"a ': ' inside a plain value is not allowed; put it in double quotes: {text!r}", line)
    if in_flow and any(c in text for c in "[]{}"):
        raise FrontMatterError(f"brackets inside a list item must be quoted: {text!r}", line)
    return text


def _scalar(rest: str, line: int, comments: list[int]) -> str:
    rest = rest.strip()
    if rest.startswith('"'):
        value, end = _double_quoted(rest, 0, line)
    elif rest.startswith("'"):
        value, end = _single_quoted(rest, 0, line)
    else:
        return _plain(rest, line, comments)
    _rest_is_blank_or_comment(rest[end:], line, comments)
    return value


def _flow_list(rest: str, line: int, comments: list[int]) -> list[str]:
    """Parse a one-line ``[a, "b", 'c']``."""
    items: list[str] = []
    i = 1  # past '['
    n = len(rest)
    expect_item = True
    while True:
        while i < n and rest[i] in " \t":
            i += 1
        if i >= n:
            raise FrontMatterError("unterminated [ list (flow lists must close on the same line)", line)
        ch = rest[i]
        if ch == "]":
            _rest_is_blank_or_comment(rest[i + 1:], line, comments)
            return items
        if not expect_item:
            if ch != ",":
                raise FrontMatterError("expected ',' or ']' in the list", line)
            i += 1
            expect_item = True
            continue
        if ch == ",":
            raise FrontMatterError("empty item in the list", line)
        if ch == '"':
            value, i = _double_quoted(rest, i, line)
        elif ch == "'":
            value, i = _single_quoted(rest, i, line)
        else:
            j = i
            while j < n and rest[j] not in ",]":
                j += 1
            value = _plain(rest[i:j], line, comments, in_flow=True)
            if not value:
                raise FrontMatterError("empty item in the list", line)
            i = j
        items.append(value)
        expect_item = False


def parse(text: str) -> tuple[Meta, str]:
    """Split ``text`` into (header values, body). Raises FrontMatterError if malformed.

    Accepts a UTF-8 BOM and CRLF line endings (files edited on Windows over the share).
    The body is returned exactly as it follows the closing ``---`` line.
    Every value is a ``str`` or ``list[str]``: there is no YAML null/bool/number typing.
    """
    meta, body, _ = parse_detailed(text)
    return meta, body


def parse_detailed(text: str) -> tuple[Meta, str, list[int]]:
    """Like ``parse``, plus the 1-based line numbers of every ``#`` comment in the header.

    Comments are not kept in ``Meta``; callers that rewrite a header use this to refuse
    rather than silently drop them.
    """
    comments: list[int] = []
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise FrontMatterError("does not start with a '---' line", 1)
    meta: Meta = {}
    open_list: str | None = None  # key whose block list we are reading
    for idx in range(1, len(lines)):
        lineno = idx + 1
        raw = lines[idx].rstrip("\r\n")
        if raw in ("---", "..."):
            if open_list is not None and not meta[open_list]:
                meta[open_list] = ""  # `key:` with nothing after it is an empty value
            return meta, "".join(lines[idx + 1:]), comments
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            comments.append(lineno)
            continue
        indent = raw[:len(raw) - len(raw.lstrip())]
        if "\t" in indent:
            raise FrontMatterError("tabs are not allowed for indentation", lineno)
        if stripped == "-" or stripped.startswith("- "):
            if open_list is None:
                raise FrontMatterError("list item without a 'key:' line above it", lineno)
            item = _scalar(stripped[1:], lineno, comments)
            current = meta[open_list]
            assert isinstance(current, list)
            current.append(item)
            continue
        if indent:
            raise FrontMatterError("unexpected indentation (nested values are not supported)", lineno)
        m = _KEY_LINE_RE.fullmatch(raw)
        if not m:
            raise FrontMatterError(f"expected 'key: value', got {raw!r}", lineno)
        if open_list is not None and not meta[open_list]:
            meta[open_list] = ""
        open_list = None
        key, rest = m.group("key"), (m.group("rest") or "").strip()
        if key in meta:
            raise FrontMatterError(f"duplicate key {key!r}", lineno)
        if rest == "" or rest.startswith("#"):
            if rest:
                comments.append(lineno)
            meta[key] = []
            open_list = key
        elif rest.startswith("["):
            meta[key] = _flow_list(rest, lineno, comments)
        else:
            meta[key] = _scalar(rest, lineno, comments)
    raise FrontMatterError("no closing '---' line", len(lines))


# --------------------------------------------------------------------------- validation

def validate(meta: Meta) -> list[tuple[str, str]]:
    """Problems with the required fields, as (kind, message); kind is missing-field or bad-field."""
    problems: list[tuple[str, str]] = []
    for key in REQUIRED:
        if key not in meta:
            problems.append(("missing-field", f"missing '{key}'"))
            continue
        value = meta[key]
        if key in LIST_KEYS:
            if not isinstance(value, list):
                problems.append(("bad-field", f"'{key}' must be a list (use [] when empty)"))
            elif any(not item for item in value):
                problems.append(("bad-field", f"'{key}' has an empty item"))
        elif not isinstance(value, str):
            problems.append(("bad-field", f"'{key}' must be text, not a list"))
        elif key == "title" and not value.strip():
            problems.append(("bad-field", "'title' is empty"))
        elif key == "added" and not is_iso_date(value):
            problems.append(("bad-field", f"'added' must be a date YYYY-MM-DD, got {value!r}"))
    return problems
