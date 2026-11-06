"""Read-only Money Hub, and where the network code may live (CONTRACTS section 12.14, section 12.18.2)."""
import ast
import datetime as dt
import io
import os
import re
import time
import unittest
from unittest import mock

from finance_digest import config, digest, moneyhub
from finance_digest.moneyhub import ALLOWED, MoneyHub, MoneyHubError

from . import fakes

PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "finance_digest")
WRITE_METHODS = re.compile(r"\b(POST|PUT|DELETE|PATCH)\b")
# The only modules that may send a body, and the one method each may use.
METHOD_EXCEPTIONS = {"broker.py": {"POST"}, "notion.py": {"PATCH"}}
# The only modules that may touch the network at all.
NETWORK_MODULES = {"httpcore.py", "moneyhub.py", "broker.py", "notion.py"}
NETWORK_NAMES = {"urllib.request", "http.client", "socket", "ssl", "urlopen", "HTTPConnection", "HTTPSConnection",
                 "Request", "OpenerDirector", "create_connection"}


def sources():
    for name in sorted(os.listdir(PKG)):
        if name.endswith(".py"):
            with open(os.path.join(PKG, name), encoding="utf-8") as fh:
                yield name, fh.read()


class AllowlistTest(unittest.TestCase):
    def setUp(self):
        self.hub = fakes.FakeHub().__enter__()
        self.addCleanup(self.hub.__exit__, None, None, None)
        self.mh = MoneyHub(self.hub.base_url, 1)

    def test_allowlist_is_exactly_the_spec(self):
        self.assertEqual(
            dict(ALLOWED),
            {"/api/state": frozenset(), "/api/accounts": frozenset(), "/api/budget": {"month"},
             "/api/bills": {"period"}, "/api/debts": frozenset(), "/api/payday": {"period", "check"}},
        )
        with self.assertRaises(TypeError):
            ALLOWED["/api/sync"] = frozenset()  # frozen

    def test_paths_off_the_list_are_refused_before_any_request(self):
        for path in ("/api/sync", "/api/transactions", "/api/state/../sync", "/api/state?x=1", "", None,
                     "http://evil.example/api/state", "/API/STATE", "/api/state/"):
            with self.assertRaises(ValueError, msg=path):
                self.mh.get(path)
        self.assertEqual(self.hub.requests, [])

    def test_params_are_validated_before_any_request(self):
        for path, params in (("/api/state", {"month": "2030-03"}), ("/api/budget", {"month": "2030-13"}),
                             ("/api/budget", {"month": "2030-3"}), ("/api/budget", {"period": "2030-03"}),
                             ("/api/payday", {"period": "2030-03", "check": "2nd"}),
                             ("/api/bills", {"period": "2030-03&x=1"}), ("/api/bills", "period=2030-03")):
            with self.assertRaises(ValueError, msg=(path, params)):
                self.mh.get(path, params)
        self.assertEqual(self.hub.requests, [])

    def test_a_full_run_only_sends_allowlisted_gets(self):
        cfg = config.validate({"moneyhub_url": self.hub.base_url, "ai_summary": False})
        for day in (dt.date(2030, 3, 20), dt.date(2030, 3, 28), dt.date(2030, 3, 1)):
            digest.run(cfg, day=day, dry_run=True, out=io.StringIO())
        self.assertTrue(self.hub.requests)
        for method, path, query in self.hub.requests:
            self.assertEqual(method, "GET")
            self.assertIn(path, ALLOWED)
            self.assertLessEqual(set(query), ALLOWED[path])

    def test_redirect_refused(self):
        self.hub.mode["/api/state"] = "redirect"
        with self.assertRaisesRegex(MoneyHubError, "redirect"):
            self.mh.get("/api/state")
        self.assertEqual([p for _, p, _ in self.hub.requests], ["/api/state"])  # never followed

    def test_size_cap(self):
        self.hub.override["/api/accounts"] = ["x" * 1000] * 3000  # about 3 MB
        with self.assertRaisesRegex(MoneyHubError, "larger than"):
            self.mh.get("/api/accounts")

    def test_overall_deadline(self):
        self.hub.mode["/api/state"] = "slow"
        start = time.monotonic()
        with self.assertRaises(MoneyHubError):
            self.mh.get("/api/state")
        self.assertLess(time.monotonic() - start, 5)

    def test_overall_deadline_covers_trickled_headers(self):
        self.hub.mode["/api/state"] = "trickle"
        start = time.monotonic()
        with self.assertRaisesRegex(MoneyHubError, "took too long"):
            self.mh.get("/api/state")  # timeout 1 s: per-read 1 s, whole request 3 s
        self.assertLess(time.monotonic() - start, 6)

    def test_errors_never_carry_the_url(self):
        for mode in ("500", "notjson", "redirect"):
            self.hub.mode["/api/state"] = mode
            try:
                self.mh.get("/api/state")
            except MoneyHubError as e:
                self.assertNotIn("127.0.0.1", str(e))
                self.assertNotIn(str(self.hub.port), str(e))
        with self.assertRaises(MoneyHubError) as cm:
            MoneyHub("http://user:pw@127.0.0.1:1", 1).get("/api/state")
        self.assertNotIn("pw", str(cm.exception))

    def test_proxy_environment_is_ignored(self):
        with mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1",
                                          "no_proxy": "", "NO_PROXY": ""}):
            self.assertIsInstance(self.mh.get("/api/state"), dict)


