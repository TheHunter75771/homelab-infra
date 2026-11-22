"""The AI-summary guard (CONTRACTS section 12.18.2, Lead decision D3 after Reviewer H).

The summary may contain NO numbers at all. Every figure in the entry comes from code (the
headline and the tables), so a model can never state a wrong amount, a right amount for the
wrong row, or a flipped sign. A reply is dropped ("did not match the figures") if it has:

* any numeral in any script: ASCII or other digits, superscripts, fractions, Roman-numeral
  characters (checked on the raw text and again after NFKC with invisibles removed, so a
  zero-width character or a math-alphabet digit can't hide one);
* a spelled-out cardinal or ordinal (zero ... ninety, hundred, thousand, first, twentieth, ...),
  a multiplier or fraction word (half, double, twice, quarter, ...), a count word (couple,
  few, several, both, none, nothing, nil, umpteen, ...) or a money word (dollar, buck, penny,
  nickel, dime, cent, quarter); inflected forms too ("hundreds", "tenfold", "doubling");
* a Roman numeral written with letters: 2+ letters in any case (II, xii), or a single upper-case
  V, X, L, C, D or M;
* more than ``MAX_SENTENCES`` sentences, counted on the raw text: ``.``, ``!``, ``?``, ``;``, the
  ellipsis, the fullwidth and CJK stops, newlines and list markers all end one;
* more than ``MAX_CHARS`` characters.

Deliberately spelled evasions (Cyrillic look-alike letters, a hyphen inside a word, accents,
"l0w") are accepted as residual (Lead, round 2). What survives is cleaned (``text.clean``): one
line, no markup. A wrong statement without numbers
("the sync is healthy") can't be caught here; the code headline above it states the real status,
and the label says the table is authoritative.
"""
from __future__ import annotations

import re
import unicodedata

from .text import _is_invisible, clean

MAX_SENTENCES = 5
MAX_CHARS = 1200

SKIPPED = "AI summary skipped (did not match the figures)"
UNAVAILABLE = "AI summary skipped (local model unavailable)"  # broker error or timeout
NO_FIGURES = "AI summary skipped (no figures available)"  # Money Hub gave no facts at all

_CARDINALS = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty thirty forty fourty fifty sixty seventy eighty ninety hundred thousand "
    "million billion trillion"
).split()
_ORDINALS = (
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth "
    "fifteenth sixteenth seventeenth eighteenth nineteenth twentieth thirtieth fortieth fiftieth sixtieth "
    "seventieth eightieth ninetieth hundredth thousandth millionth billionth"
).split()
_QUANTITY = (
    "half halves halved halving quarter quarters third thirds double doubled doubles triple tripled quadruple "
    "twice thrice once dozen dozens score grand couple pair pairs few several both single none nothing nobody "
    "percent percentage percentages per-cent k trio quartet nil zilch nought naught zero umpteen umpteenth "
    "gazillion zillion bazillion decade fortnight century"
).split()
# Money words: an amount without digits ("over by a dollar") is still a number (Lead, round 2).
_CURRENCY = "dollar buck penny pennies nickel dime cent quarter".split()
NUMBER_WORDS = frozenset(_CARDINALS + _ORDINALS + _QUANTITY + _CURRENCY)
# Endings stripped before the lookup, so inflections are caught too: "hundreds", "twenties", "tenfold",
# "doubling", "tripled", "thirds".
_ENDINGS = ("fold", "ies", "es", "s", "ing", "ed")
_WORD_RE = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*")  # letter runs, hyphenated compounds kept together
# Runs of 2+ Roman letters in any case ("XII", "xii"); a single letter only in upper case and never
# "I", so the pronoun is fine (Lead, round 2).
_ROMAN_RE = re.compile(r"(?i)\b(?=[IVXLCDM]{2,}\b)M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})\b")
_SINGLE_ROMAN_RE = re.compile(r"\b[VXLCDM]\b")  # "Category V"; a lone "I" is the pronoun
# Sentence ends, on the raw text. List markers count too: a line (or text) starting with -, *, +, a
# bullet, or a number (numbers are refused anyway).
_END_RE = re.compile(r"[.!?;\u2026\uff01\uff1f\uff1b\u3002\u203c\u2047\u2048\u2049]+|\r\n|\r|\n|\u2028|\u2029")
_LIST_MARKER_RE = re.compile(r"(?:^|\s)[-*+\u2022\u2023\u2043\u25e6](?=\s)")


def _has_numeral(text: str) -> bool:
    for ch in text:
        if "0" <= ch <= "9" or unicodedata.category(ch) in ("Nd", "Nl", "No"):
            return True
        if unicodedata.numeric(ch, None) is not None:  # e.g. CJK numerals (category Lo)
            return True
    return False


def sentence_count(raw: str) -> int:
    marked = _LIST_MARKER_RE.sub(".", raw)
    return sum(1 for part in _END_RE.split(marked) if any(ch.isalnum() for ch in part))


def stems(word: str) -> set[str]:
    """The word and its forms with one or two endings stripped ("tenfold" -> "ten",
    "doubling" -> "doubl"/"double", "twenties" -> "twenty", "triples" -> "tripl"/"triple")."""
    out = {word}
    for _ in range(2):
        for w in list(out):
            for end in _ENDINGS:
                if w.endswith(end) and len(w) > len(end) + 1:
                    base = w[: -len(end)]
                    out.add(base)
                    if end == "ies":
                        out.add(base + "y")
                    if end in ("ing", "ed", "es", "s"):
                        out.add(base + "e")
    return out


def _number_word(text: str) -> str | None:
    for m in _WORD_RE.finditer(text):
        word = m.group().lower()
        for part in [word] + word.split("-"):
            if stems(part) & NUMBER_WORDS:
                return word
    if _ROMAN_RE.search(text) or _SINGLE_ROMAN_RE.search(text):
        return "roman numeral"
    return None


def check(raw: object) -> tuple[str | None, str]:
    """(clean summary or None, reason)."""
    if not isinstance(raw, str) or not raw.strip():
        return None, "empty reply"
    if len(raw) > MAX_CHARS:
        return None, "reply too long"
    if sentence_count(raw) > MAX_SENTENCES:
        return None, f"more than {MAX_SENTENCES} sentences"
    visible = unicodedata.normalize("NFKC", "".join(ch for ch in raw if not _is_invisible(ch)))
    if _has_numeral(raw) or _has_numeral(visible):
        return None, "contains a number"
    word = _number_word(visible)
    if word:
        return None, "contains a number word"
    text = clean(raw, MAX_CHARS + 1)
    if not text or len(text) > MAX_CHARS:
        return None, "reply too long"
    return text, "ok"
