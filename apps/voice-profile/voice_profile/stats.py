"""Style statistics, computed in code (CONTRACTS §12.21.2 step 3). Pure functions, no I/O.

Over the kept, cleaned samples (headings, marked ``## ``, are left out of every count):

* sentence length (words) and paragraph length (words, sentences) distributions;
* vocabulary richness: type/token ratio, a moving-average TTR (window ``MATTR_WINDOW``), the share
  of words used once, mean word length;
* the most-used transitions (a fixed list) and sentence openers (first word, first two words),
  and how often a sentence opens with a transition, "I" or a subordinating word;
* punctuation habits per 1000 words, and commas per sentence;
* first- vs third-person pronouns ("it" is not counted as a person), second person;
* contractions per 1000 words, and the share of negations written as "n't";
* a passive-voice estimate: the share of sentences with a form of "be"/"get" followed by a past
  participle (regular -ed, or a fixed irregular list), allowing one -ly adverb between;
* citation habits: APA (author, year), APA narrative, MLA (author page), numeric [n], and the
  reference-list headings found while cleaning.

Percentiles use the nearest-rank method; the median is the usual middle value (the mean of the two
middle values for an even count). Every figure is deterministic.
"""
from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from dataclasses import dataclass

from .clean import QUOTE_MARK

MATTR_WINDOW = 100
TOP = 10
SENTENCE_BUCKETS = ((1, 10), (11, 20), (21, 30), (31, 40), (41, None))

_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*")
_ALPHA_RE = re.compile(r"[A-Za-z]")
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e", "al", "p", "pp",
    "no", "vol", "fig", "u.s", "inc", "ltd", "co", "approx", "dept", "gen", "sgt", "cpl", "lt",
    "col", "capt", "maj", "cf", "ed", "eds", "n.d",
})
# A break: a run of . ! ? … (never entered mid-run, and possessive, so a long run of dots or
# spaces costs linear time), closing quotes/brackets, whitespace, then a capital, digit or opener.
_BREAK_RE = re.compile(r"(?<![.!?…])[.!?…]++[\"”’)\]]*+\s++(?=[\"“‘(\[]?[A-Z0-9])")
_LAST_TOKEN_RE = re.compile(r"(?<![A-Za-z.])([A-Za-z][A-Za-z.]{0,30})\.$")
TOKEN_WINDOW = 40         # an abbreviation or initial is looked for in this many characters only

TRANSITIONS = (
    "however", "therefore", "thus", "hence", "moreover", "furthermore", "additionally", "also",
    "in addition", "for example", "for instance", "in fact", "in conclusion", "to conclude",
    "in summary", "overall", "ultimately", "finally", "first", "second", "third", "lastly", "next",
    "then", "meanwhile", "similarly", "likewise", "in contrast", "on the other hand", "instead",
    "although", "even though", "because", "since", "as a result", "consequently", "so",
    "in other words", "specifically", "in particular", "of course", "nevertheless", "nonetheless",
    "still", "yet", "besides", "after all", "at the same time", "to be clear", "honestly",
)
SUBORDINATORS = frozenset({
    "although", "because", "since", "when", "while", "if", "after", "before", "as", "though",
    "unless", "until", "whereas", "once",
})
FIRST_SINGULAR = frozenset({"i", "me", "my", "mine", "myself"})
FIRST_PLURAL = frozenset({"we", "us", "our", "ours", "ourselves"})
SECOND = frozenset({"you", "your", "yours", "yourself", "yourselves"})
THIRD = frozenset({"he", "him", "his", "himself", "she", "her", "hers", "herself", "they", "them",
                   "their", "theirs", "themselves"})
_CONTRACTION_RE = re.compile(
    r"(?i)\b(?:[a-z]+n['’]t|[a-z]+['’](?:re|ve|ll|d|m)|(?:it|that|there|here|what|who|where|he|she|"
    r"let|how|everyone|nobody|something)['’]s)\b")
_NT_RE = re.compile(r"(?i)\b[a-z]+n['’]t\b")
_NOT_RE = re.compile(r"(?i)\b(?:not|cannot)\b")
_IRREGULAR = (
    "been|begun|bitten|blown|born|borne|bought|brought|built|caught|chosen|come|done|drawn|driven|"
    "eaten|fallen|felt|forgotten|found|given|gone|grown|heard|held|hidden|hit|hurt|kept|known|laid|"
    "led|left|lent|lost|made|meant|met|paid|put|read|run|said|seen|sent|set|shown|shut|sold|spent|"
    "spoken|stolen|taken|taught|told|thought|thrown|understood|won|worn|written")