class SourceTest(unittest.TestCase):
    def test_sources_are_ascii(self):
        # No literal bidi, zero-width or control characters can hide in the code (escapes only).
        for name, src in sources():
            self.assertTrue(src.isascii(), name)
        for name in sorted(os.listdir(os.path.dirname(os.path.abspath(__file__)))):
            if name.endswith(".py"):
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), encoding="utf-8") as fh:
                    self.assertTrue(fh.read().isascii(), name)

    def test_write_methods_only_where_allowed(self):
        found = {}
        for name, src in sources():
            methods = set(WRITE_METHODS.findall(src))
            if methods:
                found[name] = methods
        self.assertEqual(found, METHOD_EXCEPTIONS)

    def test_moneyhub_reader_has_no_write_method_and_no_body(self):
        src = dict(sources())["moneyhub.py"]
        self.assertIsNone(re.search(r"(?i)\b(post|put|delete|patch)\b", src))
        self.assertNotIn("data=", src)
        self.assertNotIn("/api/sync", src)
        self.assertEqual(re.findall(r'method="([A-Z]+)"', src), ["GET"])

    def test_broker_posts_only_to_the_chat_path(self):
        src = dict(sources())["broker.py"]
        self.assertEqual(re.findall(r'method="([A-Z]+)"', src), ["POST"])
        self.assertEqual(re.findall(r'"/api/[a-z]+"', src), ['"/api/chat"'])
        self.assertIn("base + CHAT_PATH", src)

    def test_notion_patches_only_api_notion_com(self):
        src = dict(sources())["notion.py"]
        self.assertEqual(re.findall(r'NOTION_HOST = "([^"]+)"', src), ["api.notion.com"])
        self.assertEqual(re.findall(r'conn\.request\("([A-Z]+)"', src), ["PATCH"])
        self.assertEqual(re.findall(r"= connect\(([^)]*)\)", src), ["NOTION_HOST, timeout"])
        self.assertIn("connect(NOTION_HOST, timeout)", src)
        self.assertEqual(re.findall(r'f"(/v1/[^"]*)"', src), ["/v1/blocks/{page_id}/children"])

    def test_network_code_only_in_network_modules(self):
        for name, src in sources():
            if name in NETWORK_MODULES:
                continue
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    mods = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                    for m in mods:
                        self.assertFalse(m in NETWORK_NAMES or m.split(".")[0] in ("socket", "ssl", "http")
                                         or m.startswith("urllib.request"), f"{name} imports {m}")
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, NETWORK_NAMES - {"Request"}, f"{name} uses .{node.attr}")

    def test_only_moneyhub_module_knows_money_hub_paths(self):
        for name, src in sources():
            if name == "moneyhub.py":
                continue
            for path in re.findall(r'"(/api/[a-z]+)"', src):
                self.assertIn(path, ALLOWED if name != "broker.py" else {"/api/chat"}, (name, path))

    def test_opener_has_no_proxy_and_refuses_redirects(self):
        opener = moneyhub.httpcore.build_opener()
        kinds = {type(h).__name__ for h in opener.handlers}
        self.assertNotIn("ProxyHandler", kinds)
        self.assertNotIn("FileHandler", kinds)
        self.assertNotIn("HTTPRedirectHandler", kinds)
        self.assertIn("_RefuseRedirects", kinds)


if __name__ == "__main__":
    unittest.main()
