"""Clean and dedupe the samples (CONTRACTS §12.21.2 step 2). Pure functions, no I/O.

* **Cover boilerplate:** the leading lines before the essay starts: title-page styles, name /
  course / instructor / date / institution lines, and short title-like lines (see ``strip_cover``).
* **References:** a "References" / "Works Cited" / "Bibliography" heading and everything after it,
  plus hanging-indent entries anywhere (reference-list formatting).
* **Someone else's words:** block quotes (a quote-like style id or style name, also through the
  style's basedOn chain, or a left indent of at least 0.5 inch, set directly or by the style, on a
  non-list paragraph), a paragraph that is one quotation, and any quoted span longer than one
  sentence or than ``LONG_QUOTE_WORDS`` words (replaced by ``QUOTE_MARK``). Quoted spans are found
  by a linear scan: “double”, "straight" and mismatched “mixed" pairs, ‘single’ quotes (a ’ followed
  by a letter is an apostrophe), and a “, „ or ‘ never closed in the paragraph runs to its end. Tables
  never reach this module (the reader drops them).
* **Peer reviews:** after NFKC and folding dashes to "-", a file name, heading or short top line
  matching ``peer<sep>(review|evaluation|feedback|critique|assessment)``, "workshop feedback" or
  "review of <name>('s) draft" excludes the document, unless its file name is listed in
  ``40-writing/include.txt``.
* **Near-duplicates:** 5-word shingles over the whole cleaned document; documents with Jaccard
  similarity >= the threshold form a group (transitively) and only the best is kept: the longest,
  then the latest (mtime), then the name. Then, paragraph by paragraph in that same order, a
  paragraph whose shingles match an already kept paragraph (Jaccard >= the threshold) is dropped,
  so shared passages across different papers count once.
"""
from __future__ import annotations

import re
import unicodedata
import zlib
from dataclasses import dataclass, field

from .chars import visible
from .docx import Paragraph

SHINGLE = 5
LONG_QUOTE_WORDS = 40
QUOTE_MARK = "[quotation omitted]"
BLOCK_INDENT = 720            # twips: 0.5 inch
COVER_MAX_PARAS = 15          # the cover block never extends past this many paragraphs
COVER_LINE_MAX_WORDS = 14

REFERENCE_HEADINGS = frozenset({
    "references", "reference list", "works cited", "work cited", "bibliography", "works consulted",
    "literature cited", "sources cited", "citations",
})
_PEER_RE = re.compile(
    r"peer[\W_]*(?:review|evaluation|feedback|critique|assessment)"
    r"|workshop[\W_]*feedback"
    r"|review[\W_]+of[\W_]+(?:[a-z]+[\W_]+){0,2}?[a-z]+(?:['’]s)?[\W_]+(?:rough[\W_]+|final[\W_]+|first[\W_]+)?draft",
    re.IGNORECASE)
_DASHES = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"})
COMMON_SHINGLE = 10           # a shingle in more paragraphs than this is not used to find duplicates
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*")
_MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
           "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
_COVER_PATTERNS = [
    re.compile(rf"(?i)\b(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b"),     # October 27, 2026
    re.compile(rf"(?i)\b\d{{1,2}}\s+(?:{_MONTHS})\.?,?\s+\d{{4}}\b"),                       # 27 October 2026
    re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b"),                                     # 10/27/2026
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b[A-Z]{2,5}[ -]?\d{3}[A-Z]?\b"),                                          # ABC 101, XYZ 210
    re.compile(r"(?i)\b(?:professor|prof\.|instructor|teacher|dr\.|mr\.|mrs\.|ms\.)"),
    re.compile(r"(?i)\b(?:university|college|department|school of|campus|worldwide)\b"),
    re.compile(r"(?i)^\s*(?:name|student|course|class|section|instructor|professor|date|due|"
               r"due date|submitted|assignment|word count|running head)\b\s*:?"),
]
_HEADING_STYLE_RE = re.compile(r"(?i)^(?:heading\s*\d|title|subtitle)$")
_COVER_STYLE_RE = re.compile(r"(?i)^(?:title|subtitle|author|date|address|coverpage)")
_QUOTE_STYLE_RE = re.compile(r"(?i)quote|blocktext|block\s*text|epigraph")
_BIBLIO_STYLE_RE = re.compile(r"(?i)bibliography|reference")
_SENTENCE_END_RE = re.compile(r"[.!?][\"”’)]*\s+[A-Z“\"(]")


