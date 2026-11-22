"""Reviewer K round 2: V2-1 (deep or huge XML markup), V2-2 (CRLF drafts), V2-3 (Noah approving
while a draft is archived or replaced), V2-4 (a swapped folder during an archive)."""
import contextlib
import errno
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from library import frontmatter as fm
from voice_profile import archive as archmod
from voice_profile import build as buildmod
from voice_profile.build import PROFILE, BuildError
from voice_profile.config import ConfigError, validate
from voice_profile.docx import MAX_MARKUP, Caps, DocxRefused, _MarkupGuard, parse_document, parse_styles

from .fakes import GOOD, Env, W, document, essay, para
from .hostile import KINDS, docx, xml_for

APP = Path(__file__).resolve().parent.parent
LIB = APP.parent / "skill-library"
APPROVED = "---\ntitle: Voice profile\nstatus: approved\n---\n# Voice profile\n\nNoah's approved text.\n"


def nested(depth: int) -> str:
    return document("<w:sdt>" * depth + "</w:sdt>" * depth + para("After the nesting."))


class DeepXmlTests(unittest.TestCase):
    def test_depth_cap(self):
        # body + document + 126 sdt = 128 open elements: fine; one more is refused.
        self.assertEqual([p.text for p in parse_document(nested(125).encode())], ["After the nesting."])
        with self.assertRaises(DocxRefused) as ctx:
            parse_document(nested(127).encode())
        self.assertIn("deeper than 128", str(ctx.exception))

    def test_depth_cap_inside_skipped_subtrees_too(self):
        xml = document("<w:tbl>" + "<w:tr>" * 200 + "</w:tr>" * 200 + "</w:tbl>")
        with self.assertRaises(DocxRefused):
            parse_document(xml.encode())

    def test_styles_depth_cap(self):
        styles = f'<w:styles xmlns:w="{W}">' + "<w:style>" * 200 + "</w:style>" * 200 + "</w:styles>"
        with self.assertRaises(DocxRefused) as ctx:
            parse_styles(styles.encode())
        self.assertIn("word/styles.xml nests elements deeper than 128", str(ctx.exception))

    def test_markup_cap_each_kind(self):
        n = MAX_MARKUP
        cases = {
            "attribute": f'<w:p x="{"a" * n}"/>',
            "attribute with >": f'<w:p x="{"a>" * (n // 2)}"/>',
            "attribute single-quoted with >": f"<w:p x='{'a>' * (n // 2)}'/>",
            "many attributes": "<w:p " + " ".join(f'a{i}="b"' for i in range(n // 6)) + "/>",
            "element name": f"<w:{'a' * n}/>",
            "end tag": f"<w:p></w:p{' ' * n}>",
            "comment with > and quotes": "<!--" + "a>'\"" * (n // 4) + "-->",
            "processing instruction": f"<?x {'a>' * (n // 2)}?>",
        }
        for what, markup in cases.items():
            with self.subTest(what=what):
                with self.assertRaises(DocxRefused) as ctx:
                    parse_document(document(markup).encode())
                self.assertIn(f"markup over {MAX_MARKUP} bytes", str(ctx.exception))
        ok = document(f'<w:p x="{"a" * (n - 100)}"/>' + "<!--" + "b" * (n - 100) + "-->" + para("Kept."))
        self.assertEqual([p.text for p in parse_document(ok.encode())], ["Kept."])

    def test_guard_is_chunk_independent(self):
        # The same bytes in 1-, 2-, 3- and 7-byte chunks: closers and openers that straddle chunks.
        xml = (document(para("One.") + "<!-- a -- b > c ' \" -->" + "<?pi x > y ?>"
                        + "<w:p x='a>b' y=\"c>d\"><w:r><w:t><![CDATA[e > f]]></w:t></w:r></w:p>" + para("Two."))
               .encode())
        for size in (1, 2, 3, 7, 64):
            with self.subTest(size=size):
                guard = _MarkupGuard("x")
                for i in range(0, len(xml), size):
                    guard.feed(xml[i:i + size])
                guard.feed(b"", True)
                self.assertEqual(guard.state, "text")
                self.assertEqual(guard.carry, b"")
        big = document("<!--" + "a" * (MAX_MARKUP + 10) + "-->").encode()
        guard = _MarkupGuard("x")
        with self.assertRaises(DocxRefused):
            for i in range(0, len(big), 5):
                guard.feed(big[i:i + 5])

    def test_real_document_through_tiny_chunks(self):
        xml = essay(5, extra_body="<!-- a comment > with quotes ' \" -->").encode()
        whole = [p.text for p in parse_document(xml)]
        chunked = [p.text for p in parse_document(xml[i:i + 3] for i in range(0, len(xml), 3))]
        self.assertEqual(whole, chunked)
        self.assertTrue(whole)

    def test_xml_cap_is_8_mib_and_config_cannot_raise_it(self):
        self.assertEqual(Caps().max_xml_bytes, 8 * 1024 * 1024)
        base = {"library_root": "/x", "broker_url": "http://h:1"}
        self.assertEqual(validate(base)["max_xml_bytes"], 8 * 1024 * 1024)
        with self.assertRaises(ConfigError):
            validate({**base, "max_xml_bytes": 8 * 1024 * 1024 + 1})

    def test_worst_shapes_in_a_fresh_process_stay_far_under_256m(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = []
        for kind in KINDS:
            data = docx(xml_for(kind, 8_000_000))
            self.assertLessEqual(len(data), 5 * 1024 * 1024, kind)
            path = Path(tmp.name) / f"{kind}.docx"
            path.write_bytes(data)
            paths.append(str(path))
        prog = (f"import sys, resource, time; sys.path[:0] = [{str(APP)!r}, {str(LIB)!r}]\n"
                "from voice_profile.docx import read_docx, Caps, DocxRefused\n"
                "for p in sys.argv[1:]:\n"
                "    t = time.perf_counter()\n"
                "    try:\n        r = 'read %d' % len(read_docx(p, Caps()))\n"
                "    except DocxRefused as e:\n        r = 'refused: %s' % e\n"
                "    hwm = [l for l in open('/proc/self/status') if l.startswith('VmHWM:')][0]\n"
                "    rss = int(hwm.split()[1]) // 1024\n"
                "    print('%s|%.2f|%d|%s' % (p.rsplit('/', 1)[1], time.perf_counter() - t, rss, r))\n")
        # One fresh interpreter per shape, so each peak is its own. VmHWM (not ru_maxrss, which Linux
        # carries over from the forking test process across exec) is this process's own peak RSS.
        for path in paths:
            with self.subTest(shape=Path(path).stem):
                out = subprocess.run([sys.executable, "-c", prog, path], capture_output=True, text=True,
                                     timeout=120, check=True).stdout.strip()
                _, secs, rss, _ = out.split("|", 3)
                self.assertLess(int(rss), 64, out)
                self.assertLess(float(secs), 10.0, out)


class CrlfDraftTests(unittest.TestCase):
    def test_a_crlf_draft_is_still_a_draft(self):
        env = Env(self)
        env.add_corpus(5)
        env.builder().build()
        prof = env.writing / "voice-profile.md"
        prof.write_bytes(prof.read_bytes().replace(b"\n", b"\r\n"))       # saved on Windows
        self.assertIn("voice-profile.md: status draft", env.builder().status())
        env.broker.replies = [GOOD.replace("Direct and practical", "Plain and practical")]
        report = env.builder().build()
        self.assertEqual(report.profile, f"wrote {PROFILE} (status: draft)")
        self.assertIn("Plain and practical", prof.read_text())
        self.assertIn(b"\r\n", (env.root / "90-archive/40-writing/voice-profile.md").read_bytes())

    def test_a_lone_cr_header_is_still_unreadable(self):
        env = Env(self)
        env.add_corpus(5)
        existing = "---\r\ntitle: x\rstatus: draft\r\n---\r\nbody\r\n"
        (env.writing / "voice-profile.md").write_bytes(existing.encode())
        env.builder().build()
        self.assertEqual((env.writing / "voice-profile.md").read_bytes(), existing.encode())
        self.assertTrue((env.writing / "voice-profile.next.md").exists())


class ApproveWhileReplacingTests(unittest.TestCase):
    """V2-3: whenever Noah saves the profile as approved during the replacement, his file survives
    at its name and no draft is written over it. With and without separate mounts."""

    def setUp(self):
        self.env = Env(self)
        self.env.add_corpus(5)
        self.env.builder().build()                                    # a draft exists
        self.prof = self.env.writing / "voice-profile.md"
        self.env.broker.replies = [GOOD.replace("Direct and practical", "Plain and practical")]

    def assertKept(self, ctx_exc):
        self.assertEqual(self.prof.read_text(), APPROVED)
        leftovers = [p.name for p in self.env.writing.iterdir() if p.name.startswith(".lib-tmp-")]
        leftovers += [p.name for p in (self.env.root / "90-archive").rglob(".lib-tmp-*")]
        self.assertEqual(leftovers, [])

    def run_build(self, separate: bool):
        if not separate:
            return self.env.builder().build()
        from .test_archive import separate_mounts
        with separate_mounts():
            return self.env.builder().build()

    def test_in_place_save_between_copy_and_unlink(self):
        real = archmod._copy

        def copy_then_save(src_fd, dst_fd):
            real(src_fd, dst_fd)
            with open(self.prof, "r+") as fh:              # same inode, new size and mtime
                fh.write(APPROVED)
                fh.truncate()

        for separate in (False, True):
            with self.subTest(separate=separate):
                self.prof.write_text(self.prof.read_text().replace("APPROVED", ""))
                with mock.patch.object(archmod, "_copy", copy_then_save), self.assertRaises(BuildError) as ctx:
                    self.run_build(separate)
                self.assertIn("changed while it was being copied", str(ctx.exception))
                self.assertKept(ctx)
                self.prof.write_text(self.prof.read_text().replace("approved", "draft"))

    def test_in_place_save_after_the_copy_before_the_name_is_taken(self):
        real = archmod._claim

        def save_then_claim(dfd, name):
            with open(self.prof, "r+") as fh:
                fh.write(APPROVED)
                fh.truncate()
            return real(dfd, name)

        with mock.patch.object(archmod, "_claim", save_then_claim), self.assertRaises(BuildError) as ctx:
            self.run_build(False)
        self.assertIn("changed after it was copied", str(ctx.exception))
        self.assertIn("it was not removed", str(ctx.exception))
        self.assertKept(ctx)

    def test_save_after_the_name_check_before_the_link(self):
        real = os.listdir

        def listdir_then_save(fd):
            names = real(fd)
            if "samples" in names and not self.prof.exists():    # write_new's check in 40-writing
                self.prof.write_text(APPROVED)
            return names

        with mock.patch.object(archmod.os, "listdir", listdir_then_save), self.assertRaises(BuildError) as ctx:
            self.run_build(False)
        self.assertIn("appeared while writing", str(ctx.exception))
        self.assertKept(ctx)

    def test_atomic_save_between_copy_and_unlink(self):
        real = archmod._copy

        def copy_then_replace(src_fd, dst_fd):
            real(src_fd, dst_fd)
            new = self.env.tmp / "noah-save.md"
            new.write_text(APPROVED)
            os.replace(new, self.prof)                     # an editor's atomic save: a new inode

        with mock.patch.object(archmod, "_copy", copy_then_replace), self.assertRaises(BuildError):
            self.run_build(False)
        self.assertKept(None)

    def test_save_in_the_last_instant_before_the_name_is_taken(self):
        from .test_archive import separate_mounts

        for separate in (False, True):
            with self.subTest(separate=separate), contextlib.ExitStack() as stack:
                if separate:
                    stack.enter_context(separate_mounts())
                inner = os.rename

                def save_then_rename(src, dst, *a, inner=inner, **kw):
                    if src == "voice-profile.md" and kw.get("src_dir_fd") is not None:
                        new = self.prof.with_name("noah-save.tmp")     # his editor saves beside it
                        new.write_text(APPROVED)
                        os.replace(new, self.prof)         # lands after the identity check
                    return inner(src, dst, *a, **kw)

                stack.enter_context(mock.patch.object(archmod.os, "rename", save_then_rename))
                with self.assertRaises(BuildError) as ctx:
                    self.run_build(False)
                stack.close()
                self.assertIn("saved again", str(ctx.exception))
                self.assertKept(ctx)
                self.prof.write_text(self.prof.read_text().replace("approved", "draft"))

    def test_put_back_never_clobbers_a_second_save(self):
        real_rename = os.rename

        def two_saves(src, dst, *a, **kw):
            if src == "voice-profile.md" and kw.get("src_dir_fd") is not None:
                self.prof.write_text(APPROVED.replace("approved text", "first save"))   # in place
                result = real_rename(src, dst, *a, **kw)
                self.prof.write_text(APPROVED)                                            # a new file
                return result
            return real_rename(src, dst, *a, **kw)

        with mock.patch.object(archmod.os, "rename", two_saves), self.assertRaises(BuildError) as ctx:
            self.run_build(False)
        self.assertIn("saved twice", str(ctx.exception))
        self.assertEqual(self.prof.read_text(), APPROVED)
        kept = [p for p in self.env.writing.iterdir() if p.name.startswith(".lib-tmp-")]
        self.assertEqual([p.read_text() for p in kept], [APPROVED.replace("approved text", "first save")])

    def test_save_between_the_status_read_and_the_archive(self):
        real = buildmod.copy_archive

        def save_then_archive(lib, rel, **kw):
            self.prof.write_text(APPROVED)
            return real(lib, rel, **kw)

        for separate in (False, True):
            with self.subTest(separate=separate), mock.patch.object(buildmod, "copy_archive", save_then_archive), \
                    self.assertRaises(BuildError) as ctx:
                self.run_build(separate)
            self.assertIn("changed before it could be archived", str(ctx.exception))
            self.assertKept(ctx)
            self.prof.write_text(self.prof.read_text().replace("approved", "draft"))

    def test_save_after_the_archive_before_the_write(self):
        real = buildmod.copy_archive
        for name in ("voice-profile.md", "Voice-Profile.md"):
            with self.subTest(name=name):
                self.env.broker.replies = [GOOD.replace("Direct and practical", f"Tone for {name}")]

                def archive_then_save(lib, rel, name=name, **kw):
                    result = real(lib, rel, **kw)
                    (self.env.writing / name).write_text(APPROVED)
                    return result

                with mock.patch.object(buildmod, "copy_archive", archive_then_save), \
                        self.assertRaises(BuildError) as ctx:
                    self.run_build(False)
                self.assertIn("appeared while writing", str(ctx.exception))
                self.assertEqual((self.env.writing / name).read_text(), APPROVED)
                self.assertEqual([p.name for p in self.env.writing.iterdir() if p.name.startswith(".lib-tmp-")], [])
                (self.env.writing / name).unlink()
                self.env.builder().build()                 # a draft again for the next case

    def test_save_right_after_the_final_status_check(self):
        real = buildmod.profile_target
        calls = []

        def target_then_save(lib):
            result = real(lib)
            calls.append(result)
            if len(calls) == 3:                            # start, after the model, before the write
                self.prof.write_text(APPROVED)
            return result

        with mock.patch.object(buildmod, "profile_target", target_then_save), \
                self.assertRaises(BuildError) as ctx:
            self.run_build(False)
        self.assertEqual(len(calls), 3)
        self.assertIn("changed while writing", str(ctx.exception))
        self.assertKept(ctx)

    def test_no_hard_links_falls_back_without_clobbering(self):
        with mock.patch.object(archmod.os, "link", side_effect=OSError(errno.EPERM, "Operation not permitted")):
            report = self.run_build(False)
        self.assertEqual(report.profile, f"wrote {PROFILE} (status: draft)")
        self.assertIn("Plain and practical", self.prof.read_text())
        self.assertEqual([p.name for p in self.env.writing.iterdir() if p.name.startswith(".lib-tmp-")], [])

    def test_profile_is_never_moved_by_rename(self):
        # The library's rename-based archive would carry away whatever is at the name; never used here.
        with mock.patch.object(buildmod.Library, "archive", side_effect=AssertionError("renamed")):
            report = self.run_build(False)
        self.assertEqual(report.profile, f"wrote {PROFILE} (status: draft)")
        self.assertEqual(fm.parse(self.prof.read_text())[0]["status"], "draft")


class SwappedFolderTests(unittest.TestCase):
    """V2-4: every step after the folder walk goes through held folder fds."""

    def setUp(self):
        self.env = Env(self)
        self.env.add_corpus(5)
        self.env.builder().build(no_ai=True)
        self.st = self.env.writing / "samples-text"
        self.original = self.st / "essay-2.md"
        self.text = self.original.read_text()
        self.other = self.env.tmp / "other"
        self.other.mkdir()
        (self.other / "essay-2.md").write_text("OTHER FILE")

    def swap(self):
        os.rename(self.st, self.env.writing / "moved")
        os.symlink(self.other, self.st)

    def test_folder_swapped_after_the_walk(self):
        real_open = os.open
        done = []

        def swap_then_open(path, flags, *a, **kw):
            if str(path).endswith("essay-2.md") and not done:
                done.append(1)
                self.swap()
            return real_open(path, flags, *a, **kw)

        with mock.patch.object(archmod.os, "open", swap_then_open):
            moved = archmod.copy_archive(self.env.lib, "40-writing/samples-text/essay-2.md")
        self.assertTrue(done)
        self.assertEqual((self.other / "essay-2.md").read_text(), "OTHER FILE")
        self.assertFalse((self.env.writing / "moved/essay-2.md").exists())       # the real one was moved
        self.assertEqual((self.env.root / moved[0][1]).read_text(), self.text)

    def test_folder_swapped_before_the_walk(self):
        real = self.env.lib.ensure_dir

        def swap_then_ensure(rel):
            self.swap()
            return real(rel)

        with mock.patch.object(self.env.lib, "ensure_dir", swap_then_ensure), \
                self.assertRaises(buildmod.PathRefused):
            archmod.copy_archive(self.env.lib, "40-writing/samples-text/essay-2.md")
        self.assertEqual((self.other / "essay-2.md").read_text(), "OTHER FILE")
        self.assertEqual((self.env.writing / "moved/essay-2.md").read_text(), self.text)

    def test_file_swapped_for_a_symlink(self):
        victim = self.env.tmp / "victim.md"
        victim.write_text("victim")
        real = self.env.lib.ensure_dir

        def swap_then_ensure(rel):
            self.original.unlink()
            self.original.symlink_to(victim)
            return real(rel)

        with mock.patch.object(self.env.lib, "ensure_dir", swap_then_ensure), \
                self.assertRaises(buildmod.PathRefused):
            archmod.copy_archive(self.env.lib, "40-writing/samples-text/essay-2.md")
        self.assertEqual(victim.read_text(), "victim")
        self.assertTrue(self.original.is_symlink())


if __name__ == "__main__":
    unittest.main()
