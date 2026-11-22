import unittest

from library import frontmatter as fm


def full(**over):
    meta = {"title": "T", "source": "", "added": "2026-10-26", "tags": [], "summary": "", "related": []}
    meta.update(over)
    return meta


class RoundTripTest(unittest.TestCase):
    def roundtrip(self, meta, body="\nBody text\n"):
        text = fm.render(meta, body)
        parsed, parsed_body = fm.parse(text)
        self.assertEqual(parsed, meta)
        self.assertEqual(parsed_body, body)
        # Writing what we read gives identical bytes.
        self.assertEqual(fm.render(parsed, parsed_body), text)
        return text

    def test_quotes_and_backslashes(self):
        self.roundtrip(full(title='He said "hi" \\o/', summary="it's 'single' and \"double\""))

    def test_colons_hashes_and_yaml_lookalikes(self):
        self.roundtrip(full(title="Part 1: the start # not a comment",
                            source="https://example.org/a?b=c#frag",
                            summary="- looks like a list: [x] {y} &anchor *ref !tag | > %"))

    def test_unicode(self):
        self.roundtrip(full(title="Ünïcödé — 日本語 🎮", summary="emoji 🎉 and a non-breaking\xa0space",
                            tags=["café", "日本"]))

    def test_control_and_line_break_characters(self):
        meta = full(summary="line1\nline2\ttab\rcr \x00 nul \x7f del \x85 nel \u2028 ls \u2029 ps \ufeff bom")
        text = self.roundtrip(meta)
        header = text.split("---", 2)[1]
        self.assertEqual(header.count("\n"), 7, "six keys, each on one line")

    def test_lists(self):
        self.roundtrip(full(tags=["a", "b: c", "with \"quotes\"", "[bracket]"],
                            related=["10-sources/x/2026-10-26_a.pdf"]))

    def test_empty_values_and_extra_keys(self):
        text = self.roundtrip(full(name="my-skill", description="Use when: x"))
        self.assertIn("tags: []\n", text)
        # Required keys first in contract order, extras after, sorted.
        keys = [line.split(":")[0] for line in text.splitlines()[1:9]]
        self.assertEqual(keys, ["title", "source", "added", "tags", "summary", "related", "description", "name"])

    def test_added_is_bare_only_when_a_valid_date(self):
        self.assertIn("added: 2026-10-26\n", fm.dump(full()))
        self.assertIn('added: "yesterday"\n', fm.dump(full(added="yesterday")))
        self.assertEqual(fm.parse(fm.dump(full(added="2026-02-30")))[0]["added"], "2026-02-30")

    def test_body_kept_exactly(self):
        body = "\n# Heading\n\n---\n\nkey: not front-matter\r\n"
        self.roundtrip(full(), body)

    def test_dump_rejects_bad_values(self):
        with self.assertRaises(fm.FrontMatterError):
            fm.dump({"title": 3})
        with self.assertRaises(fm.FrontMatterError):
            fm.dump({"bad key": "x"})
        with self.assertRaises(fm.FrontMatterError):
            fm.dump({"title": "lone \udcff surrogate"})


class ParseTest(unittest.TestCase):
    def test_hand_written_forms(self):
        text = (
            "\ufeff---\r\n"
            "# a comment\r\n"
            "title: Plain title # trailing comment\r\n"
            "source: 'it''s single'\r\n"
            "added: 2026-10-26\r\n"
            "tags: [gpu, \"r o c m\", 'x']\r\n"
            "summary: \"tab\\there \\u00e9 \\x41\"\r\n"
            "related:\r\n"
            "  - a\r\n"
            "  - \"b\"\r\n"
            "\r\n"
            "...\r\n"
            "body\r\n"
        )
        meta, body = fm.parse(text)
        self.assertEqual(meta, {
            "title": "Plain title", "source": "it's single", "added": "2026-10-26",
            "tags": ["gpu", "r o c m", "x"], "summary": "tab\there é A", "related": ["a", "b"],
        })
        self.assertEqual(body, "body\r\n")
        self.assertEqual(fm.validate(meta), [])

    def test_key_with_nothing_is_empty_text(self):
        meta, _ = fm.parse("---\ntitle: x\nsummary:\ntags: []\n---\n")
        self.assertEqual(meta["summary"], "")

    def test_errors_name_the_line(self):
        cases = {
            "no opening": ("title: x\n---\n", 1),
            "no closing": ("---\ntitle: x\n", 2),
            "duplicate": ("---\ntitle: a\ntitle: b\n---\n", 3),
            "nested": ("---\ntitle: a\n  sub: b\n---\n", 3),
            "tab indent": ("---\ntags:\n\t- a\n---\n", 3),
            "orphan item": ("---\n- a\n---\n", 2),
            "unterminated": ('---\ntitle: "abc\n---\n', 2),
            "unterminated single": ("---\ntitle: 'abc\n---\n", 2),
            "bad escape": ('---\ntitle: "\\q"\n---\n', 2),
            "bad hex": ('---\ntitle: "\\u12"\n---\n', 2),
            "block scalar": ("---\nsummary: |\n  text\n---\n", 2),
            "anchor": ("---\ntitle: &a x\n---\n", 2),
            "colon in plain": ("---\ntitle: a: b\n---\n", 2),
            "junk after quote": ('---\ntitle: "a" b\n---\n', 2),
            "no space after colon": ("---\ntitle:x\n---\n", 2),
            "not a key line": ("---\njust text\n---\n", 2),
            "open flow list": ("---\ntags: [a, b\n---\n", 2),
            "empty flow item": ("---\ntags: [a,,b]\n---\n", 2),
            "nested flow": ("---\ntags: [a, [b]]\n---\n", 2),
            "flow junk": ("---\ntags: [a] b\n---\n", 2),
        }
        for label, (text, line) in cases.items():
            with self.subTest(label):
                with self.assertRaises(fm.FrontMatterError) as ctx:
                    fm.parse(text)
                self.assertEqual(ctx.exception.line, line)

    def test_has_header(self):
        self.assertTrue(fm.has_header("---\n"))
        self.assertTrue(fm.has_header("\ufeff---\r\nx"))
        self.assertFalse(fm.has_header("# title\n---\n"))
        self.assertFalse(fm.has_header("----\n"))


class ValidateTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(fm.validate(full()), [])

    def test_problems(self):
        problems = fm.validate({"title": " ", "added": "2026-13-01", "tags": "a", "summary": ["x"],
                                "related": ["", "b"]})
        kinds = sorted(problems)
        self.assertEqual(kinds, sorted([
            ("missing-field", "missing 'source'"),
            ("bad-field", "'title' is empty"),
            ("bad-field", "'added' must be a date YYYY-MM-DD, got '2026-13-01'"),
            ("bad-field", "'tags' must be a list (use [] when empty)"),
            ("bad-field", "'summary' must be text, not a list"),
            ("bad-field", "'related' has an empty item"),
        ]))


if __name__ == "__main__":
    unittest.main()
