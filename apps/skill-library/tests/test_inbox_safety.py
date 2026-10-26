"""Review round 1 (M1-M3 and minors): lossless inbox, move/log integrity, crash recovery, bad names."""
import datetime as dt
import io
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from library import Library, LibraryError, naming
from library import frontmatter as fm
from library import safefs
from library.cli import main

NOW = dt.datetime(2026, 10, 26, 9, 30, tzinfo=dt.timezone.utc)
TODAY = "2026-10-26"


class Crash(BaseException):
    """Stands in for SIGKILL / power loss: nothing in the library catches it."""


def snapshot(root):
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            snap[os.path.relpath(p, root)] = "dir" if os.path.isdir(p) else Path(p).read_bytes()
    return snap


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "lib"
        self.lib, _ = Library.init(self.root, clock=lambda: NOW)
        self.inbox = self.root / "00-inbox"

    def drop(self, name, data):
        path = self.inbox / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def log(self):
        return (self.root / "90-archive/inbox-log.md").read_text(encoding="utf-8")

    def events(self):
        return [m.group(1) for m in re.finditer(r"^- \S+ (moving|done|undone) \[m-", self.log(), re.M)]

    def ls(self, rel):
        path = self.root / rel
        return sorted(os.listdir(path)) if path.exists() else []

    def reopen(self):
        return Library.open(self.root, clock=lambda: NOW)


HEADER_OK = (f'---\ntitle: "Tax form"\nsource: "scanner"\nadded: {TODAY}\ntags: [tax]\n'
             'summary: "precious notes"\nrelated: []\nmine: "extra key"\n---\nmy notes\n')


class LosslessInboxTest(Base):
    """M1: an inbox header that cannot be carried over exactly stays in the inbox, untouched."""

    P1 = """---
title:
  - "Part one"
  - "Part two"
source:
  - "https://a.example/x"
  - "book p.12"
added: 2020-01-01
tags: rocm
summary:
  - "Key finding A, irreplaceable"
  - "Key finding B"
related: []
# my private note in the header
---
body text
"""

    def assert_left_untouched(self, name, text, reason):
        self.drop(name, text)
        before = snapshot(self.root)
        res = self.lib.inbox(default_topic="t")
        self.assertEqual(res.moved, [])
        self.assertEqual(len(res.left), 1)
        self.assertIn(reason, res.left[0][1])
        self.assertEqual(snapshot(self.root), before, "nothing moved or rewritten, nothing logged")
        os.remove(self.inbox / name)

    def test_reviewer_p1_case(self):
        self.assert_left_untouched("notes.md", self.P1, "comments")

    def test_each_lossy_header_is_refused(self):
        cases = {
            "list title": ("---\ntitle:\n  - a\n  - b\n---\nx\n", "'title' in 00-inbox/n.md is a list"),
            "list source": ("---\nsource: [a, b]\n---\nx\n", "'source' in"),
            "list summary": ("---\nsummary:\n  - a\n---\nx\n", "'summary' in"),
            "comment line": ("---\ntitle: a\n# note\n---\nx\n", "comments (line 3)"),
            "trailing comment": ("---\ntitle: a # note\n---\nx\n", "comments (line 2)"),
            "comment after quote": ('---\ntitle: "a" # note\n---\nx\n', "comments"),
            "comment on key": ("---\ntags: # none yet\n---\nx\n", "comments"),
            "string tags": ("---\ntags: rocm\n---\nx\n", "'tags' in 00-inbox/n.md must be a list"),
            "other added": ("---\nadded: 2020-01-01\n---\nx\n", "says added: 2020-01-01"),
            "malformed": ("---\ntitle: a: b\n---\nx\n", "malformed front-matter"),
        }
        for label, (text, reason) in cases.items():
            with self.subTest(label):
                self.assert_left_untouched("n.md", text, reason)

    def test_lossy_dropped_sidecar_keeps_the_pair_in_the_inbox(self):
        self.drop("scan.pdf", b"%PDF")
        self.drop("scan.pdf.md", self.P1)
        res = self.lib.inbox(default_topic="t")
        self.assertEqual([src for src, _ in res.left], ["00-inbox/scan.pdf"])
        self.assertEqual(self.ls("00-inbox"), ["scan.pdf", "scan.pdf.md"])

    def test_carried_over_header_keeps_every_value(self):
        self.drop("scan.pdf", b"%PDF")
        self.drop("scan.pdf.md", HEADER_OK)
        res = self.lib.inbox(default_topic="t")
        dest = res.moved[0][1]
        meta, body = fm.parse((self.root / f"{dest}.md").read_text())
        self.assertEqual(meta, {"title": "Tax form", "source": "scanner", "added": TODAY, "tags": ["tax"],
                                "summary": "precious notes", "related": [], "mine": "extra key"})
        self.assertEqual(body, "my notes\n")

    def test_missing_fields_are_added_and_present_ones_kept(self):
        self.drop("n.md", '---\ntitle: "Kept"\nsource: ""\n---\nbody\n')
        res = self.lib.inbox(default_topic="t")
        meta, body = fm.parse((self.root / res.moved[0][1]).read_text())
        self.assertEqual(meta, {"title": "Kept", "source": "", "added": TODAY, "tags": [], "summary": "",
                                "related": []})
        self.assertEqual(body, "body\n")

    def test_file_may_still_merge(self):
        src = self.base / "n.md"
        src.write_text(self.P1.replace("# my private note in the header\n", ""))
        res = self.lib.file(src, "t", title="Merged")  # `file` copies, so it may merge
        self.assertEqual(fm.parse((self.root / res.item).read_text())[0]["title"], "Merged")
        self.assertIn("Part one", src.read_text(), "the original keeps everything")


