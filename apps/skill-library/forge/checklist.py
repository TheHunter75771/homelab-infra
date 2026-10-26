"""Checkbox files (``searches.md``, ``sources.md``) and the exact-match rules behind approval.

Noah approves by ticking a box. The files are the source of truth, so the parser is strict
and fails closed: a line counts only if it has exactly the shape the forge writes, and only if
everything in it is visible to Noah.

* A checkbox line starts at column 0 with ``- [ ] `` (pending) or ``- [x] `` / ``- [X] ``
  (approved). Anything else that looks like a checkbox (``-[x]``, ``- [x ]``, ``* [x]``, an
  indented ``- [x]``, ``- [y]``) is ignored and reported as malformed, never guessed at.
* **Nothing hidden.** A line counts only if every character is printable (``str.isprintable``:
  no control, format (Cf), private-use (Co), unassigned (Cn) or surrogate (Cs) characters, no
  space other than the ASCII space, so no tag characters, zero-width characters or bidi
  overrides), has no variation selector or other invisible filler, and has no ``<``, ``>`` or
  backtick (which hide text when the Markdown is rendered). A ticked line that breaks a rule is
  NOT approved and is reported by ``status``, even if Noah pasted it himself.
* **HTML comments.** ``<!--`` anywhere on a line opens a comment, until ``-->``. Any line that
  is inside or touches a comment is never a checkbox (box-like ones are reported).
  Lines inside fenced code blocks (````` / ``~~~``) are ignored too.
* ``searches.md``: ``- [x] <query> — why: <justification>``. The separator is exactly
  `` — why:`` (space, EM DASH, space) and occurs once. The query and the justification use only
  ``A-Z a-z 0-9``, space and ``. , : ; ? ' " / + = % @ - ( )``, and may not contain ``why:`` in
  any case (CONTRACTS §12.9, round 3), checked at propose time and at parse time. None of these
  characters means anything to a Markdown or HTML renderer, so no lookalike separator, hidden
  glyph, link, image, entity or emphasis can exist in them. The query is the text before the
  separator, without surrounding spaces. If Noah edits the query, the edited text is what is
  approved.
* ``sources.md``: ``- [x] `<url>` — title: ... — why: ... — take: ...``. The URL is written in
  a code span (exactly one opening and one closing backtick around it), so Markdown inside a URL
  (``_x_``, ``*a*``, ``~~``, ``&lt;``) can never change the rendered view; a line without a
  well-formed span is NOT approved. The rest is for Noah to read; it may be Unicode
  (visible characters only) but never ``[ ] ! & \\ * _ ~ |`` or ``< > ` `` (no links, images,
  entities, escapes, emphasis or tables), at propose time and at parse time.
* ``forge status <id>`` prints every parsed query and URL as a Python repr with its tick state.
  That raw listing, not a rendered view of the file, is the authoritative view of what is approved.

Authorization is exact:

* **Queries** compare code point for code point: no case folding, no Unicode or whitespace
  normalization. A lookalike query is a different query.
* **URLs** are canonicalized only by lowercasing the scheme and the host (ASCII letters only, so
  a Unicode character can never lowercase into an ASCII lookalike). Path, query string and port
  are kept byte for byte: a trailing slash, a case change in the path or ``http`` vs ``https``
  is a different URL. Only ``http`` and ``https`` are accepted, with no user info
  (``user@host``), no fragment (``#``), a plain ASCII host name without a trailing dot, or a
  strict dotted-quad IPv4 / bracketed IPv6 literal (no ``0x7f.1``-style numbers; an IDN must be
  written in its ``xn--`` form), and no whitespace, control characters, backslashes or
  ``<>"`{}|^`` anywhere.
"""
from __future__ import annotations

import re
import unicodedata
import urllib.parse
from dataclasses import dataclass

# ------------------------------------------------------------------ character rules

