import unittest

from voice_profile.clean import QUOTE_MARK
from voice_profile.stats import Dist, compute, mattr, per_1000, percentile, render, sentences, starts_with_transition

P1 = "I think this works. However, we tested it twice, and it failed! Was it broken?"
P2 = "The report was written by the team. They didn't stop; they kept going — slowly."


class SentenceTests(unittest.TestCase):
    def test_split(self):
        self.assertEqual(sentences(P1), ["I think this works.", "However, we tested it twice, and it failed!",
                                         "Was it broken?"])

    def test_abbreviations_and_initials_do_not_split(self):
        self.assertEqual(len(sentences("Dr. Smith went home at 5 p.m. today. He slept.")), 2)
        self.assertEqual(len(sentences("J. R. Tolkien wrote books. Many read them.")), 2)
        self.assertEqual(len(sentences("Use a tool, e.g. A hammer. Then stop.")), 2)
        self.assertEqual(len(sentences("Smith et al. Found it. Then left.")), 2)

    def test_quotes_and_lowercase(self):
        self.assertEqual(sentences('He said "Go." Then he left.'), ['He said "Go."', "Then he left."])
        self.assertEqual(len(sentences("this ends. and lowercase continues")), 1)


class MathTests(unittest.TestCase):
    def test_percentile_nearest_rank(self):
        v = list(range(1, 11))
        self.assertEqual([percentile(v, p) for p in (10, 25, 50, 75, 90, 100)], [1, 3, 5, 8, 9, 10])
        self.assertEqual(percentile([], 50), 0)

    def test_dist(self):
        d = Dist.of([4, 8, 3, 7, 7])
        self.assertEqual((d.count, d.mean, d.median, d.p10, d.p25, d.p75, d.p90, d.min, d.max),
                         (5, 5.8, 7, 3, 4, 7, 8, 3, 8))
        self.assertEqual(Dist.of([1, 2]).median, 1.5)

    def test_mattr(self):
        self.assertEqual(mattr(list("abab c".replace(" ", "")), 2), 1.0)
        self.assertEqual(mattr(list("aabb"), 2), 0.667)
        self.assertEqual(mattr(list("aab"), 5), 0.667)  # shorter than the window: plain TTR
        self.assertEqual(mattr([]), 0.0)

    def test_per_1000(self):
        self.assertEqual(per_1000(1, 29), 34.48)
        self.assertEqual(per_1000(3, 0), 0.0)

    def test_transition_openers(self):
        self.assertTrue(starts_with_transition("However, it works."))
        self.assertTrue(starts_with_transition("“On the other hand, no.”"))
        self.assertFalse(starts_with_transition("Howeverish is not a word."))


class ComputeTests(unittest.TestCase):
    def setUp(self):
        self.s = compute([[P1, "## Results", P2]], ["references"])

    def test_counts_and_lengths(self):
        s = self.s
        self.assertEqual((s.documents, s.paragraphs, s.sentences, s.words), (1, 2, 5, 29))
        self.assertEqual((s.sentence_words.mean, s.sentence_words.median, s.sentence_words.p90), (5.8, 7, 8))
        self.assertEqual((s.paragraph_words.mean, s.paragraph_words.median), (14.5, 14.5))
        self.assertEqual(s.paragraph_sentences.median, 2.5)
        self.assertEqual(s.sentence_buckets[0], ("1-10", 5, 1.0))

    def test_vocabulary(self):
        self.assertEqual((self.s.ttr, self.s.mattr, self.s.hapax_share), (0.828, 0.828, 0.833))

    def test_transitions_and_openers(self):
        s = self.s
        self.assertEqual(s.transitions, [("however", 1, 34.48)])
        self.assertEqual(s.openers, [("however", 1), ("i", 1), ("the", 1), ("they", 1), ("was", 1)])
        self.assertEqual((s.open_transition, s.open_i, s.open_subordinate), (0.2, 0.2, 0.0))

    def test_punctuation(self):
        p = {name: count for name, count, _ in self.s.punctuation}
        self.assertEqual((p["commas"], p["semicolons"], p["colons"], p["em dashes (— or --)"],
                          p["question marks"], p["exclamation marks"], p["quotation pairs"]), (2, 1, 0, 1, 1, 1, 0))
        self.assertEqual(self.s.commas_per_sentence, 0.4)

    def test_person_contractions_passive(self):
        s = self.s
        self.assertEqual((s.first_singular, s.first_plural, s.third, s.first_share), (34.48, 34.48, 68.97, 0.5))
        self.assertEqual((s.contractions, s.nt_share), (34.48, 1.0))
        self.assertEqual(s.passive_share, 0.2)

    def test_citations(self):
        text = ("Smith (2020) argued this point well. Others agree (Lee & Park, 2019, p. 4). "
                "One more (Jones 23). A numbered one [2]. And (Kim et al., 2021).")
        s = compute([[text]])
        counts = dict(s.citations)
        self.assertEqual(counts["APA parenthetical (Author, 2020)"], 2)
        self.assertEqual(counts["APA narrative Author (2020)"], 1)
        self.assertEqual(counts["MLA (Author 23)"], 1)
        self.assertEqual(counts["numeric [1]"], 1)
        self.assertEqual(s.citation_style, "APA parenthetical")
        self.assertEqual(self.s.citation_style, "no in-text citations found; reference lists headed 'references'")
        self.assertEqual(compute([[P1]]).citation_style, "no citations found")

    def test_quote_marker_and_list_prefix_not_counted(self):
        s = compute([[f"- I wrote {QUOTE_MARK} here."]])
        self.assertEqual(s.words, 3)  # "I wrote here"

    def test_empty_and_render_deterministic(self):
        empty = compute([])
        self.assertEqual((empty.words, empty.ttr, empty.sentence_words.count), (0, 0.0, 0))
        self.assertEqual(render(self.s), render(compute([[P1, "## Results", P2]], ["references"])))
        text = render(self.s)
        self.assertIn("- documents 1, paragraphs 2, sentences 5, words 29", text)
        self.assertIn("however 1 (34.48)", text)
        self.assertIn("passive-voice estimate: 20 % of sentences", text)


if __name__ == "__main__":
    unittest.main()
