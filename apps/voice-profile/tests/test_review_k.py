"""Reviewer K round 1 (V-1 .. V-8, source cap): bounded work and memory on hostile samples, more of
other people's text kept out, peer-review variants, odd header line breaks, the old draft archived,
macro content types, the two surviving mutants, the approved-profile note."""
import io
import random
import string
import struct
import subprocess
import sys
import tempfile
import time
import tracemalloc
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from library import frontmatter as fm
from voice_profile import build as buildmod
from voice_profile.build import NEXT, PROFILE, BuildError
from voice_profile.clean import Cleaned, Sample, clean_document, dedupe_paragraphs, is_peer_review, remove_quotes
from voice_profile.docx import Caps, DocxRefused, TooMuchText, parse_document, read_docx
from voice_profile.stats import sentences
from voice_profile.validate import NOTE

from .fakes import GOOD, Env, W, document, docx_bytes, essay, para, prose

APPROVED = "---\ntitle: Voice profile\nstatus: approved\n---\n# Voice profile\n\nNoah's own edits.\n"
HERE = Path(__file__).resolve().parent
APP = HERE.parent
LIB = APP.parent / "skill-library"


def random_words(n_chars: int, seed: int = 1) -> str:
    """Incompressible-ish text (random letters in short words): no zip-bomb ratio, real size."""
    rng = random.Random(seed)
    out, size = [], 0
    while size < n_chars:
        w = "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(2, 9)))
        out.append(w)
        size += len(w) + 1
    return " ".join(out)[:n_chars]


