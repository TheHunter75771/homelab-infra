"""Check the model's voice profile and render the file that is written (CONTRACTS §12.21.2 step 4;
the rules are the skill drafter's, §12.19, adapted to this template).

The reply is untrusted text. It is refused (``Invalid``, a one-line reason) unless:

* it is at most ``MAX_BYTES`` (12 KiB) and every character is printable (line breaks are the only
  whitespace other than space);
* its header parses and says ``status: draft`` (anything else, ``approved`` included, is refused:
  only Noah approves); the first body line is a ``# `` title;
* the sections Tone, Sentence style, Word choice, Structure, Do, Don't and Examples are each
  present exactly once and non-empty (headings may be indented up to 3 spaces), with no other
  ``##`` section, no second ``#`` title, and ``###`` headings only inside Examples;
* Do and Don't each have a ``- `` item; Examples has at least two ``Before:`` / ``After:`` pairs;
* no line can change the rendered structure: no fenced code, no ``>`` blockquote, no indented,
  nested or look-alike heading, no rule / setext underline / ``---`` line;
* nothing contains a link, URL, ``//host``, web or e-mail address, image, footnote, wiki link,
  HTML tag or entity or ``]:``; and no line anywhere looks like a checkbox (NFKC-folded, after
  stripping container prefixes).

The written profile is **re-rendered by code**: a library header the code writes (with
``status: draft``), ``# Voice profile``, a fixed note, and the sections in template order. The
rendered text is checked again (size, printable, header reads back as a draft).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from library import frontmatter as fm

from .chars import char_problem
from .prompt import SECTIONS

MAX_BYTES = 12 * 1024
TITLE = "Voice profile"
NOTE = ("Written by the local model from style-stats.md and short excerpts of Noah's own samples. "
        "Noah reviews and approves this profile; the AI uses it only for drafts he reviews.")


class Invalid(ValueError):
    """The reply does not fit the template; the message is a safe one-line reason."""


_STRICT_PREFIX_RE = re.compile(r"^(?:[ \t>]|[-*+•‣⁃](?=[ \t]|$)|\d{1,9}[.)](?=[ \t]|$))*")
_LOOSE_PREFIX_RE = re.compile(r"^(?:[ \t>]|[-*+•‣⁃]|\d{1,9}[.)])*")
_FENCE_START_RE = re.compile(r"^(?:`{3,}|~{3,})")
_HEADING_START_RE = re.compile(r"^#{1,6}(?:[ \t]|$)")
_TOP_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_RULE_LINE_RE = re.compile(r"^[ \t>]*([-=*_+])(?:[ \t]*\1)*[ \t]*$")
_OUTER_FENCE_RE = re.compile(r"^(```|~~~)[ \t]*(?:markdown|md)?[ \t]*$", re.IGNORECASE)
_H1_RE = re.compile(r"^ {0,3}#[ \t]+(.*?)[ \t]*#*[ \t]*$")
_LINKISH = [
    (re.compile(r"://"), "a URL"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "an e-mail address"),
    (re.compile(r"(?i)\bwww\."), "a web address"),
    (re.compile(r"(?i)\b(?:mailto|javascript|vbscript|file|tel|sms|blob|data):"), "a link scheme"),
    (re.compile(r"(?i)\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|org|net|edu|gov|mil|io|dev|"
                r"info|co|uk|us|ai|app|ly|me|xyz|biz|ru|cn|de)(?=[/:?#]|\b)"), "a web address"),
    (re.compile(r"!\["), "an image"),
    (re.compile(r"\]\s*\("), "a link"),
    (re.compile(r"\]\s*\["), "a reference link"),
    (re.compile(r"\]:"), "a link definition"),
    (re.compile(r"//[A-Za-z0-9]"), "a protocol-relative address"),
    (re.compile(r"\[\["), "a wiki link"),
    (re.compile(r"\[\^"), "a footnote"),
    (re.compile(r"<[A-Za-z/!?]"), "an HTML tag"),
    (re.compile(r"(?i)&(?:#[0-9]+|#x[0-9a-f]+|[a-z][a-z0-9]*);"), "an HTML entity"),
]
_BOX_RE = re.compile(r"^\[\s*\S?\s*\]")
_BALLOT_RE = re.compile(r"^[☐☑☒]")
_ITEM_RE = re.compile(r"^\s{0,3}[-*+]\s+\S")
_BEFORE_RE = re.compile(r"(?im)^\s*(?:[-*+]\s+)?[*_]{0,2}before[*_]{0,2}\s*:\s*\S")
_AFTER_RE = re.compile(r"(?im)(?:^\s*(?:[-*+]\s+)?|(?:→|->)\s*)[*_]{0,2}after[*_]{0,2}\s*:\s*\S")


@dataclass
class Profile:
    sections: dict[str, str]
    text: str = ""


def printable_problem(text: str) -> str | None:
    for ch in text:
        if ch != "\n":
            problem = char_problem(ch)
            if problem:
                return problem
    return None


def is_boxlike(line: str) -> bool:
    folded = unicodedata.normalize("NFKC", line)
    rest = folded[_LOOSE_PREFIX_RE.match(folded).end():]
    return bool(_BOX_RE.match(rest) or _BALLOT_RE.match(rest))


def structure_problem(line: str) -> str | None:
    folded = unicodedata.normalize("NFKC", line)
    prefix_end = _STRICT_PREFIX_RE.match(folded).end()
    prefix, rest = folded[:prefix_end], folded[prefix_end:]
    if ">" in prefix:
        return "a '>' blockquote line"
    if _FENCE_START_RE.match(rest):
        return "a fenced code line (``` or ~~~)"
    if folded.strip() and _RULE_LINE_RE.match(folded):
        return "a rule, underline or empty list item line (---, ===, ***)"
    if _HEADING_START_RE.match(rest) and not (_TOP_HEADING_RE.match(line) and folded == line):
        return "an indented, nested or look-alike heading"
    return None


def linkish_problem(text: str) -> str | None:
    folded = unicodedata.normalize("NFKC", text)
    for candidate in (text, folded):
        for pattern, what in _LINKISH:
            m = pattern.search(candidate)
            if m:
                return f"{what} ({candidate[max(0, m.start() - 10):m.end() + 10]!r})"
    return None


def _key(text: str) -> str:
    """A section heading, compared loosely: case, a trailing colon and the apostrophe's shape."""
    text = unicodedata.normalize("NFKC", text).replace("’", "'").replace("‘", "'")
    text = text.rstrip(":").strip().casefold()
    return "don't" if text in ("dont", "do not") else text