@dataclass
class Sample:
    """One sample document as read from ``40-writing/samples/``."""
    name: str                       # the file name, e.g. "Essay 2 final.docx"
    mtime: float
    paragraphs: list[Paragraph]


@dataclass
class Cleaned:
    name: str
    mtime: float
    paragraphs: list[str]           # cleaned paragraphs ("## " prefix marks a heading)
    cover_lines: int = 0
    reference_paras: int = 0
    block_quotes: int = 0
    quotes_removed: int = 0
    duplicate_paras: int = 0
    reference_heading: str = ""     # e.g. "references", "works cited" (casefolded)

    @property
    def words(self) -> int:
        return sum(len(words(p)) for p in self.paragraphs if not p.startswith("## "))

    def body(self) -> str:
        return "\n\n".join(self.paragraphs)


@dataclass
class Outcome:
    kept: list[Cleaned] = field(default_factory=list)
    merged: list[tuple[str, str, float]] = field(default_factory=list)    # (dropped, kept, jaccard)
    excluded: list[tuple[str, str]] = field(default_factory=list)         # (name, reason)


def words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def _is_heading_style(style: str) -> bool:
    return bool(_HEADING_STYLE_RE.match(style.replace(" ", "")) or _HEADING_STYLE_RE.match(style))


def _ends_sentence(line: str) -> bool:
    return line.rstrip().endswith((".", "!", "?", ".”", ".\"", "?”", "!”"))


# ---------------------------------------------------------------------- peer reviews

def _peer(text: str) -> bool:
    return bool(_PEER_RE.search(unicodedata.normalize("NFKC", text).translate(_DASHES)))


def is_peer_review(sample: Sample) -> bool:
    """True if the file name, a heading-styled paragraph, or a short line near the top marks the
    document as a review of someone else's work."""
    stem = sample.name.rsplit(".", 1)[0]
    if _peer(stem):
        return True
    for i, para in enumerate(sample.paragraphs):
        for line in para.text.split("\n"):
            if not _peer(line):
                continue
            short = len(words(line)) <= COVER_LINE_MAX_WORDS
            if _is_heading_style(para.style) or (short and (i < COVER_MAX_PARAS or not _ends_sentence(line))):
                return True
    return False


def parse_include(text: str) -> set[str]:
    """File names Noah lists in ``include.txt``: one per line, ``#`` comments, compared by casefold."""
    out = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line.casefold())
    return out


# ---------------------------------------------------------------------- one document

def _is_cover_line(line: str) -> bool:
    """A short line that names a person, course, instructor, school or date, or a short line with
    no sentence ending (a title, a name or a heading on a title page). Prose is never short."""
    n = len(words(line))
    if not 0 < n <= COVER_LINE_MAX_WORDS:
        return False
    return any(p.search(line) for p in _COVER_PATTERNS) or not _ends_sentence(line)


def strip_cover(paras: list[Paragraph]) -> tuple[list[Paragraph], int]:
    """(paragraphs after the cover block, lines dropped). The cover block is the run of leading
    paragraphs, at most ``COVER_MAX_PARAS``, whose every line is a cover line (blank lines and
    title-page styles included); it ends at the first line that reads like prose."""
    dropped = 0
    for i, para in enumerate(paras[:COVER_MAX_PARAS]):
        lines = [ln for ln in para.text.split("\n") if ln.strip()]
        if not lines:
            continue
        if _COVER_STYLE_RE.match(para.style) or all(_is_cover_line(ln) for ln in lines):
            dropped += len(lines)
            continue
        # A paragraph that opens with cover lines (an MLA heading typed with line breaks).
        keep = list(lines)
        while keep and _is_cover_line(keep[0]) and len(keep) > 1:
            keep.pop(0)
            dropped += 1
        rest = Paragraph("\n".join(keep), para.style, para.left, para.hanging, para.first_line, para.is_list)
        return [rest] + paras[i + 1:], dropped
    return paras[COVER_MAX_PARAS:] if len(paras) > COVER_MAX_PARAS else [], dropped


