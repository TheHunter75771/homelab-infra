"""HTML to plain text with the stdlib parser: nothing is executed, rendered or fetched.

The contents of script, style, noscript, nav, form, svg, iframe, template and head are dropped
(the document title is kept separately). Entities are decoded, source line breaks are spaces
(except inside ``pre``), block elements become line breaks, runs of spaces collapse to one,
and at most one blank line separates paragraphs.
Control characters (other than newline) are removed so the stored text is safe to print.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

DROP = frozenset({"script", "style", "noscript", "nav", "form", "svg", "iframe", "template",
                  "head", "object", "embed", "select", "button", "math"})
BLOCK = frozenset({
    "p", "div", "br", "hr", "li", "ul", "ol", "dl", "dt", "dd", "tr", "table", "thead", "tbody",
    "section", "article", "aside", "header", "footer", "main", "h1", "h2", "h3", "h4", "h5", "h6",
    "pre", "blockquote", "figure", "figcaption", "address", "details", "summary", "caption", "td", "th",
})
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")
_ANY_SPACE_RE = re.compile(r"\s+")
_SPACES_RE = re.compile(r"[ \t\f\v\u00a0\u2000-\u200a\u202f\u205f\u3000]+")


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self._drop: dict[str, int] = {}
        self._in_title = 0
        self._pre = 0

    def _dropping(self) -> bool:
        return any(self._drop.values())

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in DROP:
            self._drop[tag] = self._drop.get(tag, 0) + 1
        elif tag == "title":
            self._in_title += 1
        elif tag == "pre":
            self._pre += 1
        if tag in BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        if tag in BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP and self._drop.get(tag):
            self._drop[tag] -= 1
        elif tag == "title" and self._in_title:
            self._in_title -= 1
        elif tag == "pre" and self._pre:
            self._pre -= 1
        if tag in BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
            return
        if not self._dropping():
            # Outside <pre>, line breaks in the source are just spaces (as a browser shows them).
            self.parts.append(data if self._pre else _ANY_SPACE_RE.sub(" ", data))


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    lines = [_SPACES_RE.sub(" ", line).strip() for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out) + ("\n" if out else "")


def html_to_text(html: str) -> tuple[str, str]:
    """(text, title) of an HTML document."""
    parser = _Extractor()
    parser.feed(html)
    parser.close()
    title = " ".join(clean_text("".join(parser.title)).split())
    return clean_text("".join(parser.parts)), title
