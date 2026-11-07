"""Check the model's reply against the fixed template and render the SKILL.md that is filed.

Everything here is decided in code; the reply is untrusted text. A reply is refused (``Invalid``,
with a one-line reason) unless:

* it is at most 16 KiB and every character is printable (no control, format, private-use,
  unassigned or invisible characters; line breaks are the only whitespace other than space);
* its header has a kebab-case ``name`` (1-48 characters), a one-line ``description`` and
  ``version: 1``; the first body line is ``# <name>``;
* the sections When to use, Rules, Checklist, Examples and Sources are each present exactly once
  (headings may be indented up to 3 spaces, as Markdown allows), with no other ``##`` section, no
  second ``#`` title and no ``###`` heading named like a section;
* no line can change the rendered structure: no fenced code (``` or ~~~), no ``>`` blockquote, no
  heading indented 4+ spaces, nested in a list or written with look-alike characters, and no
  thematic break, setext underline or YAML-like ``---`` line;
* Rules has a numbered item, Checklist has an unticked ``- [ ] `` item and nothing box-like in
  any other shape, Examples has at least two ``Input:`` / ``Output:`` pairs;
* nothing it keeps contains a link, URL, ``//host`` address, web address, e-mail address, image,
  footnote, wiki link, HTML tag, HTML entity or ``]:`` (a link definition, even split over lines);
* no line outside Checklist looks like a checkbox (after NFKC folding and stripping any run of
  container prefixes: spaces, ``>``, list markers), so a draft cannot show something that reads like one of Noah's ticks.

The filed SKILL.md is then **re-rendered by code**: a header the drafter writes (the library's
required keys plus name, description, version: 1), ``# <name>``, the sections in template order,
and a Sources section that lists the ingested URLs as code spans. The model's own Sources text is
discarded unread. The rendered result is checked again (size, printable, checkboxes).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from forge.checklist import char_problem
from library import frontmatter as fm
from library import naming

from .prompt import SECTIONS

MAX_BYTES = 16 * 1024
DESCRIPTION_MAX = 300


class Invalid(ValueError):
    """The reply does not fit the template; the message is a safe one-line reason."""


@dataclass
class Draft:
    name: str
    description: str
    text: str            # the SKILL.md to file


# Container prefixes (blockquote markers, list markers followed by a space, spaces): what Markdown
# strips before deciding whether a line is a heading, fence, quote or task item.
_STRICT_PREFIX_RE = re.compile(r"^(?:[ \t>]|[-*+\u2022\u2023\u2043](?=[ \t]|$)|\d{1,9}[.)](?=[ \t]|$))*")
# For checkbox detection, any run of them, even without the space ("-[x]" is refused too).
_LOOSE_PREFIX_RE = re.compile(r"^(?:[ \t>]|[-*+\u2022\u2023\u2043]|\d{1,9}[.)])*")
_FENCE_START_RE = re.compile(r"^(?:`{3,}|~{3,})")
_HEADING_START_RE = re.compile(r"^#{1,6}(?:[ \t]|$)")
_TOP_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
# A line of one repeated rule/underline character: a thematic break ("---" can also open a
# YAML-like block), a setext underline, or an empty list item. None belongs in a card.
_RULE_LINE_RE = re.compile(r"^[ \t>]*([-=*_+])(?:[ \t]*\1)*[ \t]*$")
_OUTER_FENCE_RE = re.compile(r"^(```|~~~)[ \t]*(?:markdown|md)?[ \t]*$", re.IGNORECASE)
_H1_RE = re.compile(r"^ {0,3}#[ \t]+(.*?)[ \t]*#*[ \t]*$")

# Anything that renders as (or smuggles) a link, image or HTML in some Markdown viewer.
_LINKISH = [
    (re.compile(r"://"), "a URL"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "an e-mail address"),
    (re.compile(r"(?i)\bwww\."), "a web address"),
    (re.compile(r"(?i)\b(?:mailto|javascript|vbscript|file|tel|sms|blob):"), "a link scheme"),
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

# A line that reads like a checkbox once its container prefixes are stripped, after NFKC folding
# (full-width brackets become [ ], NBSP and ideographic spaces become spaces).
_BOX_RE = re.compile(r"^\[\s*\S?\s*\]")
_BALLOT_RE = re.compile(r"^[\u2610\u2611\u2612]")
_CHECK_ITEM_RE = re.compile(r"^- \[ \] \S")
_RULE_RE = re.compile(r"^\d+\.\s+\S")
_INPUT_RE = re.compile(r"(?im)^\s*(?:[-*+]\s+)?[*_]{0,2}input[*_]{0,2}\s*:")
_OUTPUT_RE = re.compile(r"(?im)(?:^\s*(?:[-*+]\s+)?|(?:→|->)\s*)[*_]{0,2}output[*_]{0,2}\s*:")


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
    """Why a body line could change the card's rendered structure, or None. Top-level ``#`` to
    ``######`` headings (up to 3 spaces of indent) pass here and are classified by the caller."""
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


