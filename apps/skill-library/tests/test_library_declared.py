"""Declared tool outputs in the layout: the Payday tool's ``50-finance/payday-runs/`` (CONTRACTS
§12.27, H2 note) and the voice-profile outputs and Noah's samples in ``40-writing/`` plus their
archive under ``90-archive/40-writing/`` (§12.21, Reviewer K's note)."""
import os
import unittest

from library import Library, LibraryError
from library import frontmatter as fm
from library.core import LAYOUT_DIRS

from .test_library import TODAY, LibTestCase, clock, snapshot

RUNS = "50-finance/payday-runs"


def payday_text(**over):
    """The header exactly as payday_tool._render_file writes it, then a body."""
    meta = {"title": "Payday run 2026-10-28 (1st check)", "source": "payday-tool", "added": TODAY,
            "tags": ["finance", "payday"], "summary": "Paycheck $1,000.00; bills $412.50.", "related": []}
    meta.update(over)
    meta = {k: v for k, v in meta.items() if v is not None}
    return fm.dump(meta) + "\n# Payday run\n\nSaved by the Payday tool.\n"


def voice_text(**over):
    """A voice-profile header (build.py _header / validate.render), then a body."""
    meta = {"title": "Writing sample: Essay 1", "source": "Essay 1.docx", "added": TODAY,
            "tags": ["writing-sample", "voice-profile"], "summary": "Noah's own writing.",
            "related": ["40-writing/samples-text/MANIFEST.md"], "generator": "voice-profile"}
    meta.update(over)
    meta = {k: v for k, v in meta.items() if v is not None}
    return fm.dump(meta) + "\nbody\n"


class DeclaredCase(LibTestCase):
    def write(self, rel, data):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))

    def found(self):
        before = snapshot(self.root)
        got = {(v.path, v.kind) for v in self.lib.check()}
        self.assertEqual(snapshot(self.root), before, "check never changes anything")
        return got


