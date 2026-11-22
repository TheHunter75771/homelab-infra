import io
import json
import os
import re
import socket
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from library import CapExceeded, LibraryError
from library import frontmatter as fm
from voice_profile.__main__ import main
from voice_profile.build import NEXT, PROFILE, BuildError

from .fakes import GOOD, Env, document, essay, para, snapshot

APPROVED = "---\ntitle: Voice profile\nstatus: approved\n---\n# Voice profile\n\nNoah's own edits.\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)

    def build(self, **kw):
        return self.env.builder().build(**kw)

    def profile(self) -> str:
        return self.env.read("voice-profile.md")


class BuildTests(Base):
    def test_full_build_writes_every_output(self):
        self.env.add_corpus(5)
        self.env.add("Technical Report Peer Review Sheet for Classmate.docx", essay(900))
        self.env.add("Essay 1 copy.docx", essay(100, title="Essay Number 1"), mtime=1_600_000_000)
        self.env.add("notes.docx.bak", essay(1))  # not .docx: listed, not read
        (self.env.samples / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0")
        report = self.build()
        self.assertEqual(report.errors, [])
        self.assertEqual([k[0] for k in report.kept], [f"Essay {i}.docx" for i in range(1, 6)])
        self.assertEqual(report.merged, [("Essay 1 copy.docx", "Essay 1.docx", 1.0)])
        reasons = dict(report.excluded)
        self.assertIn("peer review", reasons["Technical Report Peer Review Sheet for Classmate.docx"])
        self.assertIn("not a .docx", reasons["old.doc"])

        text = self.env.read("samples-text/essay-1.md")
        meta, body = fm.parse(text)
        self.assertEqual((meta["source"], meta["generator"], meta["added"]), ("Essay 1.docx", "voice-profile", "2026-10-27"))
        self.assertFalse(fm.validate(meta))
        for canary in ("REFCANARY", "Professor", "ABC 101", "October 1, 2026", "COMMENTCANARY", "HEADERCANARY"):
            self.assertNotIn(canary, body)
        manifest = self.env.read("samples-text/MANIFEST.md")
        self.assertIn("## Kept (5)", manifest)
        self.assertIn("- `Essay 1 copy.docx` merged into `Essay 1.docx` (5-word shingle Jaccard 1.0)", manifest)
        self.assertIn("`Technical Report Peer Review Sheet for Classmate.docx`: peer review", manifest)
        stats = self.env.read("style-stats.md")
        self.assertIn("documents 5,", stats)
        self.assertEqual(fm.parse(stats)[0]["generator"], "voice-profile")

        profile = self.profile()
        self.assertEqual(fm.parse(profile)[0]["status"], "draft")
        self.assertIn("Direct and practical", profile)
        self.assertEqual(report.profile, f"wrote {PROFILE} (status: draft)")
        self.assertFalse((self.env.writing / "voice-profile.next.md").exists())
        self.assertFalse(any(v.kind == "bad-front-matter" for v in self.env.lib.check()))

    def test_broker_request_shape_and_prompt(self):
        self.env.add_corpus(6)
        self.build()
        req = self.env.broker.requests[0]
        self.assertEqual(req["path"], "/api/chat")
        body = req["body"]
        self.assertEqual((body["model"], body["stream"], body["think"]), ("assistant", False, False))
        self.assertEqual(body["options"]["num_ctx"], 16384)
        user = self.env.broker.user_prompt()
        self.assertEqual(len([ln for ln in user.split("\n") if ln.startswith("<excerpt ")]), 6)
        self.assertIn("<stats id=", user)
        self.assertNotIn("REFCANARY", user)

    def test_four_excerpts_minimum(self):
        for i in range(3):
            self.env.add(f"E{i}.docx", essay(300 + i, paras=1))
        report = self.build()
        self.assertIn("at least 4 are needed", report.profile)
        self.assertEqual(self.env.broker.calls, 0)
        self.assertFalse((self.env.writing / "voice-profile.md").exists())

    def test_rebuild_is_byte_identical_and_reports_unchanged(self):
        self.env.add_corpus(5)
        self.build()
        before = snapshot(self.env.writing)
        self.env.clock.now = self.env.clock.now.replace(day=28)
        report = self.build()
        self.assertEqual(snapshot(self.env.writing), before)
        self.assertEqual(report.written, [])
        self.assertEqual(report.profile, f"unchanged: {PROFILE}")

    def test_stale_output_is_archived_not_deleted(self):
        self.env.add_corpus(5)
        self.build(no_ai=True)
        (self.env.samples / "Essay 5.docx").rename(self.env.tmp / "gone.docx")
        report = self.build(no_ai=True)
        self.assertFalse((self.env.writing / "samples-text/essay-5.md").exists())
        archived = self.env.root / "90-archive/40-writing/samples-text/essay-5.md"
        self.assertTrue(archived.exists())
        self.assertEqual(report.archived, [("40-writing/samples-text/essay-5.md",
                                            "90-archive/40-writing/samples-text/essay-5.md")])
        self.assertIn("essay-5.md: its sample is no longer kept", self.env.read("samples-text/MANIFEST.md"))

    def test_archive_failure_writes_nothing_new(self):
        self.env.add_corpus(5)
        self.build(no_ai=True)
        (self.env.samples / "Essay 5.docx").unlink()
        self.env.add("Essay 6.docx", essay(999))
        archive = self.env.root / "90-archive"
        archive.chmod(0o555)
        self.addCleanup(archive.chmod, 0o755)
        before = snapshot(self.env.writing)
        with self.assertRaises(BuildError) as ctx:
            self.build(no_ai=True)
        self.assertIn("nothing new written", str(ctx.exception))
        self.assertEqual(snapshot(self.env.writing), before)

    def test_samples_are_only_read(self):
        self.env.add_corpus(5)
        before = snapshot(self.env.samples)
        self.build()
        self.assertEqual(snapshot(self.env.samples), before)

    def test_hand_made_files_are_never_replaced(self):
        self.env.add_corpus(5)
        (self.env.writing / "samples-text").mkdir()
        (self.env.writing / "samples-text/mine.md").write_text("Noah's own note\n")
        (self.env.writing / "style-stats.md").write_text("---\ntitle: mine\n---\nhand made\n")
        with self.assertRaises(BuildError):
            self.build(no_ai=True)
        self.assertEqual(self.env.read("style-stats.md"), "---\ntitle: mine\n---\nhand made\n")
        (self.env.writing / "style-stats.md").unlink()
        self.build(no_ai=True)
        self.assertEqual(self.env.read("samples-text/mine.md"), "Noah's own note\n")
        self.assertIn("mine.md: not written by voice-profile; not changed", self.env.read("samples-text/MANIFEST.md"))

    def test_case_collision_refused(self):
        self.env.add_corpus(5)
        (self.env.writing / "Style-Stats.md").write_text("x")
        with self.assertRaises(LibraryError):
            self.build(no_ai=True)

    def test_output_names_unique_and_safe(self):
        self.env.add("Essay.docx", essay(1))
        self.env.add("essay!.docx", essay(2))
        self.env.add("Manifest.docx", essay(3))
        self.env.add("CON.docx", essay(4))
        report = self.build(no_ai=True)
        outs = sorted(Path(k[1]).name for k in report.kept)
        self.assertTrue(all(re.fullmatch(r"[a-z0-9-]{1,60}\.md", n) for n in outs))
        self.assertEqual(sorted(p.name for p in (self.env.writing / "samples-text").iterdir()),
                         sorted(outs + ["MANIFEST.md"]))  # only per-sample texts + MANIFEST (W3 indexes the rest)
        self.assertEqual(outs, ["essay-2.md", "essay.md", "manifest-2.md", "sample-con.md"])

    def test_hostile_sample_is_excluded_and_run_continues(self):
        self.env.add_corpus(5)
        self.env.add("Bomb.docx", document(para("x")) + " " * (40 * 1024 * 1024))
        self.env.add("Doctype.docx", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>')
        self.env.add("Traversal.docx", essay(5), extra={"../../escape.xml": "x"})
        target = self.env.tmp / "outside.docx"
        target.write_bytes(b"x")
        (self.env.samples / "Link.docx").symlink_to(target)
        report = self.build(no_ai=True)
        reasons = dict(report.excluded)
        self.assertIn("zip bomb", reasons["Bomb.docx"])
        self.assertIn("DOCTYPE", reasons["Doctype.docx"])
        self.assertIn("unsafe entry name", reasons["Traversal.docx"])
        self.assertIn("symlink", reasons["Link.docx"])
        self.assertEqual(len(report.kept), 5)
        self.assertFalse((self.env.tmp / "escape.xml").exists())

    def test_cap_is_enforced(self):
        self.env.add_corpus(5)
        with self.assertRaises(CapExceeded):
            self.env.builder(cap_bytes=1000).build(no_ai=True)
        self.assertFalse((self.env.writing / "samples-text").exists())

    def test_sentinel_required(self):
        (self.env.root / ".ai-library-sentinel").unlink()
        with self.assertRaises(BuildError) as ctx:
            self.build()
        self.assertIn("sentinel", str(ctx.exception))

    def test_symlinked_samples_folder_refused(self):
        real = self.env.tmp / "elsewhere"
        real.mkdir()
        os.rmdir(self.env.samples)
        self.env.samples.symlink_to(real)
        with self.assertRaises(LibraryError):
            self.build(no_ai=True)

    def test_symlinked_output_refused(self):
        self.env.add_corpus(5)
        victim = self.env.tmp / "victim.md"
        victim.write_text("keep me")
        (self.env.writing / "style-stats.md").symlink_to(victim)
        with self.assertRaises(LibraryError):
            self.build(no_ai=True)
        self.assertEqual(victim.read_text(), "keep me")


class DraftTests(Base):
    def setUp(self):
        super().setUp()
        self.env.add_corpus(5)

    def test_invalid_draft_writes_no_profile(self):
        self.env.broker.replies = [GOOD.replace("## Tone", "## Mood")]
        report = self.build()
        self.assertIn("invalid: unexpected section", report.profile)
        self.assertFalse((self.env.writing / "voice-profile.md").exists())
        self.assertTrue((self.env.writing / "style-stats.md").exists())

    def test_truncated_prompt_refused(self):
        self.env.broker.replies = [{"content": GOOD, "prompt_eval_count": 16384 - 256}]
        report = self.build()
        self.assertIn("prompt truncated", report.profile)
        self.assertFalse((self.env.writing / "voice-profile.md").exists())
        self.env.broker.replies = [{"content": GOOD, "prompt_eval_count": 16384 - 257}]
        self.assertEqual(self.build().profile, f"wrote {PROFILE} (status: draft)")

    def test_length_cut_reply_refused(self):
        self.env.broker.replies = [{"content": GOOD, "done_reason": "length"}]
        report = self.build()
        self.assertIn("length limit", report.profile)
        self.assertFalse((self.env.writing / "voice-profile.md").exists())

    def test_broker_errors(self):
        self.env.broker.replies = [{"status": 500, "body": b"oops"}]
        self.assertIn("HTTP 500", self.build().profile)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        from voice_profile.broker import Broker
        report = self.env.builder(broker=Broker(f"http://127.0.0.1:{port}", "assistant", timeout=2, num_ctx=4096)).build()
        self.assertIn("unreachable", report.profile)
        self.assertFalse((self.env.writing / "voice-profile.md").exists())

    def test_approved_profile_is_never_overwritten(self):
        (self.env.writing / "voice-profile.md").write_text(APPROVED)
        before = os.stat(self.env.writing / "voice-profile.md")
        report = self.build()
        self.assertEqual(self.profile(), APPROVED)
        self.assertEqual(os.stat(self.env.writing / "voice-profile.md").st_ino, before.st_ino)
        self.assertEqual(report.profile, f"wrote {NEXT} (status: draft)")
        self.assertEqual(fm.parse(self.env.read("voice-profile.next.md"))[0]["status"], "draft")
        # a second run refreshes the .next draft, still never the approved file
        self.env.broker.replies = [GOOD.replace("Direct and practical", "Plain and practical")]
        self.build()
        self.assertEqual(self.profile(), APPROVED)
        self.assertIn("Plain and practical", self.env.read("voice-profile.next.md"))

    def test_unreadable_or_odd_status_is_protected(self):
        for existing in ("no header at all\n", "---\nstatus: [broken\n---\n", "---\nstatus: Approved \n---\n",
                         "---\nstatus: reviewed\n---\n", "---\ntitle: x\n---\n"):
            with self.subTest(existing=existing):
                (self.env.writing / "voice-profile.md").write_text(existing)
                self.build()
                self.assertEqual(self.profile(), existing)
                self.assertTrue((self.env.writing / "voice-profile.next.md").exists())
                (self.env.writing / "voice-profile.next.md").unlink()

    def test_draft_profile_is_replaced(self):
        self.build()
        self.env.broker.replies = [GOOD.replace("Direct and practical", "Plain and practical")]
        self.build()
        self.assertIn("Plain and practical", self.profile())
        self.assertFalse((self.env.writing / "voice-profile.next.md").exists())

    def test_both_protected_writes_nothing_and_skips_the_model(self):
        (self.env.writing / "voice-profile.md").write_text(APPROVED)
        (self.env.writing / "voice-profile.next.md").write_text(APPROVED)
        report = self.build()
        self.assertIn("both not drafts", report.profile)
        self.assertEqual(self.env.broker.calls, 0)
        self.assertEqual(self.env.read("voice-profile.next.md"), APPROVED)

    def test_approval_during_the_model_call_is_respected(self):
        def approve_then_answer():
            (self.env.writing / "voice-profile.md").write_text(APPROVED)
            return GOOD
        self.build()  # a draft exists
        self.env.broker.replies = [approve_then_answer]
        report = self.build()
        self.assertEqual(self.profile(), APPROVED)
        self.assertEqual(report.profile, f"wrote {NEXT} (status: draft)")

    def test_symlinked_profile_refused(self):
        victim = self.env.tmp / "victim.md"
        victim.write_text("keep me")
        (self.env.writing / "voice-profile.md").symlink_to(victim)
        with self.assertRaises(LibraryError):
            self.build()
        self.assertEqual(victim.read_text(), "keep me")


class DryRunAndCliTests(Base):
    def setUp(self):
        super().setUp()
        self.env.add_corpus(5)
        self.cfg_path = self.env.tmp / "config.json"
        self.cfg_path.write_text(json.dumps({k: v for k, v in self.env.cfg.items()}))

    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--config", str(self.cfg_path), *args])
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_writes_nothing(self):
        (self.env.writing / "voice-profile.md").write_text(APPROVED)
        before = snapshot(self.env.root)
        out = io.StringIO()
        report = self.env.builder().build(dry_run=True, out=out)
        self.assertEqual(snapshot(self.env.root), before)
        self.assertEqual(self.env.broker.calls, 1)
        self.assertIn(f"would write {NEXT}", report.profile)
        self.assertIn("status: draft", out.getvalue())
        self.assertTrue(report.written)
        code, stdout, _ = self.cli("build", "--dry-run", "--no-ai")
        self.assertEqual(code, 0)
        self.assertIn("dry run: kept 5", stdout)
        self.assertEqual(snapshot(self.env.root), before)

    def test_dry_run_archives_nothing(self):
        self.env.builder().build(no_ai=True)
        (self.env.samples / "Essay 5.docx").unlink()
        before = snapshot(self.env.root)
        report = self.env.builder().build(dry_run=True, no_ai=True)
        self.assertEqual(snapshot(self.env.root), before)
        self.assertEqual(report.archived, [("40-writing/samples-text/essay-5.md", "(would be archived)")])

    def test_no_ai_never_calls_the_broker(self):
        code, stdout, _ = self.cli("build", "--no-ai")
        self.assertEqual(code, 0)
        self.assertEqual(self.env.broker.calls, 0)
        self.assertIn("profile: skipped (--no-ai)", stdout)
        self.assertFalse((self.env.writing / "voice-profile.md").exists())

    def test_exit_codes(self):
        self.assertEqual(self.cli("build")[0], 0)
        self.env.broker.replies = ["not a profile"]
        self.env.broker.replies.append("not a profile")
        (self.env.writing / "voice-profile.md").unlink()
        self.assertEqual(self.cli("build")[0], 1)
        (self.env.root / ".ai-library-sentinel").unlink()
        code, _, err = self.cli("build")
        self.assertEqual(code, 2)
        self.assertIn("sentinel", err)
        self.cfg_path.write_text(json.dumps({"library_root": "/x", "broker_url": "http://h:1", "typo": 1}))
        self.assertEqual(self.cli("status")[0], 2)

    def test_status(self):
        self.cli("build")
        code, stdout, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("samples: 5 .docx file(s)", stdout)
        self.assertIn("samples-text: 5 cleaned text(s)", stdout)
        self.assertIn("voice-profile.md: status draft", stdout)
        self.assertIn("voice-profile.next.md: missing", stdout)
        self.assertIn("next draft goes to: voice-profile.md", stdout)
        self.assertIn("broker: reachable", stdout)


if __name__ == "__main__":
    unittest.main()