def _unwrap(text: str) -> str:
    """Drop one code fence wrapped around the whole reply (``` or ```markdown)."""
    lines = text.split("\n")
    if len(lines) >= 2 and _OUTER_FENCE_RE.match(lines[0]) and lines[-1].strip() == lines[0][:3]:
        return "\n".join(lines[1:-1])
    return text


def _split_sections(body: str) -> tuple[str, dict[str, list[str]]]:
    """(the title text, {canonical section name: lines}). Raises Invalid."""
    lines = body.split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i == len(lines):
        raise Invalid("the body is empty")
    m = _H1_RE.match(lines[i])
    if not m:
        raise Invalid("the first body line must be the title '# <name>'")
    title = m.group(1)
    known = {s.casefold(): s for s in SECTIONS}
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for n, line in enumerate(lines[i + 1:], start=i + 2):
        problem = structure_problem(line)
        if problem:
            raise Invalid(f"{problem} (body line {n}): {line[:60]!r}")
        h = _TOP_HEADING_RE.match(line)
        if h:
            level = len(h.group(1))
            text = re.sub(r"(?:^|[ \t]+)#+$", "", h.group(2) or "").strip()
            key = text.rstrip(":").strip().casefold()
            if level == 1:
                raise Invalid("a second '#' title line")
            if level == 2:
                if key not in known:
                    raise Invalid(f"unexpected section {text!r} (the template has only: "
                                  f"{', '.join(SECTIONS)})")
                name = known[key]
                if name in sections:
                    raise Invalid(f"section {name!r} appears twice")
                current = sections[name] = []
                continue
            if key in known:
                raise Invalid(f"a {'#' * level} {text!r} heading looks like a section of the template")
        if current is not None:
            current.append(line)
    missing = [s for s in SECTIONS if s not in sections]
    if missing:
        raise Invalid(f"missing sections: {', '.join(missing)}")
    return title, sections


def _content(lines: list[str]) -> str:
    return "\n".join(lines).strip("\n")


def source_lines(urls: list[str]) -> list[str]:
    """The Sources section: every ingested URL as a code span (nothing renders as a link)."""
    out = []
    for url in urls:
        if "`" in url or "\n" in url or printable_problem(url):
            continue  # cannot happen for a canonical URL; never let one break the span
        out.append(f"- `{url}`")
    return out or ["- (none)"]


