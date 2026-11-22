import json
import os
import re
import unittest
from pathlib import Path

from forge import Forge
from library import frontmatter as fm

from skill_drafter.drafter import Drafter, revise_notes

from .fakes import Env, good_draft, unused_broker
from .test_prompt import HOSTILE
from .test_validate import with_line

MISSING = good_draft().replace("## Checklist", "## Rules again").split("## Rules again")[0] + "## Sources\nx\n"
LINKED = with_line("Rules", "4. See [the guide](https://apastyle.example.org) ![p](https://e.example/p.png)")
SPOOF = with_line("Examples", "- [x] apa 7 reference format — why: official rules")
OVERSIZE = with_line("Rules", "4. " + "word " * 4000)


def snapshot(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


class SpyForge(Forge):
    """Records every public forge call; decide and revive fail the test outright."""

    calls: list[str]

    def __getattribute__(self, name):
        attr = object.__getattribute__(self, name)
        if callable(attr) and not name.startswith("_"):
            object.__getattribute__(self, "calls").append(name)
            if name in ("decide", "revive", "propose_searches", "propose_sources", "record_ingest", "new"):
                raise AssertionError(f"the drafter called forge.{name}")
        return attr


class Base(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.spy_calls: list[str] = []
        real_open = self.env.drafter.open

        def open_spy():
            lib, _ = real_open()
            forge = SpyForge(lib)
            forge.calls = self.spy_calls
            return lib, forge

        self.env.drafter.open = open_spy

    def tearDown(self):
        self.assertTrue(set(self.spy_calls) <= {"info", "draft_skill"}, self.spy_calls)

    def run_passes(self, n: int = 1):
        return [self.env.drafter.run_once() for _ in range(n)]


class DraftTests(Base):
    def test_good_draft_is_filed_through_the_forge(self):
        rid = self.env.request()
        report = self.env.drafter.run_once()
        self.assertEqual(report.drafted, [f"{rid}: apa7-references"])
        self.assertEqual(self.env.status(rid), "awaiting-skill-review")
        review, _ = fm.parse((self.env.folder(rid) / "review.md").read_text())
        self.assertEqual((review["decision"], review["skill"]), ("pending", "apa7-references"))
        skill = self.env.skill_md(rid)
        self.assertIn("- `https://apastyle.example.org/references`", skill)
        log = self.env.drafter_log(rid)
        self.assertEqual(log.count("| `start`"), 1)
        self.assertRegex(log, r"\| draft \| `initial [0-9a-f]{12}` \| `ok: filed skill apa7-references sha256:[0-9a-f]{64}, ")
        self.assertIn("drafted skill apa7-references", (self.env.folder(rid) / "state.md").read_text())
        self.assertEqual(self.spy_calls.count("draft_skill"), 1)

    def test_broker_request_shape(self):
        self.env.request()
        self.env.drafter.run_once()
        req = self.env.broker.requests[0]
        self.assertEqual(req["path"], "/api/chat")
        body = req["body"]
        self.assertEqual((body["model"], body["stream"], body["think"]), ("assistant", False, False))
        self.assertEqual(body["options"], {"temperature": 0.2, "num_ctx": 16384})
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])

    def test_approved_skill_passes_library_check(self):
        rid = self.env.request()
        self.env.drafter.run_once()
        path = self.env.folder(rid) / "review.md"
        path.write_text(path.read_text().replace("decision: pending", "decision: approve", 1))
        self.env.forge.decide(rid)  # Noah's channel, not the drafter
        self.assertEqual([v for v in self.env.lib.check() if "30-skills" in str(v)], [])

    def test_idempotent_no_redraft_once_a_draft_exists(self):
        rid = self.env.request()
        self.run_passes(4)
        self.assertEqual(self.env.broker.calls, 1)
        self.assertEqual(self.env.drafter_log(rid).count("| `start`"), 1)
        self.assertFalse((self.env.folder(rid) / "previous").exists())

    def test_existing_skill_folder_is_never_overwritten(self):
        rid = self.env.request()
        (self.env.folder(rid) / "skill").mkdir()
        (self.env.folder(rid) / "skill" / "SKILL.md").write_text("Noah's own draft")
        self.run_passes(2)
        self.assertEqual(self.env.broker.calls, 0)
        self.assertEqual(self.env.skill_md(rid), "Noah's own draft")
        self.assertIn("already exists", "\n".join(self.env.drafter.status()))

    def test_no_ingested_source_means_nothing_due(self):
        rid = self.env.request(pages={}, ticked_extra=["https://a.example.org/x"])
        self.run_passes(2)
        self.assertEqual(self.env.broker.calls, 0)
        self.assertEqual(self.env.drafter_log(rid), "")

    def test_waits_for_the_gate_to_finish_ticked_sources(self):
        rid = self.env.request(ticked_extra=["https://slow.example.org/x"])
        self.run_passes(2)
        self.assertEqual(self.env.broker.calls, 0)
        self.assertIn("waiting for the research gate: 1", "\n".join(self.env.drafter.status()))
        gate_log = self.env.folder(rid) / "gate-log.md"
        gate_log.write_text("---\ntitle: g\n---\n"
                            "2026-10-27T09:00:00+00:00 | fetch | `https://slow.example.org/x` | `start`\n"
                            "2026-10-27T09:00:01+00:00 | fetch | `https://slow.example.org/x` | `error: timeout`\n")
        self.env.drafter.run_once()
        self.assertEqual(self.env.broker.calls, 0)  # one gate attempt of 3 used: still pending
        with gate_log.open("a") as fh:
            fh.write("2026-10-27T09:01:00+00:00 | refuse | `https://slow.example.org/x` | `fetch refused: private`\n")
        self.env.drafter.run_once()
        self.assertEqual(self.env.broker.calls, 1)
        self.assertEqual(self.env.status(rid), "awaiting-skill-review")

    def test_gate_attempts_used_up_counts_as_settled(self):
        rid = self.env.request(ticked_extra=["https://slow.example.org/x"])
        lines = "".join(f"2026-10-27T09:0{i}:00+00:00 | fetch | `https://slow.example.org/x` | `{r}`\n"
                        for i, r in enumerate(["start", "error: a", "start", "error: not counted: x",
                                               "start", "error: b", "start", "error: c"]))
        (self.env.folder(rid) / "gate-log.md").write_text("---\ntitle: g\n---\n" + lines)
        self.env.drafter.run_once()
        self.assertEqual(self.env.broker.calls, 1)

    def test_only_pdf_sources_are_skipped_without_a_broker_call(self):
        rid = self.env.forge.new("learn x", "learn x\n")
        self.env.forge.propose_searches(rid, [("x", "y")])
        self.env.tick(rid, "searches.md", "x")
        self.env.forge.propose_sources(rid, [("https://a.example.org/x.pdf", "pdf", "w", "t")])
        self.env.tick(rid, "sources.md", "x.pdf")
        self.env.ingest(rid, "https://a.example.org/x.pdf", "%PDF-1.4", ext="pdf")
        self.run_passes(3)
        self.assertEqual(self.env.broker.calls, 0)
        log = self.env.drafter_log(rid)
        self.assertEqual(log.count("skipped: no ingested source has text"), 1)

    def test_one_broken_request_never_blocks_another(self):
        bad = self.env.request(title="learn broken")
        (self.env.folder(bad) / "state.md").write_text("garbage")
        good = self.env.request(title="learn good")
        report = self.env.drafter.run_once()
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(self.env.status(good), "awaiting-skill-review")


