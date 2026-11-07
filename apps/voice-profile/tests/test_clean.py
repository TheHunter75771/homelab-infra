import unittest

from voice_profile.clean import (
    QUOTE_MARK,
    Sample,
    _h,
    clean_all,
    clean_document,
    dedupe,
    is_peer_review,
    jaccard,
    parse_include,
    remove_quotes,
    shingles,
    strip_cover,
)
from voice_profile.docx import Paragraph, parse_document

from .fakes import document, essay, para, prose


def sample(name: str, xml: str, mtime: float = 0.0) -> Sample:
    return Sample(name, mtime, parse_document(xml.encode()))


def P(text: str, **kw) -> Paragraph:
    return Paragraph(text, **kw)


class CoverTests(unittest.TestCase):
    def test_apa_cover_page_dropped(self):
        doc = clean_document(sample("e.docx", essay(1)))
        self.assertEqual(doc.cover_lines, 6)
        body = doc.body()
        for gone in ("Student Q. Example", "ABC 101", "Professor", "October 1, 2026", "University", "A Synthetic Essay"):
            self.assertNotIn(gone, body)
        self.assertTrue(body.startswith(prose(1)[0][:40]))

    def test_mla_heading_in_one_paragraph_with_breaks(self):
        first = prose(2, 1)[0]
        paras, dropped = strip_cover([P("Student Example\nProfessor Placeholder\nXYZ 210\n27 October 2026\n" + first)])
        self.assertEqual(dropped, 4)
        self.assertEqual(paras[0].text, first)

    def test_prose_first_paragraph_is_never_cover(self):
        text = "I studied at Example University and met Dr. Placeholder there, which changed how I write essays for class."
        paras, dropped = strip_cover([P(text), P("More prose here that keeps going for a while.")])
        self.assertEqual(dropped, 0)
        self.assertEqual(paras[0].text, text)

    def test_title_style_dropped(self):
        paras, dropped = strip_cover([P("My Title Is a Full Sentence.", style="Title"), P(prose(3, 1)[0])])
        self.assertEqual(dropped, 1)
        self.assertEqual(len(paras), 1)

    def test_cover_block_is_bounded(self):
        paras, _ = strip_cover([P("Short line") for _ in range(20)] + [P(prose(4, 1)[0])])
        self.assertEqual(len(paras), 6)  # only the first 15 can be cover


class ReferenceAndQuoteTests(unittest.TestCase):
    def test_references_section_dropped(self):
        for heading in ("References", "Works Cited", "WORKS CITED:", "Bibliography"):
            with self.subTest(heading=heading):
                body = "".join(para(p) for p in prose(5, 3)) + para(heading) + para("After the heading. " * 5)
                doc = clean_document(sample("e.docx", document(body)))
                self.assertNotIn("After the heading", doc.body())
                self.assertEqual(doc.reference_paras, 2)
                self.assertEqual(doc.reference_heading, heading.rstrip(":").casefold())

    def test_hanging_indent_entries_dropped_anywhere(self):
        body = para(prose(6, 1)[0]) + para("Author, A. (2020). REFCANARY. Publisher.", left=720, hanging=720)
        doc = clean_document(sample("e.docx", document(body)))
        self.assertNotIn("REFCANARY", doc.body())

    def test_block_quotes_dropped(self):
        quote = "QUOTECANARY " + prose(7, 1)[0]
        for kw in ({"style": "Quote"}, {"style": "IntenseQuote"}, {"left": 720}, {"left": 1440}):
            with self.subTest(kw=kw):
                body = para(prose(8, 1)[0]) + para(quote, **kw) + para(prose(9, 1)[0])
                doc = clean_document(sample("e.docx", document(body)))
                self.assertNotIn("QUOTECANARY", doc.body())
                self.assertEqual(doc.block_quotes, 1)

    def test_list_items_with_indent_are_kept(self):
        body = para(prose(8, 1)[0]) + para("A list item of mine.", left=720, hanging=360, numbered=True)
        doc = clean_document(sample("e.docx", document(body)))
        self.assertIn("- A list item of mine.", doc.paragraphs)

    def test_whole_paragraph_quotation_dropped(self):
        body = para(prose(8, 1)[0]) + para("“QUOTECANARY is a short line.” (Author, 2020)")
        doc = clean_document(sample("e.docx", document(body)))
        self.assertNotIn("QUOTECANARY", doc.body())

    def test_long_inline_quote_replaced_short_kept(self):
        text, n = remove_quotes('He said "yes" and then "First sentence here. Second one follows." later.')
        self.assertEqual(n, 1)
        self.assertEqual(text, f'He said "yes" and then {QUOTE_MARK} later.')
        long_quote = "“" + " ".join(["word"] * 41) + "”"
        self.assertEqual(remove_quotes(f"A {long_quote} b")[0], f"A {QUOTE_MARK} b")
        forty = "“" + " ".join(["word"] * 40) + "”"
        self.assertEqual(remove_quotes(f"A {forty} b")[1], 0)