_PASSIVE_RE = re.compile(
    rf"(?i)\b(?:am|is|are|was|were|be|been|being|get|gets|got|gotten|getting)\s+(?:[a-z]+ly\s+)?"
    rf"(?:[a-z]+ed|{_IRREGULAR})\b")
_NAME = r"[A-Z][A-Za-z'’\-]+"
_AUTHORS = rf"{_NAME}(?: et al\.|,? (?:&|and) {_NAME})?"
_CITATION_PATTERNS = (
    ("APA parenthetical (Author, 2020)",
     re.compile(rf"\({_AUTHORS}, (?:\d{{4}}|n\.d\.)[a-z]?(?:, (?:p|pp|para)\. ?\d+(?:[-–]\d+)?)?"
                rf"(?:; {_AUTHORS}, (?:\d{{4}}|n\.d\.)[a-z]?)*\)")),
    ("APA narrative Author (2020)", re.compile(rf"\b{_AUTHORS} \((?:\d{{4}}|n\.d\.)[a-z]?\)")),
    ("MLA (Author 23)", re.compile(rf"\({_AUTHORS} \d+(?:[-–]\d+)?\)")),
    ("numeric [1]", re.compile(r"\[\d+(?:\s*[,–-]\s*\d+)*\]")),
)
_PUNCTUATION = (
    ("commas", re.compile(r",")),
    ("semicolons", re.compile(r";")),
    ("colons", re.compile(r":(?!\d)")),
    ("em dashes (— or --)", re.compile(r"—|(?<!-)--(?!-)")),
    ("spaced en/hyphen dashes ( – )", re.compile(r" [–-] ")),
    ("parentheses", re.compile(r"\(")),
    ("question marks", re.compile(r"\?")),
    ("exclamation marks", re.compile(r"!")),
    ("ellipses", re.compile(r"\.\.\.|…")),
)


def words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def sentences(paragraph: str) -> list[str]:
    """Split a paragraph into sentences at . ! ? (and …) followed by space and a capital, digit or
    opening quote, except after a known abbreviation or a single-letter initial. Linear time: each
    break looks back at most ``TOKEN_WINDOW`` characters."""
    out, start = [], 0
    for m in _BREAK_RE.finditer(paragraph):
        end = m.start() + 1
        last = _LAST_TOKEN_RE.search(paragraph, max(start, end - TOKEN_WINDOW), end)
        if paragraph[m.start()] == "." and last:
            token = last.group(1).casefold()
            if token in _ABBREVIATIONS or (len(token) == 1 and token.isalpha()):
                continue
        out.append(paragraph[start:m.end()].strip())
        start = m.end()
    tail = paragraph[start:].strip()
    if tail:
        out.append(tail)
    return [s for s in out if words(s)]


def percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile of an ascending list (p in 0-100)."""
    if not sorted_values:
        return 0
    rank = max(1, math.ceil(p / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


@dataclass
class Dist:
    count: int
    mean: float
    median: float
    p10: float
    p25: float
    p75: float
    p90: float
    min: float
    max: float

    @classmethod
    def of(cls, values: list[float]) -> Dist:
        if not values:
            return cls(0, 0, 0, 0, 0, 0, 0, 0, 0)
        v = sorted(values)
        return cls(len(v), round(statistics.fmean(v), 2), statistics.median(v), percentile(v, 10),
                   percentile(v, 25), percentile(v, 75), percentile(v, 90), v[0], v[-1])

    def line(self, unit: str) -> str:
        return (f"mean {_num(self.mean)} {unit}, median {_num(self.median)}, p10 {_num(self.p10)}, "
                f"p25 {_num(self.p25)}, p75 {_num(self.p75)}, p90 {_num(self.p90)}, "
                f"min {_num(self.min)}, max {_num(self.max)} (n = {self.count})")


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.2f}".rstrip("0").rstrip(".")


def per_1000(count: int, total_words: int) -> float:
    return round(count * 1000 / total_words, 2) if total_words else 0.0


def share(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 0.0


def mattr(tokens: list[str], window: int = MATTR_WINDOW) -> float:
    """Moving-average type/token ratio; the plain TTR when there are fewer tokens than the window."""
    if not tokens:
        return 0.0
    if len(tokens) <= window:
        return round(len(set(tokens)) / len(tokens), 3)
    counts = Counter(tokens[:window])
    total = len(counts)
    for i in range(window, len(tokens)):
        old, new = tokens[i - window], tokens[i]
        counts[old] -= 1
        if counts[old] == 0:
            del counts[old]
        counts[new] += 1
        total += len(counts)
    return round(total / ((len(tokens) - window + 1) * window), 3)


def _phrase_re(phrase: str) -> re.Pattern[str]:
    return re.compile(r"(?i)\b" + r"\s+".join(map(re.escape, phrase.split())) + r"\b")


_TRANSITION_RES = [(t, _phrase_re(t)) for t in TRANSITIONS]


_STARTS_TRANSITION_RE = re.compile(
    r"\s*(?:" + "|".join(r"\s+".join(map(re.escape, t.split()))
                         for t in sorted(TRANSITIONS, key=len, reverse=True)) + r")\b")


def starts_with_transition(sentence: str) -> bool:
    return bool(_STARTS_TRANSITION_RE.match(sentence.lstrip("\"“‘(").casefold()))


@dataclass
class Stats:
    documents: int
    paragraphs: int
    sentences: int
    words: int
    sentence_words: Dist
    sentence_buckets: list[tuple[str, int, float]]
    paragraph_words: Dist
    paragraph_sentences: Dist
    ttr: float
    mattr: float
    hapax_share: float
    mean_word_length: float
    transitions: list[tuple[str, int, float]]
    openers: list[tuple[str, int]]
    openers2: list[tuple[str, int]]
    open_transition: float
    open_i: float
    open_subordinate: float
    punctuation: list[tuple[str, int, float]]
    commas_per_sentence: float
    first_singular: float
    first_plural: float
    second: float
    third: float
    first_share: float
    contractions: float
    nt_share: float
    passive_share: float
    citations: list[tuple[str, int]]
    citation_style: str
    reference_headings: list[tuple[str, int]]


def compute(texts: list[list[str]], reference_headings: list[str] | None = None) -> Stats:
    """``texts``: per document, its cleaned paragraphs."""
    paras = [p for doc in texts for p in doc if not p.startswith("## ")]
    paras = [(p.removeprefix("- ")).replace(QUOTE_MARK, " ") for p in paras]
    sents = [s for p in paras for s in sentences(p)]
    para_sents = [len(sentences(p)) for p in paras]
    sent_words = [len(words(s)) for s in sents]
    lower = [w.casefold() for p in paras for w in words(p)]   # one word list, not three
    total = len(lower)
    joined = "\n".join(paras)
    alpha = [w for w in lower if _ALPHA_RE.search(w)]
    counts = Counter(alpha)

    buckets = []
    for lo, hi in SENTENCE_BUCKETS:
        n = sum(1 for x in sent_words if x >= lo and (hi is None or x <= hi))
        buckets.append((f"{lo}-{hi}" if hi else f"{lo}+", n, share(n, len(sent_words))))

    trans = []
    for phrase, rx in _TRANSITION_RES:
        n = len(rx.findall(joined))
        if n:
            trans.append((phrase, n, per_1000(n, total)))
    trans.sort(key=lambda t: (-t[1], t[0]))

    first = [ws[0].casefold() for ws in (words(s) for s in sents) if ws]
    first2 = [" ".join(w.casefold() for w in ws[:2]) for ws in (words(s) for s in sents) if len(ws) >= 2]
    top1 = sorted(Counter(first).items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]
    top2 = sorted(Counter(first2).items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]

    punct = [(name, len(rx.findall(joined))) for name, rx in _PUNCTUATION]
    punct.append(("quotation pairs", joined.count("“") + joined.count('"') // 2))
    punct = [(n, c, per_1000(c, total)) for n, c in punct]

    fs = sum(1 for w in lower if w in FIRST_SINGULAR)
    fp = sum(1 for w in lower if w in FIRST_PLURAL)
    sec = sum(1 for w in lower if w in SECOND)
    th = sum(1 for w in lower if w in THIRD)

    nt = len(_NT_RE.findall(joined))
    nots = len(_NOT_RE.findall(joined))

    cites = [(name, len(rx.findall(joined))) for name, rx in _CITATION_PATTERNS]
    best = max(cites, key=lambda c: c[1])
    heads = sorted(Counter(h for h in (reference_headings or []) if h).items(), key=lambda kv: (-kv[1], kv[0]))
    if best[1]:
        style = best[0].split(" (")[0].split(" [")[0]
    elif heads:
        style = f"no in-text citations found; reference lists headed {heads[0][0]!r}"
    else:
        style = "no citations found"

    return Stats(
        documents=len(texts), paragraphs=len(paras), sentences=len(sents), words=total,
        sentence_words=Dist.of(sent_words), sentence_buckets=buckets,
        paragraph_words=Dist.of([len(words(p)) for p in paras]), paragraph_sentences=Dist.of(para_sents),
        ttr=round(len(counts) / len(alpha), 3) if alpha else 0.0, mattr=mattr(alpha),
        hapax_share=share(sum(1 for c in counts.values() if c == 1), len(counts)),
        mean_word_length=round(statistics.fmean(len(w) for w in alpha), 2) if alpha else 0.0,
        transitions=trans[:TOP], openers=top1, openers2=top2,
        open_transition=share(sum(1 for s in sents if starts_with_transition(s)), len(sents)),
        open_i=share(sum(1 for w in first if w == "i"), len(sents)),
        open_subordinate=share(sum(1 for w in first if w in SUBORDINATORS), len(sents)),
        punctuation=punct, commas_per_sentence=round(joined.count(",") / len(sents), 2) if sents else 0.0,
        first_singular=per_1000(fs, total), first_plural=per_1000(fp, total), second=per_1000(sec, total),
        third=per_1000(th, total), first_share=share(fs + fp, fs + fp + th),
        contractions=per_1000(len(_CONTRACTION_RE.findall(joined)), total), nt_share=share(nt, nt + nots),
        passive_share=share(sum(1 for s in sents if _PASSIVE_RE.search(s)), len(sents)),
        citations=cites, citation_style=style, reference_headings=heads,
    )


def render(s: Stats) -> str:
    """The body of ``style-stats.md`` (also given to the model, fenced, as data)."""
    def pairs(items, fmt):
        return "; ".join(fmt(*i) for i in items) or "(none)"

    lines = [
        "# Style statistics",
        "",
        "Computed in code from the kept, cleaned samples (see samples-text/MANIFEST.md). Headings are not counted.",
        "",
        "## Corpus",
        f"- documents {s.documents}, paragraphs {s.paragraphs}, sentences {s.sentences}, words {s.words}",
        "",
        "## Sentences",
        f"- length: {s.sentence_words.line('words')}",
        "- length buckets: " + pairs(s.sentence_buckets, lambda b, n, f: f"{b} words: {n} ({_num(round(f * 100, 1))} %)"),
        "",
        "## Paragraphs",
        f"- length: {s.paragraph_words.line('words')}",
        f"- sentences per paragraph: {s.paragraph_sentences.line('sentences')}",
        "",
        "## Vocabulary",
        f"- type/token ratio {_num(s.ttr)}; moving-average TTR (window {MATTR_WINDOW}) {_num(s.mattr)}",
        f"- share of distinct words used once {_num(s.hapax_share)}; mean word length {_num(s.mean_word_length)} letters",
        "",
        "## Transitions and openers",
        "- most-used transitions (count, per 1000 words): "
        + pairs(s.transitions, lambda t, n, r: f"{t} {n} ({_num(r)})"),
        "- most common first words: " + pairs(s.openers, lambda w, n: f"{w} {n}"),
        "- most common first two words: " + pairs(s.openers2, lambda w, n: f"{w} {n}"),
        (f"- sentences opening with a transition {_num(round(s.open_transition * 100, 1))} %, "
        f"with \"I\" {_num(round(s.open_i * 100, 1))} %, "
        f"with a subordinating word {_num(round(s.open_subordinate * 100, 1))} %"),
        "",
        "## Punctuation (count, per 1000 words)",
        "- " + pairs(s.punctuation, lambda n, c, r: f"{n} {c} ({_num(r)})"),
        f"- commas per sentence {_num(s.commas_per_sentence)}",
        "",
        "## Person",
        (f"- per 1000 words: first person singular {_num(s.first_singular)}, plural {_num(s.first_plural)}, "
        f"second person {_num(s.second)}, third person {_num(s.third)}"),
        f"- first-person share of first + third person pronouns {_num(round(s.first_share * 100, 1))} %",
        "",
        "## Contractions and voice",
        (f"- contractions per 1000 words {_num(s.contractions)}; negations written as n't "
        f"{_num(round(s.nt_share * 100, 1))} %"),
        f"- passive-voice estimate: {_num(round(s.passive_share * 100, 1))} % of sentences",
        "",
        "## Citations",
        "- in-text citations: " + pairs(s.citations, lambda n, c: f"{n} {c}"),
        "- reference-list headings: " + pairs(s.reference_headings, lambda h, c: f"{h} {c}"),
        f"- typical style: {s.citation_style}",
    ]
    return "\n".join(lines) + "\n"
