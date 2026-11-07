"""The EXDEV fallback for archiving (Lead, §12.21): in the unit, 40-writing and 90-archive/40-writing
are separate mounts, so rename(2) between them fails with EXDEV. Simulated here by an os.replace /
os.rename that refuses any move between two different folders."""
import errno
import os
import unittest
from pathlib import Path
from unittest import mock

from library import LibraryError
from voice_profile import archive as archmod
from voice_profile.archive import archive
from voice_profile.build import PROFILE

from .fakes import GOOD, Env

REAL_REPLACE = os.replace
REAL_RENAME = os.rename


def _cross(fn):
    def move(src, dst, *a, **kw):
        if os.path.dirname(os.path.abspath(src)) != os.path.dirname(os.path.abspath(dst)):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return fn(src, dst, *a, **kw)
    return move


def separate_mounts():
    """Patch os.replace and os.rename: a move between two folders fails with EXDEV."""
    return mock.patch.multiple(os, replace=_cross(REAL_REPLACE), rename=_cross(REAL_RENAME))


TEXT = "---\ntitle: t\nsource: s\nadded: 2026-10-27\ntags: []\nsummary: s\nrelated: []\ngenerator: voice-profile\n---\nbody\n"
REL = "40-writing/samples-text/old.md"
DEST = "90-archive/40-writing/samples-text"


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        (self.env.root / "40-writing/samples-text").mkdir()
        self.src = self.env.root / REL
        self.src.write_text(TEXT)

    def dest_dir(self) -> Path:
        return self.env.root / DEST

    def leftovers(self) -> list[str]:
        d = self.dest_dir()
        return sorted(p.name for p in d.iterdir()) if d.exists() else []

    def test_plain_rename_when_same_device(self):
        self.assertEqual(archive(self.env.lib, REL), [(REL, f"{DEST}/old.md")])
        self.assertFalse(self.src.exists())

    def test_exdev_falls_back_to_copy_rename_unlink(self):
        with separate_mounts(), mock.patch.object(archmod, "copy_archive", wraps=archmod.copy_archive) as spy:
            moved = archive(self.env.lib, REL)
        spy.assert_called_once()
        self.assertEqual(moved, [(REL, f"{DEST}/old.md")])
        self.assertEqual((self.dest_dir() / "old.md").read_text(), TEXT)
        self.assertFalse(self.src.exists())
        self.assertEqual(self.leftovers(), ["old.md"])          # no temp file, no placeholder

    def test_other_errors_are_not_retried(self):
        boom = OSError(errno.EACCES, "denied")
        with mock.patch.object(self.env.lib, "archive", side_effect=boom), \
                mock.patch.object(archmod, "copy_archive") as fallback, self.assertRaises(OSError):
            archive(self.env.lib, REL)
        fallback.assert_not_called()
        self.assertTrue(self.src.exists())

    def test_name_collision_gets_a_suffix_and_never_clobbers(self):
        self.dest_dir().mkdir(parents=True)
        (self.dest_dir() / "old.md").write_text("ARCHIVED BEFORE")
        (self.dest_dir() / "OLD-2.md").write_text("ALSO BEFORE")      # case-insensitive, like NTFS
        with separate_mounts():
            moved = archive(self.env.lib, REL)
        self.assertEqual(moved, [(REL, f"{DEST}/old-3.md")])
        self.assertEqual((self.dest_dir() / "old.md").read_text(), "ARCHIVED BEFORE")
        self.assertEqual((self.dest_dir() / "OLD-2.md").read_text(), "ALSO BEFORE")
        self.assertEqual((self.dest_dir() / "old-3.md").read_text(), TEXT)

    def test_failure_mid_copy_keeps_the_source_and_leaves_nothing(self):
        real_write = os.write
        calls = []

        def flaky(fd, data):
            calls.append(len(data))
            if len(calls) == 2:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data[:3])                  # a short write first

        self.src.write_text(TEXT * 50)
        with separate_mounts(), mock.patch.object(archmod, "CHUNK", 64), \
                mock.patch.object(archmod.os, "write", flaky), self.assertRaises(OSError):
            archive(self.env.lib, REL)
        self.assertEqual(self.src.read_text(), TEXT * 50)
        self.assertEqual(self.leftovers(), [])

    def test_failure_at_the_final_rename_keeps_the_source(self):
        def same_dir_fails(src, dst, *a, **kw):
            if os.path.dirname(os.path.abspath(src)) == os.path.dirname(os.path.abspath(dst)):
                raise OSError(errno.EIO, "I/O error")
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        with mock.patch.object(os, "replace", same_dir_fails), self.assertRaises(OSError):
            archive(self.env.lib, REL)
        self.assertEqual(self.src.read_text(), TEXT)
        self.assertEqual(self.leftovers(), [])

    def test_durability_order(self):
        events = []
        real_fsync, real_unlink, real_replace = os.fsync, os.unlink, _cross(REAL_REPLACE)

        def fsync(fd):
            events.append("fsync-dir" if os.path.isdir(f"/proc/self/fd/{fd}") else "fsync-file")
            return real_fsync(fd)

        def replace(src, dst, *a, **kw):
            result = real_replace(src, dst, *a, **kw)
            events.append("rename")
            return result

        graves = []
        cross_rename = _cross(REAL_RENAME)

        def rename(src, dst, *a, **kw):
            if src == "old.md":
                graves.append(dst)
                events.append("take-name")
            return cross_rename(src, dst, *a, **kw)

        def unlink(path, *a, **kw):
            if path in graves:
                events.append("unlink-source")
            return real_unlink(path, *a, **kw)

        with mock.patch.multiple(os, fsync=fsync, unlink=unlink, replace=replace, rename=rename):
            archive(self.env.lib, REL)
        moved = events.index("rename")
        take, gone = events.index("take-name"), events.index("unlink-source")
        self.assertIn("fsync-file", events[:moved])                  # the copy is on disk before its name
        self.assertIn("fsync-dir", events[:moved])
        self.assertIn("fsync-dir", events[moved + 1:take])          # the archived name is durable
        self.assertLess(moved, take)                                 # the source goes last
        self.assertLess(take, gone)
        self.assertEqual(events[-1], "fsync-dir")                    # and its removal is durable

    def test_symlinked_destination_folder_refused(self):
        elsewhere = self.env.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.env.root / "90-archive/40-writing").symlink_to(elsewhere)
        with separate_mounts(), self.assertRaises(LibraryError):
            archive(self.env.lib, REL)
        self.assertEqual(self.src.read_text(), TEXT)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_symlinked_source_refused(self):
        target = self.env.tmp / "target.md"
        target.write_text("outside")
        self.src.unlink()
        self.src.symlink_to(target)
        with separate_mounts(), self.assertRaises(LibraryError):
            archmod.copy_archive(self.env.lib, REL)
        self.assertTrue(self.src.is_symlink())
        self.assertEqual(target.read_text(), "outside")

    def test_build_archives_across_mounts(self):
        # The real flow: a stale cleaned text, and a replaced draft profile, under separate mounts.
        env = Env(self)
        env.add_corpus(5)
        env.builder().build()
        (env.samples / "Essay 5.docx").unlink()
        env.broker.replies = [GOOD.replace("Direct and practical", "Plain and practical")]
        with separate_mounts():
            report = env.builder().build()
        self.assertEqual(report.errors, [])
        archived = dict(report.archived)
        self.assertEqual(archived["40-writing/samples-text/essay-5.md"], f"{DEST}/essay-5.md")
        self.assertEqual(archived[PROFILE], "90-archive/40-writing/voice-profile.md")
        self.assertIn("Direct and practical", (env.root / "90-archive/40-writing/voice-profile.md").read_text())
        self.assertIn("Plain and practical", (env.root / PROFILE).read_text())
        self.assertFalse((env.root / "40-writing/samples-text/essay-5.md").exists())


if __name__ == "__main__":
    unittest.main()
