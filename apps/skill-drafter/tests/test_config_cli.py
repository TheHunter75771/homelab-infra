import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from skill_drafter.__main__ import main
from skill_drafter.config import ConfigError, validate

from .fakes import Env

HERE = Path(__file__).resolve().parent.parent


class ConfigTests(unittest.TestCase):
    base = {"library_root": "/srv/ai-library", "broker_url": "http://100.64.0.1:11434"}

    def test_defaults(self):
        cfg = validate(dict(self.base))
        self.assertEqual((cfg["model"], cfg["max_attempts"], cfg["max_source_chars"]), ("assistant", 3, 16000))
        self.assertEqual(cfg["broker_url"], "http://100.64.0.1:11434")

    def test_refusals(self):
        bad = [
            {"library_root": "relative"}, {"library_root": None},
            {"broker_url": "http://h:1/api"}, {"broker_url": "http://u:p@h:1"}, {"broker_url": "ftp://h"},
            {"broker_url": "http://h:1?x=1"}, {"broker_url": "http://h:99999"}, {"broker_url": None},
            {"model": ""}, {"model": "a b"}, {"model": 3},
            {"max_attempts": 0}, {"max_attempts": True}, {"max_attempts": 11}, {"max_source_chars": 10},
            {"timeout": 0}, {"poll_interval": "60"}, {"num_ctx": 100},
            {"typo_key": 1},
        ]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ConfigError):
                validate({**self.base, **change})
        with self.assertRaises(ConfigError):
            validate([])

    def test_example_config_is_valid(self):
        validate(json.loads((HERE / "config.example.json").read_text()))


class CliTests(unittest.TestCase):
    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def config(self, env: Env) -> str:
        path = Path(tempfile.mkdtemp()) / "config.json"
        path.write_text(json.dumps({"library_root": str(env.root), "broker_url": env.broker.url, "timeout": 10}))
        self.addCleanup(path.unlink)
        return str(path)

    def test_status_and_run_once(self):
        env = Env(self)
        rid = env.request()
        cfg = self.config(env)
        code, out, _ = self.cli("--config", cfg, "status")
        self.assertEqual(code, 0)
        self.assertIn(f"35-forge/{rid}  [awaiting-source-approval]  due round 'initial ", out)
        code, out, _ = self.cli("--config", cfg, "run-once")
        self.assertEqual((code, out.strip()), (0, "attempts 1, drafted 1, invalid 0, failed 0, errors 0"))
        code, out, _ = self.cli("--config", cfg, "status")
        self.assertIn("[awaiting-skill-review]  idle: draft filed; awaiting Noah's review", out)

    def test_run_once_exit_1_on_an_invalid_draft(self):
        env = Env(self)
        env.request()
        env.broker.replies = ["no template at all"]
        code, out, _ = self.cli("--config", self.config(env), "run-once")
        self.assertEqual(code, 1)
        self.assertIn("invalid 1", out)

    def test_bad_config_and_missing_library_exit_2(self):
        code, _, err = self.cli("--config", "/nonexistent/config.json", "status")
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)
        path = Path(tempfile.mkdtemp()) / "c.json"
        path.write_text(json.dumps({"library_root": str(path.parent), "broker_url": "http://127.0.0.1:9"}))
        code, _, err = self.cli("--config", str(path), "run-once")
        self.assertEqual(code, 2)
        self.assertIn("not an initialized AI library", err)

    def test_launcher_runs_under_isolated_mode(self):
        proc = subprocess.run([sys.executable, "-I", str(HERE / "skill-drafter"), "--help"],
                              capture_output=True, text=True, timeout=30, env={"PATH": os.environ.get("PATH", "")})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("serve", proc.stdout)


if __name__ == "__main__":
    unittest.main()