def _unwrap(text: str) -> str:
    lines = text.split("\n")
    if len(lines) >= 2 and _OUTER_FENCE_RE.match(lines[0]) and lines[-1].strip() == lines[0][:3]:
        return "\n".join(lines[1:-1])
    return text


def _split_sections(body: str) -> dict[str, list[str]]:
    lines = body.split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i == len(lines):
        raise Invalid("the body is empty")
    if not _H1_RE.match(lines[i]):
        raise Invalid("the first body line must be the title '# ...'")
    known = {_key(s): s for s in SECTIONS}
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    current_name = ""
    for n, line in enumerate(lines[i + 1:], start=i + 2):
        problem = structure_problem(line)
        if problem:
            raise Invalid(f"{problem} (body line {n}): {line[:60]!r}")
        if is_boxlike(line):
            raise Invalid(f"a checkbox line (body line {n}): {line[:60]!r}")
        h = _TOP_HEADING_RE.match(line)
        if h:
            level = len(h.group(1))
            text = re.sub(r"(?:^|[ \t]+)#+$", "", h.group(2) or "").strip()
            if level == 1:
                raise Invalid("a second '#' title line")
            if level == 2:
                if _key(text) not in known:
                    raise Invalid(f"unexpected section {text[:40]!r} (the template has only: {', '.join(SECTIONS)})")
                current_name = known[_key(text)]
                if current_name in sections:
                    raise Invalid(f"section {current_name!r} appears twice")
                current = sections[current_name] = []
                continue
            if _key(text) in known:
                raise Invalid(f"a {'#' * level} {text[:40]!r} heading looks like a section of the template")
            if current_name != "Examples" or level != 3:
                raise Invalid(f"a {'#' * level} heading outside Examples (body line {n})")
        if current is not None:
            current.append(line)
        # text between the title and the first section is dropped (the code writes its own note)
    missing = [s for s in SECTIONS if s not in sections]
    if missing:
        raise Invalid(f"missing sections: {', '.join(missing)}")
    return sections


