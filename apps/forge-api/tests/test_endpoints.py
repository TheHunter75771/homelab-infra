"""Every endpoint, the caps, strict JSON, the HTTP edges and traversal on /items."""
import json
import os
import unittest

from forge import Forge
from forge_api.server import strict_json
from forge_api.service import PROVENANCE, ApiError

# The contract's caps, written out (not imported) so a weakened constant fails the tests.
MAX_QUERIES = 10
MAX_URLS = 30
ITEM_MAX_BYTES = 20 * 1024
from library import frontmatter as fm

from .helpers import APPROVER, TOKEN, Env, Served

PROPOSAL = {"title": "APA 7 citation rules", "why": "Noah asked how APA 7 citations work.",
            "queries": [{"query": "apa 7 citation rules", "why": "the core rules"},
                        {"query": "apa 7 reference list", "why": "reference list format"},
                        {"query": "apa 7 in-text citation", "why": "in-text form"}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.srv = Served(self, self.env)

    def call(self, *a, **kw):
        return self.srv.call(*a, **kw)

    def create(self, body=None):
        code, out, _ = self.call("POST", "/requests", body or PROPOSAL)
        self.assertEqual(code, 201, out)
        return out["rid"]


class CreateTests(Base):
    def test_create(self):
        code, out, resp = self.call("POST", "/requests", PROPOSAL)
        self.assertEqual(code, 201, out)
        self.assertEqual(resp.getheader("Content-Type"), "application/json; charset=utf-8")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        rid = out["rid"]
        self.assertTrue(rid.endswith("_apa-7-citation-rules"), rid)
        self.assertEqual((out["proposed"], out["status"]), (3, "awaiting-search-approval"))
        searches = self.env.read(rid, "searches.md")
        self.assertIn("- [ ] apa 7 citation rules \u2014 why: the core rules", searches)
        self.assertNotIn("\n- [x] ", searches)
        request = self.env.read(rid, "request.md")
        self.assertTrue(request.endswith("Noah asked how APA 7 citations work." + PROVENANCE), request)

    def test_duplicate_queries_listed_once(self):
        body = {**PROPOSAL, "queries": [PROPOSAL["queries"][0]] * 2}
        code, out, _ = self.call("POST", "/requests", body)
        self.assertEqual((code, out["proposed"]), (201, 1))

    def test_forge_checks_apply(self):
        bad = [
            {"title": "x <img src=a>"}, {"title": "a ![x](http://e/)"}, {"title": ""}, {"title": "a\nb"},
            {"why": "see ![i](https://evil.example/?q=secret)"}, {"why": "a <b>"}, {"why": "a\u202eb"},
            {"why": "a why: b"}, {"why": " "},
            {"queries": [{"query": "a `b`", "why": "x"}]},
            {"queries": [{"query": "rocm \u2014 why: fake", "why": "x"}]},
            {"queries": [{"query": "a why: b", "why": "x"}]},
            {"queries": [{"query": "[x] y", "why": "x"}]},
            {"queries": [{"query": "caf\u00e9", "why": "x"}]},
            {"queries": [{"query": "ok", "why": "x & y"}]},
            {"queries": [{"query": "", "why": "x"}]},
        ]
        for over in bad:
            with self.subTest(over=over):
                code, out, _ = self.call("POST", "/requests", {**PROPOSAL, **over})
                self.assertEqual(code, 400, out)
                self.assertIsInstance(out["error"], str)
        self.assertEqual(os.listdir(self.env.root / "35-forge"), [], "nothing was created")

    def test_shape_and_caps(self):
        bad = [
            {**PROPOSAL, "extra": 1},
            {k: v for k, v in PROPOSAL.items() if k != "why"},
            {**PROPOSAL, "title": 5}, {**PROPOSAL, "why": ["x"]}, {**PROPOSAL, "queries": "x"},
            {**PROPOSAL, "queries": []},
            {**PROPOSAL, "queries": [{"query": f"q{i}", "why": "b"} for i in range(MAX_QUERIES + 1)]},
            {**PROPOSAL, "queries": [{"query": "a" * 201, "why": "b"}]},
            {**PROPOSAL, "queries": [{"query": "a", "why": "b", "user": APPROVER}]},
            {**PROPOSAL, "queries": [{"query": "a"}]},
            {**PROPOSAL, "queries": ["a"]},
            {**PROPOSAL, "queries": [{"query": "a", "why": "b" * 301}]},
            {**PROPOSAL, "title": "t" * 121},
            {**PROPOSAL, "why": "w" * 2001},
        ]
        for body in bad:
            with self.subTest(body=str(body)[:80]):
                code, out, _ = self.call("POST", "/requests", body)
                self.assertEqual(code, 400, out)
        ok = {**PROPOSAL, "queries": [{"query": "q" * 200, "why": "b"}] + [{"query": f"q{i}", "why": "b"} for i in range(9)]}
        self.assertEqual(self.call("POST", "/requests", ok)[0], 201)


class ListAndDetailTests(Base):
    def test_list(self):
        a = self.create()
        b = self.create({**PROPOSAL, "title": "ROCm install"})
        code, out, _ = self.call("GET", "/requests")
        self.assertEqual(code, 200)
        self.assertEqual(out["total"], 2)
        self.assertEqual({r["rid"] for r in out["requests"]}, {a, b})
        r = next(r for r in out["requests"] if r["rid"] == a)
        self.assertEqual(r, {"rid": a, "where": "35-forge", "status": "awaiting-search-approval",
                             "title": "APA 7 citation rules", "searches": {"approved": 0, "listed": 3},
                             "sources": {"approved": 0, "listed": 0}, "ingested": 0})

    def test_detail(self):
        rid = self.env.with_sources(["https://example.com/a", "https://docs.example.org/b?x=1"])
        url = "https://example.com/a"
        self.env.hand_tick(rid, "sources.md", url + "`")
        self.env.ingest(rid, url, b"page text")
        code, out, _ = self.call("GET", f"/requests/{rid}")
        self.assertEqual(code, 200, out)
        self.assertEqual(out["status"], "awaiting-source-approval")
        self.assertEqual(out["searches"][0], {"n": 1, "query": "apa 7 citation rules", "why": "learn the rules",
                                              "approved": True})
        self.assertEqual(out["pending_searches"], 2)
        self.assertEqual(out["sources"], [
            {"n": 1, "url": url, "domain": "example.com", "title": "Result 0 title", "approved": True},
            {"n": 2, "url": "https://docs.example.org/b?x=1", "domain": "docs.example.org",
             "title": "Result 1 title", "approved": False}])
        self.assertEqual(out["pending_sources"], 1)
        self.assertEqual(len(out["ingested"]), 1)
        self.assertEqual(out["ingested"][0]["n"], 1)
        self.assertEqual(out["ingested"][0]["url"], url)
        self.assertTrue(out["ingested"][0]["path"].startswith("10-sources/apa-7-citations/"))
        self.assertEqual(out["malformed_lines"], 0)

    def test_detail_errors(self):
        self.assertEqual(self.call("GET", "/requests/2026-10-27_nope")[0], 404)
        self.assertEqual(self.call("GET", "/requests/2026-13-45_bad")[0], 400)
        self.assertEqual(self.call("GET", "/requests/NOT-AN-ID")[0], 404)  # route pattern
        self.assertEqual(self.call("GET", "/requests/x")[0], 400)

    def test_shelved_request_readable_but_not_tickable(self):
        rid = self.create()
        (self.env.root / "95-shelf").mkdir(exist_ok=True)
        os.rename(self.env.folder(rid), self.env.root / "95-shelf" / "apa-skill")
        code, out, _ = self.call("GET", f"/requests/{rid}")
        self.assertEqual((code, out["where"]), (200, "95-shelf"))
        code, out, _ = self.call("POST", f"/requests/{rid}/tick-searches",
                                 {"queries": ["apa 7 citation rules"], "user": APPROVER})
        self.assertEqual(code, 409, out)


class TickTests(Base):
    def test_tick_searches(self):
        rid = self.create()
        code, out, _ = self.call("POST", f"/requests/{rid}/tick-searches",
                                 {"queries": ["apa 7 citation rules", "apa 7 in-text citation", "not listed"],
                                  "user": APPROVER})
        self.assertEqual(code, 200, out)
        self.assertEqual(out["results"], [{"query": "apa 7 citation rules", "result": "ticked"},
                                          {"query": "apa 7 in-text citation", "result": "ticked"},
                                          {"query": "not listed", "result": "not-found",
                                           "reason": "not listed in searches.md"}])
        self.assertEqual(out["counts"], {"ticked": 2, "already": 0, "not-found": 1})
        searches = self.env.read(rid, "searches.md")
        self.assertIn("- [x] apa 7 citation rules", searches)
        self.assertIn("- [ ] apa 7 reference list", searches)
        self.assertIn("- [x] apa 7 in-text citation", searches)
        forge = Forge(self.env.lib)
        self.assertTrue(forge.is_query_approved(rid, "apa 7 citation rules"))
        self.assertFalse(forge.is_query_approved(rid, "apa 7 reference list"))
        # the forge writes the audit line (state.md, with by=); this service writes no gate-log line
        audit = [ln for ln in self.env.read(rid, "state.md").splitlines() if "approve-chat" in ln]
        self.assertEqual(len(audit), 1, audit)
        self.assertIn(f"approve-chat by={APPROVER} search: ticked 2", audit[0])
        self.assertIn("`apa 7 citation rules`, `apa 7 in-text citation`", audit[0])
        self.assertFalse((self.env.folder(rid) / "gate-log.md").exists())
        # again: already, and no new audit line
        code, out, _ = self.call("POST", f"/requests/{rid}/tick-searches",
                                 {"queries": ["apa 7 citation rules"], "user": APPROVER})
        self.assertEqual(out["results"], [{"query": "apa 7 citation rules", "result": "already"}])
        self.assertEqual(self.env.read(rid, "state.md").count("approve-chat"), 1)

    def test_query_matched_as_checked(self):
        rid = self.create()
        code, out, _ = self.call("POST", f"/requests/{rid}/tick-searches",
                                 {"queries": ["  apa 7 citation rules  ", "apa 7 citation rules"], "user": APPROVER})
        self.assertEqual(code, 200, out)
        self.assertEqual(out["results"], [{"query": "apa 7 citation rules", "result": "ticked"}])

    def test_tick_sources(self):
        rid = self.env.with_sources(["https://example.com/a", "https://example.com/b"])
        code, out, _ = self.call("POST", f"/requests/{rid}/tick-sources",
                                 {"urls": ["HTTPS://Example.COM/b", "https://example.com/zzz"], "user": APPROVER})
        self.assertEqual(code, 200, out)
        self.assertEqual(out["results"], [{"url": "https://example.com/b", "result": "ticked"},
                                          {"url": "https://example.com/zzz", "result": "not-found",
                                           "reason": "not listed in sources.md"}])
        forge = Forge(self.env.lib)
        self.assertTrue(forge.is_url_approved(rid, "https://example.com/b"))
        self.assertFalse(forge.is_url_approved(rid, "https://example.com/a"))
        self.assertIn(f"approve-chat by={APPROVER} source: ticked 1", self.env.read(rid, "state.md"))
        self.assertFalse((self.env.folder(rid) / "gate-log.md").exists())

    def test_wrong_state_is_409(self):
        rid = self.create()
        code, out, _ = self.call("POST", f"/requests/{rid}/tick-sources",
                                 {"urls": ["https://example.com/a"], "user": APPROVER})
        self.assertEqual(code, 409, out)
        self.assertIn("refused by the forge", out["error"])

    def test_tick_caps_and_checks(self):
        rid = self.create()
        url_rid = self.env.with_sources(["https://example.com/a"])
        bad = [
            ("searches", {"queries": [], "user": APPROVER}),
            ("searches", {"queries": [f"q{i}" for i in range(MAX_QUERIES + 1)], "user": APPROVER}),
            ("searches", {"queries": ["a" * 201], "user": APPROVER}),
            ("searches", {"queries": ["a <b>"], "user": APPROVER}),
            ("searches", {"queries": ["a why: b"], "user": APPROVER}),
            ("searches", {"queries": "apa", "user": APPROVER}),
            ("searches", {"queries": [5], "user": APPROVER}),
            ("searches", {"queries": ["a"], "user": APPROVER, "approved": True}),
            ("searches", {"queries": ["a"]}),
            ("sources", {"urls": [f"https://e.com/{i}" for i in range(MAX_URLS + 1)], "user": APPROVER}),
            ("sources", {"urls": ["javascript:alert(1)"], "user": APPROVER}),
            ("sources", {"urls": ["https://e.com/#frag"], "user": APPROVER}),
            ("sources", {"urls": ["https://u:p@e.com/"], "user": APPROVER}),
            ("sources", {"urls": ["https://e.com/" + "a" * 2100], "user": APPROVER}),
            ("sources", {"queries": ["a"], "user": APPROVER}),
        ]
        for kind, body in bad:
            with self.subTest(kind=kind, body=str(body)[:80]):
                r = rid if kind == "searches" else url_rid
                code, out, _ = self.call("POST", f"/requests/{r}/tick-{kind}", body)
                self.assertEqual(code, 400, out)
        self.assertNotIn("\n- [x] ", self.env.read(rid, "searches.md"))
        ok = {"urls": [f"https://e.com/{i}" for i in range(MAX_URLS)], "user": APPROVER}
        self.assertEqual(self.call("POST", f"/requests/{url_rid}/tick-sources", ok)[0], 200)

    def test_user_checked_before_items(self):
        rid = self.create()
        code, _, _ = self.call("POST", f"/requests/{rid}/tick-searches", {"queries": ["a" * 999], "user": "nobody"})
        self.assertEqual(code, 403)

    def test_unknown_request(self):
        code, _, _ = self.call("POST", "/requests/2026-10-27_nope/tick-searches", {"queries": ["a"], "user": APPROVER})
        self.assertEqual(code, 404)


class ItemTests(Base):
    def setUp(self):
        super().setUp()
        self.url = "https://example.com/a"
        self.rid = self.env.with_sources([self.url, "https://example.com/b", "https://example.com/c"])
        for u in ("/a`", "/b`", "/c`"):
            self.env.hand_tick(self.rid, "sources.md", u)

    def test_text_item(self):
        item = self.env.ingest(self.rid, self.url, "Hello\r\nworld\x00\x1b[31m red\u202e\u200b ok\ttab\n".encode())
        code, out, _ = self.call("GET", f"/items/{self.rid}/1")
        self.assertEqual(code, 200, out)
        self.assertEqual(out["text"], "Hello\nworld[31m red ok\ttab\n")
        self.assertEqual((out["url"], out["path"], out["kind"], out["truncated"]), (self.url, item, "text", False))

    def test_clipped_to_20_kb(self):
        self.env.ingest(self.rid, self.url, ("\u00e9" * 30000).encode())
        code, out, _ = self.call("GET", f"/items/{self.rid}/1")
        self.assertEqual(code, 200)
        self.assertTrue(out["truncated"])
        self.assertLessEqual(len(out["text"].encode()), ITEM_MAX_BYTES)
        self.assertGreater(len(out["text"].encode()), ITEM_MAX_BYTES - 2)
        self.assertEqual(set(out["text"]), {"\u00e9"})

    def test_pdf_item(self):
        self.env.ingest(self.rid, self.url, b"%PDF-1.4 fake", ext="pdf")
        code, out, _ = self.call("GET", f"/items/{self.rid}/1")
        self.assertEqual((code, out["kind"], out["text"]), (200, "pdf", ""))

    def test_numbering_and_range(self):
        self.env.ingest(self.rid, self.url, b"one")
        self.env.ingest(self.rid, "https://example.com/b", b"two")
        self.assertEqual(self.call("GET", f"/items/{self.rid}/2")[1]["text"], "two")
        for n in ("3", "9999"):
            self.assertEqual(self.call("GET", f"/items/{self.rid}/{n}")[0], 404)
        for n in ("0", "01", "-1", "1.0", "10000", "%31", "1/", "1/../2"):
            with self.subTest(n=n):
                self.assertEqual(self.call("GET", f"/items/{self.rid}/{n}")[0], 404)

    def test_traversal_in_the_path_never_reaches_a_file(self):
        item = self.env.ingest(self.rid, self.url, b"one")
        (self.env.root / "50-finance" / "secret.txt").write_text("SECRET")
        for path in (f"/items/{self.rid}/{item}", f"/items/{self.rid}/50-finance/secret.txt",
                     f"/items/{self.rid}/README.md", f"/items/{self.rid}/35-forge/{self.rid}/state.md",
                     f"/items/{self.rid}//etc/passwd", f"/items/{item}", f"/{item}",
                     f"/items/{self.rid}/../../../../etc/passwd", "/items/../35-forge/x/1",
                     f"/items/{self.rid}/1/..", "/items/%2e%2e/1", f"/items/{self.rid}%2f..%2f..%2f/1",
                     "/items//etc/passwd/1", "/items/..%2f..%2fetc/1", f"/items/{self.rid}/1%00",
                     "/items/2026-10-27_x/../1", "/items/10-sources/1", "//items/x/1"):
            with self.subTest(path=path):
                code, out, _ = self.call("GET", path)
                self.assertIn(code, (400, 404), out)
                self.assertNotIn("SECRET", json.dumps(out))
                self.assertNotIn("text", out)

    def _rewrite_state(self, old, new):
        p = self.env.folder(self.rid) / "state.md"
        text = p.read_text()
        self.assertIn(old, text)
        p.write_text(text.replace(old, new))

    def test_tampered_record_refused(self):
        item = self.env.ingest(self.rid, self.url, b"one")
        secret = self.env.root / "50-finance" / "secret.txt"
        secret.write_text("SECRET")
        for bad in ("../../../../etc/passwd", "50-finance/secret.txt", "10-sources/../50-finance/secret.txt",
                    "/etc/passwd", "10-sources/apa-7-citations/../../50-finance/secret.txt",
                    "10-sources/x/y/z.txt"):
            with self.subTest(bad=bad):
                self._rewrite_state(item, bad)
                code, out, _ = self.call("GET", f"/items/{self.rid}/1")
                self.assertEqual(code, 409, out)
                self.assertNotIn("SECRET", json.dumps(out))
                self._rewrite_state(bad, item)
        self.assertEqual(self.call("GET", f"/items/{self.rid}/1")[0], 200)

    def test_record_outside_10_sources_refused_even_with_a_matching_header(self):
        item = self.env.ingest(self.rid, self.url, b"one")
        secret = self.env.root / "50-finance" / "secret.txt"
        secret.write_text("SECRET")
        sidecar = (self.env.root / (item + ".md")).read_text()
        (self.env.root / "50-finance" / "secret.txt.md").write_text(sidecar)
        self._rewrite_state(item, "50-finance/secret.txt")
        code, out, _ = self.call("GET", f"/items/{self.rid}/1")
        self.assertEqual(code, 409, out)
        self.assertNotIn("SECRET", json.dumps(out))

    def test_record_pointing_at_another_requests_item_refused(self):
        item = self.env.ingest(self.rid, self.url, b"one")
        sidecar = self.env.root / (item + ".md")
        meta, body = fm.parse(sidecar.read_text())
        self.assertEqual(meta["forge"], self.rid)
        meta["forge"] = "2026-10-27_other"
        sidecar.write_text(fm.render(meta, body))
        self.assertEqual(self.call("GET", f"/items/{self.rid}/1")[0], 409)

    def test_header_url_mismatch_refused(self):
        item = self.env.ingest(self.rid, self.url, b"one")
        self._rewrite_state(f"{self.url} -> {item}", f"https://example.com/b -> {item}")
        self.assertEqual(self.call("GET", f"/items/{self.rid}/1")[0], 409)

    def test_symlinked_item_refused(self):
        item = self.env.ingest(self.rid, self.url, b"one")
        target = self.env.base / "outside.txt"
        target.write_text("OUTSIDE")
        p = self.env.root / item
        p.unlink()
        p.symlink_to(target)
        code, out, _ = self.call("GET", f"/items/{self.rid}/1")
        self.assertEqual(code, 409, out)
        self.assertNotIn("OUTSIDE", json.dumps(out))

    def test_symlinked_topic_dir_refused(self):
        item = self.env.ingest(self.rid, self.url, b"one")
        topic = self.env.root / os.path.dirname(item)
        moved = self.env.base / "moved"
        os.rename(topic, moved)
        topic.symlink_to(moved)
        self.assertEqual(self.call("GET", f"/items/{self.rid}/1")[0], 409)

    def test_item_file_missing(self):
        item = self.env.ingest(self.rid, self.url, b"one")
        (self.env.root / item).unlink()
        self.assertEqual(self.call("GET", f"/items/{self.rid}/1")[0], 409)


class HttpEdgeTests(Base):
    def test_methods(self):
        for method, path in (("PUT", "/requests"), ("DELETE", "/requests"), ("GET", "/requests/2026-10-27_x/tick-searches"),
                             ("POST", "/items/2026-10-27_x/1"), ("HEAD", "/requests")):
            with self.subTest(method=method, path=path):
                code, _, resp = self.call(method, path)
                self.assertEqual(code, 405)
                self.assertTrue(resp.getheader("Allow"))

    def test_query_strings_and_odd_targets(self):
        self.assertEqual(self.call("GET", "/requests?x=1")[0], 400)
        self.assertEqual(self.call("GET", "/requests/")[0], 404)
        self.assertEqual(self.call("GET", "/REQUESTS")[0], 404)
        out = self.srv.raw(f"GET http://127.0.0.1/requests HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n\r\n".encode())
        self.assertIn(b" 400 ", out.split(b"\r\n")[0])

    def test_malformed_json(self):
        good = json.dumps(PROPOSAL)[1:]   # everything after the opening brace
        bodies = [b"{", b"[]", b'"x"', b"null", b"1", b'{"title": NaN}', b'{"a": Infinity}',
                  ('{"title": "a", ' + good).encode(), ('{"x": NaN, ' + good).encode(),
                  ('{"x": -Infinity, ' + good).encode(), b"\xff\xfe{}",
                  b"\xef\xbb\xbf{}", b"{} {}", b"{'a': 1}", b"[" * 50000, b"", b'{"a": 1e999999}',
                  b'{"a": 1' + b"0" * 5000 + b"}"]
        for raw in bodies:
            with self.subTest(raw=raw[:30]):
                code, out, _ = self.call("POST", "/requests", raw_body=raw)
                self.assertEqual(code, 400, out)
                self.assertEqual(set(out), {"error"})
                self.assertNotIn("Traceback", out["error"])

    def test_strict_json_unit(self):
        self.assertEqual(strict_json(b'{"a": [1, "b"]}'), {"a": [1, "b"]})
        for raw in (b'{"a": 1, "a": 2}', b'{"a": {"b": 1, "b": 2}}', b'{"a": NaN}', b'{"a": Infinity}',
                    b'{"a": -Infinity}', b"[]", b"[{}]", b'"s"', b"3", b"null", b"true", b"\xc3\x28"):
            with self.subTest(raw=raw), self.assertRaises(ApiError) as cm:
                strict_json(raw)
            self.assertEqual(cm.exception.status, 400)

    def test_body_limits(self):
        big = json.dumps({**PROPOSAL, "why": "a" * (64 * 1024)}).encode()
        code, out, _ = self.call("POST", "/requests", raw_body=big)
        self.assertEqual(code, 413, out)
        exact = b'{"x": "' + b"a" * (64 * 1024 - 10) + b'"}'
        self.assertEqual(len(exact), 64 * 1024 - 1)
        self.assertEqual(self.call("POST", "/requests", raw_body=exact)[0], 400)  # read, then refused by shape

    def test_content_type_and_length(self):
        body = json.dumps(PROPOSAL).encode()
        for ct in (None, "text/plain", "application/json; charset=latin-1", "application/jsonx", "multipart/form-data"):
            with self.subTest(ct=ct):
                self.assertEqual(self.call("POST", "/requests", raw_body=body, content_type=ct)[0], 415)
        self.assertEqual(self.call("POST", "/requests", raw_body=body, content_type="Application/JSON; Charset=UTF-8")[0], 201)
        auth = f"Authorization: Bearer {TOKEN}\r\n"
        head = f"POST /requests HTTP/1.1\r\nHost: x\r\n{auth}Content-Type: application/json\r\n"
        cases = {
            411: [head + "\r\n", head + "Transfer-Encoding: chunked\r\n\r\n5\r\n{}\r\n0\r\n\r\n",
                  head + "Transfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n{}",
                  head + "Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}"],
            400: [head + "Content-Length: -1\r\n\r\n", head + "Content-Length: 0x10\r\n\r\n",
                  head + "Content-Length: 50\r\n\r\n{}"],
        }
        for want, raws in cases.items():
            for raw in raws:
                with self.subTest(raw=raw[-40:]):
                    out = self.srv.raw(raw.encode())
                    self.assertIn(f" {want} ".encode(), out.split(b"\r\n")[0], out[:200])

    def test_garbage_request_lines_get_json(self):
        for raw in (b"\x00\x01\x02\r\n\r\n", b"GET\r\n\r\n", b"GET / HTTP/9.9\r\n\r\n", b"A" * 9000 + b"\r\n\r\n",
                    b"GET /requests HTTP/1.1\r\n" + b"X: y\r\n" * 200 + b"\r\n"):
            with self.subTest(raw=raw[:20]):
                out = self.srv.raw(raw)
                status = out.split(b"\r\n")[0]
                self.assertRegex(status, rb"^HTTP/1\.[01] (4\d\d|5\d\d) ", out[:200])
                self.assertIn(b"application/json", out)
                self.assertNotIn(b"<html", out.lower())

    def test_error_bodies_have_no_traceback(self):
        def boom(*a, **kw):
            raise RuntimeError("kaboom internal detail")
        self.env.service.list_requests = boom
        code, out, _ = self.call("GET", "/requests")
        self.assertEqual((code, out), (500, {"error": "internal error"}))

    def test_ticks_unavailable_with_an_old_forge(self):
        import types

        from forge_api.service import Service
        from forge_api.ticks import ForgeTicks
        self.env.service = Service(self.env.cfg, ticker_factory=lambda forge: ForgeTicks(types.SimpleNamespace()),
                                   library_kwargs={"clock": self.env.clock})
        self.srv.app.service = self.env.service
        rid = self.create()
        code, out, _ = self.call("POST", f"/requests/{rid}/tick-searches", {"queries": ["apa 7 citation rules"], "user": APPROVER})
        self.assertEqual(code, 501, out)
        self.assertNotIn("\n- [x] ", self.env.read(rid, "searches.md"))

if __name__ == "__main__":
    unittest.main()