# Characters the printable test lets through but that render as nothing (or change how
# neighbours render): variation selectors, the combining grapheme joiner, Hangul and Khmer
# fillers, the Mongolian free variation selectors. Bidi controls are Cf, listed for clarity.
_INVISIBLE = frozenset(
    list(range(0xFE00, 0xFE10)) + list(range(0xE0100, 0xE01F0)) + list(range(0x180B, 0x1810))
    + [0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x17B4, 0x17B5, 0x2800]
    + [0x061C, 0x200E, 0x200F] + list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A))
)
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cn", "Cs", "Zl", "Zp"})
HIDING_CHARS = "<>`"  # HTML comments/tags and code spans hide or restyle text when rendered
_EXTRA_DASHES = frozenset("-\u2012\u2013\u2014\u2015\u2212")
SEPARATOR = " \u2014 why:"  # " — why:", the one the forge writes


def char_problem(text: str) -> str | None:
    """Why ``text`` may hide something from Noah, or None if every character is visible."""
    for ch in text:
        cp = ord(ch)
        if (not ch.isprintable() or cp in _INVISIBLE
                or unicodedata.category(ch) in _REFUSED_CATEGORIES):
            name = unicodedata.name(ch, "unnamed")
            return f"invisible or control character U+{cp:04X} ({name})"
    return None


def _is_dash(ch: str) -> bool:
    return ch in _EXTRA_DASHES or unicodedata.category(ch) == "Pd"


def dash_why_positions(text: str) -> list[int]:
    """Index of every dash-like character followed by optional spaces and ``why:`` (any case)."""
    found = []
    for i, ch in enumerate(text):
        if _is_dash(ch) and text[i + 1:].lstrip(" ").lower().startswith("why:"):
            found.append(i)
    return found


def check_field(value: str, what: str) -> str:
    """A single-line field: stripped, non-empty, every character visible (see the module doc)."""
    if not isinstance(value, str):
        raise ValueError(f"{what} is not text")
    value = value.strip(" ")
    if not value:
        raise ValueError(f"{what} is empty")
    problem = char_problem(value)
    if problem:
        raise ValueError(f"{what} has an {problem}: {value!r}")
    return value


# Markdown syntax refused in source title/why/take (they may be Unicode): links and images need
# [ ] (and !), entities need &, escapes \\, emphasis/strikethrough * _ ~, tables |.
SOURCE_FIELD_REFUSED = frozenset("[]!&\\*_~|") | frozenset(HIDING_CHARS)


def source_text_problem(text: str) -> str | None:
    """Character rules for source text (title, why, take, notes): visible, no Markdown syntax."""
    problem = char_problem(text)
    if problem:
        return problem
    bad = sorted({c for c in text if c in SOURCE_FIELD_REFUSED})
    if bad:
        return f"contains {''.join(bad)!r}, which Markdown renders (links, images, entities, emphasis)"
    return None


def check_proposal_text(value: str, what: str) -> str:
    """A source field the forge writes into a checkbox line: visible, no Markdown, no fake separator."""
    value = check_field(value, what)
    bad = sorted({c for c in value if c in SOURCE_FIELD_REFUSED})
    if bad:
        raise ValueError(f"{what} may not contain {''.join(bad)!r}: {value!r}")
    if dash_why_positions(value):
        raise ValueError(f"{what} contains a dash followed by 'why:', which fakes the separator: {value!r}")
    return value


# CONTRACTS §12.9 round 3: queries and justifications are plain words and a little punctuation.
# Nothing here has a meaning in Markdown or HTML (no [ ] ! ( ) used as a link needs both [ and ],
# no & entities, no * _ ~ emphasis, no # headings, no \\ escapes, no ` code, no < > tags), so a
# rendered view shows exactly the characters that are approved and loads nothing.
QUERY_ALLOWED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,:;?'\"/+=%@-()"
)