def check_reply(reply: str, *, expected_name: str | None = None) -> tuple[str, str, dict[str, str]]:
    """(name, description, {section: content}) from the model's reply, or Invalid."""
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
        meta, body = fm.parse(text + ("\n" if not text.endswith("\n") else ""))
    except fm.FrontMatterError as exc:
        raise Invalid(f"no valid '---' header ({exc})") from None
    name = meta.get("name")
    if not isinstance(name, str) or not naming.is_kebab_name(name.strip()):
        raise Invalid(f"name {name!r} is not kebab-case (a-z, 0-9 and single hyphens, 1-48 characters)")
    name = name.strip()
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise Invalid("the header has no description")
    description = " ".join(description.split())
    if len(description) > DESCRIPTION_MAX:
        raise Invalid(f"the description is longer than {DESCRIPTION_MAX} characters")
    if meta.get("version") != "1":
        raise Invalid(f"version must be 1 (got {meta.get('version')!r})")
    title, sections = _split_sections(body)
    if title.strip() != name:
        raise Invalid(f"the title line must be '# {name}' (got {title!r})")

    kept = {s: _content(sections[s]) for s in SECTIONS if s != "Sources"}  # model's Sources: discarded
    for what, value in [("description", description)] + list(kept.items()):
        problem = linkish_problem(value)
        if problem:
            raise Invalid(f"{what} contains {problem}")
    for section, value in kept.items():
        for n, line in enumerate(value.split("\n"), start=1):
            if section == "Checklist":
                if is_boxlike(line) and not _CHECK_ITEM_RE.match(line):
                    raise Invalid(f"Checklist line {n} is not an unticked '- [ ] item': {line[:60]!r}")
            elif is_boxlike(line):
                raise Invalid(f"a checkbox line outside Checklist ({section} line {n}): {line[:60]!r}")
    if not kept["When to use"].strip():
        raise Invalid("'When to use' is empty")
    if not any(_RULE_RE.match(line) for line in kept["Rules"].split("\n")):
        raise Invalid("Rules has no numbered item ('1. ...')")
    if not any(_CHECK_ITEM_RE.match(line) for line in kept["Checklist"].split("\n")):
        raise Invalid("Checklist has no '- [ ] item'")
    pairs = min(len(_INPUT_RE.findall(kept["Examples"])), len(_OUTPUT_RE.findall(kept["Examples"])))
    if pairs < 2:
        raise Invalid(f"Examples has {pairs} Input/Output pair(s); at least 2 are needed")
    if expected_name:
        name = expected_name  # a redraft keeps the skill's name; the code decides, not the model
    return name, description, kept


def render(name: str, description: str, kept: dict[str, str], *, rid: str, today: str,
           urls: list[str]) -> str:
    meta: fm.Meta = {
        "name": name,
        "description": description,
        "version": "1",
        "title": f"Skill: {name}",
        "source": f"skill drafter (local model) for forge request {rid}",
        "added": today,
        "tags": ["skill", "forge"],
        "summary": description,
        "related": [],
    }
    head = fm.dump(meta)
    head = re.sub(r'(?m)^version: "1"$', "version: 1", head)
    parts = [f"# {name}"]
    for section in SECTIONS:
        if section == "Sources":
            body = "\n".join(source_lines(urls)) + (
                "\n\nListed by the skill drafter from the forge's ingest record, not written by the model.")
        else:
            body = kept[section]
        parts.append(f"## {section}\n\n{body}")
    return head + "\n" + "\n\n".join(parts) + "\n"


def validate(reply: str, *, rid: str, today: str, urls: list[str],
             expected_name: str | None = None) -> Draft:
    """The SKILL.md to file, or Invalid."""
    name, description, kept = check_reply(reply, expected_name=expected_name)
    text = render(name, description, kept, rid=rid, today=today, urls=urls)
    size = len(text.encode("utf-8"))
    if size > MAX_BYTES:
        raise Invalid(f"too large once rendered: {size} bytes (the limit is {MAX_BYTES})")
    problem = printable_problem(text)
    if problem:
        raise Invalid(f"not printable once rendered: {problem}")
    meta, body = fm.parse(text)
    if meta.get("name") != name or meta.get("version") != "1":
        raise Invalid("the rendered header does not read back")  # defence in depth
    _, sections = _split_sections(body)
    for section, lines in sections.items():
        if section != "Checklist" and any(is_boxlike(line) for line in lines):
            raise Invalid(f"a checkbox line outside Checklist once rendered ({section})")
    return Draft(name, description, text)
