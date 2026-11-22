"""The auth matrix: peer x token x user, over real TCP connections."""
import itertools
import unittest

from forge_api.auth import BearerAuth, PeerCheck, same_user

from .helpers import ALLOWED, APPROVER, OTHER, TOKEN, Env, Served

TOKENS = {
    "right": TOKEN,
    "missing": None,
    "wrong": "x" * len(TOKEN),
    "prefix": TOKEN[:-1],
    "longer": TOKEN + "x",
    "first-char": TOKEN[0],
    "empty": "",
}
USERS = {"approver": APPROVER, "other": "11111111-2222-3333-4444-555555555555",
         "prefix": APPROVER[:-1], "case": APPROVER.upper(), "empty": ""}


class UnitTests(unittest.TestCase):
    def test_bearer(self):
        auth = BearerAuth(TOKEN)
        self.assertTrue(auth.check([f"Bearer {TOKEN}"]))
        self.assertTrue(auth.check([f"bearer {TOKEN}"]))
        self.assertTrue(auth.check([f"  Bearer   {TOKEN}  "]))
        for bad in ([], [f"Bearer {TOKEN}"] * 2, [f"Basic {TOKEN}"], [TOKEN], [f"Bearer {TOKEN[:-1]}"],
                    [f"Bearer {TOKEN}x"], ["Bearer "], [f"Bearer{TOKEN}"], [f"Bearer {TOKEN} {TOKEN}"]):
            with self.subTest(bad=bad):
                self.assertFalse(auth.check(bad))

    def test_bearer_compares_digests_in_constant_time(self):
        from unittest import mock

        import forge_api.auth as auth_mod
        calls = []
        real = auth_mod.hmac.compare_digest

        def spy(a, b):
            calls.append((len(a), len(b)))
            return real(a, b)
        auth = BearerAuth(TOKEN)
        with mock.patch.object(auth_mod.hmac, "compare_digest", spy):
            self.assertTrue(auth.check([f"Bearer {TOKEN}"]))
            self.assertFalse(auth.check([f"Basic {TOKEN}"]))
            self.assertFalse(auth.check(["Bearer x"]))
        self.assertEqual(calls, [(32, 32)] * 3)   # always runs, always on equal-length digests

    def test_bearer_holds_no_token(self):
        auth = BearerAuth(TOKEN)
        self.assertNotIn(TOKEN, repr(vars(auth)))

    def test_peer(self):
        p = PeerCheck(["172.20.0.4"])
        self.assertTrue(p.allows("172.20.0.4"))
        self.assertTrue(p.allows("::ffff:172.20.0.4"))
        for bad in ("172.20.0.5", "172.20.0.1", "127.0.0.1", "", "garbage", None, "172.20.0.4/32"):
            with self.subTest(bad=bad):
                self.assertFalse(p.allows(bad))

    def test_same_user(self):
        self.assertTrue(same_user(APPROVER, APPROVER))
        for bad in (APPROVER[:-1], APPROVER + "x", APPROVER.upper(), "", None, 5):
            self.assertFalse(same_user(bad, APPROVER))


class MatrixTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.srv = Served(self, self.env)
        self.rid = self.env.with_sources(["https://example.com/a"])

    def expected(self, peer, token, user, tick):
        if peer != ALLOWED:
            return 403
        if token != "right":
            return 401
        if tick and user != "approver":
            return 403
        return 200

    def test_tick_endpoints(self):
        for (peer, (tname, tok), (uname, user)) in itertools.product(
                (ALLOWED, OTHER), TOKENS.items(), USERS.items()):
            for path, body in ((f"/requests/{self.rid}/tick-searches", {"queries": ["apa 7 reference list format"]}),
                               (f"/requests/{self.rid}/tick-sources", {"urls": ["https://example.com/a"]})):
                with self.subTest(peer=peer, token=tname, user=uname, path=path):
                    code, out, resp = self.srv.call("POST", path, {**body, "user": user}, token=tok, source=peer)
                    want = self.expected(peer, tname, uname, True)
                    self.assertEqual(code, want, out)
                    if want == 401:
                        self.assertEqual(resp.getheader("WWW-Authenticate"), "Bearer")
                    if want != 200:
                        self.assertEqual(set(out), {"error"})
        # only the approver's calls ticked anything, and each item once
        searches = self.env.read(self.rid, "searches.md")
        self.assertIn("- [x] apa 7 reference list format", searches)
        self.assertIn("- [ ] apa 7 in-text citations", searches)
        state = self.env.read(self.rid, "state.md")
        self.assertEqual(state.count(f"approve-chat by={APPROVER}"), 2, state)
        self.assertEqual(state.count("approve-chat"), 2, state)

    def test_read_endpoints_ignore_user_but_need_peer_and_token(self):
        for peer, (tname, tok) in itertools.product((ALLOWED, OTHER), TOKENS.items()):
            for method, path, body in (("GET", "/requests", None), ("GET", f"/requests/{self.rid}", None),
                                       ("GET", f"/items/{self.rid}/1", None),
                                       ("POST", "/requests", {"title": "x", "why": "y", "queries": []})):
                with self.subTest(peer=peer, token=tname, path=path, method=method):
                    code, out, _ = self.srv.call(method, path, body, token=tok, source=peer)
                    want = self.expected(peer, tname, None, False)
                    if want == 200:
                        self.assertNotIn(code, (401, 403), out)
                    else:
                        self.assertEqual(code, want, out)

    def test_unknown_path_and_method_need_auth_first(self):
        for path, method in (("/nope", "GET"), ("/requests", "DELETE"), ("/../etc/passwd", "GET")):
            with self.subTest(path=path):
                self.assertEqual(self.srv.call(method, path, source=OTHER)[0], 403)
                self.assertEqual(self.srv.call(method, path, token=None)[0], 401)

    def test_spoofed_forwarding_headers_do_not_help(self):
        spoof = {"X-Forwarded-For": ALLOWED, "X-Real-IP": ALLOWED, "Forwarded": f"for={ALLOWED}"}
        code, _, _ = self.srv.call("GET", "/requests", source=OTHER, headers=spoof)
        self.assertEqual(code, 403)

    def test_two_authorization_headers_refused(self):
        raw = (f"GET /requests HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
               f"Authorization: Bearer {TOKEN}\r\n\r\n").encode()
        self.assertIn(b" 401 ", self.srv.raw(raw).split(b"\r\n")[0])

    def test_no_interim_response_before_the_checks(self):
        raw = (b"POST /requests HTTP/1.1\r\nHost: x\r\nExpect: 100-continue\r\n"
               b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n")
        out = self.srv.raw(raw, source=OTHER)
        self.assertTrue(out.startswith(b"HTTP/1.0 403 "), out[:80])
        self.assertNotIn(b"100 Continue", out)

    def test_basic_scheme_refused(self):
        code, _, _ = self.srv.call("GET", "/requests", token=None, headers={"Authorization": f"Basic {TOKEN}"})
        self.assertEqual(code, 401)


if __name__ == "__main__":
    unittest.main()
