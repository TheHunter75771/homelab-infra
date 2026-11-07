"""Forge.tick_searches / Forge.tick_sources (CONTRACTS §12.25, module K3)."""
import os
import threading
import unittest
from unittest import mock

from forge import ForgeError, TickResult, checklist
from forge.core import (
    ALREADY,
    LOCK,
    NOT_FOUND,
    S_APPROVED,
    S_REDRAFT,
    S_REVIEW,
    S_SEARCH,
    S_SOURCE,
    TICKED,
)
from library import PathRefused

from .test_forge import RID, URL, URL2, ForgeTestCase

SEARCHES = f"35-forge/{RID}/searches.md"
SOURCES = f"35-forge/{RID}/sources.md"
STATE = f"35-forge/{RID}/state.md"
BY = "noah-admin.1@webui"


def outcomes(result: TickResult) -> dict[str, str]:
    return {i.item: i.outcome for i in result.items}


class TickCase(ForgeTestCase):
    def setup_searches(self, proposals=(("pdf summarization techniques", "learn the methods"),
                                        ("pdf text extraction python", "how to read PDFs"))):
        self.assertEqual(self.new(), RID)
        self.forge.propose_searches(RID, list(proposals))

    def bytes_of(self, rel):
        return self.p(rel).read_bytes()

    def assert_untouched(self, rel, before, state_before=None):
        self.assertEqual(self.bytes_of(rel), before)
        if state_before is not None:
            self.assertEqual(self.bytes_of(STATE), state_before)

    def append_raw(self, rel, text):
        with open(self.p(rel), "a", encoding="utf-8", newline="") as fh:
            fh.write(text)


# -------------------------------------------------------------------- happy paths

class HappyPathTest(TickCase):
    def test_tick_searches(self):
        self.setup_searches()
        self.assertEqual(self.forge.approved_queries(RID), [])
        res = self.forge.tick_searches(RID, ["pdf text extraction python", "pdf summarization techniques"], by=BY)
        self.assertEqual(res.kind, "search")
        self.assertEqual(res.ticked, ["pdf text extraction python", "pdf summarization techniques"])
        self.assertEqual(res.already, [])
        self.assertEqual(res.not_found, [])
        self.assertEqual(self.forge.approved_queries(RID),
                         ["pdf summarization techniques", "pdf text extraction python"])
        self.assertTrue(self.forge.is_query_approved(RID, "pdf text extraction python"))
        self.assertIn("- [x] pdf summarization techniques — why: learn the methods\n", self.text(SEARCHES))
        log = self.text(STATE).splitlines()[-1]
        self.assertTrue(log.endswith(
            f" approve-chat by={BY} search: ticked 2 search(es) in searches.md by tick-searches "
            "(0 already ticked, 0 not found): `pdf text extraction python`, `pdf summarization techniques`"), log)
        self.assertEqual(self.status(), S_SEARCH)  # ticking never moves the request on

    def test_tick_sources(self):
        self.to_sources()  # URL is ticked by hand, URL2 is not
        res = self.forge.tick_sources(RID, [URL2, URL], by=BY)
        self.assertEqual(res.kind, "source")
        self.assertEqual(outcomes(res), {URL2: TICKED, URL: ALREADY})
        self.assertEqual(self.forge.approved_urls(RID), [URL, URL2])
        self.assertTrue(self.forge.is_url_approved(RID, URL2))
        self.assertIn(f"- [x] `{URL2}` — title: Guide — why: background — take: nothing yet\n",
                      self.text(SOURCES))
        self.assertTrue(self.text(STATE).splitlines()[-1].endswith(
            f" approve-chat by={BY} source: ticked 1 source(s) in sources.md by tick-sources "
            f"(1 already ticked, 0 not found): `{URL2}`"))
        self.assertEqual(self.status(), S_SOURCE)

    def test_query_with_surrounding_spaces_is_the_checked_query(self):
        self.setup_searches()
        res = self.forge.tick_searches(RID, ["  pdf text extraction python "], by=BY)
        self.assertEqual(res.items[0].requested, "  pdf text extraction python ")
        self.assertEqual(res.ticked, ["pdf text extraction python"])

    def test_repeated_items_are_reported_once(self):
        self.setup_searches()
        res = self.forge.tick_searches(RID, ["pdf text extraction python", "pdf text extraction python "], by=BY)
        self.assertEqual(len(res.items), 1)
        self.assertEqual(res.ticked, ["pdf text extraction python"])


# -------------------------------------------------------------------- approver and audit line