class InboxLogIntegrityTest(Base):
    """M2: the log is checked first; every move is bracketed; a split pair is repaired."""

    def test_utf16_log_refuses_before_anything_moves(self):
        log = self.root / "90-archive/inbox-log.md"
        log.write_bytes(log.read_text().encode("utf-16"))
        self.drop("a.txt", "a")
        before = snapshot(self.root)
        with self.assertRaises(LibraryError) as ctx:
            self.lib.inbox(default_topic="t")
        self.assertIn("not UTF-8", str(ctx.exception))
        self.assertEqual(snapshot(self.root), before)
        out, err = io.StringIO(), io.StringIO()
        code = main(["--root", str(self.root), "inbox", "--default-topic", "t"], clock=lambda: NOW,
                    environ={}, stdout=out, stderr=err)
        self.assertEqual(code, 2)
        self.assertIn("nothing was moved", err.getvalue())
        self.assertEqual(self.ls("00-inbox"), ["a.txt"])

    def test_symlinked_log_refused(self):
        log = self.root / "90-archive/inbox-log.md"
        target = self.base / "elsewhere.md"
        target.write_text(log.read_text())
        log.unlink()
        log.symlink_to(target)
        self.drop("a.txt", "a")
        with self.assertRaises(LibraryError):
            self.lib.inbox(default_topic="t")
        self.assertEqual(self.ls("00-inbox"), ["a.txt"])

    def test_each_move_is_bracketed(self):
        self.drop("a.txt", "a")
        self.lib.inbox(default_topic="t")
        self.assertEqual(self.events(), ["moving", "done"])

    def test_failure_between_the_two_renames_rolls_back(self):
        self.drop("x.pdf", b"%PDF")
        self.drop("x.pdf.md", HEADER_OK)
        real = os.replace
        calls = {"n": 0}

        def flaky(a, b):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(28, "No space left on device")
            return real(a, b)

        with mock.patch("library.core.os.replace", side_effect=flaky):
            res = self.lib.inbox(default_topic="t")
        self.assertEqual(res.moved, [])
        self.assertIn("No space left on device", res.left[0][1])
        self.assertEqual(self.ls("00-inbox"), ["x.pdf", "x.pdf.md"], "the pair stays together")
        self.assertEqual(self.ls("10-sources/t"), [], "no placeholders left")
        self.assertEqual(self.events(), ["moving", "undone"])
        res = self.lib.inbox(default_topic="t")
        self.assertEqual(res.moved, [("00-inbox/x.pdf", f"10-sources/t/{TODAY}_tax-form.pdf")])
        self.assertEqual(self.ls("10-sources/t"), [f"{TODAY}_tax-form.pdf", f"{TODAY}_tax-form.pdf.md"])
        self.assertEqual(self.lib.check(), [])

    def crash_on(self, target, nth):
        """Raise Crash on the nth os.replace inside library.core (target names the call)."""
        assert target == "replace"
        real = os.replace
        calls = {"n": 0}

        def boom(a, b):
            calls["n"] += 1
            if calls["n"] == nth:
                raise Crash()
            return real(a, b)

        return mock.patch("library.core.os.replace", side_effect=boom)

    def test_crash_between_the_two_renames_is_repaired_next_run(self):
        self.drop("x.pdf", b"%PDF")
        self.drop("x.pdf.md", HEADER_OK)
        with self.crash_on("replace", 2), self.assertRaises(Crash):
            self.lib.inbox(default_topic="t")
        # The split: item filed, sidecar still in the inbox, an empty placeholder beside the item.
        self.assertEqual(self.ls("00-inbox"), ["x.pdf.md"])
        self.assertEqual(self.events(), ["moving"])
        res = self.reopen().inbox(default_topic="t")
        self.assertEqual(res.repaired, [("00-inbox/x.pdf", f"10-sources/t/{TODAY}_tax-form.pdf")])
        self.assertEqual(res.moved, [], "the orphaned sidecar is never filed as a note of its own")
        self.assertEqual(self.ls("00-inbox"), [])
        meta, body = fm.parse((self.root / f"10-sources/t/{TODAY}_tax-form.pdf.md").read_text())
        self.assertEqual((meta["summary"], body), ("precious notes", "my notes\n"))
        self.assertEqual(self.events(), ["moving", "done"])
        self.assertEqual(self.lib.check(), [])

    def test_crash_before_the_move_is_rolled_back_next_run(self):
        self.drop("a.pdf", b"%PDF")
        with self.crash_on("replace", 1), self.assertRaises(Crash):
            self.lib.inbox(default_topic="t")
        self.assertEqual(self.ls("10-sources/t"), [f"{TODAY}_a.pdf", f"{TODAY}_a.pdf.md"], "empty placeholders")
        res = self.reopen().inbox(default_topic="t")
        self.assertEqual(res.moved, [("00-inbox/a.pdf", f"10-sources/t/{TODAY}_a-2.pdf")])
        self.assertEqual(self.events(), ["moving", "undone", "moving", "done"])
        # Recovery never unlinks: the empty placeholders stay, and check reports them.
        self.assertEqual((self.root / f"10-sources/t/{TODAY}_a.pdf").stat().st_size, 0)
        self.assertEqual([(v.path, v.kind) for v in self.lib.check()],
                         [(f"10-sources/t/{TODAY}_a.pdf", "empty-placeholder"),
                          (f"10-sources/t/{TODAY}_a.pdf.md", "empty-placeholder")])

    def test_crash_before_the_header_is_finished(self):
        self.drop("n.md", "plain\n")
        with mock.patch.object(Library, "_complete_header", side_effect=Crash()), self.assertRaises(Crash):
            self.lib.inbox(default_topic="t")
        self.assertEqual(self.lib.check()[0].kind, "missing-front-matter")
        res = self.reopen().inbox(default_topic="t")
        self.assertEqual(res.repaired, [("00-inbox/n.md", f"10-sources/t/{TODAY}_n.md")])
        self.assertEqual(self.lib.check(), [])

    def test_done_line_failure_never_aborts_after_a_move(self):
        self.drop("a.txt", "a")
        self.drop("b.txt", "b")
        real = safefs.append_text

        def no_done(path, text):
            if " done [" in text:
                raise OSError(5, "Input/output error")
            return real(path, text)

        with mock.patch("library.core.append_text", side_effect=no_done):
            res = self.lib.inbox(default_topic="t")
        self.assertEqual(len(res.moved), 2, "both moves stand")
        self.assertEqual(len(res.problems), 2)
        self.assertIn("the next inbox run finishes it", res.problems[0])
        res = self.lib.inbox(default_topic="t")
        self.assertEqual(len(res.repaired), 2)
        self.assertEqual(self.events().count("done"), 2)

    def test_intent_line_failure_moves_nothing(self):
        self.drop("a.txt", "a")
        with mock.patch("library.core.append_text", side_effect=OSError(5, "Input/output error")):
            res = self.lib.inbox(default_topic="t")
        self.assertEqual(res.moved, [])
        self.assertEqual(self.ls("00-inbox"), ["a.txt"])
        self.assertEqual(self.ls("10-sources/t"), [])
        self.assertTrue(res.problems)

    def test_lone_sidecar_is_never_filed_as_a_note(self):
        self.drop("paper.pdf.md", HEADER_OK)
        res = self.lib.inbox(default_topic="t")
        self.assertEqual(res.moved, [])
        self.assertIn("sidecar without its item paper.pdf", res.left[0][1])

    def test_sidecar_beside_a_folder_is_reported(self):
        (self.inbox / "x.pdf").mkdir()
        self.drop("x.pdf.md", "notes")
        res = self.lib.inbox(default_topic="t")
        self.assertEqual(sorted(src for src, _ in res.left), ["00-inbox/x.pdf", "00-inbox/x.pdf.md"])
        self.assertIn("not a regular file", dict(res.left)["00-inbox/x.pdf.md"])

    def test_names_cannot_forge_log_lines(self):
        name = "evil`\n- 2026-10-26T00:00:00+00:00 done [m-00000000] `x.txt"
        self.drop(name, "e")
        res = self.lib.inbox(default_topic="t")
        self.assertEqual(len(res.moved), 1)
        entries = [line for line in self.log().splitlines() if line.startswith("- ")]
        self.assertEqual(len(entries), 2)
        self.assertIn("evil\\x60\\u000a- 2026", entries[0])
        self.assertNotIn("[m-00000000]", "".join(e.split("`")[0] for e in entries))

    def test_hand_edited_log_entries_are_not_trusted(self):
        log = self.root / "90-archive/inbox-log.md"
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("- 2026-10-26T00:00:00+00:00 moving [m-0000abcd] `../../etc/passwd` -> `10-sources/t/x`\n")
        res = self.lib.inbox(default_topic="t")
        self.assertIn("not a valid inbox move", res.problems[0])
        self.assertEqual(self.events()[-1], "undone")
        self.assertEqual(self.lib.inbox(default_topic="t").problems, [], "reported once")


