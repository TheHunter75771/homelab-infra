"""Fake MC1 for verify_http.yml: GET /api/cards on 127.0.0.1:<port> (and /healthz, no identity, like MC1).
Usage: fake_server.py <port> <login> <mode>
  good      200 when Tailscale-User-Login == login, else 403 (what MC1 must do)
  open      200 for everyone (no identity check)
  deny-all  403 for everyone
  any-login 200 for any non-empty header, 403 without one
  no-header 200 without the header or with the login, 403 for another login (trusts a missing header)
"""
import http.server
import sys

PORT, LOGIN, MODE = int(sys.argv[1]), sys.argv[2], sys.argv[3]


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Length", "11")
            self.end_headers()
            self.wfile.write(b'{"ok": true}'[:11])
            return
        who = self.headers.get("Tailscale-User-Login")
        ok = {"good": who == LOGIN, "open": True, "deny-all": False, "any-login": bool(who),
              "no-header": who is None or who == LOGIN}[MODE]
        body = b'{"cards": []}' if ok else b'{"error": "forbidden"}'
        self.send_response(200 if ok and self.path == "/api/cards" else 403 if not ok else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


http.server.HTTPServer(("127.0.0.1", PORT), H).serve_forever()