class PaydayRunsTest(DeclaredCase):
    def test_declared_in_the_layout_and_created_by_init(self):
        self.assertIn(RUNS, LAYOUT_DIRS)
        self.assertTrue((self.root / RUNS).is_dir())
        fresh = self.base / "fresh"
        _, created = Library.init(fresh, clock=clock)
        self.assertIn(f"{RUNS}/", created)
        self.assertEqual(self.lib.check(), [])

    def test_accepts_exactly_the_tool_names(self):
        for name in ("2026-10-28_1st.md", "2026-10-15_15th.md", "2026-10-28_1st-2.md",
                     "2026-10-28_1st-9.md", "2026-10-28_1st-10.md", "2026-10-28_15th-99.md",
                     "2024-02-29_1st.md"):
            self.write(f"{RUNS}/{name}", payday_text())
        self.assertEqual(self.found(), set())

    def test_reports_every_other_name(self):
        bad = ("2026-10-28_2nd.md",        # not a check
               "2026-10-28_1st_2.md",      # underscore re-run suffix: the tool writes -2
               "2026-10-28_1st-1.md",      # the tool never writes -1
               "2026-10-28_1st-100.md",    # past the tool's -99
               "2026-10-28_1st-02.md",
               "2026-02-30_1st.md",        # not a real date
               "2025-02-29_15th.md",
               "2026-10-28_october.md",    # a valid general item name, but not a payday run
               "2026-10-28_1ST.md",
               "2026-10-28-1st.md",
               "26-10-28_1st.md",
               "2026-10-28_1st.md.md",
               "README.md")
        for name in bad:
            self.write(f"{RUNS}/{name}", payday_text())
        self.write(f"{RUNS}/2026-10-28_1st.txt", b"x")
        self.write(f"{RUNS}/2026-10-28_1st.pdf", b"%PDF")
        self.write(f"{RUNS}/2026-10-28_1st.pdf.md", payday_text())  # a sidecar is not a run either
        expected = {(f"{RUNS}/{n}", "bad-name") for n in bad + (
            "2026-10-28_1st.txt", "2026-10-28_1st.pdf", "2026-10-28_1st.pdf.md")}
        self.assertEqual(self.found(), expected)

    def test_header_must_be_the_library_header_from_the_payday_tool(self):
        self.write(f"{RUNS}/2026-10-01_1st.md", payday_text(source="Noah"))
        self.write(f"{RUNS}/2026-10-02_1st.md", payday_text(source=None))
        self.write(f"{RUNS}/2026-10-03_1st.md", payday_text(related=None))
        self.write(f"{RUNS}/2026-10-04_1st.md", "# no header\n")
        self.write(f"{RUNS}/2026-10-05_1st.md", payday_text(added="28/10/2026"))
        self.write(f"{RUNS}/2026-10-06_1st.md", payday_text(source="PAYDAY-TOOL"))
        self.write(f"{RUNS}/2026-10-07_1st.md", payday_text(source=["payday-tool"]))
        self.write(f"{RUNS}/2026-10-08_1st.md", "---\ntitle: a: b\n---\n")
        self.write(f"{RUNS}/Bad Name.md", payday_text(source="x"))  # name and header both reported
        self.assertEqual(self.found(), {
            (f"{RUNS}/2026-10-01_1st.md", "bad-field"),
            (f"{RUNS}/2026-10-02_1st.md", "missing-field"),
            (f"{RUNS}/2026-10-03_1st.md", "missing-field"),
            (f"{RUNS}/2026-10-04_1st.md", "missing-front-matter"),
            (f"{RUNS}/2026-10-05_1st.md", "bad-field"),
            (f"{RUNS}/2026-10-06_1st.md", "bad-field"),
            (f"{RUNS}/2026-10-07_1st.md", "bad-field"),
            (f"{RUNS}/2026-10-08_1st.md", "bad-front-matter"),
            (f"{RUNS}/Bad Name.md", "bad-name"),
            (f"{RUNS}/Bad Name.md", "bad-field"),
        })
        messages = [v.message for v in self.lib.check() if v.path == f"{RUNS}/2026-10-02_1st.md"]
        self.assertEqual(messages, ["missing 'source'"], "a missing source is reported once")
        (v,) = [v for v in self.lib.check() if v.path == f"{RUNS}/2026-10-01_1st.md"]
        self.assertIn("'source' must be 'payday-tool' here", v.message)

    def test_folders_inside_and_other_finance_folders_are_misplaced(self):
        self.write(f"{RUNS}/2026/2026-10-28_1st.md", payday_text())
        self.write("50-finance/payday/2026-10-28_1st.md", payday_text())
        self.write("50-finance/2026-10-28_1st.md", payday_text())
        self.write(f"50-finance/log/{TODAY}_october.md", payday_text(source="Noah"))  # log/ unchanged
        self.assertEqual(self.found(), {
            (f"{RUNS}/2026", "misplaced"),
            (f"{RUNS}/2026/2026-10-28_1st.md", "misplaced"),
            ("50-finance/payday", "misplaced"),
            ("50-finance/payday/2026-10-28_1st.md", "misplaced"),
            ("50-finance/2026-10-28_1st.md", "misplaced"),
        })

    def test_payday_names_stay_payday_only(self):
        # The rules belong to the folder: elsewhere a run name is just an item name.
        self.write("50-finance/log/2026-10-28_1st.md", payday_text(source="Noah"))
        self.write("10-sources/money/2026-10-28_1st_2.md", payday_text())
        self.assertEqual(self.found(), {("10-sources/money/2026-10-28_1st_2.md", "bad-name")})

    def test_open_does_not_require_it_but_check_and_init_do(self):
        os.rmdir(self.root / RUNS)
        Library.open(self.root)  # a live library from before §12.27 still opens
        self.assertEqual(self.found(), {(RUNS, "missing-layout")})
        _, created = Library.init(self.root, clock=clock)
        self.assertEqual(created, [f"{RUNS}/"])
        self.assertEqual(self.lib.check(), [])

    def test_open_still_requires_the_older_layout(self):
        os.rmdir(self.root / "50-finance" / "log")
        with self.assertRaises(LibraryError):
            Library.open(self.root)

    def test_a_file_in_its_place_is_reported(self):
        os.rmdir(self.root / RUNS)
        self.write(RUNS, payday_text())
        Library.open(self.root)
        self.assertIn((RUNS, "missing-layout"), self.found())

    def test_the_folder_cannot_be_archived_but_a_run_can(self):
        with self.assertRaises(LibraryError):
            self.lib.archive(RUNS)
        self.write(f"{RUNS}/2026-10-28_1st.md", payday_text())
        self.lib.archive(f"{RUNS}/2026-10-28_1st.md")
        self.assertTrue((self.root / "90-archive" / RUNS / "2026-10-28_1st.md").is_file())
        self.assertEqual(self.lib.check(), [])

    def test_indexed_with_its_title(self):
        self.write(f"{RUNS}/2026-10-28_1st.md", payday_text())
        self.lib.index()
        self.assertIn("- [Payday run 2026-10-28 (1st check)](50-finance/payday-runs/2026-10-28_1st.md) — "
                      f"{TODAY}", self.read("INDEX.md"))