class InvalidReplyTests(Base):
    def check_invalid(self, reply: str, reason: str):
        rid = self.env.request()
        self.env.broker.replies = [reply, good_draft()]
        self.env.drafter.run_once()
        self.assertEqual(self.env.status(rid), "awaiting-source-approval")
        self.assertFalse((self.env.folder(rid) / "skill").exists())
        self.assertIn(f"| `invalid: {reason}", self.env.drafter_log(rid))
        self.env.drafter.run_once()  # retried next poll
        self.assertEqual(self.env.status(rid), "awaiting-skill-review")
        self.assertEqual(self.env.broker.calls, 2)
        self.assertNotIn("](", self.env.skill_md(rid))

    def test_missing_sections(self):
        self.check_invalid(MISSING, "missing sections: Checklist, Examples")

    def test_missing_sections_named(self):
        text = good_draft()
        self.check_invalid(text[:text.index("## Examples")] + "## Sources\n", "missing sections: Examples")

    def test_link_or_image(self):
        self.check_invalid(LINKED, "Rules contains a URL")

    def test_spoof_checkbox(self):
        self.check_invalid(SPOOF, "a checkbox line outside Checklist")

    def test_oversize(self):
        self.check_invalid(OVERSIZE, "too large")

    def test_retries_are_capped_and_marked_failed(self):
        rid = self.env.request(title="learn failing")
        other = self.env.request(title="learn fine")
        calls = []

        def reply():
            calls.append(1)
            prompt = self.env.broker.requests[-1]["body"]["messages"][1]["content"]
            return LINKED if "learn failing" in prompt else good_draft()

        self.env.broker.replies = [reply]
        self.run_passes(6)
        log = self.env.drafter_log(rid)
        self.assertEqual(log.count("| `start`"), 3)
        self.assertEqual(log.count("| `invalid: "), 3)
        self.assertEqual(log.count("| `failed: draft failed after 3 attempts"), 1)
        self.assertEqual(len(calls), 4)  # 3 for the failing request, 1 for the other
        self.assertEqual(self.env.status(rid), "awaiting-source-approval")
        self.assertEqual(self.env.status(other), "awaiting-skill-review")
        self.assertIn("]  failed round 'initial ", "\n".join(self.env.drafter.status()))

    def test_cap_holds_after_a_crash_before_the_failed_mark(self):
        rid = self.env.request()
        target = self.env.drafter.assess(self.env.lib, self.env.forge, rid).target
        from skill_drafter.drafterlog import DrafterLog
        dlog = DrafterLog(self.env.lib, f"35-forge/{rid}", rid)
        for _ in range(3):  # three attempts, then a crash: no `failed:` line was written
            dlog.write(target, "start")
            dlog.write(target, "invalid: x")
        self.run_passes(2)
        self.assertEqual(self.env.broker.calls, 0)
        self.assertEqual(self.env.drafter_log(rid).count("| `failed: draft failed after 3 attempts"), 1)

    def test_new_source_starts_a_new_round_after_a_failure(self):
        rid = self.env.request()
        self.env.broker.replies = [LINKED]
        self.run_passes(4)
        self.assertEqual(self.env.broker.calls, 3)
        # Noah ticks one more source; the gate ingests it: new inputs, fresh attempts.
        self.env.forge.propose_sources(rid, [("https://more.example.org/y", "more", "w", "t")])
        self.env.tick(rid, "sources.md", "more.example.org")
        self.env.ingest(rid, "https://more.example.org/y", "more text")
        self.env.broker.replies = [good_draft()]
        self.env.drafter.run_once()
        self.assertEqual(self.env.status(rid), "awaiting-skill-review")

    def test_broker_http_error_and_length_cutoff_count_as_attempts(self):
        rid = self.env.request()
        self.env.broker.replies = [{"status": 500, "body": b"{}"},
                                   {"status": 200, "body": b'{"message": {"content": "x"}, "done_reason": "length"}'},
                                   {"status": 200, "body": b"not json"}]
        self.run_passes(4)
        log = self.env.drafter_log(rid)
        self.assertIn("error: broker answered HTTP 500", log)
        self.assertIn("error: the model stopped at its length limit", log)
        self.assertIn("error: broker reply is not JSON", log)
        self.assertIn("failed: draft failed after 3 attempts", log)
        self.assertEqual(self.env.broker.calls, 3)

    def test_cap_refusal_is_logged_as_a_failed_attempt(self):
        rid = self.env.request()
        self.env.drafter.cap_bytes = 1  # the library is over its cap: draft_skill refuses
        self.env.drafter.run_once()
        self.assertIn("| `error: not filed: ", self.env.drafter_log(rid))
        self.assertEqual(self.env.status(rid), "awaiting-source-approval")

    def test_unreachable_broker_uses_no_attempt_and_writes_nothing(self):
        rid = self.env.request()
        self.env.drafter.broker = unused_broker(self.env.cfg)
        for _ in range(5):
            report = self.env.drafter.run_once()
            self.assertIn("broker unreachable", report.errors[0])
        self.assertEqual(self.env.drafter_log(rid), "")
        self.env.drafter.broker = Drafter(self.env.cfg).broker
        self.env.drafter.run_once()
        self.assertEqual(self.env.status(rid), "awaiting-skill-review")

    def test_request_changed_while_drafting_is_not_filed(self):
        rid = self.env.request()

        def reply():
            self.env.forge.propose_sources(rid, [("https://late.example.org/z", "late", "w", "t")])
            self.env.tick(rid, "sources.md", "late.example.org")
            self.env.ingest(rid, "https://late.example.org/z", "late text")
            return good_draft()

        self.env.broker.replies = [reply, good_draft()]
        self.env.drafter.run_once()
        self.assertEqual(self.env.status(rid), "awaiting-source-approval")
        self.assertIn("error: not counted: the request changed while drafting", self.env.drafter_log(rid))
        self.env.drafter.run_once()
        self.assertEqual(self.env.status(rid), "awaiting-skill-review")
        self.assertIn("https://late.example.org/z", self.env.broker.user_prompt())