class ApproverAuditTest(TickCase):
    def test_bad_by_refused_and_nothing_written(self):
        self.setup_searches()
        before, state_before = self.bytes_of(SEARCHES), self.bytes_of(STATE)
        for by in ("", "x" * 65, "noah admin", "noah\nadmin", "noah`x", "<b>", "noah*", "n[1]", "ünïcode",
                   "a/b", "a:b", "a\u200bb", None, 7, b"noah"):
            with self.assertRaisesRegex(ForgeError, "approver's id", msg=repr(by)):
                self.forge.tick_searches(RID, ["pdf text extraction python"], by=by)
            with self.assertRaisesRegex(ForgeError, "approver's id", msg=repr(by)):
                self.forge.tick_sources(RID, [URL], by=by)
        with self.assertRaises(TypeError):
            self.forge.tick_searches(RID, ["pdf text extraction python"])  # by is required
        with self.assertRaises(TypeError):
            self.forge.tick_searches(RID, ["pdf text extraction python"], BY)  # and keyword-only
        self.assert_untouched(SEARCHES, before, state_before)
        for by in ("a", "x" * 64, "Noah.N_1@open-webui", "0d5c6f9e-1b2a-4c3d-9e8f-7a6b5c4d3e2f"):
            self.assertEqual(len(self.forge.tick_searches(RID, ["not listed"], by=by).items), 1)

    def test_log_line_records_approver_kind_and_only_ticked_items(self):
        self.setup_searches()
        self.tick(SEARCHES, "pdf summarization techniques")
        self.forge.tick_searches(RID, ["pdf text extraction python", "pdf summarization techniques",
                                       "never listed"], by="user-42")
        log = self.text(STATE).splitlines()[-1]
        self.assertIn(" approve-chat by=user-42 search: ticked 1 search(es)", log)
        self.assertIn("(1 already ticked, 1 not found): `pdf text extraction python`", log)
        self.assertNotIn("pdf summarization techniques", log)
        self.assertNotIn("never listed", log)
        self.assertTrue(log.endswith("`pdf text extraction python`"))

    def test_nothing_ticked_writes_no_log_line(self):
        self.setup_searches()
        self.tick(SEARCHES, "pdf text extraction python")
        state_before = self.bytes_of(STATE)
        res = self.forge.tick_searches(RID, ["pdf text extraction python", "never listed"], by=BY)
        self.assertEqual(res.ticked, [])
        self.assertEqual(self.bytes_of(STATE), state_before)

    def test_every_ticked_item_is_logged_in_full_ten_per_line(self):
        self.to_sources()
        odd = "https://example.net/a_b*c~d&e%3Cx%3E?q=(1)&r=_x_"
        long_url = "https://example.net/" + "p" * 300
        urls = [odd, long_url] + [f"https://example.net/n{i}" for i in range(26)]
        self.forge.propose_sources(RID, [(u, "t", "w", "k") for u in urls])
        before = self.text(STATE).splitlines()
        res = self.forge.tick_sources(RID, ["HTTPS://EXAMPLE.NET/a_b*c~d&e%3Cx%3E?q=(1)&r=_x_"] + urls[1:] + [URL2],
                                      by=BY)
        self.assertEqual(len(res.ticked), 29)
        new = self.text(STATE).splitlines()[len(before):]
        self.assertEqual(len(new), 3)                                       # 29 items: 10 + 10 + 9
        self.assertIn(f"approve-chat by={BY} source: ticked 29 source(s) in sources.md by tick-sources "
                      "(0 already ticked, 0 not found): ", new[0])
        self.assertIn(f" approve-chat by={BY} source (continued, 2/3): ", new[1])
        self.assertIn(f" approve-chat by={BY} source (continued, 3/3): ", new[2])
        stamps = {line.split(" ")[1] for line in new}
        self.assertEqual(len(stamps), 1)                                    # one write, one stamp
        logged = []
        for line in new:
            items = line.split("): ", 1)[1].split(", ")
            self.assertLessEqual(len(items), 10)
            logged += items
        self.assertEqual(logged, [f"`{u}`" for u in res.ticked])          # all, in full, canonical, in order
        self.assertEqual(logged[0], f"`{odd}`")                             # Markdown kept inside a code span
        self.assertIn(f"`{long_url}`", logged)                              # no clipping
        self.assertEqual(self.status(), S_SOURCE)

    def test_exactly_ten_items_fit_on_one_line(self):
        self.setup_searches(proposals=[(f"q{i}", "w") for i in range(10)])
        before = len(self.text(STATE).splitlines())
        self.forge.tick_searches(RID, [f"q{i}" for i in range(10)], by=BY)
        new = self.text(STATE).splitlines()[before:]
        self.assertEqual(len(new), 1)
        self.assertTrue(new[0].endswith(": " + ", ".join(f"`q{i}`" for i in range(10))), new[0])

    def test_query_items_with_punctuation_are_spanned(self):
        q = "apa 7 \"et al.\" (2+ authors) 50% @ref; a/b = c?"
        self.setup_searches(proposals=((q, "w"),))
        self.forge.tick_searches(RID, [q], by=BY)
        self.assertTrue(self.text(STATE).splitlines()[-1].endswith(f": `{q}`"))


# -------------------------------------------------------------------- outcomes