class WritingOutputsTest(DeclaredCase):
    def put_outputs(self, prefix="40-writing"):
        self.write(f"{prefix}/samples-text/essay-1.md", voice_text())
        self.write(f"{prefix}/samples-text/sample-con.md", voice_text())
        self.write(f"{prefix}/samples-text/{'a' * 80}.md", voice_text())
        self.write(f"{prefix}/samples-text/MANIFEST.md", voice_text(title="Writing samples manifest"))
        self.write(f"{prefix}/style-stats.md", voice_text(title="Style statistics"))
        self.write(f"{prefix}/voice-profile.md", voice_text(title="Noah's writing voice", status="approved"))
        self.write(f"{prefix}/voice-profile.next.md", voice_text(title="Noah's writing voice", status="draft"))
        self.write(f"{prefix}/include.txt", "Technical Report Peer Review Sheet for Sam.docx\n")
        self.write(f"{prefix}/samples/Essay 1.docx", b"PK\x03\x04")
        self.write(f"{prefix}/samples/XYZ 210 - Final Paper (v2).DOCX", b"PK\x03\x04")
        self.write(f"{prefix}/samples/~$say 1.docx", b"")
        self.write(f"{prefix}/samples/{TODAY}_essay.docx", b"PK\x03\x04")  # dated: no sidecar needed either

    def test_clean_outputs_have_no_violations(self):
        self.put_outputs()
        self.write(f"40-writing/samples/{TODAY}_essay.md", voice_text(generator=None))  # unchanged rules
        self.write(f"40-writing/drafts/{TODAY}_draft.md", voice_text(generator=None))
        self.assertEqual(self.found(), set())

    def test_samples_text_names(self):
        bad = ("Essay 1.md", "Essay-1.md", "essay_1.md", "-essay.md", "essay--1.md", "con.md",
               "manifest.md", "MANIFEST-2.md", "essay.MD", f"{'a' * 81}.md",
               f"{TODAY}_essay.md")
        for name in bad:
            self.write(f"40-writing/samples-text/{name}", voice_text())
        self.write("40-writing/samples-text/notes.txt", b"x")
        self.write("40-writing/samples-text/Essay 1.docx", b"PK")
        self.write("40-writing/samples-text/MANIFEST.txt", b"x")
        expected = {(f"40-writing/samples-text/{n}", "bad-name")
                    for n in bad + ("notes.txt", "Essay 1.docx", "MANIFEST.txt")}
        self.assertEqual(self.found(), expected)
        (v,) = [v for v in self.lib.check() if v.path == "40-writing/samples-text/notes.txt"]
        self.assertIn("<slug>.md", v.message)

    def test_samples_text_needs_the_generator(self):
        self.write("40-writing/samples-text/mine.md", voice_text(generator=None))
        self.write("40-writing/samples-text/other.md", voice_text(generator="noah"))
        self.write("40-writing/samples-text/MANIFEST.md", voice_text(generator=None))
        self.write("40-writing/samples-text/bare.md", "no header\n")
        self.write("40-writing/samples-text/half.md", voice_text(summary=None))
        self.assertEqual(self.found(), {
            ("40-writing/samples-text/mine.md", "missing-field"),
            ("40-writing/samples-text/other.md", "bad-field"),
            ("40-writing/samples-text/MANIFEST.md", "missing-field"),
            ("40-writing/samples-text/bare.md", "missing-front-matter"),
            ("40-writing/samples-text/half.md", "missing-field"),
        })
        (v,) = [v for v in self.lib.check() if v.path == "40-writing/samples-text/mine.md"]
        self.assertIn("'generator'", v.message)

    def test_top_files_need_the_header(self):
        self.write("40-writing/style-stats.md", "stats\n")
        self.write("40-writing/voice-profile.md", voice_text(title=""))
        self.write("40-writing/voice-profile.next.md", voice_text(added="soon"))
        self.write("40-writing/include.txt", b"")  # plain text, any content (even none)
        self.assertEqual(self.found(), {
            ("40-writing/style-stats.md", "missing-front-matter"),
            ("40-writing/voice-profile.md", "bad-field"),
            ("40-writing/voice-profile.next.md", "bad-field"),
        })

    def test_anything_else_in_writing_is_reported(self):
        self.write("40-writing/notes.md", voice_text())
        self.write("40-writing/style-stats-2.md", voice_text())       # numbered only in the archive
        self.write("40-writing/voice-profile.MD", voice_text())
        self.write("40-writing/Voice-Profile.next.md", voice_text())
        self.write("40-writing/include.md", voice_text())
        self.write("40-writing/drafts/include.txt", b"x")              # the names belong to 40-writing/
        self.write("40-writing/drafts/style-stats.md", voice_text())
        self.write("40-writing/samples-text/old/essay.md", voice_text())
        self.write("40-writing/samples/Essay 1.pdf", b"%PDF")          # only .docx keeps Noah's names
        self.write("40-writing/samples/Essay 1.docx.md", voice_text())
        self.write("40-writing/samples/sub/Essay 2.docx", b"PK")
        self.write("40-writing/other/x.docx", b"PK")
        self.assertEqual(self.found(), {
            ("40-writing/notes.md", "misplaced"),
            ("40-writing/notes.md", "bad-name"),
            ("40-writing/style-stats-2.md", "misplaced"),
            ("40-writing/style-stats-2.md", "bad-name"),
            ("40-writing/voice-profile.MD", "misplaced"),
            ("40-writing/voice-profile.MD", "bad-name"),
            ("40-writing/Voice-Profile.next.md", "misplaced"),
            ("40-writing/Voice-Profile.next.md", "bad-name"),
            ("40-writing/include.md", "misplaced"),
            ("40-writing/include.md", "bad-name"),
            ("40-writing/drafts/include.txt", "bad-name"),
            ("40-writing/drafts/style-stats.md", "bad-name"),
            ("40-writing/samples-text/old", "misplaced"),
            ("40-writing/samples-text/old/essay.md", "misplaced"),
            ("40-writing/samples-text/old/essay.md", "bad-name"),
            ("40-writing/samples/Essay 1.pdf", "bad-name"),
            ("40-writing/samples/Essay 1.docx.md", "bad-name"),
            ("40-writing/samples/sub", "misplaced"),
            ("40-writing/samples/sub/Essay 2.docx", "misplaced"),
            ("40-writing/samples/sub/Essay 2.docx", "bad-name"),
            ("40-writing/other", "misplaced"),
            ("40-writing/other/x.docx", "misplaced"),
            ("40-writing/other/x.docx", "bad-name"),
        })

    def test_index_notes_no_missing_header_for_accepted_plain_files(self):
        self.put_outputs()
        self.write(f"40-writing/drafts/{TODAY}_x.pdf", b"%PDF")  # no sidecar: still noted
        self.lib.index()
        index = self.read("INDEX.md")
        self.assertIn("- [Essay 1.docx](40-writing/samples/Essay%201.docx)\n", index)
        self.assertIn("- [include.txt](40-writing/include.txt)\n", index)
        self.assertIn(f"- [{TODAY}_x.pdf](40-writing/drafts/{TODAY}_x.pdf) — (no valid front-matter; run check)",
                      index)