def dense_zip(entries: int) -> bytes:
    """A zip of ``entries`` tiny entries (plus word/document.xml), built fast by hand."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("word/document.xml", document(para("x")))
        for i in range(entries):
            zf.writestr(f"{i:x}", b"")
    return buf.getvalue()


def max_rss_mib(code: str) -> float:
    """Run ``code`` in a fresh interpreter (the app and library on sys.path) and return its peak
    resident set size in MiB (VmHWM: ru_maxrss would include the forking test process)."""
    prog = (f"import sys, resource; sys.path[:0] = [{str(APP)!r}, {str(LIB)!r}]\n{code}\n"
            "print([l for l in open('/proc/self/status') if l.startswith('VmHWM:')][0].split()[1])")
    out = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True, timeout=120, check=True)
    return int(out.stdout.strip().splitlines()[-1]) / 1024


class BoundedWorkTests(unittest.TestCase):
    """V-1: no single sample that passes the caps can stall the build or exhaust MemoryMax=256M."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, data: bytes, name: str = "s.docx") -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_sentence_splitter_is_linear(self):
        for text in ("A. B. C. " * 15000, "." * 120_000 + " A", "." * 20_000 + "x", "! " * 10_000 + "x",
                     "x" * 120_000 + ". A", "Dr. " * 30000,
                     "Word. " * 20000, " " * 120_000 + "."):
            with self.subTest(text=text[:12]):
                t0 = time.perf_counter()
                sentences(text)
                self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(sentences("Dr. A. B. Smith wrote it. Then e.g. this went. Done.")), 3)

    def test_paragraph_dedupe_with_common_shingles_is_bounded(self):
        common = "the same words open every single paragraph in this long document "
        docs = [Cleaned(f"D{d}.docx", 0, [common + " ".join(f"w{d}x{i}y{j}" for j in range(8)) + "."
                                          for i in range(4000)]) for d in range(2)]
        t0 = time.perf_counter()
        dedupe_paragraphs(docs, 0.6)
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(sum(d.duplicate_paras for d in docs), 0)

    def test_quote_scan_is_linear(self):
        t0 = time.perf_counter()
        remove_quotes("“" * 20000 + "”")
        remove_quotes("‘" * 20000)
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_huge_paragraph_refused_with_low_memory(self):
        path = self.write(docx_bytes(document(para(random_words(5_000_000)))))
        self.assertLess(path.stat().st_size, 5 * 1024 * 1024)
        tracemalloc.start()
        try:
            t0 = time.perf_counter()
            with self.assertRaises(TooMuchText) as ctx:
                read_docx(path, Caps())
            elapsed = time.perf_counter() - t0
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertIn("a paragraph over 20000 characters", str(ctx.exception))
        self.assertLess(peak, 32 * 1024 * 1024)
        self.assertLess(elapsed, 5.0)

    def test_text_caps(self):
        many = document("".join(para(f"Paragraph {i}.") for i in range(5001)))
        with self.assertRaises(TooMuchText) as ctx:
            parse_document(many.encode())
        self.assertIn("more than 5000 paragraphs", str(ctx.exception))
        big = document("".join(para(random_words(19_000, i)) for i in range(60)))
        with self.assertRaises(TooMuchText) as ctx:
            parse_document(big.encode())
        self.assertIn("over 1000000 characters", str(ctx.exception))
        empty_paras = document("<w:p/>" * 6000 + para("Only this."))
        self.assertEqual([p.text for p in parse_document(empty_paras.encode())], ["Only this."])

    def test_dense_zip_refused_from_its_end_record(self):
        data = dense_zip(60_000)
        self.assertLess(len(data), 5 * 1024 * 1024)
        path = self.write(data)
        tracemalloc.start()
        try:
            t0 = time.perf_counter()
            with self.assertRaises(DocxRefused) as ctx:
                read_docx(path, Caps())
            elapsed = time.perf_counter() - t0
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertIn("more than 2000 zip entries", str(ctx.exception))
        self.assertLess(peak, 2 * len(data) + 4 * 1024 * 1024)   # the file itself, nothing per entry
        self.assertLess(elapsed, 1.0)

    def test_lying_end_record_count_refused(self):
        data = bytearray(dense_zip(3000))
        pos = data.rfind(b"PK\x05\x06")
        data[pos + 8:pos + 12] = struct.pack("<HH", 10, 10)          # claims 10 entries
        # The hand walk refuses it before zipfile builds a ZipInfo per entry.
        with mock.patch("voice_profile.docx.zipfile.ZipFile", side_effect=AssertionError("zipfile reached")), \
                self.assertRaises(DocxRefused) as ctx:
            read_docx(self.write(bytes(data)), Caps())
        self.assertIn("more than 2000 zip entries", str(ctx.exception))
        data = bytearray(dense_zip(5))
        pos = data.rfind(b"PK\x05\x06")
        data[pos + 8:pos + 12] = struct.pack("<HH", 3, 3)            # fewer than the records
        with self.assertRaises(DocxRefused) as ctx:
            read_docx(self.write(bytes(data)), Caps())
        self.assertIn("corrupt zip central directory", str(ctx.exception))

    def test_end_record_count_is_checked_first(self):
        data = bytearray(docx_bytes(document(para("x"))))
        pos = data.rfind(b"PK\x05\x06")
        data[pos + 8:pos + 12] = struct.pack("<HH", 50_000, 50_000)
        with self.assertRaises(DocxRefused) as ctx:
            read_docx(self.write(bytes(data)), Caps())
        self.assertIn("more than 2000 zip entries", str(ctx.exception))

    def test_zipfile_level_entry_cap_backs_up_the_walk(self):
        with mock.patch("voice_profile.docx.check_directory"), self.assertRaises(DocxRefused) as ctx:
            read_docx(self.write(dense_zip(3000)), Caps())
        self.assertIn("more than 2000 zip entries", str(ctx.exception))

    def test_zip64_and_multidisk_refused(self):
        data = bytearray(docx_bytes(document(para("x"))))
        pos = data.rfind(b"PK\x05\x06")
        zip64 = bytes(data[:pos + 8]) + b"\xff\xff\xff\xff" + bytes(data[pos + 12:])
        with self.assertRaises(DocxRefused) as ctx:
            read_docx(self.write(zip64), Caps())
        self.assertIn("ZIP64", str(ctx.exception))
        multi = bytes(data[:pos + 4]) + struct.pack("<H", 1) + bytes(data[pos + 6:])
        with self.assertRaises(DocxRefused) as ctx:
            read_docx(self.write(multi), Caps())
        self.assertIn("multi-disk", str(ctx.exception))

    def test_peak_rss_of_hostile_inputs_stays_far_under_256m(self):
        big = self.write(docx_bytes(document(para(random_words(5_000_000)))), "big.docx")
        dense = self.write(dense_zip(60_000), "dense.docx")
        many = self.write(docx_bytes(document("".join(para(random_words(19_000, i)) for i in range(52)))),
                          "many.docx")
        for path in (big, dense, many):
            with self.subTest(path=path.name):
                rss = max_rss_mib(
                    "from voice_profile.docx import read_docx, Caps, DocxRefused\n"
                    f"try:\n    read_docx({str(path)!r}, Caps())\nexcept DocxRefused:\n    pass")
                self.assertLess(rss, 128)

    def test_largest_accepted_sample_end_to_end_is_bounded(self):
        # A sample just under every text cap goes through clean, dedupe and stats in bounded time.
        text = [random_words(19_000, i) for i in range(52)]       # ~990 K characters
        xml = document("".join(para(t) for t in text))
        tracemalloc.start()
        try:
            t0 = time.perf_counter()
            paras = parse_document(xml.encode())
            doc = clean_document(Sample("Big.docx", 0, paras))
            dedupe_paragraphs([doc], 0.6)
            from voice_profile.stats import compute
            compute([doc.paragraphs])
            elapsed = time.perf_counter() - t0
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(elapsed, 20.0)
        self.assertLess(peak, 200 * 1024 * 1024)