def ascii_problem(value: str) -> str | None:
    """Why ``value`` is not a plain query/justification, or None (CONTRACTS §12.9, round 3)."""
    for ch in value:
        if ch not in QUERY_ALLOWED:
            return (f"character {ch!r} (U+{ord(ch):04X}) is not allowed; use letters, digits, spaces "
                    "and . , : ; ? ' \" / + = % @ - ( )")
    if "why:" in value.lower():
        return "contains 'why:', which only the forge's separator may contain"
    return None


def check_ascii(value: str, what: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} is not text")
    value = value.strip(" ")
    if not value:
        raise ValueError(f"{what} is empty")
    problem = ascii_problem(value)
    if problem:
        raise ValueError(f"{what}: {problem}: {value!r}")
    return value


def check_query(query: str) -> str:
    query = check_ascii(query, "query")
    if query.startswith(("[", "]")):
        raise ValueError(f"the query may not start with a bracket: {query!r}")
    return query


def check_why(why: str) -> str:
    return check_ascii(why, "justification")


def line_problem(text: str) -> str | None:
    """Character rules for a whole checkbox line (everything after ``] ``)."""
    problem = char_problem(text)
    if problem:
        return problem
    bad = sorted({c for c in text if c in HIDING_CHARS})
    if bad:
        return f"contains {''.join(bad)!r}, which can hide text when rendered"
    return None


# ------------------------------------------------------------------ line parsing

_BOX_RE = re.compile(r"- \[(?P<mark>[ xX])\] (?P<rest>.*\S)[ \t]*")
# Anything a person might have meant as a checkbox: a list bullet followed by '['.
_LOOKS_LIKE_BOX_RE = re.compile(r"\s*[-*+]\s*\[")
_FENCE_RE = re.compile(r" {0,3}(```|~~~)")


@dataclass(frozen=True)
class Box:
    """One checkbox line. ``text`` is everything after ``] `` (trailing whitespace removed)."""
    line: int
    checked: bool
    text: str


@dataclass(frozen=True)
class Malformed:
    line: int
    text: str
    reason: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.reason}: {self.text!r}"


def _scan_comments(line: str, in_comment: bool) -> tuple[bool, bool]:
    """(line touches a comment, still inside a comment at the end of the line)."""
    touched = in_comment
    i = 0
    while True:
        if in_comment:
            j = line.find("-->", i)
            if j == -1:
                return True, True
            in_comment, i, touched = False, j + 3, True
        else:
            j = line.find("<!--", i)
            if j == -1:
                return touched, False
            in_comment, i, touched = True, j + 4, True


def parse_boxes(text: str, *, url_span: bool = False) -> tuple[list[Box], list[Malformed]]:
    """Every well-formed checkbox line, plus the lines that looked like one but were not.

    The character rules are applied here: a box whose text could hide something is reported
    as malformed and not returned, whether it is ticked or not.
    """
    boxes: list[Box] = []
    bad: list[Malformed] = []
    in_fence: str | None = None
    in_comment = False
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.removeprefix("\ufeff") if lineno == 1 else raw
        looks_like_box = bool(_LOOKS_LIKE_BOX_RE.match(line))
        if in_fence is None:
            touched, in_comment = _scan_comments(line, in_comment)
            if touched:
                if looks_like_box:
                    bad.append(Malformed(lineno, line, "inside or next to an HTML comment; ignored"))
                continue
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            if in_fence is None:
                in_fence = marker
            elif in_fence == marker:
                in_fence = None
            continue
        if in_fence is not None:
            continue
        m = _BOX_RE.fullmatch(line)
        if not m:
            if looks_like_box:
                bad.append(Malformed(lineno, line, "not a '- [ ] ' or '- [x] ' line at column 0; ignored"))
            continue
        rest = m.group("rest").rstrip(" \t")
        # sources.md lines carry their URL in a code span, so their backticks are checked by
        # parse_sources (exactly one span, around the URL); everything else gets the full rule.
        problem = char_problem(rest) if url_span else line_problem(rest)
        if problem:
            bad.append(Malformed(lineno, line, f"{problem}; NOT approved"))
            continue
        boxes.append(Box(lineno, m.group("mark") in "xX", rest))
    return boxes, bad


