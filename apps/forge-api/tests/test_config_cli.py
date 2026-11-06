import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from forge_api.__main__ import main, serve
from forge_api.config import DEFAULTS, ConfigError, load, load_token, validate
from forge_api.server import App, ForgeApiServer

from .helpers import APPROVER, TOKEN, Env, Served

HERE = Path(__file__).resolve().parent.parent
BASE = {"library_root": "/mnt/ai-library", "bind": "172.20.0.1", "approver_user_id": APPROVER}


class ConfigTests(unittest.TestCase):
    def test_defaults_and_keys(self):
        cfg = validate(dict(BASE))
        self.assertEqual(cfg["port"], 879)
        self.assertEqual(cfg["allowed_peers"], ["172.20.0.4"])
        self.assertEqual(cfg["token_file"], "/etc/forge-api/token")
        self.assertEqual(set(DEFAULTS), {"library_root", "bind", "port", "allowed_peers", "token_file",
                                         "approver_user_id"})

    def test_unknown_and_missing_keys(self):
        for raw in ({**BASE, "allowed_peer": ["172.20.0.4"]}, {**BASE, "debug": True},
                    {**BASE, "allow_loopback": True}):
            with self.subTest(raw=raw), self.assertRaisesRegex(ConfigError, "unknown"):
                validate(raw)
        for key in ("library_root", "bind", "approver_user_id"):
            raw = dict(BASE)
            del raw[key]
            with self.subTest(missing=key), self.assertRaisesRegex(ConfigError, "missing"):
                validate(raw)
        with self.assertRaises(ConfigError):
            validate([])

    def test_bind(self):
        for bad in ("0.0.0.0", "::", "", " 172.20.0.1", "172.20.0.0/24", "ai-box", "224.0.0.1",
                    "fe80::1%eth0", 172, None):
            with self.subTest(bind=bad), self.assertRaises(ConfigError):
                validate({**BASE, "bind": bad})
        self.assertEqual(validate({**BASE, "bind": "::ffff:172.20.0.1"})["bind"], "172.20.0.1")
        self.assertEqual(validate({**BASE, "bind": "fd00::1", "allowed_peers": ["fd00::4"]})["bind"], "fd00::1")

    def test_peers(self):
        bad = [[], "172.20.0.4", ["127.0.0.1"], ["::1"], ["0.0.0.0"], ["172.20.0.0/24"], ["172.20.0.1"],
               ["::ffff:172.20.0.1"], ["::ffff:127.0.0.1"], ["open-webui"], [1], ["172.20.0.4"] * 9]
        for peers in bad:
            with self.subTest(peers=peers), self.assertRaises(ConfigError):
                validate({**BASE, "allowed_peers": peers})
        self.assertEqual(validate({**BASE, "allowed_peers": ["172.20.0.4", "::ffff:172.20.0.4"]})["allowed_peers"],
                         ["172.20.0.4"])

    def test_other_values(self):
        for port in (1, 80, 879, 1024, 65535):
            self.assertEqual(validate({**BASE, "port": port})["port"], port)
        bad = [{"port": 0}, {"port": -1}, {"port": 65536}, {"port": 70000}, {"port": "879"}, {"port": True},
               {"port": 879.0},
               {"library_root": "relative"}, {"token_file": "token"}, {"approver_user_id": ""},
               {"approver_user_id": "a b"}, {"approver_user_id": "x" * 200}, {"approver_user_id": "`x`"},
               {"approver_user_id": 5}]
        for extra in bad:
            with self.subTest(extra=extra), self.assertRaises(ConfigError):
                validate({**BASE, **extra})

    def test_load_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text("{not json")
            with self.assertRaises(ConfigError):
                load(str(p))
            with self.assertRaises(ConfigError):
                load(str(Path(d) / "missing.json"))
            p.write_text('{"bind": "172.20.0.1", "bind": "0.0.0.0", "library_root": "/x", "approver_user_id": "a"}')
            with self.assertRaisesRegex(ConfigError, "duplicate"):
                load(str(p))
            p.write_text(json.dumps(BASE))
            self.assertEqual(load(str(p))["bind"], "172.20.0.1")

    def test_example_config_is_valid(self):
        load(str(HERE / "config.example.json"))


class TokenFileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, content, mode=0o640, name="token"):
        p = self.dir / name
        p.write_text(content)
        os.chmod(p, mode)
        return str(p)

    def test_good(self):
        self.assertEqual(load_token(self.write(TOKEN + "\n")), TOKEN)
        self.assertEqual(load_token(self.write(TOKEN, 0o600)), TOKEN)

    def test_refused(self):
        cases = {
            "world-readable": self.write(TOKEN, 0o644, "a"),
            "group/world-writable": self.write(TOKEN, 0o660, "b"),
            "shorter": self.write("abc", 0o640, "c"),
            "one token": self.write(TOKEN + " " + TOKEN, 0o640, "d"),
            "empty": self.write("", 0o640, "e"),
        }
        for why, path in cases.items():
            with self.subTest(why=why), self.assertRaisesRegex(ConfigError, why):
                load_token(path)
        link = self.dir / "link"
        link.symlink_to(self.write(TOKEN, 0o640, "f"))
        with self.assertRaisesRegex(ConfigError, "cannot open"):
            load_token(str(link))
        with self.assertRaisesRegex(ConfigError, "missing"):
            load_token(str(self.dir / "nope"))
        with self.assertRaisesRegex(ConfigError, "regular"):
            load_token(str(self.dir))

    def test_messages_never_hold_the_token(self):
        secret = "S" * 40
        for content, mode in ((secret, 0o644), (secret + " x", 0o640), (secret + "\x00", 0o640)):
            try:
                load_token(self.write(content, mode, "g"))
            except ConfigError as exc:
                self.assertNotIn(secret, str(exc))
            else:
                self.fail("accepted")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.token_path = self.env.base / "token"
        self.token_path.write_text(TOKEN + "\n")
        os.chmod(self.token_path, 0o640)
        self.cfg_path = self.env.base / "config.json"
        self.write_cfg()

    def write_cfg(self, **over):
        raw = {"library_root": str(self.env.root), "bind": "127.0.0.1", "port": 8790,
               "allowed_peers": ["172.20.0.4"], "token_file": str(self.token_path),
               "approver_user_id": APPROVER, **over}
        self.cfg_path.write_text(json.dumps(raw))

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", str(self.cfg_path), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_check_config_ok(self):
        code, out, err = self.run_main("check-config")
        self.assertEqual(code, 0, err)
        self.assertIn("config ok", out)
        self.assertIn("library opens", out)
        self.assertNotIn(TOKEN, out + err)

    def test_check_config_library_missing(self):
        self.write_cfg(library_root=str(self.env.base / "nope"))
        code, out, _ = self.run_main("check-config")
        self.assertEqual(code, 0)
        self.assertIn("NOT available", out)
        code, _, _ = self.run_main("check-config", "--require-library")
        self.assertEqual(code, 2)

    def test_bad_config_and_token(self):
        self.write_cfg(bind="0.0.0.0")
        self.assertEqual(self.run_main("check-config")[0], 2)
        self.assertEqual(self.run_main("serve")[0], 2)
        self.write_cfg()
        os.chmod(self.token_path, 0o644)
        code, _, err = self.run_main("check-config")   # serve loads the token the same way, first
        self.assertEqual(code, 2)
        self.assertIn("world-readable", err)
        self.assertNotIn(TOKEN, err)

    def test_serve_cannot_bind(self):
        holder = socket.socket()
        self.addCleanup(holder.close)
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        self.write_cfg(port=holder.getsockname()[1])
        code, _, err = self.run_main("serve")
        self.assertEqual(code, 3)
        self.assertIn("cannot listen", err)

    def test_plain_bind_on_a_privileged_port_fails_without_the_socket(self):
        """Production always uses systemd's socket on 879; a plain bind there needs privileges."""
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", 879))
            self.skipTest("this host lets unprivileged users bind 879 (ip_unprivileged_port_start)")
        except PermissionError:
            pass
        finally:
            probe.close()
        self.write_cfg(port=879)
        code, _, err = self.run_main("serve")
        self.assertEqual(code, 3)
        self.assertIn("cannot listen on 127.0.0.1 port 879", err)

    def test_serve_loop_and_host_is_forbidden(self):
        """The production rule: peers are open-webui's address, so a call from the host is 403."""
        cfg = load(str(self.cfg_path))
        server = ForgeApiServer(App(cfg, TOKEN), "127.0.0.1", 0)
        stop = threading.Event()
        t = threading.Thread(target=serve, args=(server, stop), daemon=True)
        t.start()
        port = server.server_address[1]
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/requests", headers={"Authorization": f"Bearer {TOKEN}",
                                                  "X-Forwarded-For": "172.20.0.4", "X-Real-IP": "172.20.0.4"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 403)
        self.assertEqual(json.loads(resp.read()), {"error": "forbidden"})
        conn.close()
        stop.set()
        t.join(5)
        self.assertFalse(t.is_alive())

    def test_launcher_runs(self):
        import subprocess
        import sys
        r = subprocess.run([sys.executable, "-I", str(HERE / "forge-api"), "--config", str(self.cfg_path),
                            "check-config"], capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("config ok", r.stdout)

    def test_library_unavailable_is_503(self):
        env = self.env
        env.cfg["library_root"] = str(env.base / "unmounted")
        srv = Served(self, env)
        code, body, _ = srv.call("GET", "/requests")
        self.assertEqual((code, body), (503, {"error": "the AI library is not available right now"}))


if __name__ == "__main__":
    unittest.main()