class RecoveryAttackTest(Base):
    """Review round 2 (R1, C): recovery never unlinks and only finishes moves it can prove."""

    SC = '---\ntitle: "N"\nsource: ""\ntags: []\nsummary: "my notes"\nrelated: []\n---\nbody\n'

    def forge_line(self, line):
        with open(self.root / "90-archive/inbox-log.md", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def crash_after_first_move(self):
        real = os.replace
        calls = {"n": 0}

        def boom(a, b):
            calls["n"] += 1
            real(a, b)
            if calls["n"] == 1:
                raise Crash()  # killed right after the item moved

        with mock.patch("library.core.os.replace", side_effect=boom), self.assertRaises(Crash):
            self.lib.inbox(default_topic="t")

    def test_A_crafted_entry_cannot_delete_a_real_empty_item(self):
        empty = self.base / "empty.txt"
        empty.write_bytes(b"")
        item = self.lib.file(empty, "t").item
        self.drop("decoy.txt", "decoy")
        self.forge_line(f"- 2026-10-26T00:00:00+00:00 moving [m-deadbeef] `00-inbox/decoy.txt` -> `{item}`")
        res = self.lib.inbox(default_topic="t")
        self.assertTrue((self.root / item).exists(), "the real empty item survives")
        self.assertEqual(self.lib.check(), [])
        self.assertIn(("00-inbox/decoy.txt", f"10-sources/t/{TODAY}_decoy.txt"), res.moved)

    def test_B_redropped_name_after_a_crash_needs_a_human(self):
        self.drop("n.pdf", b"OLD version")
        self.drop("n.pdf.md", self.SC.replace("tags", f"added: {TODAY}\ntags"))
        self.crash_after_first_move()
        self.drop("n.pdf", b"NEW version")
        before = snapshot(self.root)
        res = self.reopen().inbox(default_topic="t")
        self.assertEqual((res.moved, res.repaired), ([], []))
        self.assertIn("both exist", res.problems[0])
        self.assertIn("needs checking by hand", dict(res.left)["00-inbox/n.pdf"])
        self.assertEqual(snapshot(self.root), before, "nothing moved, removed or logged")
        self.assertEqual(self.events(), ["moving"], "the entry stays pending")
        # Once Noah moves the new file aside, the next run finishes the earlier move.
        os.rename(self.inbox / "n.pdf", self.base / "new-n.pdf")
        res = self.reopen().inbox(default_topic="t")
        self.assertEqual(res.repaired, [("00-inbox/n.pdf", f"10-sources/t/{TODAY}_n.pdf")])
        self.assertEqual((self.root / f"10-sources/t/{TODAY}_n.pdf").read_bytes(), b"OLD version")
        self.assertIn("my notes", (self.root / f"10-sources/t/{TODAY}_n.pdf.md").read_text())
        self.assertEqual(self.lib.check(), [])

    def test_B0_a_moved_empty_file_is_never_unlinked(self):
        self.drop("z.txt", b"")
        self.crash_after_first_move()
        self.drop("z.txt", b"re-dropped")
        res = self.reopen().inbox(default_topic="t")
        self.assertIn("both exist", res.problems[0])
        self.assertTrue((self.root / f"10-sources/t/{TODAY}_z.txt").exists())
        self.assertEqual(self.ls("00-inbox"), ["z.txt"])
        out, err = io.StringIO(), io.StringIO()
        code = main(["--root", str(self.root), "inbox", "--default-topic", "t"], clock=lambda: NOW,
                    environ={}, stdout=out, stderr=err)
        self.assertEqual(code, 1)
        self.assertIn("problem: ", err.getvalue())

    def test_C_recovery_refuses_destinations_it_did_not_reserve(self):
        t = self.root / "10-sources" / "t"
        t.mkdir()
        (t / f"{TODAY}_victim.pdf").write_bytes(b"%PDF victim")
        self.drop("ghost.pdf.md", self.SC)
        self.forge_line(f"- 2026-10-26T00:00:00+00:00 moving [m-0badf00d] `00-inbox/ghost.pdf` -> "
                        f"`10-sources/t/{TODAY}_victim.pdf` (with sidecar `00-inbox/ghost.pdf.md`)")
        before = snapshot(self.root)
        res = self.lib.inbox(default_topic="t")
        self.assertIn("is not provably the file move m-0badf00d moved (no inode recorded)", res.problems[0])
        self.assertEqual(snapshot(self.root), before, "no sidecar grafted on, nothing logged")
        self.assertIn("needs checking by hand", dict(res.left)["00-inbox/ghost.pdf.md"])
        # A wrong inode is refused the same way (kept pending), a date other than the log line's
        # is not a valid entry at all (marked undone once).
        ino = (t / f"{TODAY}_victim.pdf").stat().st_ino + 1
        self.forge_line(f"- 2026-10-26T00:00:00+00:00 moving [m-0badf00e] `00-inbox/ghost.pdf` -> "
                        f"`10-sources/t/{TODAY}_victim.pdf` (with sidecar `00-inbox/ghost.pdf.md`) (inode {ino})")
        self.forge_line("- 2026-10-27T00:00:00+00:00 moving [m-0badf00f] `00-inbox/ghost.pdf` -> "
                        f"`10-sources/t/{TODAY}_victim.pdf`")
        res = self.lib.inbox(default_topic="t")
        self.assertEqual(len(res.problems), 3)
        self.assertIn("its inode differs", res.problems[1])
        self.assertIn("not a valid inbox move", res.problems[2])
        self.assertEqual(self.ls("10-sources/t"), [f"{TODAY}_victim.pdf"])
        self.assertEqual(len(self.lib.inbox(default_topic="t").problems), 2, "pending entries keep reporting")
        self.assertEqual(self.events().count("undone"), 1, "no false 'nothing changed' lines")
        # Resolved by hand: Noah appends a done line, and the note can be filed normally again.
        for move_id in ("m-0badf00d", "m-0badf00e"):
            self.forge_line(f"- 2026-10-26T13:00:00+00:00 done [{move_id}] checked by hand")
        res = self.lib.inbox(default_topic="t")
        self.assertEqual(res.problems, [])
        self.assertIn("sidecar without its item", dict(res.left)["00-inbox/ghost.pdf.md"])

    def test_markdown_rewritten_before_a_crash_needs_a_human(self):
        self.drop("n.md", "plain\n")
        real = safefs.append_text

        def crash_on_done(path, text):
            if " done [" in text:
                raise Crash()
            return real(path, text)

        with mock.patch("library.core.append_text", side_effect=crash_on_done), self.assertRaises(Crash):
            self.lib.inbox(default_topic="t")  # header rewritten (new inode), then killed
        before = snapshot(self.root)
        res = self.reopen().inbox(default_topic="t")
        self.assertIn("its inode differs", res.problems[0])
        self.assertEqual(snapshot(self.root), before, "nothing changed, no 'undone' line")
        self.assertEqual(self.events(), ["moving"])
        self.assertEqual(self.lib.check(), [], "the filed note itself is complete")

    def test_D_crash_during_recovery_then_again(self):
        self.drop("n.pdf", b"%PDF")
        self.drop("n.pdf.md", self.SC)
        self.crash_after_first_move()
        with self.crash_on_any_replace(), self.assertRaises(Crash):
            self.reopen().inbox(default_topic="t")  # recovery moves the sidecar, then dies
        res = self.reopen().inbox(default_topic="t")
        self.assertEqual(res.repaired, [("00-inbox/n.pdf", f"10-sources/t/{TODAY}_n.pdf")])
        self.assertIn("my notes", (self.root / f"10-sources/t/{TODAY}_n.pdf.md").read_text())
        self.assertEqual(self.lib.check(), [])

    def crash_on_any_replace(self):
        real = os.replace

        def boom(a, b):
            real(a, b)
            raise Crash()

        return mock.patch("library.core.os.replace", side_effect=boom)

    def test_E_crafted_paths_are_refused(self):
        outside = self.base / f"{TODAY}_outside.txt"
        outside.write_bytes(b"")
        (self.root / "10-sources" / "evil").symlink_to(self.base)
        self.drop("x", "x")
        for src, dest in [("00-inbox/../README.md", f"10-sources/t/{TODAY}_a.txt"),
                          ("00-inbox\\u002f..\\u002fINDEX.md", f"10-sources/t/{TODAY}_a.txt"),
                          ("00-inbox/x", f"10-sources/evil/{TODAY}_outside.txt"),
                          ("00-inbox/x", f"90-archive/{TODAY}_a.txt")]:
            self.forge_line(f"- 2026-10-26T00:00:00+00:00 moving [m-{os.urandom(4).hex()}] `{src}` -> `{dest}`")
        res = self.lib.inbox()
        self.assertEqual(len(res.problems), 4)
        self.assertTrue(outside.exists())
        self.assertEqual(self.ls("00-inbox"), ["x"])


class RoundThreeTest(Base):
    """Follow-ups after approval: placeholders in check, the forge's layout, public helpers."""

    def write(self, rel, data=b""):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))

    def header(self, **extra):
        meta = {"title": "T", "source": "", "added": TODAY, "tags": [], "summary": "", "related": []}
        meta.update(extra)
        return fm.dump(meta)

    def test_check_flags_empty_placeholders_directly(self):
        self.write(f"10-sources/t/{TODAY}_lone.pdf")                       # no sidecar at all
        self.write(f"10-sources/t/{TODAY}_pair.pdf")
        self.write(f"10-sources/t/{TODAY}_pair.pdf.md")                    # both empty
        self.write(f"10-sources/t/{TODAY}_note.md")                        # empty Markdown item
        self.write(f"10-sources/t/{TODAY}_real-empty.txt")                 # a real empty item...
        self.write(f"10-sources/t/{TODAY}_real-empty.txt.md", self.header())  # ...with its sidecar
        self.write(f"10-sources/t/{TODAY}_full.pdf", b"%PDF")
        self.write(f"10-sources/t/{TODAY}_full.pdf.md")                    # empty sidecar of a real item
        found = sorted((v.path, v.kind) for v in self.lib.check())
        self.assertEqual(found, [
            (f"10-sources/t/{TODAY}_full.pdf.md", "missing-front-matter"),
            (f"10-sources/t/{TODAY}_lone.pdf", "empty-placeholder"),
            (f"10-sources/t/{TODAY}_note.md", "empty-placeholder"),
            (f"10-sources/t/{TODAY}_pair.pdf", "empty-placeholder"),
            (f"10-sources/t/{TODAY}_pair.pdf.md", "empty-placeholder"),
        ])

    def test_forge_folders_are_in_the_layout_and_left_to_the_forge(self):
        self.assertTrue((self.root / "35-forge").is_dir())
        self.assertTrue((self.root / "95-shelf").is_dir())
        self.write("35-forge/2026-10-26_pdf-tool/request.md", "Noah's words, no header")
        self.write("35-forge/.forge-lock")
        self.write("95-shelf/old-skill/skill/SKILL.md", "draft")
        self.assertEqual(self.lib.check(), [])
        _, count = self.lib.index()
        self.assertEqual(count, 1)  # only the inbox log
        self.assertNotIn("35-forge", (self.root / "INDEX.md").read_text())
        self.assertNotIn("95-shelf", (self.root / "INDEX.md").read_text())
        (self.root / "35-forge" / "link").symlink_to(self.base)
        self.assertEqual([(v.path, v.kind) for v in self.lib.check()], [("35-forge/link", "symlink")])

    def test_skill_code_and_conventional_names(self):
        self.write("30-skills/pdf-tool/SKILL.md", self.header(name="pdf-tool"))
        self.write("30-skills/pdf-tool/manifest.md", self.header())
        self.write("30-skills/pdf-tool/CHANGELOG.md", self.header())
        self.write("30-skills/pdf-tool/code/main.py", "print(1)\n")
        self.write("30-skills/pdf-tool/code/lib/Helper_Module.py", "x = 1\n")
        self.write("30-skills/pdf-tool/code/README.md", "no header needed")
        self.write("30-skills/pdf-tool/code/.gitignore", "*.pyc\n")
        self.assertEqual(self.lib.check(), [])
        self.write("30-skills/pdf-tool/manifest.md", "no header")   # the exemption is for the name only
        self.write("30-skills/pdf-tool/notes/x.md", self.header())  # only code/ may hold folders
        found = sorted((v.path, v.kind) for v in self.lib.check())
        self.assertEqual(found, [
            ("30-skills/pdf-tool/manifest.md", "missing-front-matter"),
            ("30-skills/pdf-tool/notes", "misplaced"),
            ("30-skills/pdf-tool/notes/x.md", "bad-name"),
            ("30-skills/pdf-tool/notes/x.md", "misplaced"),
        ])

    def test_any_markdown_page_at_the_top_of_a_skill_is_exempt_from_dated_names(self):
        self.write("30-skills/pdf-tool/SKILL.md", self.header())
        self.write("30-skills/pdf-tool/rules.md", self.header())
        self.write("30-skills/pdf-tool/examples.md", self.header())
        self.write("30-skills/pdf-tool/Checklist Page.md", self.header())
        self.assertEqual(self.lib.check(), [])
        self.write("30-skills/pdf-tool/rules.md", "no header")         # the header is still required
        self.write("30-skills/pdf-tool/diagram.png", b"png")           # non-Markdown: dated name + sidecar
        self.write("10-sources/t/rules.md", self.header())              # only inside a skill folder
        found = sorted((v.path, v.kind) for v in self.lib.check())
        self.assertEqual(found, [
            ("10-sources/t/rules.md", "bad-name"),
            ("30-skills/pdf-tool/diagram.png", "bad-name"),
            ("30-skills/pdf-tool/rules.md", "missing-front-matter"),
        ])

    def test_public_helpers_and_aliases(self):
        lib = self.lib
        self.assertEqual(lib.abs_path("20-notes"), self.root.resolve() / "20-notes")
        self.assertEqual(lib.ensure_dir("20-notes/new-topic"), self.root.resolve() / "20-notes/new-topic")
        lib.write_new("20-notes/new-topic/2026-10-26_a.md", self.header())
        with self.assertRaises(LibraryError):
            lib.write_new("20-notes/new-topic/2026-10-26_a.md", "again")
        self.assertIsNone(lib.cap_gate(10))
        for public, private in (("abs_path", "_abs"), ("ensure_dir", "_ensure_dir"),
                                ("write_new", "_write_new"), ("cap_gate", "_cap_gate")):
            self.assertIs(getattr(Library, public), getattr(Library, private))
        lib.move_dir("20-notes/new-topic", "95-shelf/new-topic")
        self.assertEqual(self.ls("95-shelf/new-topic"), ["2026-10-26_a.md"])
        (self.root / "20-notes" / "Other").mkdir()
        (self.root / "20-notes" / "x").mkdir()
        with self.assertRaises(LibraryError):
            lib.move_dir("20-notes/x", "20-notes/other")  # case-insensitive clash
        with self.assertRaises(LibraryError):
            lib.move_dir("20-notes/missing", "20-notes/y")
        with self.assertRaises(LibraryError):
            lib.move_dir("20-notes/x", "../escape")
        (self.root / "20-notes" / "via").symlink_to(self.base)
        with self.assertRaises(LibraryError):
            lib.move_dir("20-notes/x", "20-notes/via/x")
        self.assertTrue((self.root / "20-notes" / "x").is_dir())