class BuildBoundsTests(unittest.TestCase):
    def test_oversized_sample_is_excluded_with_a_reason_and_the_build_goes_on(self):
        env = Env(self)
        env.add_corpus(5)
        env.add("Huge.docx", document(para(random_words(30_000))))
        report = env.builder().build(no_ai=True)
        reasons = dict(report.excluded)
        self.assertIn("too much text", reasons["Huge.docx"])
        self.assertEqual(len(report.kept), 5)
        self.assertIn("`Huge.docx`: refused: too much text", env.read("samples-text/MANIFEST.md"))

    def test_corpus_cap(self):
        env = Env(self)
        env.add_corpus(5)
        with mock.patch.object(buildmod, "MAX_CORPUS_CHARS", 4000):
            report = env.builder().build(no_ai=True)
        self.assertTrue(any("together hold over 4000 characters" in why for _, why in report.excluded))
        self.assertLess(len(report.kept), 5)

    def test_source_capped_at_200(self):
        env = Env(self)
        name = "A" * 230 + ".docx"
        env.add(name, essay(77))
        report = env.builder().build(no_ai=True)
        meta = fm.parse((env.root / report.kept[0][1]).read_text())[0]
        self.assertEqual(len(meta["source"]), 200)
        self.assertTrue(meta["source"].endswith("…"))
        self.assertLessEqual(len(meta["title"]), 200)


class OtherPeoplesTextTests(unittest.TestCase):
    """V-2."""

    def body(self, xml_body: str, styles: str | None = None) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "s.docx"
        extra = {"word/styles.xml": styles} if styles else None
        path.write_bytes(docx_bytes(document(xml_body), extra=extra))
        return clean_document(Sample("s.docx", 0, read_docx(path, Caps()))).body()

    def test_single_quoted_multi_sentence_removed_apostrophes_kept(self):
        text, n = remove_quotes("She said ‘I don’t know. It’s late now.’ and left. I didn’t.")
        self.assertEqual((text, n), ("She said [quotation omitted] and left. I didn’t.", 1))
        self.assertEqual(remove_quotes("It’s Noah’s ‘short’ note.")[1], 0)

    def test_mismatched_and_unclosed_quotes(self):
        self.assertEqual(remove_quotes('A “First one. Second one." b')[0], "A [quotation omitted] b")
        self.assertEqual(remove_quotes('A "First one. Second one.” b')[0], "A [quotation omitted] b")
        self.assertEqual(remove_quotes("A “First one. Second one never closes")[0], "A [quotation omitted]")
        self.assertEqual(remove_quotes('He is 5\'10" tall. Then more text. And more.')[1], 0)

    def test_tables_dropped(self):
        table = (f"<w:tbl><w:tr><w:tc>{para('TABLECANARY rubric cell with words.')}</w:tc>"
                 f"<w:tc><w:tbl><w:tr><w:tc>{para('NESTEDCANARY')}</w:tc></w:tr></w:tbl></w:tc></w:tr></w:tbl>")
        body = self.body(para(prose(1, 1)[0]) + table + para(prose(2, 1)[0]))
        self.assertNotIn("CANARY", body)
        self.assertIn(prose(2, 1)[0][:30], body)

    STYLES = (f'<?xml version="1.0"?><w:styles xmlns:w="{W}">'
              '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
              '<w:pPr><w:ind w:left="{normal}"/></w:pPr></w:style>'
              '<w:style w:type="paragraph" w:styleId="Style1"><w:name w:val="Block Quote"/>'
              '<w:basedOn w:val="Normal"/></w:style>'
              '<w:style w:type="paragraph" w:styleId="Indented"><w:name w:val="My Indent"/>'
              '<w:basedOn w:val="Normal"/><w:pPr><w:ind w:left="{indented}"/></w:pPr></w:style>'
              '<w:style w:type="paragraph" w:styleId="Child"><w:name w:val="Child"/>'
              '<w:basedOn w:val="Style1"/></w:style>'
              '<w:style w:type="character" w:styleId="Quote"><w:name w:val="Quote Char"/></w:style>'
              '</w:styles>')

    def test_quote_style_named_only_in_styles_xml(self):
        styles = self.STYLES.format(normal=0, indented=1440)
        for sid in ("Style1", "Child", "Indented"):
            with self.subTest(sid=sid):
                body = self.body(para(prose(3, 1)[0]) + para("STYLECANARY " + prose(4, 1)[0], style=sid), styles)
                self.assertNotIn("STYLECANARY", body)

    def test_default_style_indent_does_not_drop_everything(self):
        styles = self.STYLES.format(normal=720, indented=720)
        body = self.body(para(prose(3, 1)[0]) + para("KEPTMARK " + prose(4, 1)[0], style="Indented"), styles)
        self.assertIn("KEPTMARK", body)
        self.assertIn(prose(3, 1)[0][:30], body)

    def test_direct_indent_overrides_style(self):
        styles = self.STYLES.format(normal=0, indented=1440)
        body = self.body(para("KEPTMARK " + prose(4, 1)[0], style="Indented", left=0, first_line=720), styles)
        self.assertIn("KEPTMARK", body)

    def test_first_line_only_still_inherits_the_style_left_indent(self):
        styles = self.STYLES.format(normal=0, indented=1440)
        body = self.body(para(prose(3, 1)[0]) + para("STYLECANARY " + prose(4, 1)[0], style="Indented",
                                                      first_line=0), styles)
        self.assertNotIn("STYLECANARY", body)

    def test_hostile_styles_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "s.docx"
        path.write_bytes(docx_bytes(document(para("x")),
                                    extra={"word/styles.xml": '<!DOCTYPE s [<!ENTITY a "b">]><s>&a;</s>'}))
        with self.assertRaises(DocxRefused):
            read_docx(path, Caps())