class WritingArchiveTest(DeclaredCase):
    def test_archived_outputs_are_accepted(self):
        WritingOutputsTest.put_outputs(self, "90-archive/40-writing")
        self.assertEqual(self.found(), set())

    def test_numbered_archive_names_are_accepted(self):
        a = "90-archive/40-writing"
        self.write(f"{a}/samples-text/essay-5-2.md", voice_text())
        self.write(f"{a}/samples-text/MANIFEST-2.md", voice_text())
        self.write(f"{a}/samples-text/MANIFEST-12.md", voice_text())
        self.write(f"{a}/style-stats-2.md", voice_text())
        self.write(f"{a}/voice-profile-3.md", voice_text())
        self.write(f"{a}/voice-profile.next-2.md", voice_text())
        self.write(f"{a}/include-2.txt", b"x")
        self.write(f"{a}/samples/Essay 1-2.docx", b"PK")
        self.assertEqual(self.found(), set())

    def test_archived_entries_keep_their_rules(self):
        a = "90-archive/40-writing"
        self.write(f"{a}/samples-text/essay.md", voice_text(generator=None))
        self.write(f"{a}/samples-text/MANIFEST-2.md", voice_text(generator="x"))
        self.write(f"{a}/style-stats-2.md", "no header\n")
        self.write(f"{a}/include-1.txt", b"x")             # an archive move never writes -1
        self.write(f"{a}/include-2.md", b"x")
        self.write(f"{a}/samples-text/Bad Name.md", voice_text())
        self.write(f"{a}/samples/Essay.pdf", b"%PDF")
        self.write(f"{a}/notes.md", voice_text())          # the archive keeps any path: item rules only
        self.assertEqual(self.found(), {
            (f"{a}/samples-text/essay.md", "missing-field"),
            (f"{a}/samples-text/MANIFEST-2.md", "bad-field"),
            (f"{a}/style-stats-2.md", "missing-front-matter"),
            (f"{a}/include-1.txt", "bad-name"),
            (f"{a}/include-2.md", "bad-name"),
            (f"{a}/include-2.md", "missing-front-matter"),
            (f"{a}/samples-text/Bad Name.md", "bad-name"),
            (f"{a}/samples/Essay.pdf", "bad-name"),
            (f"{a}/notes.md", "bad-name"),
        })

    def test_only_the_writing_archive_is_declared(self):
        self.write("90-archive/20-notes/t/style-stats.md", voice_text())
        self.write("90-archive/samples-text/essay.md", voice_text())
        self.write("90-archive/include.txt", b"x")
        self.write("90-archive/20-notes/style-stats.md", voice_text())
        self.write("90-archive/10-sources/samples-text/essay.md", voice_text())
        self.write("90-archive/40-writing-old/include.txt", b"x")
        self.assertEqual(self.found(), {
            ("90-archive/20-notes/style-stats.md", "bad-name"),
            ("90-archive/10-sources/samples-text/essay.md", "bad-name"),
            ("90-archive/40-writing-old/include.txt", "bad-name"),
            ("90-archive/20-notes/t/style-stats.md", "bad-name"),
            ("90-archive/samples-text/essay.md", "bad-name"),
            ("90-archive/include.txt", "bad-name"),
        })

    def test_library_archive_moves_pass_check(self):
        self.write("40-writing/samples-text/essay-5.md", voice_text())
        self.write("40-writing/samples-text/MANIFEST.md", voice_text())
        self.write("40-writing/style-stats.md", voice_text())
        self.write("40-writing/voice-profile.next.md", voice_text())
        self.write("40-writing/include.txt", b"x")
        self.write("40-writing/samples/Essay 1.docx", b"PK")
        for _ in range(2):  # the second round lands on -2 names
            for rel in ("40-writing/samples-text/essay-5.md", "40-writing/samples-text/MANIFEST.md",
                        "40-writing/style-stats.md", "40-writing/voice-profile.next.md",
                        "40-writing/include.txt", "40-writing/samples/Essay 1.docx"):
                if not (self.root / rel).exists():
                    self.write(rel, voice_text() if rel.endswith(".md") else b"x")
                self.lib.archive(rel)
        self.assertIn("voice-profile.next-2.md", self.ls("90-archive/40-writing"))
        self.assertIn("Essay 1-2.docx", self.ls("90-archive/40-writing/samples"))
        self.assertEqual(self.lib.check(), [])


if __name__ == "__main__":
    unittest.main()
