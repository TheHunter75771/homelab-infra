"""Module L follow-ups (CONTRACTS §12.17, Lead decisions on L2 and R, 2026-10-27):
the L2 mount sentinel at the library root, and ``Library.file(..., extra=...)``."""
import os
import unittest

import library
from library import FORGE_RESERVED_KEYS, SENTINEL_MAX_BYTES, SENTINEL_NAME, Library, LibraryError
from library import frontmatter as fm

from .test_library import TODAY, LibTestCase, clock, snapshot

SENTINEL_TEXT = b"AI Library sentinel (test). Do not delete.\n"


class SentinelTest(LibTestCase):
    def put_sentinel(self, rel=SENTINEL_NAME, data=SENTINEL_TEXT):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_public_name(self):
        self.assertEqual(SENTINEL_NAME, ".ai-library-sentinel")
        self.assertEqual(SENTINEL_MAX_BYTES, 4096)
        self.assertEqual(FORGE_RESERVED_KEYS, {"forge", "fetched", "final_url", "content_type"})
        for name in ("SENTINEL_NAME", "SENTINEL_MAX_BYTES", "FORGE_RESERVED_KEYS"):
            self.assertIn(name, library.__all__)

    def test_init_does_not_create_it(self):
        self.assertFalse(os.path.lexists(self.root / SENTINEL_NAME))
        other = self.base / "fresh"
        _, created = Library.init(other, clock=clock)
        self.assertFalse(os.path.lexists(other / SENTINEL_NAME))
        self.assertNotIn(SENTINEL_NAME, created)

    def test_init_and_open_leave_it_alone(self):
        self.put_sentinel()
        before = snapshot(self.root)
        _, created = Library.init(self.root, clock=clock)
        self.assertEqual(created, [])
        Library.open(self.root)
        self.assertEqual(snapshot(self.root), before)

    def test_check_accepts_exactly_the_root_sentinel(self):
        self.put_sentinel()
        self.assertEqual(self.lib.check(), [])

    def test_check_still_reports_every_other_hidden_file(self):
        self.put_sentinel()
        self.put_sentinel(".ai-library-sentinel.bak")                 # another hidden root file
        self.put_sentinel(".AI-LIBRARY-SENTINEL")                     # same name, other case
        self.put_sentinel(f"00-inbox/{SENTINEL_NAME}")               # unfiled: not checked
        self.put_sentinel(f"20-notes/t/{SENTINEL_NAME}")              # below the root
        self.put_sentinel(f"90-archive/{SENTINEL_NAME}")
        self.put_sentinel(".hidden")
        found = {(v.path, v.kind) for v in self.lib.check()}
        self.assertEqual(found, {
            (".ai-library-sentinel.bak", "hidden-file"),
            (".AI-LIBRARY-SENTINEL", "hidden-file"),
            (".AI-LIBRARY-SENTINEL", "case-collision"),
            (f"20-notes/t/{SENTINEL_NAME}", "hidden-file"),
            (f"90-archive/{SENTINEL_NAME}", "hidden-file"),
            (".hidden", "hidden-file"),
        })

    def test_check_reports_a_sentinel_that_is_not_a_regular_file(self):
        (self.root / SENTINEL_NAME).mkdir()
        self.assertEqual({(v.path, v.kind) for v in self.lib.check()}, {(SENTINEL_NAME, "hidden-file")})
        os.rmdir(self.root / SENTINEL_NAME)
        (self.root / SENTINEL_NAME).symlink_to(self.outside / "elsewhere")
        self.assertEqual({(v.path, v.kind) for v in self.lib.check()}, {(SENTINEL_NAME, "symlink")})

    def test_not_indexed(self):
        _, before_count = self.lib.index()
        before = (self.root / "INDEX.md").read_bytes()
        self.put_sentinel()
        changed, count = self.lib.index()
        self.assertFalse(changed)
        self.assertEqual(count, before_count)
        self.assertEqual((self.root / "INDEX.md").read_bytes(), before)
        self.assertNotIn("sentinel", before.decode("utf-8"))

    def test_not_counted_against_the_cap(self):
        base = self.lib.usage_bytes()
        self.put_sentinel(data=b"s" * SENTINEL_MAX_BYTES)  # the largest exempt size
        self.assertEqual(self.lib.usage_bytes(), base)
        self.assertEqual(self.lib.cap_status().usage, base)
        # Elsewhere the same name is ordinary content and still counts.
        self.put_sentinel(f"20-notes/t/{SENTINEL_NAME}", data=b"n" * 700)
        self.assertEqual(self.lib.usage_bytes(), base + 700)

    def test_cap_gate_ignores_it(self):
        base = self.lib.usage_bytes()
        self.lib.cap_bytes = base + SENTINEL_MAX_BYTES + 50
        self.put_sentinel(data=b"s" * SENTINEL_MAX_BYTES)  # would be over the cap if it were counted
        res = self.lib.file(self.src("a.bin", b"a" * 100), "t")
        self.assertIsNone(res.warning)

    def test_oversized_sentinel_counts_and_is_reported(self):
        base = self.lib.usage_bytes()
        self.put_sentinel(data=b"s" * (SENTINEL_MAX_BYTES + 1))
        self.assertEqual(self.lib.usage_bytes(), base + SENTINEL_MAX_BYTES + 1)
        (v,) = self.lib.check()
        self.assertEqual((v.path, v.kind), (SENTINEL_NAME, "oversized-sentinel"))
        self.assertIn(str(SENTINEL_MAX_BYTES + 1), v.message)

    def test_sparse_sentinel_cannot_hide_bytes(self):
        base = self.lib.usage_bytes()
        huge = 50 * 2**30  # 50 GiB apparent size, almost nothing on disk
        with open(self.root / SENTINEL_NAME, "wb") as fh:
            fh.truncate(huge)
        self.assertEqual(self.lib.usage_bytes(), base + huge)
        self.assertEqual(self.lib.cap_status().status, "ok")  # 50 GiB of 500 GB
        self.lib.cap_bytes = base + huge
        self.assertEqual(self.lib.cap_status().status, "full")
        with self.assertRaises(library.CapExceeded):
            self.lib.file(self.src("a.bin", b"a"), "t")
        self.assertEqual({(v.path, v.kind) for v in self.lib.check()}, {(SENTINEL_NAME, "oversized-sentinel")})

    def test_cannot_be_archived(self):
        self.put_sentinel()
        with self.assertRaises(LibraryError):
            self.lib.archive(SENTINEL_NAME)
        self.assertEqual((self.root / SENTINEL_NAME).read_bytes(), SENTINEL_TEXT)


