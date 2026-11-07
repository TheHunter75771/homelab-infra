import json
import os
import tempfile
import unittest

from finance_digest import notion

from . import fakes

PAGE = "0123456789abcdef0123456789abcdef"


class NotionUnitTest(unittest.TestCase):
    def setUp(self):
        self.fake = fakes.FakeNotion().__enter__()
        self.addCleanup(self.fake.__exit__, None, None, None)

    def test_chunks_of_100_blocks(self):
        children = [notion._block("paragraph", f"p{i}") for i in range(230)]
        sent = notion.append("tok", PAGE, children, timeout=2, connect=self.fake.connect)
        self.assertEqual(sent, 3)
        self.assertEqual([len(r[3]["children"]) for r in self.fake.requests], [100, 100, 30])
        self.assertEqual({r[0] for r in self.fake.requests}, {"PATCH"})

    def test_long_tables_and_text_fit_notion_limits(self):
        rows = [[f"r{i}", "x" * 5000] for i in range(150)]
        blocks = notion.to_blocks("2030-03-20", [("table", "cap", ["a", "b"], rows)], None, "skipped", "AI", "gen")
        table = next(b for b in blocks if b["type"] == "table")
        self.assertEqual(len(table["table"]["children"]), 100)  # header + 99 rows
        cell = table["table"]["children"][1]["table_row"]["cells"][1]
        self.assertTrue(all(len(t["text"]["content"]) <= 2000 for t in cell))
        self.assertEqual("".join(t["text"]["content"] for t in cell), "x" * 5000)
        self.assertTrue(any("51 more rows" in b.get("paragraph", {}).get("rich_text", [{}])[0].get("text", {}).get("content", "")
                            for b in blocks if b["type"] == "paragraph"))

    def test_load_config(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "n.json")
        self.assertIsNone(notion.load_config(p))
        with open(p, "w") as fh:
            json.dump({"token": "secret_abc", "page_id": "01234567-89AB-cdef-0123-456789abcdef"}, fh)
        self.assertEqual(notion.load_config(p), ("secret_abc", PAGE))
        os.symlink(p, os.path.join(d, "link.json"))
        with self.assertRaises(notion.NotionError):
            notion.load_config(os.path.join(d, "link.json"))
        for bad in ({"token": "secret_abc\r\nX: y", "page_id": PAGE}, {"token": "", "page_id": PAGE},
                    {"token": "secret_abc", "page_id": PAGE + "/../x"}, {"token": 5, "page_id": PAGE}):
            with open(p, "w") as fh:
                json.dump(bad, fh)
            with self.assertRaises(notion.NotionError) as cm:
                notion.load_config(p)
            self.assertNotIn("secret_abc", str(cm.exception))

    def test_bad_page_id_never_sent(self):
        with self.assertRaises(notion.NotionError):
            notion.append("tok", "../../v1/users", [], connect=self.fake.connect)
        self.assertEqual(self.fake.requests, [])


if __name__ == "__main__":
    unittest.main()