class PeerReviewVariantTests(unittest.TestCase):
    """V-3."""

    def check(self, name: str, heading: str | None = None) -> bool:
        body = (para(heading, style="Heading1") if heading else "") + "".join(para(p) for p in prose(9, 3))
        return is_peer_review(Sample(name, 0, parse_document(document(body).encode())))

    def test_file_names(self):
        for name in ("Peer-Evaluation.docx", "peer_feedback 2.docx", "Peer Critique.docx", "PEER ASSESSMENT.docx",
                     "Workshop Feedback.docx", "Review of Sam's Draft.docx", "review of sam draft.docx",
                     "Review of Sam’s rough draft.docx", "Ｐｅｅｒ Ｒｅｖｉｅｗ.docx", "Peer–Review.docx",
                     "peer‑review.docx"):
            with self.subTest(name=name):
                self.assertTrue(self.check(name))
        for name in ("Review of the Literature.docx", "Essay 2.docx", "Peerless Reviewer.docx"):
            with self.subTest(name=name):
                self.assertFalse(self.check(name))

    def test_headings(self):
        for heading in ("Peer Feedback Form", "Workshop feedback", "Review of Jordan's Draft", "PEER—EVALUATION"):
            with self.subTest(heading=heading):
                self.assertTrue(self.check("Report.docx", heading))
        self.assertFalse(self.check("Report.docx", "Literature Review"))