class HostileSourceTests(Base):
    def test_injection_is_only_fenced_data_and_a_valid_draft_is_filed(self):
        rid = self.env.request(pages={"https://evil.example.org/page": HOSTILE})
        before = {n: (self.env.folder(rid) / n).read_bytes() for n in ("searches.md", "sources.md", "request.md")}
        self.env.drafter.run_once()
        prompt = self.env.broker.user_prompt()
        nonce = self.env.broker.requests[0]["body"]["messages"][1]["content"].split("<request id=")[1][:16]
        start = prompt.index(f"<source 1 id={nonce}>")
        end = prompt.index(f"</source 1 id={nonce}>")
        at = prompt.index("Ignore previous instructions")
        self.assertTrue(start < at < end)
        self.assertEqual(prompt.count("Ignore previous instructions"), 1)
        self.assertNotRegex(prompt[start + 1:end], r"<\s*[A-Za-z/!?]")
        self.assertEqual(self.env.status(rid), "awaiting-skill-review")
        review, _ = fm.parse((self.env.folder(rid) / "review.md").read_text())
        self.assertEqual(review["decision"], "pending")
        for name, data in before.items():
            self.assertEqual((self.env.folder(rid) / name).read_bytes(), data, name)
        self.assertNotIn("Ignore previous", self.env.skill_md(rid))