class OutcomeTest(TickCase):
    def test_already_ticked_writes_nothing(self):
        self.setup_searches()
        self.tick(SEARCHES, "pdf text extraction python", mark="X")
        before, state_before = self.bytes_of(SEARCHES), self.bytes_of(STATE)
        ino = os.lstat(self.p(SEARCHES)).st_ino
        res = self.forge.tick_searches(RID, ["pdf text extraction python"], by=BY)
        self.assertEqual(outcomes(res), {"pdf text extraction python": ALREADY})
        self.assert_untouched(SEARCHES, before, state_before)
        self.assertEqual(os.lstat(self.p(SEARCHES)).st_ino, ino)

    def test_missing_item_is_reported_never_invented(self):
        self.setup_searches()
        before, state_before = self.bytes_of(SEARCHES), self.bytes_of(STATE)
        res = self.forge.tick_searches(RID, ["how to bake bread"], by=BY)
        self.assertEqual(outcomes(res), {"how to bake bread": NOT_FOUND})
        self.assertEqual(res.items[0].reason, "not listed in searches.md")
        self.assert_untouched(SEARCHES, before, state_before)
        res = self.forge.tick_searches(RID, ["how to bake bread", "pdf text extraction python"], by=BY)
        self.assertEqual(outcomes(res), {"how to bake bread": NOT_FOUND, "pdf text extraction python": TICKED})
        self.assertNotIn("bread", self.text(SEARCHES))

    def test_missing_url_is_reported_never_invented(self):
        self.to_sources()
        before = self.bytes_of(SOURCES)
        res = self.forge.tick_sources(RID, ["https://example.net/never-proposed"], by=BY)
        self.assertEqual(res.not_found, ["https://example.net/never-proposed"])
        self.assert_untouched(SOURCES, before)

    def test_two_lines_with_the_same_query_tick_nothing(self):
        self.setup_searches()
        self.append_raw(SEARCHES, "- [ ] pdf text extraction python — why: pasted again\n")
        before, state_before = self.bytes_of(SEARCHES), self.bytes_of(STATE)
        res = self.forge.tick_searches(RID, ["pdf text extraction python"], by=BY)
        self.assertEqual(outcomes(res), {"pdf text extraction python": NOT_FOUND})
        self.assertIn("more than once", res.items[0].reason)
        self.assert_untouched(SEARCHES, before, state_before)

    def test_duplicate_with_one_ticked_is_still_ambiguous(self):
        self.setup_searches()
        self.append_raw(SEARCHES, "- [x] pdf text extraction python — why: pasted again\n")
        before = self.bytes_of(SEARCHES)
        res = self.forge.tick_searches(RID, ["pdf text extraction python"], by=BY)
        self.assertEqual(res.not_found, ["pdf text extraction python"])
        self.assert_untouched(SEARCHES, before)

    def test_two_lines_with_the_same_canonical_url_tick_nothing(self):
        self.to_sources()
        self.append_raw(SOURCES, "- [ ] `HTTPS://DOCS.example.org/guide` — title: again — why: w — take: k\n")
        before = self.bytes_of(SOURCES)
        res = self.forge.tick_sources(RID, [URL2], by=BY)
        self.assertEqual(res.not_found, [URL2])
        self.assert_untouched(SOURCES, before)

    def test_malformed_lines_are_never_ticked(self):
        self.setup_searches()
        self.append_raw(SEARCHES, "".join([
            "- [ ] two seps — why: a — why: b\n",            # more than one separator
            "  - [ ] indented q — why: w\n",                  # not at column 0
            "-[ ] no space q — why: w\n",                     # not '- [ ] '
            "- [ ] no separator here\n",                      # no ' — why:'
            "- [ ] dash q - why: ascii dash\n",               # lookalike separator
            "<!--\n- [ ] commented q — why: w\n-->\n",        # inside an HTML comment
        ]))
        before, state_before = self.bytes_of(SEARCHES), self.bytes_of(STATE)
        for q in ("two seps", "indented q", "no space q", "no separator here", "dash q", "commented q"):
            res = self.forge.tick_searches(RID, [q], by=BY)
            self.assertEqual(outcomes(res), {q: NOT_FOUND}, q)
        self.assert_untouched(SEARCHES, before, state_before)

    def test_a_malformed_twin_blocks_the_valid_line(self):
        """Fail closed: if the same query also sits on a refused line, Noah must sort it out."""
        self.setup_searches()
        self.append_raw(SEARCHES, "- [ ] pdf text extraction python — why: a — why: b\n")
        before = self.bytes_of(SEARCHES)
        res = self.forge.tick_searches(RID, ["pdf text extraction python", "pdf summarization techniques"], by=BY)
        self.assertEqual(outcomes(res), {"pdf text extraction python": NOT_FOUND,
                                         "pdf summarization techniques": TICKED})
        self.assertIn("refuses", res.items[0].reason)
        self.assertNotEqual(self.bytes_of(SEARCHES), before)
        self.assertEqual(self.forge.approved_queries(RID), ["pdf summarization techniques"])

    def test_malformed_source_lines_are_never_ticked(self):
        self.to_sources()
        self.append_raw(SOURCES, "".join([
            "- [ ] https://example.net/bare — title: t — why: w — take: k\n",    # no code span
            "- [ ] `https://example.net/star` — title: *bold* — why: w — take: k\n",  # markdown in text
            "- [ ] `https://example.net/a b` — title: t — why: w — take: k\n",   # bad URL
        ]))
        self.append_raw(SOURCES, f"- [ ] {URL2} — title: twin without span — why: w — take: k\n")
        before = self.bytes_of(SOURCES)
        res = self.forge.tick_sources(RID, ["https://example.net/bare", "https://example.net/star", URL2], by=BY)
        self.assertEqual(outcomes(res), {"https://example.net/bare": NOT_FOUND,
                                         "https://example.net/star": NOT_FOUND, URL2: NOT_FOUND})
        self.assert_untouched(SOURCES, before)

    def test_hidden_text_line_is_not_matched_by_its_visible_twin(self):
        self.setup_searches(proposals=(("first", "w"),))
        self.append_raw(SEARCHES, "- [ ] sec\u200bret — why: w\n")
        before = self.bytes_of(SEARCHES)
        self.assertEqual(self.forge.tick_searches(RID, ["secret"], by=BY).not_found, ["secret"])
        with self.assertRaises(ForgeError):
            self.forge.tick_searches(RID, ["sec\u200bret"], by=BY)
        self.assert_untouched(SEARCHES, before)


# -------------------------------------------------------------------- matching

