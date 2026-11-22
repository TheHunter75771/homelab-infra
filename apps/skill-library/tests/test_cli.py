import datetime as dt
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from library.cli import main

APP = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 10, 26, 9, 30, tzinfo=dt.timezone.utc)


class CliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "lib"

    def run_cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        code = main(list(argv), clock=lambda: NOW, environ=env or {}, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def init(self):
        code, out, _ = self.run_cli("--root", str(self.root), "init")
        self.assertEqual(code, 0)
        return out

    def test_root_is_required_and_never_defaulted(self):
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 2)
        self.assertIn("AI_LIBRARY_ROOT", err)

    def test_env_root_and_options_after_the_command(self):
        self.assertIn("created README.md", self.init())
        code, out, _ = self.run_cli("check", env={"AI_LIBRARY_ROOT": str(self.root)})
        self.assertEqual((code, out), (0, "check: ok\n"))
        code, out, _ = self.run_cli("check", "--root", str(self.root))
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("--root", str(self.root), "init")
        self.assertEqual((code, out.strip()), (0, f"library at {self.root} already complete"))

    def test_not_a_library(self):
        code, _, err = self.run_cli("--root", str(self.base), "index")
        self.assertEqual(code, 2)
        self.assertIn("not an initialized AI library", err)

    def test_file_then_check_and_index(self):
        self.init()
        src = self.base / "Report Q3.pdf"
        src.write_bytes(b"pdf")
        code, out, _ = self.run_cli("--root", str(self.root), "file", str(src), "--topic", "reports",
                                    "--tags", "a, b,,", "--summary", "Quarter 3")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "filed 10-sources/reports/2026-10-26_report-q3.pdf"
            " (sidecar 10-sources/reports/2026-10-26_report-q3.pdf.md)",
            "INDEX.md updated (2 items)",
        ])
        self.assertIn("tags:\n  - \"a\"\n  - \"b\"\n",
                      (self.root / "10-sources/reports/2026-10-26_report-q3.pdf.md").read_text())
        code, out, _ = self.run_cli("--root", str(self.root), "index")
        self.assertEqual((code, out), (0, "INDEX.md unchanged (2 items)\n"))

    def test_bad_topic_is_refused(self):
        self.init()
        src = self.base / "x.pdf"
        src.write_bytes(b"x")
        code, _, err = self.run_cli("--root", str(self.root), "file", str(src), "--topic", "My Topic")
        self.assertEqual(code, 2)
        self.assertIn("is not a valid name", err)

    def test_check_exit_code(self):
        self.init()
        (self.root / "20-notes" / "t").mkdir()
        (self.root / "20-notes" / "t" / "Notes.md").write_text("x")
        code, out, _ = self.run_cli("--root", str(self.root), "check")
        self.assertEqual(code, 1)
        self.assertIn("bad-name\t20-notes/t/Notes.md", out)
        self.assertTrue(out.endswith("check: 2 problem(s)\n"))

    def test_cap_exit_codes(self):
        self.init()
        usage = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        for cap, code_expected, word in ((usage * 10, 0, "ok"), (usage + 1, 1, "warn"), (usage, 3, "full")):
            code, out, _ = self.run_cli("--root", str(self.root), "--cap", str(cap), "cap")
            self.assertEqual(code, code_expected, out)
            self.assertTrue(out.strip().endswith(word), out)
        code, out, _ = self.run_cli("--root", str(self.root), "cap", env={"AI_LIBRARY_CAP": f"{usage}"})
        self.assertEqual(code, 3)
        code, _, err = self.run_cli("--root", str(self.root), "--cap", "lots", "cap")
        self.assertEqual(code, 2)
        self.assertIn("not a size", err)

    def test_file_refused_at_cap(self):
        self.init()
        src = self.base / "big.bin"
        src.write_bytes(b"x" * 5000)
        code, _, err = self.run_cli("--root", str(self.root), "--cap", "1KB", "file", str(src), "--topic", "t")
        self.assertEqual(code, 2)
        self.assertIn("refusing", err)

    def test_inbox(self):
        self.init()
        code, _, err = self.run_cli("--root", str(self.root), "inbox")
        self.assertEqual(code, 2)
        self.assertIn("--default-topic", err)
        (self.root / "00-inbox" / "a.pdf").write_bytes(b"a")
        (self.root / "00-inbox" / "b.zip").write_bytes(b"b")
        topic_map = self.base / "map.txt"
        topic_map.write_text("papers *.pdf\n")
        code, out, _ = self.run_cli("--root", str(self.root), "inbox", "--topic-map", str(topic_map))
        self.assertEqual(code, 1, "something was left behind")
        self.assertIn("moved 00-inbox/a.pdf -> 10-sources/papers/2026-10-26_a.pdf", out)
        self.assertIn("left 00-inbox/b.zip: no topic", out)
        code, out, _ = self.run_cli("--root", str(self.root), "inbox", "--default-topic", "unsorted")
        self.assertEqual(code, 0)
        code, _, err = self.run_cli("--root", str(self.root), "inbox", "--topic-map", str(self.base / "nope"))
        self.assertEqual(code, 2)
        self.assertIn("cannot read the topic map", err)

    def test_archive(self):
        self.init()
        (self.root / "20-notes" / "t").mkdir()
        (self.root / "20-notes" / "t" / "2026-10-26_x.md").write_text("x")
        code, out, _ = self.run_cli("--root", str(self.root), "archive", "20-notes/t/2026-10-26_x.md")
        self.assertEqual(code, 0)
        self.assertIn("archived 20-notes/t/2026-10-26_x.md -> 90-archive/20-notes/t/2026-10-26_x.md", out)
        code, _, err = self.run_cli("--root", str(self.root), "archive", "../escape")
        self.assertEqual(code, 2)
        self.assertIn("'..' is not allowed", err)

    def test_module_entry_point(self):
        proc = subprocess.run([sys.executable, "-m", "library", "--help"], cwd=APP,
                              capture_output=True, text=True, env={**os.environ, "AI_LIBRARY_ROOT": ""})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for command in ("init", "file", "inbox", "index", "cap", "archive", "check"):
            self.assertIn(command, proc.stdout)


if __name__ == "__main__":
    unittest.main()
