"""Unit tests for role ai_stack's two in-container scripts (docs/CONTRACTS.md §12.18b). Stdlib only.

  python3 -m unittest discover -s roles/ai_stack/tests -p 'test_*.py' -v

files/webui_extraction_config.py runs the way the role runs it (`python3 - '<expected>'`, script on stdin)
against throwaway sqlite databases shaped like Open WebUI 0.12.4's `config` table; one in-process test spies
on sqlite3.connect. files/ocr_check.py runs in-process against a fake Tika (http.server on 127.0.0.1); the
real Pillow rendering is tested only where Pillow is installed (the Open WebUI image always has it).
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

FILES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "files")
CHECKER = os.path.join(FILES, "webui_extraction_config.py")
OCR = os.path.join(FILES, "ocr_check.py")

MEDIA = ["image/png", "image/jpeg", "image/tiff", "image/bmp", "image/gif"]
# What vars/main.yml's ai_stack_webui_extraction_expected renders to.
EXPECTED = {
    "rag.content_extraction_engine": "tika",
    "rag.tika_server_url": "http://tika:9998",
    "rag.tika_server_version": "3",
    "rag.file.max_size": 25,
    "rag.content_extraction.supported_media_mime_types": MEDIA,
}
# What compose.yaml gives the open-webui container.
ENV_GOOD = {
    "CONTENT_EXTRACTION_ENGINE": "tika",
    "TIKA_SERVER_URL": "http://tika:9998",
    "TIKA_SERVER_VERSION": "3",
    "RAG_FILE_MAX_SIZE": "25",
    "CONTENT_EXTRACTION_SUPPORTED_MEDIA_MIME_TYPES": ",".join(MEDIA),
}
# Open WebUI's first-start seed on ai-box, before these variables existed.
STALE = {
    "rag.content_extraction_engine": "",
    "rag.tika_server_url": "http://tika:9998",
    "rag.tika_server_version": "3",
    "rag.file.max_size": None,
    "rag.content_extraction.supported_media_mime_types": None,
}
# §12.22 search settings: what vars/main.yml's ai_stack_webui_search_expected + _report render to.
SEARCH = {
    "rag.embedding_engine": "ollama",
    "rag.embedding_model": "embed",
    "rag.ollama.base_url": "http://gpu-broker:11434",
    "rag.top_k": 8,
    "rag.enable_hybrid_search": False,
    "rag.embedding_batch_size": 16,
    "rag.embedding_concurrent_requests": 2,
    "task.model.default": "assistant",
    "rag.reranking_model": "",
}
# The admin's saved settings (Admin UI) as Open WebUI stores them.
SEARCH_ROWS = {**SEARCH, "rag.top_k_reranker": 8}
# What compose gives open-webui for search.
SEARCH_ENV = {
    "RAG_EMBEDDING_ENGINE": "ollama",
    "RAG_EMBEDDING_MODEL": "embed",
    "RAG_OLLAMA_BASE_URL": "http://gpu-broker:11434",
    "RAG_TOP_K": "8",
    "RAG_TOP_K_RERANKER": "8",
    "ENABLE_RAG_HYBRID_SEARCH": "false",
    "RAG_EMBEDDING_BATCH_SIZE": "16",
    "RAG_EMBEDDING_CONCURRENT_REQUESTS": "2",
    "TASK_MODEL": "assistant",
    "RAG_RERANKING_MODEL": "",
}
SCRUB = ("DATABASE_URL", "ENABLE_PERSISTENT_CONFIG", "OLLAMA_BASE_URL", *ENV_GOOD, *SEARCH_ENV)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CheckerBase(unittest.TestCase):
    """Throwaway databases and the checker run the way the role runs it (helpers only, no tests)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.db = os.path.join(self.dir, "webui.db")

    def tearDown(self):
        os.chmod(self.dir, 0o755)
        self.tmp.cleanup()

    def make_db(self, rows, wal=False):
        con = sqlite3.connect(self.db)
        if wal:
            con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value JSON NOT NULL, updated_at BIGINT)")
        con.execute("CREATE TABLE other (x)")
        con.executemany("INSERT INTO config VALUES (?, ?, 0)", [(k, json.dumps(v)) for k, v in rows.items()])
        con.commit()
        return con

    def run_checker(self, env=None, data_dir=None, timeout=60, expected=None):
        """As the role does: the script on stdin, the expected values as the only argument."""
        full = {k: v for k, v in os.environ.items() if k not in SCRUB}
        full["DATA_DIR"] = data_dir or self.dir
        full.update(ENV_GOOD if env is None else env)
        with open(CHECKER) as f:
            src = f.read()
        start = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-", json.dumps(EXPECTED if expected is None else expected)],
            input=src,
            check=False,
            capture_output=True,
            text=True,
            env=full,
            timeout=timeout,
        )
        self.elapsed = time.monotonic() - start
        out = json.loads(proc.stdout) if proc.stdout.strip() else None
        return proc.returncode, out, proc.stderr

    @staticmethod
    def bad(out):
        return sorted(k for k, v in out["settings"].items() if not v["ok"])

    def digest(self):
        state = {}
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = self.db + suffix
            if os.path.exists(path):
                with open(path, "rb") as f:
                    state[suffix or "db"] = (hashlib.sha256(f.read()).hexdigest(), os.stat(path).st_mtime_ns)
        return state