class ProfileTests(unittest.TestCase):
    """V-4, V-5, V-7 (re-check), V-8."""

    def setUp(self):
        self.env = Env(self)
        self.env.add_corpus(5)

    def test_odd_line_breaks_in_header_protect_the_profile(self):
        for brk in ("\x0c", "\x0b", "\x85", " ", " ", "\r"):
            with self.subTest(brk=repr(brk)):
                # The library parser splits lines there too and would read "status: draft"; a tool
                # (or Noah's editor) splitting on LF only would not. Never trusted: protected.
                existing = f"---\ntitle: x{brk}status: draft\n---\nNoah's text\n"
                (self.env.writing / "voice-profile.md").write_text(existing, newline="")
                report = self.env.builder().build()
                self.assertEqual((self.env.writing / "voice-profile.md").read_text(newline=""), existing)
                self.assertEqual(report.profile, f"wrote {NEXT} (status: draft)")
                (self.env.writing / "voice-profile.next.md").unlink()

    def test_replaced_draft_is_archived_first(self):
        self.env.builder().build()
        edited = (self.env.writing / "voice-profile.md").read_text().replace("Direct and practical", "Noah edit")
        (self.env.writing / "voice-profile.md").write_text(edited)
        report = self.env.builder().build()
        archived = self.env.root / "90-archive/40-writing/voice-profile.md"
        self.assertEqual(archived.read_text(), edited)
        self.assertIn(("40-writing/voice-profile.md", "90-archive/40-writing/voice-profile.md"), report.archived)
        self.assertIn("Direct and practical", (self.env.writing / "voice-profile.md").read_text())
        # an unchanged rebuild archives nothing more
        before = sorted(p.name for p in (self.env.root / "90-archive/40-writing").iterdir())
        self.env.builder().build()
        self.assertEqual(sorted(p.name for p in (self.env.root / "90-archive/40-writing").iterdir()), before)

    def test_archive_failure_keeps_the_old_draft(self):
        self.env.builder().build()
        old = (self.env.writing / "voice-profile.md").read_text()
        self.env.broker.replies = [GOOD.replace("Direct and practical", "Plain and practical")]
        archive = self.env.root / "90-archive"
        archive.chmod(0o555)
        self.addCleanup(archive.chmod, 0o755)
        with self.assertRaises(BuildError):
            self.env.builder().build()
        self.assertEqual((self.env.writing / "voice-profile.md").read_text(), old)

    def test_approval_in_the_last_window_before_the_write(self):
        self.env.builder().build()                       # a draft exists
        self.env.broker.replies = [GOOD.replace("Direct and practical", "Plain and practical")]
        real = buildmod.Builder.plan_write

        def plan_then_approve(builder, lib, rel, text, **kw):
            item = real(builder, lib, rel, text, **kw)
            if rel == PROFILE:
                (self.env.writing / "voice-profile.md").write_text(APPROVED)
            return item

        with mock.patch.object(buildmod.Builder, "plan_write", plan_then_approve), \
                self.assertRaises(BuildError) as ctx:
            self.env.builder().build()
        self.assertIn("changed while writing", str(ctx.exception))
        self.assertEqual((self.env.writing / "voice-profile.md").read_text(), APPROVED)

    def test_note_reads_well_once_approved(self):
        self.env.builder().build()
        text = (self.env.writing / "voice-profile.md").read_text()
        meta, body = fm.parse(text)
        self.assertIn(NOTE, body)
        self.assertIn("Noah reviews and approves this profile; the AI uses it only for drafts he reviews.", body)
        self.assertNotIn("draft", body.split("## Tone")[0].replace("drafts he reviews", ""))
        self.assertNotIn("draft", meta["summary"].replace("drafts Noah reviews", ""))


class ZipChecksTests(unittest.TestCase):
    """V-6 and V-7 (total ratio)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def read(self, data: bytes, caps: Caps | None = None):
        path = self.dir / "s.docx"
        path.write_bytes(data)
        return read_docx(path, caps or Caps())

    def test_macro_content_types_refused(self):
        for decl in ("application/vnd.ms-word.document.macroEnabled.main+xml",
                     "application/vnd.ms-office.vbaProject", "application/vnd.ms-word.vbaData+xml"):
            with self.subTest(decl=decl):
                types = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
                         f'content-types"><Override PartName="/word/x.bin" ContentType="{decl}"/></Types>')
                data = io.BytesIO()
                with zipfile.ZipFile(data, "w") as zf:
                    zf.writestr("[Content_Types].xml", types)
                    zf.writestr("word/document.xml", document(para("x")))
                with self.assertRaises(DocxRefused) as ctx:
                    self.read(data.getvalue())
                self.assertIn("macro", str(ctx.exception))

    def test_total_ratio_without_any_single_entry_over_the_floor(self):
        # Four entries of exactly 1 MiB (not above the per-entry floor) that each compress ~1000:1.
        extra = {f"word/media/pad{i}.xml": " " * (1024 * 1024) for i in range(4)}
        with self.assertRaises(DocxRefused) as ctx:
            self.read(docx_bytes(document(para("x")), extra=extra))
        self.assertIn("total compression ratio", str(ctx.exception))

    def test_normal_docx_still_reads(self):
        self.assertTrue(self.read(docx_bytes(essay(3))))


if __name__ == "__main__":
    unittest.main()