class MatchingTest(TickCase):
    def test_exact_query_never_a_substring_or_case_variant(self):
        self.setup_searches(proposals=(("pdf", "short"), ("pdf text extraction python", "long"),
                                       ("Python PDF", "case")))
        before = self.bytes_of(SEARCHES)
        res = self.forge.tick_searches(RID, ["text extraction", "pdf text", "python pdf", "PDF",
                                             "pdf text extraction python extra"], by=BY)
        self.assertEqual(res.ticked, [])
        self.assertEqual(len(res.not_found), 5)
        self.assert_untouched(SEARCHES, before)
        self.assertEqual(self.forge.tick_searches(RID, ["pdf"], by=BY).ticked, ["pdf"])
        self.assertEqual(self.forge.approved_queries(RID), ["pdf"])

    def test_noah_edited_query_matches_only_the_edited_text(self):
        self.setup_searches()
        self.p(SEARCHES).write_text(self.text(SEARCHES).replace(
            "- [ ] pdf text extraction python — why:", "- [ ] pdf text extraction python3 pypdf — why:"),
            encoding="utf-8")
        before = self.bytes_of(SEARCHES)
        res = self.forge.tick_searches(RID, ["pdf text extraction python"], by=BY)
        self.assertEqual(res.not_found, ["pdf text extraction python"])
        self.assert_untouched(SEARCHES, before)
        res = self.forge.tick_searches(RID, ["pdf text extraction python3 pypdf"], by=BY)
        self.assertEqual(res.ticked, ["pdf text extraction python3 pypdf"])
        self.assertEqual(self.forge.approved_queries(RID), ["pdf text extraction python3 pypdf"])

    def test_url_spelled_differently_with_the_same_canonical_form(self):
        self.to_sources()
        res = self.forge.tick_sources(RID, ["HTTPS://DOCS.Example.ORG/guide"], by=BY)
        self.assertEqual(res.items[0].requested, "HTTPS://DOCS.Example.ORG/guide")
        self.assertEqual(outcomes(res), {URL2: TICKED})
        self.assertIn(f"- [x] `{URL2}`", self.text(SOURCES))

    def test_url_line_spelled_differently_in_the_file(self):
        self.to_sources()
        self.append_raw(SOURCES, "- [ ] `HTTPS://Example.NET/Path?Q=1` — title: t — why: w — take: k\n")
        res = self.forge.tick_sources(RID, ["https://example.net/Path?Q=1"], by=BY)
        self.assertEqual(res.ticked, ["https://example.net/Path?Q=1"])
        self.assertIn("- [x] `HTTPS://Example.NET/Path?Q=1` — title", self.text(SOURCES))  # text kept

    def test_url_differences_canonicalization_keeps_are_not_matched(self):
        self.to_sources()
        before = self.bytes_of(SOURCES)
        for url in ("https://docs.example.org/guide/", "http://docs.example.org/guide",
                    "https://docs.example.org/Guide", "https://docs.example.org:443/guide",
                    "https://docs.example.org/guide?"):
            self.assertEqual(self.forge.tick_sources(RID, [url], by=BY).not_found, [url], url)
        self.assert_untouched(SOURCES, before)


# -------------------------------------------------------------------- byte preservation

