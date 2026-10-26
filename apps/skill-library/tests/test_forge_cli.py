import datetime as dt
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from forge.cli import main
from library import Library
from library import frontmatter as fm

APP = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 10, 26, 9, 30, tzinfo=dt.timezone.utc)
RID = "2026-10-26_pdf-summarizer"
URL = "https://example.com/docs/page"


class ForgeCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "lib"
        Library.init(self.root, clock=lambda: NOW)

    def run_cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        code = main(list(argv), clock=lambda: NOW, environ=env if env is not None else {"AI_LIBRARY_ROOT": str(self.root)},
                    stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def write(self, name, text):
        path = self.base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return str(path)

    def edit(self, rel, old, new):
        path = self.root / rel
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def test_root_required(self):
        code, _, err = self.run_cli("status", env={})
        self.assertEqual(code, 2)
        self.assertIn("AI_LIBRARY_ROOT", err)

    def test_full_flow(self):
        code, out, _ = self.run_cli("new", "PDF Summarizer", "--request-file", self.write("req.txt", "Summarize PDFs\n"))
        self.assertEqual(code, 0, out)
        self.assertIn(RID, out)
        self.assertIn("INDEX.md", out)

        searches = self.write("q.tsv", "# comment\n\npdf summarization\tlearn methods\n")
        self.assertEqual(self.run_cli("propose-searches", RID, "--from", searches)[0], 0)
        self.assertEqual(self.run_cli("authorize", "query", RID, "pdf summarization")[:2], (1, "not approved\n"))
        sources = self.write("s.tsv", f"{URL}\tPage\tcovers it\tthe method list\n")
        code, _, err = self.run_cli("propose-sources", RID, "--from", sources)
        self.assertEqual(code, 2)
        self.assertIn("no search is approved", err)

        self.edit(f"35-forge/{RID}/searches.md", "- [ ] pdf summarization", "- [x] pdf summarization")
        self.assertEqual(self.run_cli("authorize", "query", RID, "pdf summarization")[:2], (0, "approved\n"))
        self.assertEqual(self.run_cli("propose-sources", RID, "--from", sources)[0], 0)
        self.assertEqual(self.run_cli("authorize", "url", RID, URL)[0], 1)
        self.edit(f"35-forge/{RID}/sources.md", f"- [ ] `{URL}`", f"- [x] `{URL}`")
        self.assertEqual(self.run_cli("authorize", "url", RID, URL)[0], 0)
        self.assertEqual(self.run_cli("authorize", "url", RID, URL + "/")[0], 1)

        fetched = self.write("fetched.md", fm.render({"title": "Page", "source": URL, "forge": RID}, "\nbody\n"))
        item = Library.open(self.root, clock=lambda: NOW).file(fetched, "pdf", source=URL).item
        code, out, err = self.run_cli("record-ingest", RID, URL, item)
        self.assertEqual(code, 0, err)
        code, _, err = self.run_cli("record-ingest", RID, "https://example.com/other", item)
        self.assertEqual(code, 2)
        self.assertIn("not ticked", err)

        self.write("draft/SKILL.md", "---\nname: pdf-summarizer\n---\n# Summarize\n")
        code, out, err = self.run_cli("draft-skill", RID, "--from", str(self.base / "draft"))
        self.assertEqual(code, 0, err)
        self.assertIn("drafted pdf-summarizer (sha256:", out)
        code, _, err = self.run_cli("decide", RID)
        self.assertEqual(code, 1)
        self.assertIn("pending", err)
        code, out, _ = self.run_cli("status")
        self.assertIn("[awaiting-skill-review]", out)
        code, out, _ = self.run_cli("status", RID)
        self.assertIn("drafted skill pdf-summarizer", out)

        self.edit(f"35-forge/{RID}/review.md", "decision: pending", "decision: deny")
        code, out, _ = self.run_cli("decide", RID)
        self.assertEqual(code, 0)
        self.assertIn("95-shelf/pdf-summarizer", out)
        code, out, _ = self.run_cli("revive", "pdf-summarizer")
        self.assertEqual(code, 0)
        self.edit(f"35-forge/{RID}/review.md", "decision: pending", "decision: approve")
        code, out, _ = self.run_cli("decide", RID)
        self.assertEqual(code, 0)
        self.assertIn("approved: pdf-summarizer -> 30-skills/pdf-summarizer/", out)
        self.assertEqual(self.run_cli("decide", RID)[0], 2)  # second decide refused
        self.assertEqual(self.run_cli("verify", "pdf-summarizer")[0], 0)
        (self.root / "30-skills/pdf-summarizer/SKILL.md").write_text("tampered\n")
        code, out, _ = self.run_cli("verify", "pdf-summarizer")
        self.assertEqual(code, 1)
        self.assertIn("MISMATCH", out)

    def test_bad_tsv_reports_line(self):
        self.run_cli("new", "PDF Summarizer", "--request-file", self.write("req.txt", "x\n"))
        code, _, err = self.run_cli("propose-searches", RID, "--from", self.write("q.tsv", "ok\twhy\nno tab here\n"))
        self.assertEqual(code, 2)
        self.assertIn("line 2", err)

    def test_value_errors_are_one_line_exit_2(self):
        """Minor 3: bad text (e.g. undecodable argv) gives exit 2 and no traceback."""
        code, _, err = self.run_cli("new", "bad \udcff title", "--request-file", self.write("r.txt", "x\n"))
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error:") and err.count("\n") == 1, err)
        self.run_cli("new", "PDF Summarizer", "--request-file", self.write("req.txt", "x\n"))
        code, out, err = self.run_cli("authorize", "query", RID, "\udcff")
        self.assertEqual((code, out), (1, "not approved\n"))
        with mock.patch("forge.core.Forge.decide", side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")):
            code, _, err = self.run_cli("decide", RID)
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error:") and err.count("\n") == 1, err)

    def test_module_help(self):
        res = subprocess.run([sys.executable, "-m", "forge", "--help"], cwd=APP, capture_output=True,
                             text=True, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        for cmd in ("new", "propose-searches", "propose-sources", "record-ingest", "draft-skill",
                    "decide", "revive", "status", "verify", "authorize"):
            self.assertIn(cmd, res.stdout)


if __name__ == "__main__":
    unittest.main()
