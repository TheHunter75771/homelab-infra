import hmac
import os
import unittest
from unittest import mock

from ai_host_agent import auth
from ai_host_agent.auth import BearerAuth, TokenError, load_token
from ai_host_agent.config import DEFAULTS, ConfigError, load, validate

from .fakes import TOKEN, Env

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = load(None)
        for k, v in DEFAULTS.items():
            self.assertEqual(cfg[k], v, k)
        self.assertEqual((cfg["port"], cfg["max_concurrent"], cfg["exec_timeout"]), (11510, 16, 10))
        self.assertEqual(cfg["head_timeout"], 2)  # Lead, after review
        self.assertEqual(cfg["sudo_exes"], ["/usr/bin/sudo"])
        self.assertEqual((cfg["ollama_host"], cfg["ollama_port"]), ("127.0.0.1", 11434))

    def test_example_config_matches_defaults(self):
        cfg = load(os.path.join(APP, "config.example.json"))
        self.assertEqual(cfg, load(None))

    def test_rejects(self):
        for bad in ({"prot": 1}, {"binds": []}, {"binds": "127.0.0.1"}, {"binds": ["gaming-pc"]},
                    {"port": 0}, {"port": True}, {"max_concurrent": 17}, {"max_concurrent": 0},
                    {"exec_timeout": 11}, {"ollama_timeout": 0.1}, {"exec_max_bytes": 10},
                    {"ollama_url": "http://192.0.2.1:11434"}, {"ollama_url": "https://127.0.0.1"},
                    {"ollama_url": "http://127.0.0.1:11434/api"},
                    {"ollama_url": "http://u:p@127.0.0.1:11434"},
                    {"ollama_url": "http://127.0.0.1:x"},
                    {"ollama_url": "http://localhost:11434"},
                    {"ollama_url": "http://[::1]:11434"},
                    {"ollama_url": "http://u@127.0.0.1:11434"},
                    {"ollama_url": "http://@127.0.0.1:1"},
                    {"ollama_url": "http://127.0.0.1:11434?x=1"},
                    {"ollama_url": "http://127.0.0.1:11434#x"}, {"ollama_url": "http://:11434"},
                    {"token_file": "token"}, {"token_file": "/etc/../tmp/t"},
                    {"exec_path": "/usr/bin:bin"}, {"exec_path": ""}, {"disk_paths": []},
                    {"disk_paths": ["mnt"]}, {"sudo_exes": []},
                    {"sudo_exes": "/usr/bin/sudo"},
                    {"sudo_exes": ["sudo"]}, {"sudo_exes": ["/usr/bin/../bin/sudo"]},
                    {"sudo_exes": ["/"]}, {"sudo_exes": ["/usr/bin/"]},
                    {"sudo_exes": ["/usr/bin/su do"]}, {"sudo_exes": ["/a", "/a"]},
                    {"sudo_exes": ["/a%d" % i for i in range(9)]},
                    {"sudo_exes": [7]}, {"log_level": "LOUD"}, {"request_deadline": 0}, []):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                validate(bad)

    def test_ollama_url_host_is_loopback_or_a_bind(self):
        binds = ["127.0.0.1", "192.0.2.10", "2001:db8::10"]
        for url, host in (("http://127.0.0.1:11434", "127.0.0.1"),
                          ("http://192.0.2.10:11434", "192.0.2.10"),       # the tailnet bind
                          ("http://[2001:db8::10]:11434/", "2001:db8::10"),
                          ("http://192.0.2.10", "192.0.2.10")):
            with self.subTest(url=url):
                cfg = validate({"binds": binds, "ollama_url": url})
                self.assertEqual(cfg["ollama_host"], host)
        # 127.0.0.1 is allowed even when it isn't a bind
        self.assertEqual(validate({"binds": ["192.0.2.10"]})["ollama_host"], "127.0.0.1")
        for url in ("http://192.0.2.11:11434",          # an IP that isn't a bind
                    "http://198.51.100.7:11434",
                    "http://gaming-pc.example:11434",   # a hostname, never
                    "http://localhost:11434",
                    "https://192.0.2.10:11434",         # http only
                    "http://user:pw@192.0.2.10:11434",  # no userinfo
                    "http://user@192.0.2.10:11434",
                    "http://192.0.2.10:11434/api",      # no path
                    "http://192.0.2.10:11434/?q=1",
                    "http://127.0.0.2:11434",           # other loopback, not a bind
                    "http://[::1]:11434"):              # ::1 only if it is a bind
            with self.subTest(url=url), self.assertRaises(ConfigError):
                validate({"binds": binds, "ollama_url": url})

    def test_ipv6_and_localhost(self):
        cfg = validate({"binds": ["127.0.0.1", "::1", "192.0.2.10"],
                        "ollama_url": "http://[::1]:11434/", "disk_paths": ["/mnt/share/"]})
        self.assertEqual((cfg["ollama_host"], cfg["ollama_port"]), ("::1", 11434))
        self.assertEqual(cfg["disk_paths"], ["/mnt/share"])


class TokenFileTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.path = self.env.token_file

    def tearDown(self):
        self.env.cleanup()

    def refused(self, why):
        with self.assertRaises(TokenError) as cm:
            load_token(self.path)
        msg = str(cm.exception)
        self.assertIn(why, msg)
        self.assertNotIn(TOKEN[:12], msg)

    def test_accepts_0640_and_0600_with_newline(self):
        for mode in (0o640, 0o600, 0o400):
            self.env.write_token(TOKEN + "\n", mode)
            self.assertEqual(load_token(self.path), TOKEN)

    def test_missing(self):
        os.remove(self.path)
        self.refused("missing")

    def test_empty_and_blank(self):
        for text in ("", "\n", "   \n\t"):
            self.env.write_token(text)
            self.refused("empty")

    def test_group_or_world_writable(self):
        for mode in (0o660, 0o620, 0o602, 0o622, 0o666):
            with self.subTest(mode=oct(mode)):
                self.env.write_token(TOKEN, mode)
                self.refused("writable")

    def test_world_readable(self):
        self.env.write_token(TOKEN, 0o644)
        self.refused("world-readable")

    def test_symlink_and_directory(self):
        real = self.path + ".real"
        os.rename(self.path, real)
        os.symlink(real, self.path)
        self.refused("cannot open")
        os.remove(self.path)
        os.mkdir(self.path, 0o750)
        self.refused("not a regular file")
        os.rmdir(self.path)

    def test_bad_content(self):
        for text, why in (("short", "shorter"), (TOKEN + " " + TOKEN, "one token"),
                          ("é" * 40, "not ASCII"), ("x" * 5000, "larger")):
            with self.subTest(why=why):
                self.env.write_token(text)
                self.refused(why)


class BearerAuthTest(unittest.TestCase):
    def setUp(self):
        self.auth = BearerAuth(TOKEN)

    def test_right_wrong_missing(self):
        self.assertTrue(self.auth.check(["Bearer " + TOKEN]))
        self.assertTrue(self.auth.check(["bearer " + TOKEN]))
        for bad in ([], ["Bearer " + TOKEN[:-1]], ["Bearer " + TOKEN + "x"], ["Bearer"],
                    ["Basic " + TOKEN], [TOKEN], ["Bearer " + TOKEN, "Bearer " + TOKEN], [""]):
            with self.subTest(bad=bad):
                self.assertFalse(self.auth.check(bad))

    def test_compare_is_constant_time_digest_compare(self):
        """Every presented token, right or wrong, goes through hmac.compare_digest on two
        equal-length SHA-256 digests: never ==, never the raw token."""
        with mock.patch.object(auth.hmac, "compare_digest", wraps=hmac.compare_digest) as spy:
            self.assertTrue(self.auth.check(["Bearer " + TOKEN]))
            self.assertFalse(self.auth.check(["Bearer wrong"]))
            self.assertFalse(self.auth.check(["Basic " + TOKEN]))
        self.assertEqual(spy.call_count, 3)
        for call in spy.call_args_list:
            a, b = call.args
            self.assertEqual((type(a), len(a), type(b), len(b)), (bytes, 32, bytes, 32))
            self.assertNotIn(TOKEN.encode(), a + b)

    def test_object_does_not_keep_the_token(self):
        self.assertNotIn(TOKEN, repr(vars(self.auth)))


if __name__ == "__main__":
    unittest.main()