@dataclass(frozen=True)
class Query:
    line: int
    checked: bool
    query: str
    why: str


def parse_searches(text: str) -> tuple[list[Query], list[Malformed]]:
    """``- [x] <query> — why: <why>``: the separator is the forge's own `` — why:`` and must occur
    exactly once; the query and the justification must both be printable ASCII (see
    ``ascii_problem``), so no lookalike of the separator or hidden glyph can survive."""
    boxes, bad = parse_boxes(text)
    out: list[Query] = []
    for box in boxes:
        parts = box.text.split(SEPARATOR)
        if len(parts) != 2:
            reason = ("no ' — why: ' (space, em dash, space) after the query" if len(parts) == 1
                      else "more than one ' — why:' separator")
            bad.append(Malformed(box.line, box.text, f"{reason}; NOT approved"))
            continue
        query, why = parts[0].strip(" "), parts[1].strip(" ")
        try:
            check_query(query)
            if why:
                check_why(why)
        except ValueError as exc:
            bad.append(Malformed(box.line, box.text, f"{exc}; NOT approved"))
            continue
        out.append(Query(box.line, box.checked, query, why))
    return out, bad


@dataclass(frozen=True)
class Source:
    line: int
    checked: bool
    url: str          # canonical form
    text: str         # the whole line after "] "


def parse_sources(text: str) -> tuple[list[Source], list[Malformed]]:
    """``- [x] `<url>` — title: ...``: the URL must sit in a code span (one opening and one closing
    backtick, nothing else between them, then a space or the end of the line), so no Markdown in
    the URL can change how a viewer renders it. Backticks are refused inside URLs and in the rest
    of the line, so the span cannot be broken out of. Anything else is NOT approved."""
    boxes, bad = parse_boxes(text, url_span=True)
    out: list[Source] = []
    for box in boxes:
        url_text, rest = _url_span(box.text)
        if url_text is None:
            bad.append(Malformed(box.line, box.text,
                                 "the URL must be written in backticks: - [x] `https://...` ...; NOT approved"))
            continue
        try:
            url = canonical_url(url_text)
        except ValueError as exc:
            bad.append(Malformed(box.line, box.text, f"{exc}; NOT approved"))
            continue
        problem = source_text_problem(rest)
        if problem:
            bad.append(Malformed(box.line, box.text, f"the text after the URL {problem}; NOT approved"))
            continue
        out.append(Source(box.line, box.checked, url, box.text))
    return out, bad


def _url_span(text: str) -> tuple[str | None, str]:
    """(text inside a leading `...` span, the rest of the line) or (None, "") if malformed."""
    if not text.startswith("`") or text.startswith("``"):
        return None, ""
    end = text.find("`", 1)
    if end <= 1:
        return None, ""
    rest = text[end + 1:]
    if rest and not rest.startswith(" "):
        return None, ""
    return text[1:end], rest


def url_span(url: str) -> str:
    """How the forge writes a URL into sources.md: a code span (URLs never contain a backtick)."""
    if "`" in url:
        raise ValueError("a URL may not contain a backtick")
    return f"`{url}`"


# ------------------------------------------------------------------ URLs

_URL_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<authority>[^/?#]*)(?P<rest>.*)", re.DOTALL)
_HOSTNAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
_IPV4_RE = re.compile(r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])(?:\.(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])){3}")
_UNSAFE_URL_CHARS = frozenset('<>"`{}|^')
_IPV6_RE = re.compile(r"\[[0-9A-Fa-f:.]+\]")
_PORT_RE = re.compile(r"[0-9]{1,5}")
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
MAX_URL = 2048


