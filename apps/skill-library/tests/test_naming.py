import unittest

from library import naming


class NamingTest(unittest.TestCase):
    def test_slugify(self):
        cases = {
            "My Paper: v2": "my-paper-v2",
            "Ünïcödé Café": "unicode-cafe",
            "  --Hello__World!!  ": "hello-world",
            "../../etc/passwd": "etc-passwd",
            "日本語": "untitled",
            "": "untitled",
        }
        for title, slug in cases.items():
            with self.subTest(title):
                self.assertEqual(naming.slugify(title), slug)

    def test_slugify_truncates_at_a_word(self):
        slug = naming.slugify("word " * 40)
        self.assertLessEqual(len(slug), naming.MAX_SLUG)
        self.assertFalse(slug.endswith("-"))
        self.assertTrue(slug.endswith("word"))

    def test_kebab_names(self):
        for good in ("a", "home-network", "x1-2", "a" * 48):
            self.assertTrue(naming.is_kebab_name(good), good)
        for bad in ("", "A", "Papers", "a_b", "a--b", "-a", "a-", "a b", "../x", "a/b", "a" * 49, "con", "lpt1", "é"):
            self.assertFalse(naming.is_kebab_name(bad), bad)

    def test_item_names(self):
        self.assertEqual(naming.parse_item_name("2026-10-26_my-paper.pdf"), ("2026-10-26", "my-paper", "pdf"))
        for bad in ("2026-10-26_My-paper.pdf", "2026-13-01_x.md", "2026-10-26-x.md", "2026-10-26_x",
                    "2026-10-26_x.PDF", "2026-10-26_x.pdf.md", "2026-10-26_a--b.md", "x.md"):
            self.assertIsNone(naming.parse_item_name(bad), bad)
        self.assertEqual(naming.item_name("2026-10-26", "x", "pdf"), "2026-10-26_x.pdf")
        self.assertEqual(naming.item_name("2026-10-26", "x", "pdf", 3), "2026-10-26_x-3.pdf")
        self.assertEqual(naming.numbered("a.b.pdf", 2, is_dir=False), "a.b-2.pdf")
        self.assertEqual(naming.numbered("papers", 2, is_dir=True), "papers-2")

    def test_extensions_and_titles(self):
        self.assertEqual(naming.normalize_ext("A.PDF"), "pdf")
        self.assertEqual(naming.normalize_ext("x.tar.gz"), "gz")
        self.assertEqual(naming.normalize_ext("README"), "bin")
        self.assertEqual(naming.normalize_ext(".bashrc"), "bin")
        self.assertEqual(naming.normalize_ext("x.~!"), "bin")
        self.assertEqual(naming.title_from_filename("My_Paper  v2.pdf"), "My Paper v2")

    def test_sizes(self):
        self.assertEqual(naming.parse_size("500GB"), 500 * 10**9)
        self.assertEqual(naming.parse_size("500 g"), 500 * 10**9)
        self.assertEqual(naming.parse_size("2GiB"), 2 * 1024**3)
        self.assertEqual(naming.parse_size("1.5 KB"), 1500)
        self.assertEqual(naming.parse_size("4096"), 4096)
        for bad in ("", "abc", "-5GB", "0", "5 PB"):
            with self.assertRaises(ValueError):
                naming.parse_size(bad)
        self.assertEqual(naming.format_size(1_234_567), "1.2 MB")
        self.assertEqual(naming.format_size(12), "12 B")


if __name__ == "__main__":
    unittest.main()