class PeerReviewTests(unittest.TestCase):
    def test_filename(self):
        for name in ("Technical Report Peer Review Sheet.docx", "peer-review_2.docx", "PeerReview.docx"):
            with self.subTest(name=name):
                self.assertTrue(is_peer_review(sample(name, essay(1))))
        self.assertFalse(is_peer_review(sample("Essay.docx", essay(1))))

    def test_heading(self):
        body = para("Peer Review Worksheet", style="Heading1") + "".join(para(p) for p in prose(2, 3))
        self.assertTrue(is_peer_review(sample("Report.docx", document(body))))
        body = "".join(para(p) for p in prose(2, 3)) + para("Notes for peer review")
        self.assertTrue(is_peer_review(sample("Report.docx", document(body))))

    def test_prose_mention_is_not_a_peer_review(self):
        body = "".join(para(p) for p in prose(2, 20)) + para(
            "Later in the term I asked a classmate for a peer review, and it helped me cut my draft down.")
        self.assertFalse(is_peer_review(sample("Report.docx", document(body))))

    def test_excluded_unless_listed_in_include(self):
        samples = [sample("Peer Review for Classmate.docx", essay(11)), sample("Essay.docx", essay(12))]
        out = clean_all(samples, include=set(), threshold=0.6, min_words=50)
        self.assertEqual([d.name for d in out.kept], ["Essay.docx"])
        self.assertIn("peer review", out.excluded[0][1])
        include = parse_include("# my own peer review\n  peer review for CLASSMATE.docx  # mine\n\n")
        out = clean_all(samples, include=include, threshold=0.6, min_words=50)
        self.assertEqual(sorted(d.name for d in out.kept), ["Essay.docx", "Peer Review for Classmate.docx"])


class DedupeTests(unittest.TestCase):
    def test_shingles_and_jaccard(self):
        self.assertEqual(shingles("a b c d e f"), {_h("a b c d e"), _h("b c d e f")})
        self.assertEqual(shingles("A b"), {_h("a b")})
        self.assertNotEqual(_h("a b c d e"), _h("a b c d f"))
        self.assertEqual(jaccard({1, 2, 3}, {2, 3, 4}), 0.5)
        self.assertEqual(jaccard(set(), {1}), 0.0)

    def test_near_duplicate_pair_keeps_longest_then_latest(self):
        base = prose(20, 6)
        draft = document("".join(para(p) for p in base[:5]))
        final = document("".join(para(p) for p in base) )
        copy = document("".join(para(p) for p in base))
        other = document("".join(para(p) for p in prose(21, 6)))
        samples = [sample("Draft.docx", draft, 10), sample("Final.docx", final, 20),
                   sample("Final copy.docx", copy, 30), sample("Other.docx", other, 5)]
        out = clean_all(samples, include=set(), threshold=0.6, min_words=50)
        self.assertEqual(sorted(d.name for d in out.kept), ["Final copy.docx", "Other.docx"])
        merged = {m[0]: m[1] for m in out.merged}
        self.assertEqual(merged, {"Final.docx": "Final copy.docx", "Draft.docx": "Final copy.docx"})
        self.assertEqual([m[2] for m in sorted(out.merged)][1], 1.0)

    def test_longest_beats_latest(self):
        base = prose(22, 6)
        longer_older = document("".join(para(p) for p in base))
        shorter_newer = document("".join(para(p) for p in base[:5]))
        out = clean_all([sample("Old long.docx", longer_older, 10), sample("New short.docx", shorter_newer, 99)],
                        include=set(), threshold=0.6, min_words=50)
        self.assertEqual([d.name for d in out.kept], ["Old long.docx"])

    def test_below_threshold_both_kept(self):
        a, b = prose(30, 4), prose(31, 4)
        docs = [clean_document(sample("A.docx", document("".join(para(p) for p in a)))),
                clean_document(sample("B.docx", document("".join(para(p) for p in a[:1] + b[:3]))))]
        kept, merged = dedupe(docs, 0.6)
        self.assertEqual(len(kept), 2)
        self.assertEqual(merged, [])

    def test_shared_paragraph_counts_once_and_short_leftover_excluded(self):
        a, b = prose(40, 6), prose(41, 2)
        samples = [sample("Long.docx", document("".join(para(p) for p in a))),
                   sample("Short.docx", document("".join(para(p) for p in [a[0], a[1], b[0]])))]
        out = clean_all(samples, include=set(), threshold=0.6, min_words=150)
        self.assertEqual([d.name for d in out.kept], ["Long.docx"])
        self.assertEqual(out.excluded[0][0], "Short.docx")
        self.assertIn("2 paragraph(s) repeat other samples", out.excluded[0][1])


if __name__ == "__main__":
    unittest.main()