class FileExtraTest(LibTestCase):
    def test_extra_in_sidecar(self):
        res = self.lib.file(self.src("page.txt", b"text"), "research", title="Page",
                            source="https://example.org/p", tags=["forge"],
                            extra={"forge": "2026-10-27_rocm-tuning", "content_type": "text/html",
                                   "fetched": "2026-10-27T09:30:00+00:00"})
        meta = self.meta(res.sidecar)
        self.assertEqual(meta, {
            "title": "Page", "source": "https://example.org/p", "added": TODAY, "tags": ["forge"],
            "summary": "", "related": [], "forge": "2026-10-27_rocm-tuning",
            "content_type": "text/html", "fetched": "2026-10-27T09:30:00+00:00",
        })
        self.assertEqual((self.root / res.item).read_bytes(), b"text")
        self.assertEqual(self.lib.check(), [])
        # Deterministic: the standard keys first, then the extras sorted.
        header = self.read(res.sidecar).split("---\n")[1]
        keys = [line.split(":")[0] for line in header.splitlines() if not line.startswith(" ")]
        self.assertEqual(keys, ["title", "source", "added", "tags", "summary", "related",
                                "content_type", "fetched", "forge"])

    def test_extra_in_markdown_header_wins_over_existing(self):
        text = "---\ntitle: T\nsource: s\nadded: 2020-01-01\ntags: []\nsummary: ''\nrelated: []\nforge: old\nname: k\n---\nbody\n"
        res = self.lib.file(self.src("n.md", text), "notes-in", extra={"forge": "new-id", "name": "new"})
        self.assertIsNone(res.sidecar)
        meta, body = fm.parse(self.read(res.item))
        self.assertEqual((meta["forge"], meta["name"], body), ("new-id", "new", "body\n"))