def canonical_url(url: str) -> str:
    """The comparison form of ``url``; raises ValueError for anything the gate must not fetch."""
    if not isinstance(url, str) or not url:
        raise ValueError("empty URL")
    if len(url) > MAX_URL:
        raise ValueError(f"URL longer than {MAX_URL} characters")
    if not url.isascii():
        raise ValueError("URL has non-ASCII characters (percent-encode them; write IDN hosts as xn--)")
    if char_problem(url) or any(c.isspace() for c in url) or "\\" in url:
        raise ValueError("URL has whitespace, control characters or a backslash")
    if "#" in url:
        raise ValueError("URLs with a fragment (#...) are refused; remove it")
    unsafe = sorted({c for c in url if c in _UNSAFE_URL_CHARS})
    if unsafe:
        raise ValueError(f"URL has {''.join(unsafe)!r}; percent-encode them")
    m = _URL_RE.fullmatch(url)
    if not m:
        raise ValueError("not an absolute http(s) URL")
    scheme = m.group("scheme").translate(_ASCII_LOWER)
    if scheme not in ("http", "https"):
        raise ValueError(f"scheme {scheme!r} refused; only http and https")
    authority = m.group("authority")
    if "@" in authority:
        raise ValueError("URLs with user info (user@host) are refused")
    host, port = authority, None
    is_ipv6 = host.startswith("[")
    if is_ipv6:
        end = host.find("]")
        if end == -1:
            raise ValueError("unterminated IPv6 literal")
        host, tail = host[:end + 1], host[end + 1:]
        if not _IPV6_RE.fullmatch(host):
            raise ValueError("bad IPv6 literal")
        if tail:
            if not tail.startswith(":"):
                raise ValueError("bad text after the IPv6 literal")
            port = tail[1:]
    elif ":" in host:
        host, port = host.split(":", 1)
    if not is_ipv6:
        if not _HOSTNAME_RE.fullmatch(host):
            raise ValueError(f"bad host {host!r} (no trailing dot, no empty labels)")
        # A last label starting with a digit is an address, not a name: only a strict dotted
        # quad is accepted, so 0x7f.1, 127.1, 2130706433 or 0177.0.0.1 cannot reach the resolver.
        if host.rsplit(".", 1)[-1][:1].isdigit() and not _IPV4_RE.fullmatch(host):
            raise ValueError(f"bad host {host!r}: numeric hosts must be a plain dotted-quad IPv4 address")
    if port is not None:
        if not _PORT_RE.fullmatch(port) or not 0 < int(port) < 65536:
            raise ValueError(f"bad port {port!r}")
    if "[" in m.group("rest") or "]" in m.group("rest"):
        raise ValueError("URL has '[' or ']' outside an IPv6 host; percent-encode them")
    canon = f"{scheme}://{host.translate(_ASCII_LOWER)}" + (f":{port}" if port is not None else "") + m.group("rest")
    # Defence in depth: a standard parser must see the same host, so the gate's HTTP client
    # cannot be steered somewhere else by a parsing difference.
    split = urllib.parse.urlsplit(canon)
    if split.scheme != scheme or (split.hostname or "") != host.translate(_ASCII_LOWER).strip("[]").lower() \
            or split.username is not None or split.password is not None:
        raise ValueError("URL parses ambiguously")
    return canon


def idn_note(url: str) -> str | None:
    """For an ``xn--`` host, the Unicode it stands for (shown to Noah to spot lookalikes)."""
    host = urllib.parse.urlsplit(url).hostname or ""
    if not any(label.startswith("xn--") for label in host.split(".")):
        return None
    try:
        shown = host.encode("ascii").decode("idna")
    except UnicodeError:
        return "an invalid IDN"
    # The note is written into sources.md, so it must pass the same rules as any source text.
    return shown if source_text_problem(shown) is None else "unusual characters (check the xn-- form)"
