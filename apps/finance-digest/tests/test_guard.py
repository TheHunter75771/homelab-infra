import unittest

from finance_digest import guard


def ok(text):
    return guard.check(text)[0]


# Every case from Reviewer H's probe_guard.py (round 1). Under the no-numbers rule all are dropped.
REVIEWER_PROBES = [
    ("spelled fifty (50 is a money fact)", "Groceries has fifty dollars left."),
    ("spelled twelve via 12% fact as dollars", "You spent twelve dollars on fuel."),
    ("ordinal twenty-eighth (20 in facts)", "Rent is due on the twenty-eighth."),
    ("ordinal 'fifth' unchecked", "Rent is due on the fifth."),
    ("'half'/'double' quantity words", "Fuel is at half its cap and dining spent double."),
    ("fraction 1/4 (1,4 facts)", "You have used 1/4 of the cap."),
    ("range $1-5 (1,5 facts)", "Bills of $1-5 are due."),
    ("year as money: $2,030", "Savings stand at $2,030."),
    ("date-day as money: $27", "Groceries has $27 left."),
    ("hour as money: $13", "Fuel has $13 left."),
    ("pct threshold as money: $85", "Dining has $85 left."),
    ("pct used 12 as dollars 12.00", "Fuel spent $12.00."),
    ("sign flip: over shown as left", "Groceries has $0.01 left."),
    ("scientific 1e3 (1,3 facts)", "Debt is about 1e3 dollars."),
    ("glued '4x'", "Spending is 4x the cap."),
    ("fullwidth digits", "Savings \uff11\uff12."),
    ("math bold digit", "Savings \U0001d7cf dollars."),
    ("zero-width split 1\u200b2 -> 12 not fact", "Spent 1\u200b2\u200b3 dollars."),
    ("NBSP thousands '1\u00a0234.56'", "Savings \u00a0$1\u00a0234.56."),
    ("thin-space thousands 1\u2009234.56 (234.56 not fact)", "Savings $1\u2009234.56."),
    ("'1.2k'", "About $1.2k left."),
    ("'1.2 grand'", "About 1.2 grand left."),
    ("roman 'IV' letters", "Category IV is over."),
    ("'a couple'/'a few'", "A couple of bills are due and a few categories are over."),
    ("non-numeric falsehood", "The bank sync is healthy and nothing is over budget."),
    ("7 sentences with '!'", "Over! Over! Over! Over! Over! Over! Over!"),
    ("7 sentences with ';'", "a; b; c; d; e; f; g; h."),
    ("7 sentences, newline separated, no punctuation", "a\nb\nc\nd\ne\nf\ng"),
    ("6 sentences with '?'", "Over? Over? Over? Over? Over? Over?"),
    ("percent sign word 'percent' on money fig", "Fuel is at 50 percent."),
    ("ordinal suffix 3rd (3 fact)", "The 3rd bill is due."),
    ("hex 0x1F", "Code 0x1F."),
    ("negative -1234.56", "Balance is -$1,234.56."),
]

# Reviewer H round 2 (probe_guard2.py), the cases listed under R2-1: all must be dropped now.
R2_1_PROBES = [
    ("plural hundreds", "You owe hundreds of dollars."),
    ("thousands", "Debt is in the thousands."),
    ("tens", "Tens of dollars are left."),
    ("twenties", "Fuel spent twenties."),
    ("fifths", "Two fifths."),
    ("fourths", "Three fourths used."),
    ("tenfold", "Spending rose tenfold."),
    ("twofold", "A twofold rise."),
    ("a dollar", "Groceries is over by a dollar."),
    ("a penny", "Groceries is over by a penny."),
    ("a nickel", "Over by a nickel."),
    ("bucks", "Fifty bucks."),
    ("nil", "Nil left in fuel."),
    ("zilch", "Zilch left."),
    ("nought", "Nought left."),
    ("umpteen", "Umpteen bills."),
    ("gazillion", "A gazillion bills."),
    ("lowercase roman xii", "Category xii is over."),
    ("doubling", "Spending is doubling."),
    ("triples", "It triples."),
    ("O-for-zero letters", "Savings are lOO dollars."),
    ("trio", "A trio of bills."),
    ("decade", "Debt from the last decade."),
    ("fortnight", "Due within a fortnight."),
]
# Accepted as residual by the Lead (round 2): deliberate misspellings. Recorded so a change shows.
RESIDUAL = [("Cyrillic fifty", "Fuel has fift\u0443 left."), ("hyphen split", "Fuel has fif-ty left."),
            ("accented", "Fuel has fift\u00fd left.")]

CLEAN = ("The bank sync is working. Groceries went over its budget and Fuel is close to its cap. "
         "The insurance and water bills are due this week; see the table for the amounts.")


