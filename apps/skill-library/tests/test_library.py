import datetime as dt
import hashlib
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from library import CapExceeded, Library, LibraryError, PathRefused, parse_topic_map
from library import frontmatter as fm
from library import safefs
from library.core import LAYOUT_DIRS

NOW = dt.datetime(2026, 10, 26, 9, 30, tzinfo=dt.timezone.utc)
TODAY = "2026-10-26"


def clock():
    return NOW


def snapshot(root):
    """Every path under root with its content (or link target) and mtime; nothing followed."""
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            if os.path.islink(p):
                content = "link:" + os.readlink(p)
            elif os.path.isdir(p):
                content = "dir"
            else:
                content = Path(p).read_bytes()
            snap[os.path.relpath(p, root)] = (content, st.st_mtime_ns)
    return snap


class LibTestCase(unittest.TestCase):
    cap = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = self.base / "lib"
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.lib, _ = Library.init(self.root, clock=clock)
        if self.cap is not None:
            self.lib.cap_bytes = self.cap

    def src(self, name, data=b"original bytes\n"):
        path = self.base / "incoming" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def read(self, rel):
        return (self.root / rel).read_text(encoding="utf-8")

    def meta(self, rel):
        return fm.parse(self.read(rel))[0]

    def ls(self, rel):
        return sorted(os.listdir(self.root / rel))

    def assert_no_temp_files(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            for name in dirnames + filenames:
                self.assertFalse(name.startswith(safefs.TMP_PREFIX), os.path.join(dirpath, name))


class InitTest(LibTestCase):
    def test_layout(self):
        for rel in LAYOUT_DIRS:
            self.assertTrue((self.root / rel).is_dir(), rel)
        self.assertIn("Never delete anything", self.read("README.md"))
        self.assertIn("read `INDEX.md`", self.read("README.md"))
        self.assertTrue(self.read("INDEX.md").startswith("# AI Library index\n"))
        log = self.meta("90-archive/inbox-log.md")
        self.assertEqual(fm.validate(log), [])
        self.assertEqual(self.lib.check(), [])

    def test_idempotent_and_never_overwrites(self):
        (self.root / "README.md").write_text("Noah's edited rules\n")
        before = snapshot(self.root)
        _, created = Library.init(self.root, clock=clock)
        self.assertEqual(created, [])
        self.assertEqual(snapshot(self.root), before)

    def test_recreates_missing_parts(self):
        os.rmdir(self.root / "40-writing" / "drafts")
        _, created = Library.init(self.root, clock=clock)
        self.assertEqual(created, ["40-writing/drafts/"])

    def test_refuses_missing_parent(self):
        with self.assertRaises(LibraryError):
            Library.init(self.base / "not-mounted" / "ai-library", clock=clock)
        self.assertFalse((self.base / "not-mounted").exists())

    def test_refuses_non_empty_non_library(self):
        other = self.base / "home"
        other.mkdir()
        (other / "photo.jpg").write_bytes(b"x")
        with self.assertRaises(LibraryError):
            Library.init(other, clock=clock)
        self.assertEqual(os.listdir(other), ["photo.jpg"])

    def test_open_refuses_uninitialized(self):
        with self.assertRaises(LibraryError):
            Library.open(self.outside)
        with self.assertRaises(LibraryError):
            Library.open(self.base / "missing")

    def test_root_may_be_a_symlink(self):
        link = self.base / "stable-path"
        link.symlink_to(self.root)
        lib = Library.open(link, clock=clock)
        self.assertEqual(lib.root, self.root.resolve())
        res = lib.file(self.src("a.txt"), "notes-topic")
        self.assertTrue((self.root / res.item).is_file())

    def test_layout_dir_symlink_refused(self):
        os.rmdir(self.root / "10-sources")
        (self.root / "10-sources").symlink_to(self.outside)
        with self.assertRaises(LibraryError):
            Library.open(self.root)
        with self.assertRaises(PathRefused):
            self.lib.file(self.src("a.pdf"), "papers")
        self.assertEqual(os.listdir(self.outside), [])


class FileTest(LibTestCase):
    def test_binary_gets_copy_and_sidecar(self):
        src = self.src("My Paper: v2.PDF", b"%PDF-1.7 \x00\xff")
        res = self.lib.file(src, "papers", tags=["gpu", "rocm"], summary='Say "hi": ok', source="https://e.org/x")
        self.assertEqual(res.item, f"10-sources/papers/{TODAY}_my-paper-v2.pdf")
        self.assertEqual(res.sidecar, res.item + ".md")
        self.assertEqual((self.root / res.item).read_bytes(), b"%PDF-1.7 \x00\xff")
        self.assertEqual(src.read_bytes(), b"%PDF-1.7 \x00\xff", "the original is untouched")
        self.assertEqual(self.meta(res.sidecar), {
            "title": "My Paper: v2", "source": "https://e.org/x", "added": TODAY,
            "tags": ["gpu", "rocm"], "summary": 'Say "hi": ok', "related": [],
        })
        self.assertIn(f"[{TODAY}_my-paper-v2.pdf]", self.read(res.sidecar))
        self.assertEqual(self.lib.check(), [])
        self.assert_no_temp_files()

    def test_defaults(self):
        res = self.lib.file(self.src("scan_001.jpg"), "scans")
        meta = self.meta(res.sidecar)
        self.assertEqual((meta["title"], meta["source"], meta["tags"], meta["summary"]),
                         ("scan 001", "scan_001.jpg", [], ""))

    def test_markdown_without_header(self):
        res = self.lib.file(self.src("notes.md", "# Notes\n\nbody\n"), "notes-topic", title="ROCm notes")
        self.assertIsNone(res.sidecar)
        self.assertEqual(res.item, f"10-sources/notes-topic/{TODAY}_rocm-notes.md")
        meta, body = fm.parse(self.read(res.item))
        self.assertEqual(meta["title"], "ROCm notes")
        self.assertEqual(body, "\n# Notes\n\nbody\n")
        self.assertEqual(self.ls("10-sources/notes-topic"), [f"{TODAY}_rocm-notes.md"])

    def test_markdown_with_header_is_merged(self):
        text = ("---\ntitle: Old title\nsource: a book\nadded: 2020-01-01\ntags: [x]\n"
                "summary: kept\nrelated: [y]\nname: my-skill\n---\nbody\n")
        res = self.lib.file(self.src("skill.md", text), "skills-in", tags=["new"])
        meta, body = fm.parse(self.read(res.item))
        self.assertEqual(meta, {"title": "Old title", "source": "a book", "added": TODAY, "tags": ["new"],
                                "summary": "kept", "related": ["y"], "name": "my-skill"})
        self.assertEqual(body, "body\n")
        self.assertTrue(res.item.endswith(f"{TODAY}_old-title.md"))

    def test_malformed_markdown_refused(self):
        with self.assertRaises(LibraryError) as ctx:
            self.lib.file(self.src("bad.md", "---\ntitle: a: b\n---\n"), "papers")
        self.assertIn("malformed front-matter", str(ctx.exception))
        self.assertFalse((self.root / "10-sources" / "papers").exists())

    def test_collision_suffix(self):
        names = [Path(self.lib.file(self.src("x.pdf", f"v{i}".encode()), "papers").item).name for i in range(3)]
        self.assertEqual(names, [f"{TODAY}_x.pdf", f"{TODAY}_x-2.pdf", f"{TODAY}_x-3.pdf"])
        self.assertEqual((self.root / "10-sources/papers" / names[0]).read_bytes(), b"v0", "never overwritten")
        self.assertEqual((self.root / "10-sources/papers" / names[2]).read_bytes(), b"v2")

    def test_collision_on_sidecar_name_alone(self):
        d = self.root / "10-sources" / "papers"
        d.mkdir()
        (d / f"{TODAY}_x.pdf.md").write_text("orphan")
        res = self.lib.file(self.src("x.pdf"), "papers")
        self.assertEqual(Path(res.item).name, f"{TODAY}_x-2.pdf")
        self.assertEqual((d / f"{TODAY}_x.pdf.md").read_text(), "orphan")

    def test_case_insensitive_collision(self):
        d = self.root / "10-sources" / "papers"
        d.mkdir()
        (d / f"{TODAY}_Foo.md").write_text("upper-case original")
        res = self.lib.file(self.src("foo.md", "body\n"), "papers")
        self.assertEqual(Path(res.item).name, f"{TODAY}_foo-2.md")
        self.assertEqual((d / f"{TODAY}_Foo.md").read_text(), "upper-case original")

    def test_case_insensitive_topic_folder_clash(self):
        (self.root / "10-sources" / "Papers").mkdir()
        with self.assertRaises(PathRefused) as ctx:
            self.lib.file(self.src("x.pdf"), "papers")
        self.assertIn("case-insensitive", str(ctx.exception))
        self.assertEqual(self.ls("10-sources"), ["Papers"])

    def test_non_kebab_topic_refused(self):
        for topic in ("Papers", "my_topic", "a--b", "-a", "../escape", "a/b", "", "con", "x" * 49):
            with self.subTest(topic=topic):
                with self.assertRaises(LibraryError):
                    self.lib.file(self.src("x.pdf"), topic)
        self.assertEqual(self.ls("10-sources"), [])
        self.assertEqual(os.listdir(self.outside), [])

    def test_symlinked_topic_refused(self):
        (self.root / "10-sources" / "evil").symlink_to(self.outside)
        with self.assertRaises(PathRefused):
            self.lib.file(self.src("x.pdf"), "evil")
        self.assertEqual(os.listdir(self.outside), [])

    def test_bad_sources(self):
        with self.assertRaises(LibraryError):
            self.lib.file(self.base / "nope.pdf", "papers")
        with self.assertRaises(LibraryError):
            self.lib.file(self.outside, "papers")
        with self.assertRaises(LibraryError):
            self.lib.file(self.src("latin1.md", "caf\xe9".encode("latin-1")), "papers")

    def test_failed_write_leaves_nothing_behind(self):
        real_replace = os.replace

        def failing_replace(src, dst):
            if str(dst).endswith(".pdf"):
                raise OSError(28, "No space left on device")
            return real_replace(src, dst)

        with mock.patch("library.safefs.os.replace", side_effect=failing_replace):
            with self.assertRaises(OSError):
                self.lib.file(self.src("x.pdf"), "papers")
        self.assertEqual(self.ls("10-sources/papers"), [], "no temp file and no empty placeholder")


class InboxTest(LibTestCase):
    def drop(self, name, data=b"dropped\n"):
        path = self.root / "00-inbox" / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def log_lines(self):
        """Log entries with the random move ids replaced by ID and the inode notes dropped."""
        return [re.sub(r" \(inode \d+\)$", "", re.sub(r"\[m-[0-9a-f]{8}\]", "[ID]", line))
                for line in self.read("90-archive/inbox-log.md").splitlines() if line.startswith("- ")]

    def test_default_topic_and_log(self):
        self.drop("Scan 1.pdf", b"pdf bytes")
        self.drop("notes.md", "plain notes\n")
        res = self.lib.inbox(default_topic="unsorted")
        self.assertEqual(res.moved, [
            ("00-inbox/Scan 1.pdf", f"10-sources/unsorted/{TODAY}_scan-1.pdf"),
            ("00-inbox/notes.md", f"10-sources/unsorted/{TODAY}_notes.md"),
        ])
        self.assertEqual(res.left, [])
        self.assertEqual(self.ls("00-inbox"), [])
        self.assertEqual((self.root / f"10-sources/unsorted/{TODAY}_scan-1.pdf").read_bytes(), b"pdf bytes")
        self.assertEqual(self.meta(f"10-sources/unsorted/{TODAY}_scan-1.pdf.md")["source"], "inbox: Scan 1.pdf")
        meta, body = fm.parse(self.read(f"10-sources/unsorted/{TODAY}_notes.md"))
        self.assertEqual((meta["title"], body), ("notes", "\nplain notes\n"))
        t = "- 2026-10-26T09:30:00+00:00"
        self.assertEqual(self.log_lines(), [
            f"{t} moving [ID] `00-inbox/Scan 1.pdf` -> `10-sources/unsorted/{TODAY}_scan-1.pdf`",
            f"{t} done [ID] `00-inbox/Scan 1.pdf` -> `10-sources/unsorted/{TODAY}_scan-1.pdf`",
            f"{t} moving [ID] `00-inbox/notes.md` -> `10-sources/unsorted/{TODAY}_notes.md`",
            f"{t} done [ID] `00-inbox/notes.md` -> `10-sources/unsorted/{TODAY}_notes.md`",
        ])
        self.assertEqual(fm.validate(self.meta("90-archive/inbox-log.md")), [], "log keeps its header")
        self.assertEqual(self.lib.check(), [])

    def test_log_appends(self):
        self.drop("a.txt")
        self.lib.inbox(default_topic="t")
        self.drop("b.txt")
        self.lib.inbox(default_topic="t")
        self.assertEqual(len(self.log_lines()), 4)
        self.assertIn("done [ID] `00-inbox/a.txt`", self.log_lines()[1])
        self.assertIn("done [ID] `00-inbox/b.txt`", self.log_lines()[3])

    def test_topic_map(self):
        self.drop("ROCm-guide.PDF")
        self.drop("invoice.pdf")
        self.drop("random.bin")
        rules = parse_topic_map("# comment\n\nrocm *rocm*\nfinance-docs invoice*\npapers *.pdf\n")
        res = self.lib.inbox(topic_map=rules)
        self.assertEqual(dict(res.moved), {
            "00-inbox/ROCm-guide.PDF": f"10-sources/rocm/{TODAY}_rocm-guide.pdf",
            "00-inbox/invoice.pdf": f"10-sources/finance-docs/{TODAY}_invoice.pdf",
        })
        self.assertEqual([src for src, _ in res.left], ["00-inbox/random.bin"])
        self.assertIn("no topic", res.left[0][1])
        self.assertEqual(self.ls("00-inbox"), ["random.bin"])
        res = self.lib.inbox(topic_map=rules, default_topic="unsorted")
        self.assertEqual(res.moved, [("00-inbox/random.bin", f"10-sources/unsorted/{TODAY}_random.bin")])

    def test_topic_map_errors(self):
        for text in ("Bad-Topic *.pdf\n", "onlyonefield\n", "../x *\n"):
            with self.assertRaises(LibraryError):
                parse_topic_map(text)
        with self.assertRaises(LibraryError):
            self.lib.inbox(default_topic="Not Kebab")

    def test_dropped_sidecar_travels_with_its_item(self):
        self.drop("scan.pdf", b"pdf")
        self.drop("scan.pdf.md", "---\ntitle: Tax form\ntags: [tax]\n---\nmy note\n")
        res = self.lib.inbox(default_topic="papers")
        self.assertEqual(res.moved, [("00-inbox/scan.pdf", f"10-sources/papers/{TODAY}_tax-form.pdf")])
        meta, body = fm.parse(self.read(f"10-sources/papers/{TODAY}_tax-form.pdf.md"))
        self.assertEqual((meta["title"], meta["tags"], meta["source"], body),
                         ("Tax form", ["tax"], "inbox: scan.pdf", "my note\n"))
        self.assertEqual(self.ls("00-inbox"), [])
        self.assertIn("(with sidecar `00-inbox/scan.pdf.md`)", self.log_lines()[0])

    def test_problem_items_stay_and_are_reported(self):
        self.drop("bad.md", "---\ntitle: [unclosed\n---\n")
        (self.root / "00-inbox" / "folder").mkdir()
        secret = self.outside / "secret.txt"
        secret.write_text("outside")
        (self.root / "00-inbox" / "link.txt").symlink_to(secret)
        self.drop(".~lock.x#")
        self.drop("desktop.ini")
        res = self.lib.inbox(default_topic="unsorted")
        self.assertEqual(res.moved, [])
        self.assertEqual(sorted(src for src, _ in res.left),
                         ["00-inbox/bad.md", "00-inbox/folder", "00-inbox/link.txt"])
        self.assertEqual(self.ls("00-inbox"), [".~lock.x#", "bad.md", "desktop.ini", "folder", "link.txt"])
        self.assertEqual(secret.read_text(), "outside")
        self.assertEqual(self.log_lines(), [])

    def test_markdown_items_never_take_a_sidecar(self):
        self.drop("a.md", "first\n")
        self.drop("a.md.md", "second\n")
        res = self.lib.inbox(default_topic="t")
        self.assertEqual(len(res.moved), 2)
        bodies = sorted(fm.parse(self.read(dest))[1] for _, dest in res.moved)
        self.assertEqual(bodies, ["\nfirst\n", "\nsecond\n"])

    def test_case_insensitive_collision_in_inbox(self):
        self.drop("Paper.pdf", b"one")
        self.drop("paper.pdf", b"two")
        res = self.lib.inbox(default_topic="papers")
        self.assertEqual([dest for _, dest in res.moved],
                         [f"10-sources/papers/{TODAY}_paper.pdf", f"10-sources/papers/{TODAY}_paper-2.pdf"])
        self.assertEqual((self.root / f"10-sources/papers/{TODAY}_paper.pdf").read_bytes(), b"one")
        self.assertEqual((self.root / f"10-sources/papers/{TODAY}_paper-2.pdf").read_bytes(), b"two")


class IndexTest(LibTestCase):
    def populate(self):
        self.lib.file(self.src("b.pdf"), "papers", title="Beta [draft]", summary="Second\nline", tags=["x", "y"])
        self.lib.file(self.src("a.md", "body\n"), "papers", title="Alpha")
        self.lib.file(self.src("z.txt"), "aaa-first", title="Zed")
        (self.root / "00-inbox" / "dropped file.pdf").write_bytes(b"x")
        (self.root / "20-notes" / "t").mkdir()
        (self.root / "20-notes" / "t" / f"{TODAY}_broken.md").write_text("no header\n")

    def test_content_grouping_and_sorting(self):
        self.populate()
        changed, count = self.lib.index()
        self.assertTrue(changed)
        self.assertEqual(count, 6)
        text = self.read("INDEX.md")
        expected_tail = "\n".join([
            "6 items.",
            "",
            "## 00-inbox",
            "",
            "- [dropped file.pdf](00-inbox/dropped%20file.pdf) — unfiled",
            "",
            "## 10-sources/aaa-first",
            "",
            f"- [Zed](10-sources/aaa-first/{TODAY}_zed.txt) — {TODAY}",
            "",
            "## 10-sources/papers",
            "",
            f"- [Alpha](10-sources/papers/{TODAY}_alpha.md) — {TODAY}",
            f"- [Beta \\[draft\\]](10-sources/papers/{TODAY}_beta-draft.pdf) — {TODAY} — Second line — tags: x, y",
            "",
            "## 20-notes/t",
            "",
            f"- [{TODAY}_broken.md](20-notes/t/{TODAY}_broken.md) — (no valid front-matter; run check)",
            "",
            "## 90-archive",
            "",
            f"- [Inbox log](90-archive/inbox-log.md) — {TODAY} — Every move out of 00-inbox, oldest first. "
            "Written by the library tool. — tags: log",
            "",
        ])
        self.assertTrue(text.endswith(expected_tail), text)

    def test_idempotent_identical_bytes(self):
        self.populate()
        self.lib.index()
        first = (self.root / "INDEX.md").read_bytes()
        mtime = os.stat(self.root / "INDEX.md").st_mtime_ns
        later = Library.open(self.root, clock=lambda: NOW + dt.timedelta(days=30))
        self.assertEqual(later.index(), (False, 6))
        self.assertEqual((self.root / "INDEX.md").read_bytes(), first)
        self.assertEqual(os.stat(self.root / "INDEX.md").st_mtime_ns, mtime, "not rewritten")
        # Rebuilt from scratch, the bytes are the same too.
        os.rename(self.root / "INDEX.md", self.base / "old-index")
        self.assertEqual(later.index(), (True, 6))
        self.assertEqual((self.root / "INDEX.md").read_bytes(), first)

    def test_symlinked_index_refused(self):
        target = self.outside / "victim.md"
        target.write_text("outside")
        os.remove(self.root / "INDEX.md")
        (self.root / "INDEX.md").symlink_to(target)
        with self.assertRaises(PathRefused):
            self.lib.index()
        self.assertEqual(target.read_text(), "outside")

    def test_changes_when_library_changes(self):
        self.lib.index()
        self.lib.file(self.src("n.pdf"), "papers")
        changed, count = self.lib.index()
        self.assertTrue(changed)
        self.assertIn(f"{TODAY}_n.pdf", self.read("INDEX.md"))


class CapTest(LibTestCase):
    def set_cap_for(self, fraction_used):
        usage = self.lib.usage_bytes()
        self.lib.cap_bytes = int(usage / fraction_used)
        return usage

    def test_usage_counts_files_and_not_symlinks(self):
        base_usage = self.lib.usage_bytes()
        expected = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        self.assertEqual(base_usage, expected)
        big = self.outside / "big.bin"
        big.write_bytes(b"x" * 10_000)
        (self.root / "20-notes" / "link").symlink_to(big)
        self.assertEqual(self.lib.usage_bytes(), base_usage)
        (self.root / "00-inbox" / "a.bin").write_bytes(b"y" * 1000)
        self.assertEqual(self.lib.usage_bytes(), base_usage + 1000)

    def test_thresholds(self):
        usage = self.lib.usage_bytes()
        cases = [(usage * 2, "ok"), (int(usage / 0.9) + 1, "ok"), (int(usage / 0.9), "warn"),
                 (usage + 1, "warn"), (usage, "full"), (usage - 1, "full")]
        for cap, status in cases:
            with self.subTest(cap=cap):
                self.lib.cap_bytes = cap
                self.assertEqual(self.lib.cap_status().status, status)

    def test_file_warns_at_90_and_refuses_at_100(self):
        usage = self.lib.usage_bytes()
        self.lib.cap_bytes = usage + 4000
        res = self.lib.file(self.src("small.bin", b"s" * 100), "t")
        self.assertIsNone(res.warning)
        res = self.lib.file(self.src("mid.bin", b"m" * 3000), "t")
        self.assertIn("of its cap", res.warning)
        before = snapshot(self.root)
        with self.assertRaises(CapExceeded):
            self.lib.file(self.src("big.bin", b"b" * 2000), "t")
        with self.assertRaises(CapExceeded):
            self.lib.file(self.src("big.bin", b"b" * 2000), "new-topic")
        self.assertEqual(snapshot(self.root), before, "a refused file writes nothing")

    def test_inbox_refuses_when_full(self):
        (self.root / "00-inbox" / "a.pdf").write_bytes(b"a" * 500)
        self.lib.cap_bytes = self.lib.usage_bytes()
        before = snapshot(self.root)
        with self.assertRaises(CapExceeded):
            self.lib.inbox(default_topic="t")
        self.assertEqual(snapshot(self.root), before)

    def test_inbox_stops_when_the_cap_is_reached_midway(self):
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            (self.root / "00-inbox" / name).write_bytes(b"z")
        # Room for about one item (sidecar + two log lines ~ 450 bytes), not two.
        self.lib.cap_bytes = self.lib.usage_bytes() + 600
        res = self.lib.inbox(default_topic="t")
        self.assertTrue(res.refused)
        self.assertEqual([src for src, _ in res.moved], ["00-inbox/a.pdf"])
        self.assertEqual(res.left[0][0], "00-inbox/b.pdf")
        self.assertEqual(self.ls("00-inbox"), ["b.pdf", "c.pdf"])


class ArchiveTest(LibTestCase):
    def test_item_with_sidecar_keeps_its_path(self):
        res = self.lib.file(self.src("x.pdf", b"keep me"), "papers")
        moved = self.lib.archive(res.item)
        self.assertEqual(moved, [
            (res.item, f"90-archive/{res.item}"),
            (res.sidecar, f"90-archive/{res.sidecar}"),
        ])
        self.assertEqual((self.root / f"90-archive/{res.item}").read_bytes(), b"keep me")
        self.assertEqual(self.ls("10-sources/papers"), [])
        self.assertEqual(self.lib.check(), [])

    def test_by_sidecar_path_and_absolute_path(self):
        a = self.lib.file(self.src("a.pdf"), "papers")
        moved = self.lib.archive(a.sidecar)
        self.assertEqual([src for src, _ in moved], [a.item, a.sidecar])
        b = self.lib.file(self.src("b.md", "x\n"), "papers")
        moved = self.lib.archive(str(self.root / b.item))
        self.assertEqual(moved, [(b.item, f"90-archive/{b.item}")])

    def test_collision_suffix(self):
        first = self.lib.file(self.src("x.pdf", b"one"), "papers")
        self.lib.archive(first.item)
        again = self.lib.file(self.src("x.pdf", b"two"), "papers")
        self.assertEqual(again.item, first.item, "the freed name is reused")
        moved = self.lib.archive(again.item)
        self.assertEqual(moved[0][1], f"90-archive/10-sources/papers/{TODAY}_x-2.pdf")
        self.assertEqual(moved[1][1], f"90-archive/10-sources/papers/{TODAY}_x-2.pdf.md")
        self.assertEqual((self.root / f"90-archive/{first.item}").read_bytes(), b"one")

    def test_folder(self):
        self.lib.file(self.src("x.pdf"), "old-topic")
        self.assertEqual(self.lib.archive("10-sources/old-topic"),
                         [("10-sources/old-topic", "90-archive/10-sources/old-topic")])
        self.lib.file(self.src("y.pdf"), "old-topic")
        self.assertEqual(self.lib.archive("10-sources/old-topic/"),
                         [("10-sources/old-topic", "90-archive/10-sources/old-topic-2")])
        self.assertEqual(self.ls("90-archive/10-sources/old-topic-2"), [f"{TODAY}_y.pdf", f"{TODAY}_y.pdf.md"])

    def test_refusals(self):
        res = self.lib.file(self.src("x.pdf"), "papers")
        self.lib.archive(res.item)
        (self.root / "10-sources" / "papers" / "link.pdf").symlink_to(self.outside / "target")
        (self.outside / "victim.txt").write_text("outside")
        refused = [
            f"90-archive/{res.item}",              # already archived
            "10-sources", "40-writing/samples",    # layout
            "README.md", "INDEX.md",               # root files
            "10-sources/papers/missing.pdf",       # does not exist
            "../outside/victim.txt",               # traversal
            "10-sources/../../outside/victim.txt",
            str(self.outside / "victim.txt"),      # absolute, outside
            "10-sources/papers/link.pdf",          # symlink
            ".", "",
        ]
        before = snapshot(self.root)
        for raw in refused:
            with self.subTest(raw=raw):
                with self.assertRaises(LibraryError):
                    self.lib.archive(raw)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual((self.outside / "victim.txt").read_text(), "outside")

    def test_unfiled_inbox_item_can_be_set_aside(self):
        (self.root / "00-inbox" / "Junk File.tmp").write_bytes(b"j")
        self.assertEqual(self.lib.archive("00-inbox/Junk File.tmp"),
                         [("00-inbox/Junk File.tmp", "90-archive/00-inbox/Junk File.tmp")])
        self.assertEqual(self.lib.check(), [])

    def test_symlinked_folder_on_the_way_refused(self):
        (self.root / "20-notes" / "via").symlink_to(self.outside)
        (self.outside / "f.md").write_text("x")
        with self.assertRaises(PathRefused):
            self.lib.archive("20-notes/via/f.md")
        self.assertTrue((self.outside / "f.md").exists())


class NeverDeleteTest(LibTestCase):
    def test_every_byte_survives_a_session(self):
        def digests():
            out = []
            for p in self.root.rglob("*"):
                if p.is_file() and not p.name.endswith(".md"):
                    out.append(hashlib.sha256(p.read_bytes()).hexdigest())
            return sorted(out)

        payloads = [os.urandom(64) for _ in range(5)]
        a = self.lib.file(self.src("a.pdf", payloads[0]), "t")
        self.lib.file(self.src("a.pdf", payloads[1]), "t")
        for i, data in enumerate(payloads[2:]):
            (self.root / "00-inbox" / f"in{i}.bin").write_bytes(data)
        self.lib.inbox(default_topic="t")
        self.lib.archive(a.item)
        self.lib.archive("10-sources/t")
        self.lib.index()
        self.assertEqual(digests(), sorted(hashlib.sha256(d).hexdigest() for d in payloads))
        self.assertEqual(self.lib.check(), [])
        self.assert_no_temp_files()


class CheckTest(LibTestCase):
    def write(self, rel, data):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))

    def good_header(self, **over):
        meta = {"title": "T", "source": "", "added": TODAY, "tags": [], "summary": "", "related": []}
        meta.update(over)
        return fm.dump(meta)

    def test_clean_library_has_no_violations(self):
        self.lib.file(self.src("a.pdf"), "papers")
        self.write("30-skills/my-skill/SKILL.md", self.good_header(name="my-skill") + "steps\n")
        self.write(f"40-writing/samples/{TODAY}_essay.md", self.good_header())
        self.write(f"50-finance/log/{TODAY}_october.md", self.good_header())
        self.write("00-inbox/Anything Goes.PDF", b"x")
        self.assertEqual(self.lib.check(), [])

    def test_reports_every_kind_and_changes_nothing(self):
        h = self.good_header()
        self.write("10-sources/papers/2026-10-26_ok.md", h)
        self.write("10-sources/papers/Bad Name.md", h)                                   # bad-name
        self.write("10-sources/papers/2026-10-26_nohead.md", "just text\n")              # missing-front-matter
        self.write("10-sources/papers/2026-10-26_broken.md", "---\ntitle: a: b\n---\n")  # bad-front-matter
        self.write("10-sources/papers/2026-10-26_nosource.md", "---\ntitle: x\nadded: 2026-10-26\n"
                   "tags: []\nsummary: s\nrelated: []\n---\n")                          # missing-field
        self.write("10-sources/papers/2026-10-26_baddate.md", self.good_header(added="26/10/2026"))  # bad-field
        self.write("10-sources/papers/2026-10-26_latin.md", "---\ntitle: caf\xe9\n".encode("latin-1"))  # bad-encoding
        self.write("10-sources/papers/2026-10-26_lonely.pdf", b"x")                     # missing-sidecar
        self.write("10-sources/papers/2026-10-26_gone.pdf.md", h)                       # orphan-sidecar
        self.write("10-sources/papers/2026-10-26_case.md", h)
        self.write("10-sources/papers/2026-10-26_CASE.md", h)                           # case-collision + bad-name
        self.write("10-sources/Bad_Topic/2026-10-26_x.md", h)                           # bad-dir-name
        self.write("10-sources/papers/nested/2026-10-26_deep.md", h)                    # misplaced dir + file
        self.write("10-sources/2026-10-26_loose.md", h)                                 # misplaced file
        self.write("40-writing/other/2026-10-26_x.md", h)                               # misplaced
        self.write("loose.txt", b"x")                                                   # unknown-entry
        self.write("60-extra/2026-10-26_x.md", h)                                       # unknown-entry (once)
        self.write("20-notes/t/.hidden", b"x")                                          # hidden-file
        self.write(f"20-notes/t/{safefs.TMP_PREFIX}abc", b"x")                          # temp-file
        (self.root / "20-notes" / "t" / "link.md").symlink_to(self.outside)            # symlink
        os.rmdir(self.root / "50-finance" / "log")                                      # missing-layout
        os.remove(self.root / "INDEX.md")                                               # missing-layout

        before = snapshot(self.root)
        found = {(v.path, v.kind) for v in self.lib.check()}
        self.assertEqual(snapshot(self.root), before, "check never changes anything")
        self.assertEqual(found, {
            ("10-sources/papers/Bad Name.md", "bad-name"),
            ("10-sources/papers/2026-10-26_nohead.md", "missing-front-matter"),
            ("10-sources/papers/2026-10-26_broken.md", "bad-front-matter"),
            ("10-sources/papers/2026-10-26_nosource.md", "missing-field"),
            ("10-sources/papers/2026-10-26_baddate.md", "bad-field"),
            ("10-sources/papers/2026-10-26_latin.md", "bad-encoding"),
            ("10-sources/papers/2026-10-26_lonely.pdf", "missing-sidecar"),
            ("10-sources/papers/2026-10-26_gone.pdf.md", "orphan-sidecar"),
            ("10-sources/papers/2026-10-26_CASE.md", "bad-name"),
            ("10-sources/papers/2026-10-26_CASE.md", "case-collision"),
            ("10-sources/Bad_Topic", "bad-dir-name"),
            ("10-sources/papers/nested", "misplaced"),
            ("10-sources/papers/nested/2026-10-26_deep.md", "misplaced"),
            ("10-sources/2026-10-26_loose.md", "misplaced"),
            ("40-writing/other", "misplaced"),
            ("40-writing/other/2026-10-26_x.md", "misplaced"),
            ("loose.txt", "unknown-entry"),
            ("60-extra", "unknown-entry"),
            ("20-notes/t/.hidden", "hidden-file"),
            (f"20-notes/t/{safefs.TMP_PREFIX}abc", "temp-file"),
            ("20-notes/t/link.md", "symlink"),
            ("50-finance/log", "missing-layout"),
            ("INDEX.md", "missing-layout"),
        })

    def test_messages_are_specific(self):
        self.write("10-sources/papers/2026-10-26_broken.md", "---\ntitle: ok\ntitle: dup\n---\n")
        (v,) = self.lib.check()
        self.assertEqual(v.kind, "bad-front-matter")
        self.assertIn("line 3", v.message)
        self.assertIn("duplicate key 'title'", v.message)
        self.assertEqual(str(v), "bad-front-matter\t10-sources/papers/2026-10-26_broken.md: " + v.message)


if __name__ == "__main__":
    unittest.main()