class BadNamesAndErrorsTest(Base):
    """M3: undecodable names and bad input never crash; the CLI maps errors to exit 2."""

    def bytes_name(self, folder, raw):
        path = os.path.join(os.fsencode(self.root / folder), raw)
        with open(path, "wb") as fh:
            fh.write(b"x")

    def test_inbox_index_and_check_with_undecodable_names(self):
        self.drop("a-first.txt", "a")
        self.bytes_name("00-inbox", b"b-\xff-bad.txt")
        self.drop("c-last.txt", "c")
        res = self.lib.inbox(default_topic="t")
        self.assertEqual(len(res.moved), 2)
        self.assertEqual(res.left, [("00-inbox/b-\\xff-bad.txt", "the file name is not valid UTF-8; rename it")])
        (self.root / "20-notes" / "x").mkdir()
        self.bytes_name("20-notes/x", b"2026-10-26_\xfe.md")
        changed, _ = self.lib.index()
        self.assertTrue(changed)
        self.assertNotIn("\\xfe", (self.root / "INDEX.md").read_text())
        problems = self.lib.check()
        self.assertIn(("20-notes/x/2026-10-26_\\xfe.md", "bad-name"), [(v.path, v.kind) for v in problems])
        for v in problems:
            str(v).encode("utf-8")  # always printable

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = main([str(a) for a in argv], clock=lambda: NOW, environ={}, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_cli_maps_value_errors_to_exit_2(self):
        src = self.base / "x.pdf"
        src.write_bytes(b"x")
        code, _, err = self.run_cli("--root", self.root, "file", src, "--topic", "t", "--title", "bad \udcff")
        self.assertEqual(code, 2)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertTrue(err.startswith("error: "))
        self.assertEqual(self.ls("10-sources"), [], "refused before anything was written")
        with mock.patch.object(Library, "index", side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")):
            code, _, err = self.run_cli("--root", self.root, "index")
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error: "))

    def test_cli_check_prints_bad_names_safely(self):
        (self.root / "20-notes" / "x").mkdir()
        self.bytes_name("20-notes/x", b"bad-\xff\n- forged.md")
        code, out, _ = self.run_cli("--root", self.root, "check")
        self.assertEqual(code, 1)
        self.assertIn("bad-name\t20-notes/x/bad-\\xff\\n- forged.md", out)
        self.assertEqual(len(out.splitlines()), 2)

    def test_cli_inbox_stopped_at_the_cap_exits_2(self):
        for n in ("a.txt", "b.txt"):
            self.drop(n, "x")
        cap = self.lib.usage_bytes() + 600
        code, out, err = self.run_cli("--root", self.root, "--cap", cap, "inbox", "--default-topic", "t")
        self.assertEqual(code, 2)
        self.assertIn("stopped at the cap", err)
        self.assertIn("moved 00-inbox/a.txt", out)


class MinorFixesTest(Base):
    def test_device_names(self):
        for name in ("com0", "lpt0", "com9", "lpt9", "con", "nul"):
            self.assertFalse(naming.is_kebab_name(name), name)

    def test_index_skips_hidden_folders(self):
        (self.root / ".git" / "objects").mkdir(parents=True)
        (self.root / ".git" / "objects" / "abc").write_text("x")
        (self.root / "20-notes" / ".hidden").mkdir()
        (self.root / "20-notes" / ".hidden" / "secret.txt").write_text("x")
        _, count = self.lib.index()
        self.assertEqual(count, 1)  # only the inbox log
        self.assertNotIn("secret", (self.root / "INDEX.md").read_text())
        kinds = sorted((v.path, v.kind) for v in self.lib.check())
        self.assertEqual(kinds, [(".git", "hidden-file"), ("20-notes/.hidden", "hidden-file")])

    def test_comment_inside_flow_list_rejected(self):
        with self.assertRaises(fm.FrontMatterError):
            fm.parse("---\ntags: [a #b, c]\n---\n")
        self.assertEqual(fm.parse("---\ntags: [C#, a] # ok\n---\n")[0]["tags"], ["C#", "a"])

    def test_bom_is_not_carried_into_the_body(self):
        res = self.lib.file(self.src("bom.md", "﻿# Title\n"), "t")
        text = (self.root / res.item).read_text(encoding="utf-8")
        self.assertNotIn("﻿", text)
        self.assertTrue(text.endswith("---\n\n# Title\n"))
        self.drop("bom2.md", "﻿plain\n")
        dest = self.lib.inbox(default_topic="t").moved[0][1]
        self.assertNotIn("﻿", (self.root / dest).read_text(encoding="utf-8"))

    def src(self, name, text):
        p = self.base / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_a_source_that_grows_while_copied_is_refused(self):
        src = self.base / "grow.bin"
        src.write_bytes(b"x" * 100)
        real = safefs.shutil.copyfileobj

        def grow(fsrc, fdst, n):
            with open(src, "ab") as fh:
                fh.write(b"y" * 10_000)
            return real(fsrc, fdst, n)

        with mock.patch("library.safefs.shutil.copyfileobj", side_effect=grow):
            with self.assertRaises(LibraryError) as ctx:
                self.lib.file(src, "t")
        self.assertIn("grew while it was being copied", str(ctx.exception))
        self.assertEqual(self.ls("10-sources/t"), [], "no item, no placeholders, no temp file")


if __name__ == "__main__":
    unittest.main()
