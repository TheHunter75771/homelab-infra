"""The token never reaches the log."""
import io
import logging
import unittest

from forge_api.__main__ import setup_logging
from forge_api.server import RedactingFormatter

from .helpers import APPROVER, OTHER, TOKEN, Env, LogCapture, Served


class TokenNeverLoggedTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.logs = LogCapture(self)          # unredacted: what the code itself logs
        self.srv = Served(self, self.env)
        self.rid = self.env.with_sources(["https://example.com/a"])

    def test_token_in_every_transport_place(self):
        s, rid = self.srv, self.rid
        tok = TOKEN
        s.call("GET", "/requests")
        s.call("GET", f"/requests/{rid}")
        s.call("GET", f"/requests/{tok}")
        s.call("GET", f"/{tok}")
        s.call("GET", f"/requests?token={tok}")
        s.call("GET", f"/items/{rid}/{tok}")
        s.call("GET", "/requests", token=tok + "x")
        s.call("GET", "/requests", token=tok, source=OTHER)
        s.call("GET", "/requests", token=None, headers={"Authorization": f"Basic {tok}"})
        s.call("GET", "/requests", headers={"X-Api-Key": tok, f"X-{tok}": "1", "Cookie": f"t={tok}"})
        s.call("POST", f"/requests/{rid}/tick-searches", {"queries": ["apa 7 citation rules"], "user": tok})
        s.call("POST", f"/requests/{rid}/tick-searches", {"queries": [tok], "user": APPROVER})
        s.call("POST", f"/requests/{rid}/tick-sources", {"urls": [f"https://example.com/{tok}"], "user": APPROVER})
        s.call("POST", f"/requests/{rid}/tick-sources", {"urls": [f"https://{tok}@e.com/"], "user": APPROVER})
        s.call("POST", "/requests", {tok: 1})
        s.call("POST", "/requests", raw_body=f'{{"{tok}": 1, "{tok}": 2}}'.encode())
        s.call("POST", "/requests", raw_body=tok.encode())
        s.call("POST", "/requests", {"title": "x", "why": tok + " <", "queries": [{"query": "a", "why": "b"}]})
        s.call("POST", "/requests", {"title": "x", "why": "y", "queries": [{"query": tok + "`", "why": "b"}]})
        s.call("POST", "/requests", raw_body=b"{}", content_type=f"text/{tok}")
        s.raw(f"{tok} /requests HTTP/1.0\r\nAuthorization: Bearer {tok}\r\n\r\n".encode())
        s.raw(f"GET /{tok} HTTP/1.0\r\n\r\n".encode())
        s.raw(f"GET /requests HTTP/{tok}\r\nAuthorization: Bearer {tok}\r\n\r\n".encode())
        s.raw(f"GET /requests HTTP/1.0\r\n{tok}\r\n\r\n".encode())
        s.raw(f"POST /requests HTTP/1.0\r\nAuthorization: Bearer {tok}\r\nContent-Length: {tok}\r\n\r\n".encode())
        s.raw(f"GET /requests HTTP/1.0\r\nAuthorization: Bearer {tok}\r\n\r\n".encode(), source=OTHER)
        text = self.logs.text
        self.assertIn(f"GET /requests/{{rid}} rid={rid} 200", text)   # the log does work
        self.assertIn("401", text)
        self.assertIn("403", text)
        self.assertNotIn(tok, text)
        self.assertNotIn(tok[:16], text)
        self.assertNotIn(tok.lower()[:16], text)

    def test_internal_error_with_token_in_the_exception(self):
        def boom():
            raise RuntimeError(f"something mentioned {TOKEN}")
        self.env.service.list_requests = boom
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(RedactingFormatter("%(levelname)s %(name)s: %(message)s", [TOKEN]))
        logging.getLogger("forge_api").addHandler(handler)
        self.addCleanup(logging.getLogger("forge_api").removeHandler, handler)
        code, out, _ = self.srv.call("GET", "/requests")
        self.assertEqual((code, out), (500, {"error": "internal error"}))
        self.assertNotIn(TOKEN, str(out))
        self.assertIn("Traceback", buf.getvalue())
        self.assertIn("[REDACTED]", buf.getvalue())
        self.assertNotIn(TOKEN, buf.getvalue())

    def test_a_title_holding_the_token_is_scrubbed_by_the_formatter(self):
        lower = "abcdefghijklmnopqrstuvwxyz0123456789-secret"   # a token the slug keeps as is
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(RedactingFormatter("%(message)s", [lower]))
        logging.getLogger("forge_api").addHandler(handler)
        self.addCleanup(logging.getLogger("forge_api").removeHandler, handler)
        code, out, _ = self.srv.call("POST", "/requests", {"title": lower, "why": "y",
                                                           "queries": [{"query": "a", "why": "b"}]})
        self.assertEqual(code, 201, out)
        self.assertIn("created request", buf.getvalue())
        self.assertNotIn(lower, buf.getvalue())


class SetupLoggingTests(unittest.TestCase):
    def test_setup_logging_installs_the_redactor(self):
        root = logging.getLogger()
        saved = (list(root.handlers), root.level)

        def restore():
            for h in list(root.handlers):
                root.removeHandler(h)
            for h in saved[0]:
                root.addHandler(h)
            root.setLevel(saved[1])
        self.addCleanup(restore)
        setup_logging(False, [TOKEN])
        self.assertEqual(len(root.handlers), 1)
        buf = io.StringIO()
        root.handlers[0].setStream(buf)
        logging.getLogger("forge_api.x").warning("value %s", TOKEN)
        self.assertEqual(buf.getvalue(), "WARNING forge_api.x: value [REDACTED]\n")


if __name__ == "__main__":
    unittest.main()
