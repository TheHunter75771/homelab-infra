"""Fake SearXNG for the offline verify test: /healthz -> 200 OK, /search?q=..&format=json -> SearXNG-shaped JSON.
Usage: python3 fake_searxng.py PORTFILE  (binds 127.0.0.1 on a free port and writes it to PORTFILE)."""
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server's name)
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/healthz":
            body, ctype = b"OK", "text/plain"
        elif url.path == "/search" and query.get("format") == ["json"]:
            body = json.dumps({"query": query.get("q", [""])[0], "number_of_results": 0, "results": [],
                               "unresponsive_engines": [["duckduckgo", "timeout"]]}).encode()
            ctype = "application/json"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


server = HTTPServer(("127.0.0.1", 0), Handler)
with open(sys.argv[1], "w", encoding="ascii") as fh:
    fh.write(str(server.server_address[1]))
server.serve_forever()