def is_reference_heading(para: Paragraph) -> bool:
    text = " ".join(para.text.split()).strip().rstrip(":").strip().casefold()
    return text in REFERENCE_HEADINGS


def is_block_quote(para: Paragraph) -> bool:
    if _QUOTE_STYLE_RE.search(para.style) or _QUOTE_STYLE_RE.search(para.style_name):
        return True
    return not para.is_list and para.left >= BLOCK_INDENT and para.hanging == 0


def is_reference_entry(para: Paragraph) -> bool:
    return bool(_BIBLIO_STYLE_RE.search(para.style)) or (para.hanging > 0 and not para.is_list)


_OPEN_DOUBLE = "“\"„"
_CLOSE_DOUBLE = "”\""


def quoted_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of each top-level quoted span, in one linear pass. Double quotes pair any
    opener (“ " „) with any closer (” "), so mismatched pairs count; ‘ opens a single-quoted span
    that a ’ closes when no letter follows it (otherwise it is an apostrophe); an opener never
    closed runs to the end of the text, except a straight " (it may be an inch mark)."""
    spans: list[tuple[int, int]] = []
    start = -1
    kind = ""
    n = len(text)
    for i, ch in enumerate(text):
        if start < 0:
            if ch in _OPEN_DOUBLE:
                start, kind = i, "d"
            elif ch == "‘":
                start, kind = i, "s"
        elif ((kind == "d" and ch in _CLOSE_DOUBLE)
              or (kind == "s" and ch == "’" and not (i + 1 < n and text[i + 1].isalpha()))):
            spans.append((start, i + 1))
            start = -1
    if start >= 0 and text[start] != '"':   # a stray straight " (an inch mark?) is not a quotation
        spans.append((start, n))
    return spans


def _long_quote(span: str) -> bool:
    inner = span[1:-1] if len(span) > 1 else ""
    return len(words(inner)) > LONG_QUOTE_WORDS or bool(_SENTENCE_END_RE.search(inner))


def remove_quotes(text: str) -> tuple[str, int]:
    """(text with long quoted spans replaced by QUOTE_MARK, spans removed)."""
    out: list[str] = []
    pos = count = 0
    for a, b in quoted_spans(text):
        if _long_quote(text[a:b]):
            out.append(text[pos:a])
            out.append(QUOTE_MARK)
            pos = b
            count += 1
    out.append(text[pos:])
    return "".join(out), count


def _whole_quotation(text: str) -> bool:
    """A paragraph that is one quotation (perhaps with a trailing citation)."""
    t = text.strip()
    if not t or t[0] not in _OPEN_DOUBLE + "‘":
        return False
    spans = quoted_spans(t)
    return bool(spans) and spans[0][0] == 0 and len(words(t[spans[0][1]:])) <= 8


def clean_document(sample: Sample) -> Cleaned:
    out = Cleaned(sample.name, sample.mtime, [])
    paras, out.cover_lines = strip_cover(sample.paragraphs)
    for i, para in enumerate(paras):
        if is_reference_heading(para):
            out.reference_paras += len(paras) - i
            out.reference_heading = " ".join(para.text.split()).strip().rstrip(":").strip().casefold()
            break
        text = visible(para.text)
        text = "\n".join(" ".join(line.split()) for line in text.split("\n"))
        text = "\n".join(line for line in text.split("\n") if line).strip()
        if not text:
            continue
        if is_reference_entry(para):
            out.reference_paras += 1
            continue
        if is_block_quote(para) or _whole_quotation(text):
            out.block_quotes += 1
            continue
        text, n = remove_quotes(text)
        out.quotes_removed += n
        if not words(text.replace(QUOTE_MARK, "")):
            continue
        if _is_heading_style(para.style) or (len(words(text)) <= 8 and not _ends_sentence(text)
                                              and "\n" not in text and not para.is_list):
            text = "## " + text
        elif para.is_list:
            text = "- " + text
        out.paragraphs.append(text.replace("\n", " "))
    return out