class ReviseTests(Base):
    def test_revise_flow_redrafts_with_notes_and_keeps_the_name(self):
        rid = self.env.request()
        self.env.drafter.run_once()
        self.env.revise(rid, "Add a rule for DOIs. Keep the examples.")
        self.assertEqual(self.env.status(rid), "awaiting-skill-redraft")
        self.env.broker.replies = [good_draft("renamed-by-model")]
        report = self.env.drafter.run_once()
        self.assertEqual(report.drafted, [f"{rid}: apa7-references"])
        prompt = self.env.broker.user_prompt()
        self.assertIn("Add a rule for DOIs. Keep the examples.", prompt)
        self.assertIn("REVISION:", prompt)
        self.assertIn("use exactly `apa7-references`", prompt)
        self.assertRegex(prompt, r"<previous-draft id=[0-9a-f]+>\n---\ntitle: \"Skill: apa7-references\"")
        self.assertEqual(self.env.status(rid), "awaiting-skill-review")
        self.assertTrue((self.env.folder(rid) / "previous" / "1" / "skill" / "SKILL.md").exists())
        self.assertIn("decision: revise", (self.env.folder(rid) / "previous" / "1" / "review.md").read_text())
        review, _ = fm.parse((self.env.folder(rid) / "review.md").read_text())
        self.assertEqual((review["decision"], review["skill"]), ("pending", "apa7-references"))
        self.assertIn("# apa7-references", self.env.skill_md(rid))
        self.assertRegex(self.env.drafter_log(rid), r"`revise [0-9a-f]{12}` \| `ok: filed skill apa7-references")
        self.run_passes(2)
        self.assertEqual(self.env.broker.calls, 2)

    def test_revise_without_notes_waits(self):
        rid = self.env.request()
        self.env.drafter.run_once()
        self.env.revise(rid, None)
        self.run_passes(2)
        self.assertEqual(self.env.broker.calls, 1)
        self.assertIn("has no notes", "\n".join(self.env.drafter.status()))
        path = self.env.folder(rid) / "review.md"
        path.write_text(path.read_text() + "\n## Notes\n\nShorter rules, please.\n")
        self.env.drafter.run_once()
        self.assertEqual(self.env.broker.calls, 2)
        self.assertIn("Shorter rules, please.", self.env.broker.user_prompt())

    def test_revise_set_but_not_decided_is_not_drafted(self):
        rid = self.env.request()
        self.env.drafter.run_once()
        path = self.env.folder(rid) / "review.md"
        path.write_text(path.read_text().replace("decision: pending", "decision: revise\nnotes: more", 1))
        before = path.read_bytes()
        self.run_passes(2)
        self.assertEqual(self.env.broker.calls, 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertIn("waiting for 'forge decide'", "\n".join(self.env.drafter.status()))

    def test_edited_notes_start_a_new_round_after_a_failed_one(self):
        rid = self.env.request()
        self.env.drafter.run_once()
        self.env.revise(rid, "first notes")
        self.env.broker.replies = [MISSING]
        self.run_passes(4)
        self.assertEqual(self.env.broker.calls, 4)
        path = self.env.folder(rid) / "review.md"
        path.write_text(path.read_text().replace("first notes", "second notes"))
        self.env.broker.replies = [good_draft()]
        self.env.drafter.run_once()
        self.assertEqual(self.env.status(rid), "awaiting-skill-review")

    def test_revise_notes_sources(self):
        rid, skill = "2026-10-27_x", "x"
        from forge.core import REVIEW_INTRO
        intro = REVIEW_INTRO.format(skill=skill, rid=rid)
        self.assertEqual(revise_notes({"notes": ["a", "b"]}, intro, skill, rid), "a\nb")
        self.assertEqual(revise_notes({}, intro + "\n# Notes\nfix it\n# Other\nno", skill, rid), "fix it")
        self.assertEqual(revise_notes({}, intro + "\nplease fix the examples\n", skill, rid),
                         "please fix the examples")
        self.assertIsNone(revise_notes({}, intro, skill, rid))
        self.assertIsNone(revise_notes({"notes": "  "}, intro, skill, rid))


def raw_reply(content: str, **extra) -> dict:
    body = {"message": {"role": "assistant", "content": content}, "done": True, "done_reason": "stop"}
    body.update(extra)
    return {"status": 200, "body": json.dumps(body).encode()}


class ReviewerJDrafterTests(Base):
    def test_prompt_truncated_at_num_ctx_margin_is_an_invalid_attempt(self):
        rid = self.env.request()
        self.env.broker.replies = [raw_reply(good_draft(), prompt_eval_count=16384 - 256),
                                   raw_reply(good_draft(), prompt_eval_count=16384 - 257)]
        self.env.drafter.run_once()
        log = self.env.drafter_log(rid)
        self.assertIn("| `invalid: prompt truncated: the model read 16128 prompt tokens (num_ctx 16384, limit 16128)", log)
        self.assertEqual(self.env.status(rid), "awaiting-source-approval")
        self.env.drafter.run_once()
        self.assertEqual(self.env.status(rid), "awaiting-skill-review")
        self.assertEqual(self.env.drafter_log(rid).count("| `start`"), 2)

    def test_prompt_truncation_uses_the_configured_num_ctx(self):
        env = Env(self, num_ctx=4096)
        rid = env.request()
        env.broker.replies = [raw_reply(good_draft(), prompt_eval_count=5000)]
        env.drafter.run_once()
        self.assertIn("num_ctx 4096, limit 3840", env.drafter_log(rid))

    def test_sources_changed_after_the_draft_is_noted_once(self):
        rid = self.env.request()
        self.env.drafter.run_once()
        self.assertRegex(self.env.drafter_log(rid), r"; sources [0-9a-f]{12}`")
        self.run_passes(2)
        self.assertNotIn("sources changed", self.env.drafter_log(rid))
        # A late ingest lands after the draft (e.g. the gate's record raced the filing).
        state = self.env.folder(rid) / "state.md"
        state.write_text(state.read_text().replace(
            "ingested:\n", 'ingested:\n  - "https://late.example.org/z -> 10-sources/learn-apa/2026-10-27_late.txt"\n', 1))
        self.assertIn("sources changed after this draft", "\n".join(self.env.drafter.status()))
        self.run_passes(3)
        log = self.env.drafter_log(rid)
        self.assertEqual(log.count("note: sources changed after this draft (2 ingested now"), 1)
        self.assertEqual(self.env.broker.calls, 1)
        self.assertEqual(self.env.status(rid), "awaiting-skill-review")

    def test_name_taken_in_30_skills_gets_a_suffix(self):
        (self.env.root / "30-skills" / "apa7-references").mkdir()
        (self.env.root / "30-skills" / "APA7-References-2").mkdir()  # case-insensitive clash too
        rid = self.env.request()
        self.env.drafter.run_once()
        skill = self.env.skill_md(rid)
        self.assertIn('name: "apa7-references-3"', skill)
        self.assertIn("\n# apa7-references-3\n", skill)
        self.assertIn("ok: filed skill apa7-references-3 (apa7-references is taken in 30-skills/, so named "
                      "apa7-references-3)", self.env.drafter_log(rid))
        review = (self.env.folder(rid) / "review.md").read_text()
        self.assertIn('skill: "apa7-references-3"', review)

    def test_long_name_suffix_stays_within_48(self):
        name = "a" * 47 + "b"
        (self.env.root / "30-skills" / name).mkdir()
        self.assertEqual(Drafter._free_name(self.env.lib, name), "a" * 46 + "-2")

    def test_revise_of_the_same_skill_keeps_its_name(self):
        rid = self.env.request()
        self.env.drafter.run_once()
        self.env.revise(rid, "shorter")
        (self.env.root / "30-skills" / "apa7-references").mkdir()
        self.env.drafter.run_once()
        self.assertIn('name: "apa7-references"', self.env.skill_md(rid))
        self.assertNotIn("is taken", self.env.drafter_log(rid))

    def test_not_counted_line_gives_its_attempt_back(self):
        rid = self.env.request()
        target = self.env.drafter.assess(self.env.lib, self.env.forge, rid).target
        from skill_drafter.drafterlog import DrafterLog, parse
        dlog = DrafterLog(self.env.lib, f"35-forge/{rid}", rid)
        for _ in range(3):
            dlog.write(target, "start")
            dlog.write(target, "error: not counted: broker unreachable (ConnectionRefusedError)")
        self.assertEqual(parse(dlog.read())[target].attempts, 0)
        self.env.drafter.run_once()
        self.assertEqual(self.env.broker.calls, 1)
        self.assertEqual(self.env.status(rid), "awaiting-skill-review")

    def test_each_prompt_gets_a_fresh_nonce(self):
        self.env.request(title="learn one")
        self.env.request(title="learn two")
        self.env.drafter.run_once()
        nonces = [re.search(r"<request id=([0-9a-f]{16})>", r["body"]["messages"][1]["content"]).group(1)
                  for r in self.env.broker.requests]
        self.assertEqual(len(nonces), 2)
        self.assertNotEqual(nonces[0], nonces[1])

    def test_revise_waits_for_newly_ticked_sources(self):
        rid = self.env.request()
        self.env.drafter.run_once()
        self.env.revise(rid, "add the DOI rules")
        with (self.env.folder(rid) / "sources.md").open("a", encoding="utf-8") as fh:  # Noah adds and ticks one
            fh.write("- [x] `https://doi.example.org/rules` \u2014 title: doi \u2014 why: w \u2014 take: t\n")
        self.env.drafter.run_once()
        self.assertEqual(self.env.broker.calls, 1)
        self.env.ingest(rid, "https://doi.example.org/rules", "DOI text")
        self.env.drafter.run_once()
        self.assertEqual(self.env.broker.calls, 2)
        self.assertIn("DOI text", self.env.broker.user_prompt())


class ReadOnlyTests(Base):
    def test_only_drafter_log_and_forge_draft_files_change(self):
        rid = self.env.request()
        before = snapshot(self.env.folder(rid))
        self.env.drafter.run_once()
        after = snapshot(self.env.folder(rid))
        changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        self.assertEqual(changed, {"drafter-log.md", "state.md", "review.md", "skill/SKILL.md"})
        sources = {p.name for p in (self.env.root / "10-sources").rglob("*")}
        self.env.drafter.run_once()
        self.assertEqual({p.name for p in (self.env.root / "10-sources").rglob("*")}, sources)

    def test_symlinked_source_item_is_refused(self):
        rid = self.env.request()
        item = next((self.env.root / "10-sources" / "learn-apa").glob("*.txt"))
        os.unlink(item)
        os.symlink("/etc/hostname", item)
        report = self.env.drafter.run_once()
        self.assertEqual(self.env.broker.calls, 0)
        self.assertIn("symlink", report.errors[0])


if __name__ == "__main__":
    unittest.main()