def check_reply(reply: str) -> Profile:
    if not isinstance(reply, str):
        raise Invalid("the reply is not text")
    text = _unwrap(reply.replace("\r\n", "\n").replace("\r", "\n").strip())
    size = len(text.encode("utf-8"))
    if size > MAX_BYTES:
        raise Invalid(f"too large: {size} bytes (the limit is {MAX_BYTES})")
    problem = printable_problem(text)
    if problem:
        raise Invalid(f"not printable text: {problem}")
    try:
        meta, body = fm.parse(text + "\n")
    except fm.FrontMatterError as exc:
        raise Invalid(f"no valid '---' header ({exc})") from None
    if not fm.has_header(text):
        raise Invalid("no '---' header")
    status = meta.get("status")
    if not isinstance(status, str) or status.strip().casefold() != "draft":
        raise Invalid(f"the header must say status: draft (got {status!r})")
    sections = _split_sections(body)
    kept = {s: "\n".join(sections[s]).strip("\n") for s in SECTIONS}
    for name, value in kept.items():
        if not value.strip():
            raise Invalid(f"{name!r} is empty")
        problem = linkish_problem(value)
        if problem:
            raise Invalid(f"{name} contains {problem}")
    for name in ("Do", "Don't"):
        if not any(_ITEM_RE.match(line) for line in kept[name].split("\n")):
            raise Invalid(f"{name!r} has no '- ' item")
    pairs = min(len(_BEFORE_RE.findall(kept["Examples"])), len(_AFTER_RE.findall(kept["Examples"])))
    if pairs < 2:
        raise Invalid(f"Examples has {pairs} Before/After pair(s); at least 2 are needed")
    return Profile(kept)


def render(profile: Profile, *, today: str, model: str, samples: int) -> str:
    meta: fm.Meta = {
        "title": TITLE,
        "source": f"voice-profile: local model {model} from {samples} cleaned sample(s) and style-stats.md",
        "added": today,
        "tags": ["writing", "voice-profile"],
        "summary": "How Noah writes: tone, sentences, word choice, structure, do and don't, two "
                   "examples. Used only for drafts Noah reviews, once he approves it.",
        "related": ["40-writing/style-stats.md", "40-writing/samples-text/MANIFEST.md"],
        "status": "draft",
        "generator": "voice-profile",
    }
    head = re.sub(r'(?m)^status: "draft"$', "status: draft", fm.dump(meta))
    parts = [f"# {TITLE}", NOTE]
    for section in SECTIONS:
        parts.append(f"## {section}\n\n{profile.sections[section]}")
    return head + "\n" + "\n\n".join(parts) + "\n"


def validate(reply: str, *, today: str, model: str, samples: int) -> Profile:
    """The profile to write (``.text``), or Invalid."""
    profile = check_reply(reply)
    text = render(profile, today=today, model=model, samples=samples)
    size = len(text.encode("utf-8"))
    if size > MAX_BYTES:
        raise Invalid(f"too large once rendered: {size} bytes (the limit is {MAX_BYTES})")
    problem = printable_problem(text)
    if problem:
        raise Invalid(f"not printable once rendered: {problem}")
    meta, body = fm.parse(text)
    if meta.get("status") != "draft":
        raise Invalid("the rendered header does not read back as a draft")  # defence in depth
    _split_sections(body)
    profile.text = text
    return profile
