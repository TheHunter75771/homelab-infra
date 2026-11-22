"""A fake S for test_ai_control.yml: GET /api/summary answers with the JSON in a scenario file,
re-read on every request (the test rewrites it per case). Binds 127.0.0.1 on a free port, writes the
port to a file, and exits after a deadline. Usage: fake_summary.py <scenario.json> <port-file> <seconds>"""
import http.server
import sys
import threading

scenario, port_file, seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server's naming)
        if self.path != "/api/summary":
            self.send_error(404)
            return
        with open(scenario, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
with open(port_file + ".tmp", "w") as fh:
    fh.write(str(server.server_address[1]))
import os  # noqa: E402
os.replace(port_file + ".tmp", port_file)
threading.Timer(seconds, server.shutdown).start()
server.serve_forever()