class GuardTest(unittest.TestCase):
    def test_every_reviewer_probe_is_dropped(self):
        for name, text in REVIEWER_PROBES:
            summary, why = guard.check(text)
            self.assertIsNone(summary, f"{name}: kept ({why})")

    def test_every_r2_1_probe_is_dropped(self):
        for name, text in R2_1_PROBES:
            summary, why = guard.check(text)
            self.assertIsNone(summary, f"{name}: kept ({why})")

    def test_inflections_and_new_words(self):
        for t in ("Thousands left.", "Dozens of bills.", "Spending quadrupled.", "It tripled.", "The thirds.",
                  "Halving it.", "Seconds count.", "Cents matter.", "Pennies add up.", "Quarters left.",
                  "Dimes left.", "Zeroes everywhere.", "A naught.", "Zillions.", "Category XII.", "Category Xii.",
                  "Plan iv.", "Over by a buck.", "The ones due.", "Doublings everywhere.", "Triplings."):
            self.assertIsNone(ok(t), t)

    def test_roman_single_letters_and_ordinary_words(self):
        self.assertIsNotNone(ok("I think the sync is working."))  # the pronoun
        self.assertIsNotNone(ok("Plan v is fine."))                # a single lower-case letter is exempt
        for t in ("The tone is calm.", "Spending tends to run high.", "Money is attended to.",
                  "The scented candles are fine.", "Often the phones are on time.", "The bills are listed above."):
            self.assertIsNotNone(ok(t), t)

    def test_residual_spellings_are_documented(self):
        for name, text in RESIDUAL:
            self.assertIsNotNone(ok(text), f"{name}: now caught; move it out of RESIDUAL")

    def test_nfkc_only_and_raw_only_numerals(self):
        # U+2F00 (Kangxi radical "one", category So) is a number only after NFKC (U+4E00, numeric 1).
        self.assertIsNone(ok("Fuel \u2f00 left."))
        # U+2170 (small Roman numeral one, Nl) is a number only on the raw text: NFKC makes it "i",
        # a single letter the Roman rule exempts. U+2173 (small Roman four) is dropped either way.
        self.assertIsNone(ok("Plan \u2170 is over."))
        self.assertIsNone(ok("Plan \u2173 is over."))

    def test_clean_summary_without_numbers_is_kept(self):
        self.assertEqual(guard.check(CLEAN), (CLEAN, "ok"))

    def test_any_digit_in_any_script(self):
        for t in ("Up 5.", "Spent \u0661\u0662.", "About \u00bd of it.", "Category \u2163.", "Spent 3\u00b2.",
                  "Ref \u4e09 items.", "x\u2070y", "Bill \u2460 is due.", "0x", "1e3", "Day 9"):
            self.assertIsNone(ok(t), t)

    def test_number_words(self):
        for t in ("One bill is due.", "Two categories are over.", "Rent is due on the first.",
                  "It is the second time.", "Spending tripled.", "Rent is due twice.", "Several bills are due.",
                  "Both categories are over.", "Nothing is due.", "A dozen bills.", "Twenty-one dollars.",
                  "It rose by forty-five.", "A quarter of the cap.", "Plan C is over.", "Account XII.",
                  "Fuel is at fifty per-cent.", "A thousand dollars of debt.", "None of it.", "Use the k.",
                  "Groceries has fi\u200bfty dollars.", "Rent is due on the \uff46\uff49\uff46\uff54\uff48."):
            self.assertIsNone(ok(t), t)

    def test_ordinary_words_are_not_number_words(self):
        self.assertIsNotNone(ok("Someone paid the phone bill."))
        self.assertIsNotNone(ok("Often the fuel budget is tight."))
        self.assertIsNotNone(ok("I see the bank sync is working."))
        self.assertIsNotNone(ok("The tone of this week is calm, with done and gone bills."))

    def test_sentences_counted_on_the_raw_text(self):
        five = " ".join(["Groceries is over."] * 5)
        self.assertIsNotNone(ok(five))
        self.assertIsNone(ok(five + " Fuel is close."))
        self.assertEqual(guard.sentence_count("a! b? c\uff01 d; e\nf"), 6)
        self.assertEqual(guard.sentence_count("a\u3002b\uff1fc"), 3)
        self.assertEqual(guard.sentence_count("- a\n- b\n* c"), 3)
        self.assertEqual(guard.sentence_count("intro - a - b - c - d - e"), 6)  # inline list markers
        self.assertEqual(guard.sentence_count("Wait... really?!"), 2)
        self.assertEqual(guard.sentence_count("a\r\nb\u2028c"), 3)
        self.assertEqual(guard.sentence_count("a\u2026 b\u2026 c\u2026 d\u2026 e\u2026 f\u2026 g"), 7)  # ellipsis (R2-2)
        self.assertIsNone(ok("a\u2026 b\u2026 c\u2026 d\u2026 e\u2026 f"))
        self.assertIsNotNone(ok("a\u2026 b\u2026 c\u2026 d\u2026 e"))
        # newlines count before clean() turns them into spaces
        self.assertIsNone(ok("a\nb\nc\nd\ne\nf"))
        self.assertIsNotNone(ok("a\nb\nc\nd\ne"))

    def test_text_is_made_inert(self):
        s = ok("See [this](https://evil.example) and ![x](http://e.example/p.png) <b>now</b>.")
        self.assertIsNotNone(s)
        for bad in ("](", "![", "://", "<b>", "\n"):
            self.assertNotIn(bad, s)
        self.assertIsNone(ok(""))
        self.assertIsNone(ok("   "))
        self.assertIsNone(ok(None))
        self.assertIsNone(ok({"x": "y"}))
        self.assertIsNone(ok("a" * (guard.MAX_CHARS + 1)))


if __name__ == "__main__":
    unittest.main()