class CheckerTest(CheckerBase):
    # --- 1-5: values ----------------------------------------------------------------------------------
    def test_01_all_rows_correct(self):
        self.make_db(EXPECTED).close()
        rc, out, _ = self.run_checker()
        self.assertEqual((rc, out["ok"], out["source"], self.bad(out)), (0, True, "database", []))

    def test_02_stale_first_start_seed_is_a_mismatch(self):
        self.make_db(STALE).close()
        rc, out, _ = self.run_checker()
        self.assertEqual(rc, 1)
        self.assertEqual(
            self.bad(out),
            [
                "rag.content_extraction.supported_media_mime_types",
                "rag.content_extraction_engine",
                "rag.file.max_size",
            ],
        )

    def test_03_no_rows_means_the_environment_applies(self):
        self.make_db({}).close()
        rc, out, _ = self.run_checker()
        self.assertEqual((rc, self.bad(out)), (0, []))
        self.assertEqual({v["from"] for v in out["settings"].values()}, {"environment"})

    def test_03b_no_rows_and_no_environment(self):
        self.make_db({}).close()
        rc, out, _ = self.run_checker(env={})
        self.assertEqual(rc, 1)
        # An unset Tika version means 3.x in Open WebUI, so only that one still matches.
        self.assertEqual(
            self.bad(out),
            [
                "rag.content_extraction.supported_media_mime_types",
                "rag.content_extraction_engine",
                "rag.file.max_size",
                "rag.tika_server_url",
            ],
        )

    def test_04_admin_panel_variants_are_accepted(self):
        self.make_db(
            {
                "rag.content_extraction_engine": "tika",
                "rag.tika_server_url": "http://tika:9998/",
                "rag.tika_server_version": "3",
                "rag.file.max_size": "25",
                "rag.content_extraction.supported_media_mime_types": ["image/*"],
            }
        ).close()
        rc, out, _ = self.run_checker()
        self.assertEqual((rc, self.bad(out)), (0, []))

    def test_04b_comma_string_with_extras_and_unset_version_are_accepted(self):
        self.make_db(
            {
                **EXPECTED,
                "rag.tika_server_version": None,
                "rag.content_extraction.supported_media_mime_types": " image/png, image/JPEG,image/tiff,"
                "image/bmp,image/gif, image/webp",
            }
        ).close()
        rc, out, _ = self.run_checker()
        self.assertEqual((rc, self.bad(out)), (0, []))

    def test_04c_engine_is_compared_exactly(self):
        # Open WebUI's loader tests `engine == 'tika'`: "Tika" would silently fall back to the default loader.
        self.make_db({**EXPECTED, "rag.content_extraction_engine": "Tika"}).close()
        rc, out, _ = self.run_checker()
        self.assertEqual((rc, self.bad(out)), (1, ["rag.content_extraction_engine"]))

    def test_05_all_wrong(self):
        self.make_db(
            {
                "rag.content_extraction_engine": "tika",
                "rag.tika_server_url": "http://evil:9998",
                "rag.tika_server_version": "4",
                "rag.file.max_size": 50,
                "rag.content_extraction.supported_media_mime_types": ["image/png"],
            }
        ).close()
        rc, out, _ = self.run_checker()
        self.assertEqual(rc, 1)
        self.assertEqual(
            self.bad(out),
            [
                "rag.content_extraction.supported_media_mime_types",
                "rag.file.max_size",
                "rag.tika_server_url",
                "rag.tika_server_version",
            ],
        )

    def test_05b_persistence_off_reads_the_environment_not_the_rows(self):
        self.make_db(STALE).close()
        rc, out, _ = self.run_checker(env={**ENV_GOOD, "ENABLE_PERSISTENT_CONFIG": "false"})
        self.assertEqual((rc, out["source"].startswith("environment"), self.bad(out)), (0, True, []))

    # --- 6-9: read-only ----------------------------------------------------------------------------------
    def test_06_never_writes_rollback_journal_db(self):
        self.make_db(EXPECTED).close()
        before, listing = self.digest(), sorted(os.listdir(self.dir))
        rc, _, _ = self.run_checker()
        self.assertEqual(rc, 0)
        self.assertEqual(self.digest(), before)
        self.assertEqual(sorted(os.listdir(self.dir)), listing)

    def test_06b_opens_read_only(self):
        # In-process spy: the connection is a sqlite URI with mode=ro (mode=rw/rwc would read the same rows).
        self.make_db(EXPECTED).close()
        checker = load(CHECKER, "checker_spy")
        calls = []
        real = sqlite3.connect

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return real(*args, **kwargs)

        env = {k: v for k, v in os.environ.items() if k not in SCRUB}
        env.update(ENV_GOOD, DATA_DIR=self.dir)
        with (
            mock.patch.object(checker.sqlite3, "connect", spy),
            mock.patch.dict(os.environ, env, clear=True),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            rc = checker.main(["-", json.dumps(EXPECTED)])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(len(calls), 1)
        (uri,), kwargs = calls[0]
        self.assertEqual(uri, f"file:{self.db}?mode=ro")
        self.assertIs(kwargs.get("uri"), True)
        self.assertLessEqual(kwargs.get("timeout", 5.0), 10)

    def test_07_wal_with_an_open_writer(self):
        con = self.make_db(EXPECTED, wal=True)
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE config SET value = '\"x\"' WHERE key = 'rag.tika_server_url'")
        before = self.digest()
        try:
            rc, out, _ = self.run_checker()
            after = self.digest()
        finally:
            con.rollback()
            con.close()
        # Reads the committed values without waiting; the database and its WAL are untouched (only the
        # shared-memory index may change, which is how any WAL reader works).
        self.assertEqual((rc, self.bad(out)), (0, []))
        self.assertLess(self.elapsed, 4)
        self.assertEqual(before.get("db"), after.get("db"))
        self.assertEqual(before.get("-wal"), after.get("-wal"))

    def test_08_exclusive_lock_is_unsupported_within_the_timeout(self):
        con = self.make_db(EXPECTED)
        con.isolation_level = None
        con.execute("BEGIN EXCLUSIVE")
        try:
            rc, out, _ = self.run_checker()
        finally:
            con.execute("ROLLBACK")
            con.close()
        self.assertEqual((rc, out["ok"], out["source"]), (2, False, "unsupported"))
        self.assertIn("locked", out["error"])
        self.assertLess(self.elapsed, 15)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file modes")
    def test_09_read_only_files(self):
        self.make_db(EXPECTED).close()
        os.chmod(self.db, 0o444)
        os.chmod(self.dir, 0o555)
        rc, out, err = self.run_checker()
        self.assertEqual((rc, self.bad(out)), (0, []), err)

    # --- 10-13: can't tell ---------------------------------------------------------------------------
    def test_10_no_config_table(self):
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE x (y)")
        con.commit()
        con.close()
        rc, out, _ = self.run_checker()
        self.assertEqual((rc, out["source"]), (2, "unsupported"))
        self.assertIn("no such table", out["error"])

    def test_11_unreadable_json_value(self):
        con = self.make_db(EXPECTED)
        con.execute("UPDATE config SET value = '{bad' WHERE key = 'rag.file.max_size'")
        con.commit()
        con.close()
        rc, out, _ = self.run_checker()
        self.assertEqual((rc, out["source"]), (2, "unsupported"))

    def test_11b_not_a_database(self):
        with open(self.db, "wb") as f:
            f.write(b"this is not sqlite" * 100)
        rc, out, _ = self.run_checker()
        self.assertEqual((rc, out["source"]), (2, "unsupported"))

    def test_12_non_sqlite_database_url(self):
        rc, out, _ = self.run_checker(env={**ENV_GOOD, "DATABASE_URL": "postgresql://x"})
        self.assertEqual((rc, out["source"]), (2, "unsupported"))

    def test_13_no_database(self):
        rc, out, _ = self.run_checker(data_dir=os.path.join(self.dir, "missing"))
        self.assertEqual((rc, out["source"]), (2, "unsupported"))
        self.assertIn("no database", out["error"])


class SearchCheckerTest(CheckerBase):
    """§12.22: the same checker with the search keys (tasks/verify_search.yml)."""

    def search(self, rows=None, env=None):
        if rows is not None:
            self.make_db(rows).close()
        return self.run_checker(env=SEARCH_ENV if env is None else env, expected=SEARCH)

    def test_s1_saved_settings_match(self):
        rc, out, _ = self.search(SEARCH_ROWS)
        self.assertEqual((rc, self.bad(out)), (0, []))
        self.assertEqual(out["settings"]["rag.top_k"]["passages"], 8)

    def test_s2_live_seed_before_noah_saves(self):
        # The live database (§12.22 "Why"): in-process MiniLM, top_k 3, hybrid off, no task model.
        rows = {
            "rag.embedding_engine": "",
            "rag.embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            "rag.ollama.base_url": "http://gpu-broker:11434",
            "rag.top_k": 3,
            "rag.top_k_reranker": 3,
            "rag.enable_hybrid_search": False,
            "rag.embedding_batch_size": 1,
            "rag.embedding_concurrent_requests": 0,
            "task.model.default": "",
            "rag.reranking_model": "",
        }
        rc, out, _ = self.search(rows)
        self.assertEqual(rc, 1)
        # Everything but the Ollama URL (already the broker), hybrid (off) and the reranker (none), as wanted.
        same = ("rag.ollama.base_url", "rag.enable_hybrid_search", "rag.reranking_model")
        want = sorted(k for k in SEARCH if k not in same)
        self.assertEqual(self.bad(out), want)

    def test_s3_hybrid_keeps_min_of_top_k_and_reranker(self):
        # If hybrid gets turned on anyway: Top K 8 with Top K Reranker left at 3 keeps only 3 passages.
        rc, out, _ = self.search({**SEARCH_ROWS, "rag.top_k_reranker": 3, "rag.enable_hybrid_search": True})
        self.assertEqual(rc, 1)
        self.assertEqual(self.bad(out), ["rag.enable_hybrid_search", "rag.top_k"])
        entry = out["settings"]["rag.top_k"]
        self.assertEqual((entry["effective"], entry["top_k_reranker"], entry["passages"], entry["hybrid"]), (8, 3, 3, True))

    def test_s4_reranker_ignored_without_hybrid(self):
        # Hybrid off (R10-1): top_k alone counts, whatever top_k_reranker says.
        rows = {**SEARCH_ROWS, "rag.top_k_reranker": 3, "rag.enable_hybrid_search": False}
        rc, out, _ = self.search(rows)
        self.assertEqual((rc, self.bad(out)), (0, []))
        self.assertEqual(out["settings"]["rag.top_k"]["passages"], 8)

    def test_s4b_top_k_alone_below_the_minimum(self):
        rc, out, _ = self.search({**SEARCH_ROWS, "rag.top_k": 7})
        self.assertEqual((rc, self.bad(out)), (1, ["rag.top_k"]))

    def test_s5_more_passages_than_the_minimum_are_fine(self):
        rc, out, _ = self.search({**SEARCH_ROWS, "rag.top_k": 12, "rag.top_k_reranker": 10})
        self.assertEqual((rc, self.bad(out)), (0, []))
        self.assertEqual(out["settings"]["rag.top_k"]["passages"], 12)

    def test_s6_admin_ui_variants(self):
        # A trailing slash on the URL and numbers saved as strings are what Open WebUI accepts too.
        rows = {**SEARCH_ROWS, "rag.ollama.base_url": "http://gpu-broker:11434/", "rag.top_k": "8",
                "rag.top_k_reranker": "8", "rag.embedding_batch_size": "16", "rag.embedding_concurrent_requests": "2"}
        rc, out, _ = self.search(rows)
        self.assertEqual((rc, self.bad(out)), (0, []))

    def test_s7_exact_names(self):
        rows = {**SEARCH_ROWS, "rag.embedding_engine": "Ollama", "rag.embedding_model": "nomic-embed-text:v1.5"}
        rc, out, _ = self.search(rows)
        self.assertEqual((rc, self.bad(out)), (1, ["rag.embedding_engine", "rag.embedding_model"]))

    def test_s8_no_rows_fresh_database_uses_the_compose_env(self):
        rc, out, _ = self.search({})
        self.assertEqual((rc, self.bad(out)), (0, []))
        self.assertEqual({v["from"] for v in out["settings"].values()}, {"environment"})

    def test_s9_no_rows_no_env_uses_open_webui_defaults(self):
        rc, out, _ = self.search({}, env={})
        self.assertEqual(rc, 1)
        settings = out["settings"]
        self.assertEqual(settings["rag.embedding_engine"]["effective"], "")
        self.assertEqual(settings["rag.embedding_model"]["effective"], "sentence-transformers/all-MiniLM-L6-v2")
        self.assertEqual(settings["rag.top_k"]["effective"], 3)
        self.assertEqual(settings["rag.enable_hybrid_search"]["effective"], False)
        self.assertEqual(settings["rag.embedding_batch_size"]["effective"], 1)
        self.assertEqual(settings["rag.embedding_concurrent_requests"]["effective"], 0)
        self.assertEqual(settings["rag.ollama.base_url"]["effective"], "http://localhost:11434")

    def test_s10_read_only_and_locked(self):
        self.make_db(SEARCH_ROWS).close()
        before = self.digest()
        rc, _, _ = self.search()
        self.assertEqual((rc, self.digest()), (0, before))
        con = sqlite3.connect(self.db)
        con.isolation_level = None
        con.execute("BEGIN EXCLUSIVE")
        try:
            rc, out, _ = self.search()
        finally:
            con.execute("ROLLBACK")
            con.close()
        self.assertEqual((rc, out["source"]), (2, "unsupported"))

    def test_s12_reranker_expected(self):
        # §12.30: the expected reranker, exactly; "" and unset both mean none.
        e3 = {**SEARCH, "rag.enable_hybrid_search": True, "rag.reranking_model": "BAAI/bge-reranker-base"}
        self.make_db({**SEARCH_ROWS, "rag.enable_hybrid_search": True, "rag.reranking_model": "BAAI/bge-reranker-base"}).close()
        rc, out, _ = self.run_checker(env=SEARCH_ENV, expected=e3)
        self.assertEqual((rc, self.bad(out)), (0, []))

    def test_s12b_reranker_other_or_missing(self):
        e3 = {**SEARCH, "rag.enable_hybrid_search": True, "rag.reranking_model": "BAAI/bge-reranker-base"}
        self.make_db({**SEARCH_ROWS, "rag.enable_hybrid_search": True,
                      "rag.reranking_model": "cross-encoder/ms-marco-MiniLM-L6-v2"}).close()
        rc, out, _ = self.run_checker(env=SEARCH_ENV, expected=e3)
        self.assertEqual((rc, self.bad(out)), (1, ["rag.reranking_model"]))

    def test_s12d_reranker_old_minilm_name(self):
        # R16-3: the Admin UI saved under the old name (Hugging Face redirects it; the offline cache doesn't).
        e2 = {**SEARCH, "rag.enable_hybrid_search": True, "rag.reranking_model": "cross-encoder/ms-marco-MiniLM-L6-v2"}
        self.make_db({**SEARCH_ROWS, "rag.enable_hybrid_search": True,
                      "rag.reranking_model": "cross-encoder/ms-marco-MiniLM-L-6-v2"}).close()
        rc, out, _ = self.run_checker(env=SEARCH_ENV, expected=e2)
        self.assertEqual((rc, self.bad(out)), (1, ["rag.reranking_model"]))

    def test_s12c_no_reranker_null_or_empty(self):
        self.make_db({**SEARCH_ROWS, "rag.reranking_model": None}).close()
        rc, out, _ = self.search()
        self.assertEqual((rc, self.bad(out)), (0, []))

    def test_s11_extraction_keys_unchanged(self):
        # The search keys don't change the extraction check (§12.18b): missing rows and env still mismatch.
        self.make_db({}).close()
        rc, out, _ = self.run_checker(env={})
        self.assertEqual(out["settings"]["rag.tika_server_url"]["effective"], None)
        self.assertEqual(rc, 1)


class FakeTika:
    """127.0.0.1 HTTP server that records every PUT and answers like Tika 3.x (or not, per `mode`)."""

    def __init__(self):
        self.mode = "echo"
        self.words = ""
        self.seen = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_PUT(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                fake.seen.append((self.path, self.headers.get("Content-Type"), body))
                if fake.mode == "500":
                    self.send_response(500)
                    self.end_headers()
                    return
                text = fake.words.rsplit(" ", 1)[0] if fake.mode == "drop-last" else fake.words
                if self.path == "/tika":
                    out = text.encode()
                elif fake.mode == "garbage":
                    out = b"<html>not json</html>"
                else:
                    parsed = ["org.apache.tika.parser.DefaultParser", "org.apache.tika.parser.ocr.TesseractOCRParser"]
                    out = json.dumps({"X-TIKA:content": "\n" + text + "\n", "X-TIKA:Parsed-By": parsed}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class OcrCheckTest(unittest.TestCase):
    PNG, PDF = b"\x89PNG\r\n\x1a\nfake", b"%PDF-1.4 fake"

    @classmethod
    def setUpClass(cls):
        cls.tika = FakeTika()

    @classmethod
    def tearDownClass(cls):
        cls.tika.close()

    def setUp(self):
        self.ocr = load(OCR, "ocr_check_under_test")
        self.tika.seen.clear()

    def run_ocr(self, mode, url=None):
        self.tika.mode = mode

        def fake_render(words):
            self.tika.words = " ".join(words)
            return self.PNG, self.PDF

        with mock.patch.object(self.ocr, "render", fake_render), contextlib.redirect_stdout(io.StringIO()) as out:
            rc = self.ocr.main(["-", url or self.tika.url + "/"])
        return rc, json.loads(out.getvalue())

    def test_all_words_back(self):
        rc, out = self.run_ocr("echo")
        self.assertEqual((rc, out["ok"]), (0, True))
        self.assertEqual([c["ok"] for c in out["checks"]], [True, True, True])
        self.assertEqual(len(out["words"]), 4)
        self.assertRegex(out["words"][3], r"^[2-9]{5}$")
        # The requests Open WebUI's TikaLoader makes: /tika/text with NO Content-Type (Tika detects the type;
        # urllib would add application/x-www-form-urlencoded and Tika would return nothing).
        self.assertEqual(
            [(p, ct, body) for p, ct, body in self.tika.seen],
            [("/tika", "image/png", self.PNG), ("/tika/text", None, self.PNG), ("/tika/text", None, self.PDF)],
        )
        self.assertEqual(out["checks"][1]["parsed_by"], ["DefaultParser", "TesseractOCRParser"])

    def test_a_missing_word_fails(self):
        rc, out = self.run_ocr("drop-last")
        self.assertEqual((rc, out["ok"]), (1, False))
        for check in out["checks"]:
            self.assertEqual(check["missing"], [out["words"][3]])

    def test_http_error_fails(self):
        rc, out = self.run_ocr("500")
        self.assertEqual((rc, [c["status"] for c in out["checks"]]), (1, [500, 500, 500]))

    def test_non_json_reply_fails_and_the_rest_still_run(self):
        rc, out = self.run_ocr("garbage")
        self.assertEqual(rc, 1)
        self.assertTrue(out["checks"][0]["ok"])
        self.assertIn("JSONDecodeError", out["checks"][1]["error"])
        self.assertEqual(len(self.tika.seen), 3)

    def test_unreachable_tika_fails(self):
        with HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as probe:
            dead = f"http://127.0.0.1:{probe.server_address[1]}"
        rc, out = self.run_ocr("echo", url=dead)
        self.assertEqual(rc, 1)
        self.assertTrue(all("error" in c and not c["ok"] for c in out["checks"]))

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow not installed (it is in the Open WebUI image)")
    def test_real_render_and_stdin_run(self):
        png, pdf = self.ocr.render(["Harbor", "Lantern", "Meadow", "23456"])
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertNotIn(b"/Font", pdf)  # image only: no text layer for Tika to read without OCR
        # Run exactly as the role does (script on stdin) against the fake; it can't echo random words, so
        # the expected result is a clean rc 1 with one JSON object.
        self.tika.mode = "500"
        with open(OCR) as f:
            proc = subprocess.run(
                [sys.executable, "-", self.tika.url], input=f.read(), capture_output=True, text=True, timeout=60, check=False
            )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertFalse(json.loads(proc.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