class ForgeReservedKeysTest(LibTestCase):
    """L-2: a hand-filed Markdown file must not carry the research gate's provenance keys,
    or it could satisfy a forge record_ingest the gate never fetched."""
    CRAFTED = ("---\ntitle: Crafted\nsource: https://example.org/p\nadded: 2020-01-01\ntags: []\n"
               "summary: ''\nrelated: []\nforge: 2026-10-27_rid\nfetched: '2026-10-27T00:00:00'\n"
               "final_url: https://evil.example/\ncontent_type: text/html\nname: kept\n---\nbody\n")

    def test_file_drops_them_from_an_existing_header(self):
        res = self.lib.file(self.src("crafted.md", self.CRAFTED), "notes-in")
        meta, body = fm.parse(self.read(res.item))
        self.assertEqual(FORGE_RESERVED_KEYS & meta.keys(), set())
        self.assertEqual((meta["name"], meta["source"], body), ("kept", "https://example.org/p", "body\n"))

    def test_extra_still_sets_them(self):
        res = self.lib.file(self.src("crafted.md", self.CRAFTED), "notes-in", extra={"forge": "real-rid"})
        meta = fm.parse(self.read(res.item))[0]
        self.assertEqual(meta["forge"], "real-rid")
        self.assertEqual(FORGE_RESERVED_KEYS & meta.keys(), {"forge"})

    def test_each_key_is_dropped(self):
        for key in sorted(FORGE_RESERVED_KEYS):
            with self.subTest(key=key):
                text = f"---\ntitle: T\n{key}: x\n---\nbody\n"
                res = self.lib.file(self.src("one.md", text), "notes-in")
                self.assertNotIn(key, fm.parse(self.read(res.item))[0])

    def test_inbox_leaves_such_an_item_in_the_inbox(self):
        for key in sorted(FORGE_RESERVED_KEYS):
            with self.subTest(key=key):
                (self.root / "00-inbox" / f"{key}.md").write_text(f"---\ntitle: T\n{key}: x\n---\nbody\n")
        (self.root / "00-inbox" / "paper.pdf").write_bytes(b"%PDF")
        (self.root / "00-inbox" / "paper.pdf.md").write_text("---\ntitle: P\nforge: 2026-10-27_rid\n---\n")
        before = snapshot(self.root / "00-inbox")
        res = self.lib.inbox(default_topic="unsorted")
        self.assertEqual(res.moved, [])
        left = dict(res.left)
        self.assertEqual(set(left), {f"00-inbox/{k}.md" for k in FORGE_RESERVED_KEYS} | {"00-inbox/paper.pdf"})
        for key in FORGE_RESERVED_KEYS:
            self.assertIn(key, left[f"00-inbox/{key}.md"])
            self.assertIn("only the research gate", left[f"00-inbox/{key}.md"])
        self.assertIn("forge", left["00-inbox/paper.pdf"])
        self.assertEqual(snapshot(self.root / "00-inbox"), before)
        self.assertFalse((self.root / "10-sources" / "unsorted").exists() and
                         os.listdir(self.root / "10-sources" / "unsorted"))

    def test_none_and_empty_change_nothing(self):
        a = self.lib.file(self.src("a.pdf", b"x"), "t", title="A")
        b = self.lib.file(self.src("b.pdf", b"x"), "t", title="B", extra={})
        self.assertEqual(set(self.meta(a.sidecar)), set(fm.REQUIRED))
        self.assertEqual(set(self.meta(b.sidecar)), set(fm.REQUIRED))

    def test_value_round_trips(self):
        value = 'quotes " and \\ backslash: colon # hash é 中 [x]'
        res = self.lib.file(self.src("a.pdf", b"x"), "t", extra={"note": value, "empty": ""})
        meta = self.meta(res.sidecar)
        self.assertEqual((meta["note"], meta["empty"]), (value, ""))

    def test_key_rules(self):
        ok = ["a", "forge", "final_url", "x9", "a" + "b" * 31]
        for key in ok:
            with self.subTest(key=key):
                self.lib.file(self.src("ok.pdf", b"x"), "t", extra={key: "v"})
        bad = ["", "Forge", "FORGE", "_x", "9x", "a-b", "a.b", "a b", "a:b", "a" + "b" * 32,
               "é", "forge\n", " forge", "ﬀ", "a​"]
        for key in bad:
            with self.subTest(key=key):
                self.assert_refused({key: "v"}, "key")
        for key in fm.REQUIRED:
            with self.subTest(key=key):
                self.assert_refused({key: "v"}, "standard key")
        self.assert_refused({7: "v"}, "key")

    def test_value_rules(self):
        bad = ["two\nlines", "cr\r", "tab\there", "nul\0", "esc\x1b[31m", "del\x7f", "c1\x85",
               "ls ", "ps ", "bom﻿", "zw​", "bidi‮", "nbsp\xa0",
               "surrogate\udcff", "nonchar￿"]
        for value in bad:
            with self.subTest(value=value):
                self.assert_refused({"note": value}, "note")
        for value in (None, 3, ["a"], b"bytes"):
            with self.subTest(value=value):
                self.assert_refused({"note": value}, "must be text")

    def test_not_a_mapping(self):
        for extra in (["forge", "x"], "forge=x", [("forge", "x")]):
            with self.subTest(extra=extra):
                self.assert_refused(extra, "mapping")

    def assert_refused(self, extra, fragment):
        src = self.src("refused.pdf", b"x")
        before = snapshot(self.root)
        with self.assertRaises(LibraryError) as ctx:
            self.lib.file(src, "refusal-topic", extra=extra)
        self.assertIn(fragment, str(ctx.exception))
        self.assertEqual(snapshot(self.root), before, "nothing is written before extra is checked")


if __name__ == "__main__":
    unittest.main()