# ---------------------------------------------------------------------- near-duplicates

def shingles(text: str, n: int = SHINGLE) -> set[int]:
    """The text's ``n``-word shingles (casefolded), each as a 64-bit-ish CRC pair: small ints, not
    tuples of strings, so a big corpus stays small in memory; deterministic across runs."""
    toks = [w.casefold() for w in words(text.replace(QUOTE_MARK, " "))]
    if len(toks) < n:
        return {_h(" ".join(toks))} if toks else set()
    return {_h(" ".join(toks[i:i + n])) for i in range(len(toks) - n + 1)}


def _h(s: str) -> int:
    b = s.encode("utf-8")
    return (zlib.crc32(b) << 32) | zlib.adler32(b)


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _rank(doc: Cleaned) -> tuple:
    """Best first: longest, then latest, then name (deterministic)."""
    return (-doc.words, -doc.mtime, doc.name)


def dedupe(docs: list[Cleaned], threshold: float) -> tuple[list[Cleaned], list[tuple[str, str, float]]]:
    """(kept documents best-first, merges as (dropped, kept, jaccard with the kept one))."""
    ordered = sorted(docs, key=_rank)
    sets = [shingles(d.body()) for d in ordered]
    parent = list(range(len(ordered)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            if jaccard(sets[i], sets[j]) >= threshold:
                a, b = find(i), find(j)
                if a != b:
                    parent[max(a, b)] = min(a, b)     # the better-ranked one stays the root
    kept, merged = [], []
    for i, doc in enumerate(ordered):
        root = find(i)
        if root == i:
            kept.append(doc)
        else:
            merged.append((doc.name, ordered[root].name, round(jaccard(sets[i], sets[root]), 3)))
    return kept, merged


def dedupe_paragraphs(docs: list[Cleaned], threshold: float) -> None:
    """Drop, in place, paragraphs that repeat one already kept in a better-ranked document (or
    earlier in the same one). Headings are left alone. Candidates come from an inverted index of
    shingles; a shingle seen in more than ``COMMON_SHINGLE`` kept paragraphs is too common to
    point at a duplicate and is dropped from the index, so the work stays near-linear."""
    index: dict[int, list[int] | None] = {}
    seen: list[set] = []
    for doc in docs:
        kept = []
        for para in doc.paragraphs:
            if para.startswith("## "):
                kept.append(para)
                continue
            sh = shingles(para)
            candidates: set[int] = set()
            for s in sh:
                hits = index.get(s)
                if hits:
                    candidates.update(hits)
            if sh and any(jaccard(sh, seen[k]) >= threshold for k in candidates):
                doc.duplicate_paras += 1
                continue
            kept.append(para)
            k = len(seen)
            for s in sh:
                hits = index.get(s, [])
                if hits is None:
                    continue
                if len(hits) >= COMMON_SHINGLE:
                    index[s] = None     # common: never a candidate again
                else:
                    hits.append(k)
                    index[s] = hits
            seen.append(sh)
        doc.paragraphs = kept


def clean_all(samples: list[Sample], *, include: set[str], threshold: float, min_words: int) -> Outcome:
    out = Outcome()
    docs = []
    for sample in sorted(samples, key=lambda s: s.name):
        if is_peer_review(sample) and sample.name.casefold() not in include:
            out.excluded.append((sample.name, ("peer review of someone else's work (list it in "
                                              "include.txt to keep it)")))
            continue
        docs.append(clean_document(sample))
    kept, out.merged = dedupe(docs, threshold)
    dedupe_paragraphs(kept, threshold)
    for doc in kept:
        if doc.words < min_words:
            why = f"only {doc.words} words of Noah's own text after cleaning (minimum {min_words})"
            if doc.duplicate_paras:
                why += f"; {doc.duplicate_paras} paragraph(s) repeat other samples"
            out.excluded.append((doc.name, why))
        else:
            out.kept.append(doc)
    return out
