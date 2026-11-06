import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from research_gate.__main__ import main, serve
from research_gate.config import DEFAULTS, ConfigError, load, validate
from research_gate.htmltext import html_to_text

from .fakes import Env, page

HERE = Path(__file__).resolve().parent.parent


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = validate({"library_root": "/mnt/lib"})
        self.assertEqual(cfg["searxng_url"], "http://127.0.0.1:8888")
        self.assertEqual((cfg["poll_interval"], cfg["results_per_query"], cfg["max_bytes"],
                          cfg["fetch_timeout"], cfg["max_redirects"]), (60, 8, 5 * 1024 * 1024, 30, 3))
        self.assertTrue(cfg["user_agent"])
        self.assertIs(cfg["ipv4_only"], False)
        self.assertIs(validate({"library_root": "/x", "ipv4_only": True})["ipv4_only"], True)
        with self.assertRaises(ConfigError):
            validate({"library_root": "/x", "ipv4_only": "yes"})

    def test_no_key_relaxes_the_address_rules(self):
        for key in ("allow_loopback", "_test_allow_loopback", "allow_private", "proxy"):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                validate({"library_root": "/mnt/lib", key: True})
        self.assertFalse(any("loopback" in k or "allow" in k for k in DEFAULTS))

    def test_bad_values(self):
        bad = [{}, {"library_root": "relative"}, {"library_root": "/x", "max_redirects": 4},
               {"library_root": "/x", "max_bytes": True}, {"library_root": "/x", "poll_interval": 0},
               {"library_root": "/x", "searxng_url": "ftp://x"},
               {"library_root": "/x", "searxng_url": "http://u:p@x/"},
               {"library_root": "/x", "user_agent": "a\r\nX-Evil: 1"}]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                validate(raw)
        with self.assertRaises(ConfigError):
            validate([])

    def test_load_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text("{not json")
            with self.assertRaises(ConfigError):
                load(str(p))
            with self.assertRaises(ConfigError):
                load(str(Path(d) / "missing.json"))
            p.write_text(json.dumps({"library_root": "/mnt/x"}))
            self.assertEqual(load(str(p))["library_root"], "/mnt/x")

    def test_example_config_is_valid(self):
        load(str(HERE / "config.example.json"))


class CliTests(unittest.TestCase):
    def test_status_and_run_once(self):
        env = Env(self)
        env.web.routes["/a"] = page("<p>a</p>")
        rid = env.with_sources([env.url("/a")])
        env.tick(rid, "sources.md", "/a`")
        lines = env.gate.status()
        self.assertEqual(lines[0], f"35-forge/{rid}  [awaiting-source-approval]  pending: 0 search(es), 1 fetch(es)")
        self.assertEqual(lines[1], f"  fetch  {env.url('/a')!r}")
        env.gate.run_once()
        self.assertEqual(env.gate.status(), [f"35-forge/{rid}  [awaiting-source-approval]  pending: 0 search(es), 0 fetch(es)"])

    def test_main_status_with_config(self):
        env = Env(self)
        cfgfile = env.base / "c.json"
        cfgfile.write_text(json.dumps({"library_root": str(env.root), "searxng_url": env.searx.url}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--config", str(cfgfile), "status"]), 0)
            self.assertEqual(main(["--config", str(cfgfile), "run-once"]), 0)
        self.assertIn("searches 0, fetches 0", out.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(["--config", str(env.base / "nope.json"), "status"]), 2)
        cfgfile.write_text(json.dumps({"library_root": str(env.base / "not-a-library")}))
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(["--config", str(cfgfile), "run-once"]), 2)

    def test_startup_refused_when_tables_unreadable(self):
        from unittest import mock
        from research_gate import netguard
        env = Env(self)
        cfgfile = env.base / "c.json"
        cfgfile.write_text(json.dumps({"library_root": str(env.root), "searxng_url": env.searx.url}))
        hidden = {k: str(env.base / "hidden" / k) for k in netguard.PROC_PATHS}
        for cmd in ("serve", "run-once"):
            err = io.StringIO()
            with mock.patch.dict(netguard.PROC_PATHS, hidden), contextlib.redirect_stderr(err), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--config", str(cfgfile), cmd]), 3, cmd)
            self.assertIn("cannot start: the host's IPv4 interface tables could not be read", err.getvalue())
            self.assertIn("ProcSubset=pid", err.getvalue())
        # Only the IPv6 table hidden: refused, unless ipv4_only is set.
        only6 = dict(netguard.PROC_PATHS, if_inet6=str(env.base / "hidden" / "if_inet6"))
        with mock.patch.dict(netguard.PROC_PATHS, only6), contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(cfgfile), "run-once"]), 3)
            cfgfile.write_text(json.dumps({"library_root": str(env.root), "searxng_url": env.searx.url,
                                           "ipv4_only": True}))
            self.assertEqual(main(["--config", str(cfgfile), "run-once"]), 0)
        self.assertEqual(env.searx.calls, [])

    def test_launcher_runs_under_isolated_mode(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "c.json"
            cfg.write_text(json.dumps({"library_root": str(Path(d) / "none")}))
            proc = subprocess.run([sys.executable, "-I", str(HERE / "research-gate"), "--config", str(cfg), "status"],
                                  capture_output=True, text=True, timeout=30, env={"PATH": os.environ.get("PATH", "")})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("is not a directory", proc.stderr)

    def test_serve_survives_a_failing_pass_and_stops(self):
        class Flaky:
            calls = 0

            def run_once(self):
                Flaky.calls += 1
                if Flaky.calls == 1:
                    raise OSError("share not mounted")
                if Flaky.calls >= 3:
                    stop.set()

        stop = threading.Event()
        with self.assertLogs("research_gate", "ERROR"):
            t = threading.Thread(target=serve, args=(Flaky(), 0.01, stop))
            t.start()
            t.join(5)
        self.assertFalse(t.is_alive())
        self.assertGreaterEqual(Flaky.calls, 3)


class HtmlTextTests(unittest.TestCase):
    def test_title_pre_and_nesting(self):
        text, title = html_to_text("<title> A &amp;\n B </title><div><nav><nav>x</nav>still nav</nav>"
                                   "<pre>  keep\n  this</pre><svg/>after<br>line\x07bell</div>")
        self.assertEqual(title, "A & B")
        self.assertEqual(text, "keep\nthis\nafter\nlinebell\n")

    def test_unclosed_script_drops_the_rest(self):
        text, _ = html_to_text("<p>before</p><script>evil() <p>not text")
        self.assertEqual(text, "before\n")

    def test_comments_and_cdata_dropped(self):
        text, _ = html_to_text("<!-- hidden --><p>shown</p><![CDATA[x]]><?php echo 1 ?>")
        self.assertEqual(text, "shown\n")


if __name__ == "__main__":
    unittest.main()
