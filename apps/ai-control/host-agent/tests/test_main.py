"""The real entrypoint in a child process: refuses unsafe token files, serves, logs to stderr."""

import json
import os
import signal
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

from .fakes import TOKEN, Env, free_port

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(APP, "ai-host-agent")


class MainTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.port = free_port()
        self.cfg_path = os.path.join(self.env.root, "config.json")
        with open(self.cfg_path, "w") as f:
            json.dump({"token_file": self.env.token_file, "exec_path": self.env.bin,
                       "port": self.port, "binds": ["127.0.0.1"],
                       "mountinfo": self.env.mountinfo, "cgroup_root": self.env.cgroup}, f)

    def tearDown(self):
        self.env.cleanup()

    def start(self, *extra):
        return subprocess.Popen([sys.executable, "-I", LAUNCHER, "--config", self.cfg_path,
                                 *extra], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)

    def test_refuses_unsafe_token_files(self):
        cases = [(0o660, TOKEN, "group/world-writable"), (0o646, TOKEN, "group/world-writable"),
                 (0o644, TOKEN, "world-readable"), (0o640, "", "empty"),
                 (0o640, "\n", "empty")]
        for mode, text, why in cases:
            with self.subTest(why=why, mode=oct(mode)):
                self.env.write_token(text, mode)
                p = self.start()
                out, err = p.communicate(timeout=10)
                self.assertEqual(p.returncode, 2)
                self.assertIn("ai-host-agent: token:", err)
                self.assertIn(why, err)
                self.assertNotIn(TOKEN[:8], err + out)
        os.remove(self.env.token_file)
        p = self.start()
        _, err = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 2)
        self.assertIn("missing", err)

    def test_check_mode(self):
        p = self.start("--check")
        _, err = p.communicate(timeout=10)
        self.assertEqual((p.returncode, err.strip()), (0, "ai-host-agent: config and token OK"))

    def test_bad_config(self):
        with open(self.cfg_path, "w") as f:
            f.write('{"binds": ["gaming-pc.example"]}')
        p = self.start()
        _, err = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 2)
        self.assertIn("config: binds must be IP addresses", err)

    def test_serves_and_logs_to_stderr_without_the_token(self):
        p = self.start()
        try:
            base = "http://127.0.0.1:%d" % self.port
            deadline = time.monotonic() + 10
            got = None
            while time.monotonic() < deadline:
                req = urllib.request.Request(base + "/healthz",
                                             headers={"Authorization": "Bearer " + TOKEN})
                try:
                    with urllib.request.urlopen(req, timeout=2) as r:
                        got = (r.status, json.loads(r.read()))
                    break
                except (urllib.error.URLError, ConnectionError):
                    time.sleep(0.1)
            self.assertEqual(got, (200, {"ok": True}))
            req = urllib.request.Request(base + "/v1/logs?unit=gpu-gate&x=" + TOKEN,
                                         headers={"Authorization": "Bearer " + TOKEN + "x"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 401)
            cm.exception.close()
        finally:
            p.send_signal(signal.SIGTERM)
            out, err = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(out, "")
        self.assertIn("listening on 127.0.0.1:%d" % self.port, err)
        self.assertIn("ai_host_agent.http: GET /healthz 200", err)
        self.assertIn("ai_host_agent.http: GET /v1/logs 401", err)
        self.assertIn("stopping", err)
        self.assertNotIn(TOKEN[:8], err)
        self.assertNotIn("Authorization", err)


if __name__ == "__main__":
    unittest.main()