class BytePreservationTest(TickCase):
    def test_only_the_box_characters_change(self):
        self.new()
        content = (
            "\ufeff- [ ] first q — why: line one\r\n"
            "Prose with unicode é ü 日本 — and a form feed\x0c"
            "- [ ] second q — why: after a form feed\r"
            "  trailing spaces   \n"
            "line separator and a vertical tab\x0b"
            "<!-- - [ ] commented q — why: x -->\n"
            "```\n- [ ] fenced q — why: x\n```\n"
            "- [ ] middle q — why: keep   \t\r\n"
            "- [X] done q — why: x\n"
            "\n\n"
            "- [ ] untouched q — why: stays\n"
            "- [ ] last q — why: no newline at the end"
        )
        self.p(SEARCHES).write_bytes(content.encode("utf-8"))
        res = self.forge.tick_searches(RID, ["first q", "second q", "middle q", "last q", "done q",
                                             "commented q", "fenced q"], by=BY)
        self.assertEqual(outcomes(res), {"first q": TICKED, "second q": TICKED, "middle q": TICKED,
                                         "last q": TICKED, "done q": ALREADY, "commented q": NOT_FOUND,
                                         "fenced q": NOT_FOUND})
        expected = content
        for q in ("first q", "second q", "middle q", "last q"):
            self.assertEqual(expected.count(f"- [ ] {q} "), 1)
            expected = expected.replace(f"- [ ] {q} ", f"- [x] {q} ")
        self.assertEqual(self.bytes_of(SEARCHES), expected.encode("utf-8"))
        self.assertEqual(self.forge.approved_queries(RID), ["first q", "second q", "middle q", "done q", "last q"])

    def test_sources_file_bytes_preserved(self):
        self.to_sources()
        self.append_raw(SOURCES, "Some note Noah wrote, with ü and trailing space \r\n"
                                 "- [ ] `https://example.net/a_b*c~d&e` — title: Ünïcode tïtle — why: w — take: k")
        before = self.bytes_of(SOURCES).decode("utf-8")
        res = self.forge.tick_sources(RID, ["https://example.net/a_b*c~d&e", URL2], by=BY)
        self.assertEqual(res.ticked, ["https://example.net/a_b*c~d&e", URL2])
        expected = (before.replace("- [ ] `https://example.net/a_b", "- [x] `https://example.net/a_b")
                    .replace(f"- [ ] `{URL2}`", f"- [x] `{URL2}`"))
        self.assertEqual(self.bytes_of(SOURCES), expected.encode("utf-8"))

    def test_reparse_mismatch_writes_nothing(self):
        """Defence in depth: if the edited file would not parse as 'the same, plus these ticks',
        nothing is written (forced here by a parser that sees an extra line afterwards)."""
        self.setup_searches()
        before, state_before = self.bytes_of(SEARCHES), self.bytes_of(STATE)
        real = checklist.parse_searches
        calls = []

        def parser(text):
            calls.append(text)
            queries, bad = real(text)
            if len(calls) > 1:
                bad = bad + [checklist.Malformed(99, "x", "invented")]
            return queries, bad
        with mock.patch.object(checklist, "parse_searches", parser), \
                self.assertRaisesRegex(ForgeError, "would change more than the requested boxes"):
            self.forge.tick_searches(RID, ["pdf text extraction python"], by=BY)
        self.assertEqual(len(calls), 2)
        self.assert_untouched(SEARCHES, before, state_before)

    def test_write_is_atomic_rename(self):
        self.setup_searches()
        ino = os.lstat(self.p(SEARCHES)).st_ino
        self.forge.tick_searches(RID, ["pdf text extraction python"], by=BY)
        self.assertNotEqual(os.lstat(self.p(SEARCHES)).st_ino, ino)

    def test_failed_write_leaves_the_file_and_no_temp_files(self):
        self.setup_searches()
        before, state_before = self.bytes_of(SEARCHES), self.bytes_of(STATE)
        with mock.patch("os.fsync", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.forge.tick_searches(RID, ["pdf text extraction python"], by=BY)
        self.assert_untouched(SEARCHES, before, state_before)
        self.assertEqual([n for n in os.listdir(self.p(f"35-forge/{RID}")) if n.startswith(".")], [])
        self.assertFalse(os.path.lexists(self.p(f"35-forge/{LOCK}")))


# -------------------------------------------------------------------- state and input checks

class RefusalTest(TickCase):
    def test_states(self):
        self.setup_searches()
        with self.assertRaisesRegex(ForgeError, "ticking sources is not allowed while the request is "
                                                "awaiting-search-approval"):
            self.forge.tick_sources(RID, [URL], by=BY)

    def test_after_a_draft_searches_are_closed(self):
        self.to_review()
        before = self.bytes_of(SEARCHES)
        with self.assertRaisesRegex(ForgeError, f"ticking searches is not allowed while the request is {S_REVIEW}"):
            self.forge.tick_searches(RID, ["pdf text extraction python"], by=BY)
        with self.assertRaisesRegex(ForgeError, "ticking sources is not allowed"):
            self.forge.tick_sources(RID, [URL2], by=BY)
        self.assert_untouched(SEARCHES, before)

    def test_redraft_allows_source_ticks_like_the_gate(self):
        self.to_review()
        self.set_decision("revise")
        self.forge.decide(RID)
        self.assertEqual(self.status(), S_REDRAFT)
        with self.assertRaises(ForgeError):
            self.forge.tick_searches(RID, ["pdf text extraction python"], by=BY)
        self.assertEqual(self.forge.tick_sources(RID, [URL2], by=BY).ticked, [URL2])

    def test_decided_requests_refused(self):
        self.to_review()
        self.set_decision("approve")
        self.set_review("confirm_hash", "")
        self.forge.decide(RID)
        self.assertEqual(self.status(), S_APPROVED)
        before = self.bytes_of(SOURCES)
        with self.assertRaises(ForgeError):
            self.forge.tick_sources(RID, [URL2], by=BY)
        with self.assertRaises(ForgeError):
            self.forge.tick_searches(RID, ["pdf text extraction python"], by=BY)
        self.assert_untouched(SOURCES, before)

    def test_shelved_request_refused(self):
        self.to_review()
        self.set_decision("deny")
        self.forge.decide(RID)
        with self.assertRaisesRegex(ForgeError, "no request"):
            self.forge.tick_sources(RID, [URL2], by=BY)

    def test_bad_request_ids(self):
        for rid in ("../x", "2026-10-26_../x", "2026-10-26_X", "", "/etc", "2026-10-26_a/b"):
            with self.assertRaises(ForgeError, msg=rid):
                self.forge.tick_searches(rid, ["q"], by=BY)
            with self.assertRaises(ForgeError, msg=rid):
                self.forge.tick_sources(rid, [URL], by=BY)
        with self.assertRaisesRegex(ForgeError, "no request"):
            self.forge.tick_searches("2026-10-26_missing", ["q"], by=BY)

    def test_bad_inputs_refused_before_anything_is_read(self):
        self.setup_searches()
        before, state_before = self.bytes_of(SEARCHES), self.bytes_of(STATE)
        bad_queries = [
            "pdf text extraction python",            # a str, not a list
            [], [f"q{i}" for i in range(11)],
            [None], [b"q"], ["ok", "<img src=x>"], ["q `x`"], ["a — why: b"], ["why: x"],
            ["[x] q"], ["x" * 201], ["   "], ["q\u202e"],
        ]
        for queries in bad_queries:
            with self.assertRaises(ForgeError, msg=repr(queries)[:60]):
                self.forge.tick_searches(RID, queries, by=BY)
        self.assertEqual(len(self.forge.tick_searches(RID, [f"q{i}" for i in range(10)], by=BY).items), 10)
        self.assertEqual(self.forge.tick_searches(RID, ["x" * 200], by=BY).not_found, ["x" * 200])
        self.assert_untouched(SEARCHES, before, state_before)

    def test_bad_urls_refused(self):
        self.to_sources()
        before = self.bytes_of(SOURCES)
        for urls in (URL, [], [f"https://e.com/{i}" for i in range(31)], [None],
                     ["javascript:alert(1)"], ["https://e.com/#frag"], ["https://user@e.com/"],
                     ["ftp://e.com/"], ["https://0x7f.1/"], ["https://e.com/a`b"], ["https://é.com/"]):
            with self.assertRaises(ForgeError, msg=repr(urls)[:60]):
                self.forge.tick_sources(RID, urls, by=BY)
        self.assertEqual(len(self.forge.tick_sources(RID, [f"https://e.com/{i}" for i in range(30)], by=BY)
                             .not_found), 30)
        self.assert_untouched(SOURCES, before)

    def test_missing_sources_file_reports_not_found(self):
        self.setup_searches()
        self.tick(SEARCHES, "pdf text extraction python")
        # Only reachable by hand (status set to awaiting-source-approval with no sources.md).
        self.p(STATE).write_text(self.text(STATE).replace(f"status: {S_SEARCH}", f"status: {S_SOURCE}"))
        res = self.forge.tick_sources(RID, [URL], by=BY)
        self.assertEqual(res.not_found, [URL])
        self.assertFalse(os.path.lexists(self.p(SOURCES)))


# -------------------------------------------------------------------- confinement

class TickSymlinkTest(TickCase):
    def test_symlinked_searches_file_refused(self):
        self.setup_searches()
        target = self.outside / "s.md"
        target.write_text("- [ ] q — why: w\n", encoding="utf-8")
        os.replace(self.p(SEARCHES), self.base / "kept.md")
        os.symlink(target, self.p(SEARCHES))
        with self.assertRaises(PathRefused):
            self.forge.tick_searches(RID, ["q"], by=BY)
        self.assertEqual(target.read_text(encoding="utf-8"), "- [ ] q — why: w\n")
        self.assertTrue(os.path.islink(self.p(SEARCHES)))

    def test_symlinked_sources_file_refused(self):
        self.to_sources()
        target = self.outside / "src.md"
        target.write_text(f"- [ ] `{URL2}` — title: t — why: w — take: k\n", encoding="utf-8")
        os.replace(self.p(SOURCES), self.base / "kept.md")
        os.symlink(target, self.p(SOURCES))
        with self.assertRaises(PathRefused):
            self.forge.tick_sources(RID, [URL2], by=BY)
        self.assertIn("- [ ] ", target.read_text(encoding="utf-8"))

    def test_symlinked_request_folder_refused(self):
        self.setup_searches()
        evil = self.outside / "evil"
        os.rename(self.p(f"35-forge/{RID}"), evil)
        os.symlink(evil, self.p(f"35-forge/{RID}"))
        before = (evil / "searches.md").read_bytes()
        with self.assertRaises(PathRefused):
            self.forge.tick_searches(RID, ["pdf text extraction python"], by=BY)
        self.assertEqual((evil / "searches.md").read_bytes(), before)


# -------------------------------------------------------------------- locking and concurrency

class TickLockTest(TickCase):
    def test_refused_while_another_forge_command_holds_the_lock(self):
        self.setup_searches()
        before = self.bytes_of(SEARCHES)
        (self.p(f"35-forge/{LOCK}")).write_text("pid 1\n")
        with self.assertRaisesRegex(ForgeError, "another forge command is running"):
            self.forge.tick_searches(RID, ["pdf text extraction python"], by=BY)
        self.assert_untouched(SEARCHES, before)

    def test_propose_during_tick_is_refused_by_the_lock(self):
        self.setup_searches()
        real = self.forge._read_guarded
        inner = []

        def racing(rel):
            result = real(rel)
            try:
                self.forge.propose_searches(RID, [("racing proposal", "w")])
                inner.append("ran")
            except ForgeError as exc:
                inner.append(str(exc))
            return result
        self.forge._read_guarded = racing
        try:
            res = self.forge.tick_searches(RID, ["pdf text extraction python"], by=BY)
        finally:
            self.forge._read_guarded = real
        self.assertEqual(len(inner), 1)
        self.assertIn("another forge command is running", inner[0])
        self.assertEqual(res.ticked, ["pdf text extraction python"])
        self.assertNotIn("racing proposal", self.text(SEARCHES))
        self.assertFalse(os.path.lexists(self.p(f"35-forge/{LOCK}")))

    def test_tick_during_propose_is_refused_by_the_lock(self):
        self.setup_searches()
        real = self.forge._append_lines
        inner = []

        def racing(rel, lines):
            try:
                self.forge.tick_searches(RID, ["pdf text extraction python"], by=BY)
                inner.append("ran")
            except ForgeError as exc:
                inner.append(str(exc))
            return real(rel, lines)
        self.forge._append_lines = racing
        try:
            self.forge.propose_searches(RID, [("another one", "w")])
        finally:
            self.forge._append_lines = real
        self.assertIn("another forge command is running", inner[0])
        self.assertEqual(self.forge.approved_queries(RID), [])
        self.assertIn("- [ ] another one — why: w\n", self.text(SEARCHES))

    def test_noah_edit_during_tick_survives(self):
        self.setup_searches()
        state_before = self.bytes_of(STATE)
        real = self.forge._read_guarded

        def racing(rel):
            result = real(rel)
            if rel.endswith("searches.md"):
                self.p(rel).write_text(self.text(rel) + "- [x] noah added this — why: mine\n", encoding="utf-8")
            return result
        self.forge._read_guarded = racing
        try:
            with self.assertRaisesRegex(ForgeError, "changed while"):
                self.forge.tick_searches(RID, ["pdf text extraction python"], by=BY)
        finally:
            self.forge._read_guarded = real
        self.assertEqual(self.forge.approved_queries(RID), ["noah added this"])
        self.assertEqual(self.bytes_of(STATE), state_before)  # no audit line for a tick that never landed

    def write_searches(self, content):
        self.new()
        self.p(SEARCHES).write_bytes(content.encode("utf-8"))

    def test_edit_saved_during_the_temp_fsync_is_not_lost(self):
        """Reviewer P-1: Noah un-ticks b (in place, same size) while the tick's temp file is
        fsynced, after the first check and before the rename."""
        self.write_searches("- [ ] a q — why: w\n- [x] b q — why: w\n")
        state_before = self.bytes_of(STATE)
        real_fsync = os.fsync
        done = []

        def fsync(fd):
            if not done:
                done.append(1)
                with open(self.p(SEARCHES), "r+b") as fh:
                    data = fh.read().replace(b"- [x] b q", b"- [ ] b q")
                    fh.seek(0)
                    fh.write(data)
            return real_fsync(fd)
        with mock.patch("os.fsync", fsync), self.assertRaisesRegex(ForgeError, "changed while"):
            self.forge.tick_searches(RID, ["a q"], by=BY)
        self.assertEqual(done, [1])
        self.assertEqual(self.text(SEARCHES), "- [ ] a q — why: w\n- [ ] b q — why: w\n")
        self.assertEqual(self.forge.approved_queries(RID), [])
        self.assertEqual(self.bytes_of(STATE), state_before)
        self.assertEqual([n for n in os.listdir(self.p(f"35-forge/{RID}")) if n.startswith(".")], [])

    def test_same_size_edit_with_the_same_mtime_is_caught_by_the_bytes(self):
        """An in-place edit of the same size inside one mtime tick leaves inode, size and mtime
        alike; the byte comparison before the rename still refuses."""
        self.write_searches("- [ ] a q — why: w\n- [x] b q — why: w\n")
        real_fsync = os.fsync
        done = []

        def fsync(fd):
            if not done:
                done.append(1)
                path = self.p(SEARCHES)
                st = os.lstat(path)
                with open(path, "r+b") as fh:
                    data = fh.read().replace(b"- [x] b q", b"- [ ] b q")
                    fh.seek(0)
                    fh.write(data)
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
            return real_fsync(fd)
        with mock.patch("os.fsync", fsync), self.assertRaisesRegex(ForgeError, "changed while"):
            self.forge.tick_searches(RID, ["a q"], by=BY)
        self.assertEqual(self.forge.approved_queries(RID), [])

    def test_file_swapped_for_a_symlink_before_the_rename_is_refused(self):
        self.write_searches("- [ ] a q — why: w\n")
        target = self.outside / "t.md"
        target.write_text("- [ ] a q — why: w\n", encoding="utf-8")
        real_fsync = os.fsync
        done = []

        def fsync(fd):
            if not done:
                done.append(1)
                os.replace(self.p(SEARCHES), self.base / "kept.md")
                os.symlink(target, self.p(SEARCHES))
            return real_fsync(fd)
        with mock.patch("os.fsync", fsync), self.assertRaisesRegex(ForgeError, "changed while"):
            self.forge.tick_searches(RID, ["a q"], by=BY)
        self.assertTrue(os.path.islink(self.p(SEARCHES)))
        self.assertEqual(target.read_text(encoding="utf-8"), "- [ ] a q — why: w\n")

    def test_symlink_to_the_same_file_before_the_rename_is_refused(self):
        """Same inode, size, mtime and bytes, but reached through a symlink: still refused."""
        self.write_searches("- [ ] a q — why: w\n")
        real_fsync = os.fsync
        done = []

        def fsync(fd):
            if not done:
                done.append(1)
                os.replace(self.p(SEARCHES), self.outside / "moved.md")
                os.symlink(self.outside / "moved.md", self.p(SEARCHES))
            return real_fsync(fd)
        with mock.patch("os.fsync", fsync), self.assertRaisesRegex(ForgeError, "changed while"):
            self.forge.tick_searches(RID, ["a q"], by=BY)
        self.assertTrue(os.path.islink(self.p(SEARCHES)))

    def test_concurrent_proposals_and_ticks_lose_nothing(self):
        self.setup_searches(proposals=[(f"seed {i}", "w") for i in range(10)])
        errors = []

        def retry(fn):
            for _ in range(20000):
                try:
                    return fn()
                except ForgeError as exc:
                    if "another forge command" not in str(exc) and "changed while" not in str(exc):
                        errors.append(exc)
                        return None
            errors.append("gave up")
            return None

        def proposer():
            for i in range(15):
                retry(lambda i=i: self.forge.propose_searches(RID, [(f"new {i}", "w")]))

        def ticker():
            for i in range(10):
                retry(lambda i=i: self.forge.tick_searches(RID, [f"seed {i}"], by=BY))

        threads = [threading.Thread(target=proposer), threading.Thread(target=ticker)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        text = self.text(SEARCHES)
        for i in range(10):
            self.assertEqual(text.count(f"- [x] seed {i} — why: w\n"), 1, i)
        for i in range(15):
            self.assertEqual(text.count(f"- [ ] new {i} — why: w\n"), 1, i)
        self.assertEqual(sorted(self.forge.approved_queries(RID)), sorted(f"seed {i}" for i in range(10)))


# -------------------------------------------------------------------- swaps before the rename (P round 2)

def open_fds():
    return len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None


class SwapBeforeRenameTest(TickCase):
    def setUp(self):
        super().setUp()
        self.new()
        self.p(SEARCHES).write_bytes("- [ ] a q — why: w\n- [x] b q — why: w\n".encode())
        self.state_before = self.bytes_of(STATE)

    def during_fsync(self, action):
        real_fsync = os.fsync
        done = []

        def fsync(fd):
            if not done:
                done.append(1)
                action()
            return real_fsync(fd)
        return mock.patch("os.fsync", fsync), done

    def dots(self):
        return sorted(n for n in os.listdir(self.p(f"35-forge/{RID}")) if n.startswith("."))

    def assert_refused_cleanly(self, action, timeout=5):
        patch, done = self.during_fsync(action)
        fds = open_fds()
        outcome = {}

        def run():
            try:
                with patch:
                    self.forge.tick_searches(RID, ["a q"], by=BY)
                outcome["r"] = "written"
            except BaseException as exc:  # noqa: BLE001 - reported below
                outcome["r"] = exc
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            with open(self.p(SEARCHES), "wb"):
                pass  # unblock a FIFO reader so the thread can end
            t.join(timeout)
            self.fail("the tick blocked (lock held)")
        self.assertIsInstance(outcome["r"], ForgeError, outcome["r"])
        self.assertIn("changed while", str(outcome["r"]))
        self.assertEqual(done, [1])
        self.assertEqual(self.dots(), [])                       # no temp file, no lock left
        self.assertEqual(self.bytes_of(STATE), self.state_before)
        if fds is not None:
            self.assertEqual(open_fds(), fds)                   # no fd leaked

    def test_directory_swapped_in_is_a_forge_error_without_fd_leak(self):
        def act():
            os.replace(self.p(SEARCHES), self.base / "gone.md")
            os.mkdir(self.p(SEARCHES))
        self.assert_refused_cleanly(act)
        self.assertTrue(os.path.isdir(self.p(SEARCHES)))

    def test_fifo_swapped_in_does_not_block(self):
        def act():
            os.replace(self.p(SEARCHES), self.base / "gone.md")
            os.mkfifo(self.p(SEARCHES))
        self.assert_refused_cleanly(act)

    def test_identical_bytes_saved_by_rename_with_the_mtime_kept_is_refused(self):
        """Reviewer P2-4: a rename-style save of the same bytes (new inode, mtime copied) is a
        different file; fail safe."""
        def act():
            path = self.p(SEARCHES)
            st = os.lstat(path)
            tmp = self.base / "x.md"
            tmp.write_bytes(path.read_bytes())
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.replace(tmp, path)
        self.assert_refused_cleanly(act)

    def test_file_deleted_is_refused_and_not_recreated(self):
        self.assert_refused_cleanly(lambda: os.unlink(self.p(SEARCHES)))
        self.assertFalse(os.path.lexists(self.p(SEARCHES)))

    def test_fifo_or_directory_in_place_before_the_tick_is_refused_without_blocking(self):
        for make in (os.mkfifo, os.mkdir):
            if os.path.lexists(self.p(SEARCHES)):
                os.replace(self.p(SEARCHES), self.base / "kept.md")
            make(self.p(SEARCHES))
            fds = open_fds()
            outcome = {}

            def run():
                try:
                    self.forge.tick_searches(RID, ["a q"], by=BY)
                    outcome["r"] = "written"
                except BaseException as exc:  # noqa: BLE001
                    outcome["r"] = exc
            t = threading.Thread(target=run, daemon=True)
            t.start()
            t.join(5)
            if t.is_alive():
                with open(self.p(SEARCHES), "wb"):
                    pass
                t.join(5)
                self.fail(f"{make.__name__}: the tick blocked")
            self.assertIsInstance(outcome["r"], PathRefused, outcome["r"])
            if fds is not None:
                self.assertEqual(open_fds(), fds)
            self.assertFalse(os.path.lexists(self.p(f"35-forge/{LOCK}")))
            os.rmdir(self.p(SEARCHES)) if make is os.mkdir else os.unlink(self.p(SEARCHES))


# -------------------------------------------------------------------- review.md in decide / revive

class ReviewRewriteTest(TickCase):
    """decide (approve) and revive rewrite review.md through _replace_if_same: an in-place edit by
    Noah of the same size, with the mtime unchanged, is caught by the byte check."""

    def edit_review_in_place(self, rel):
        real = self.forge._replace_if_same
        done = []

        def racing(r, fingerprint, old, new):
            if r == rel:
                done.append(1)
                path = self.p(rel)
                st = os.lstat(path)
                with open(path, "r+b") as fh:
                    data = fh.read().replace(b"Read the draft", b"READ the draft")
                    fh.seek(0)
                    fh.write(data)
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
            return real(r, fingerprint, old, new)
        self.forge._replace_if_same = racing
        self.addCleanup(setattr, self.forge, "_replace_if_same", real)
        return done

    def test_revive_keeps_noahs_review_edit(self):
        self.to_review()
        self.set_decision("deny")
        self.forge.decide(RID)
        rel = "95-shelf/pdf-summarizer/review.md"
        done = self.edit_review_in_place(rel)
        with self.assertRaisesRegex(ForgeError, "changed while"):
            self.forge.revive("pdf-summarizer")
        self.assertEqual(done, [1])
        self.assertIn("READ the draft", self.text(rel))
        self.assertEqual(self.meta(rel)["decision"], "deny")       # not reset: nothing was written
        self.assertTrue(os.path.isdir(self.p("95-shelf/pdf-summarizer")))
        self.assertFalse(os.path.lexists(self.p(f"35-forge/{RID}")))

    def test_approve_keeps_noahs_review_edit_and_verify_flags_it(self):
        self.to_review()
        self.set_decision("approve")
        self.set_review("confirm_hash", "")
        rel = f"35-forge/{RID}/review.md"
        done = self.edit_review_in_place(rel)
        with self.assertRaisesRegex(ForgeError, "changed while"):
            self.forge.decide(RID)
        self.assertEqual(done, [1])
        self.assertIn("READ the draft", self.text(rel))
        self.assertNotIn("approved_hash", self.text(rel))
        result = self.forge.verify("pdf-summarizer")          # installed, but the record is missing
        self.assertFalse(result.ok)
        self.assertTrue(any("review.md" in p for p in result.problems), result.problems)

    def test_unedited_review_is_rewritten_by_rename(self):
        self.to_review()
        self.set_decision("deny")
        self.forge.decide(RID)
        ino = os.lstat(self.p("95-shelf/pdf-summarizer/review.md")).st_ino
        self.forge.revive("pdf-summarizer")
        rel = f"35-forge/{RID}/review.md"
        self.assertIn("decision: pending", self.text(rel))
        self.assertNotEqual(os.lstat(self.p(rel)).st_ino, ino)


if __name__ == "__main__":
    unittest.main()
