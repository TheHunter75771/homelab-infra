"""Tests for mc-notify (B2-CONTRACT.md §5): stdlib unittest, a local fake Mission Control (HTTP on 127.0.0.1, and
HTTPS with a throwaway self-signed certificate when the openssl CLI exists). No network, no root, no real token:
every token is generated per test (secrets.token_urlsafe).

Run from the repo root:  python3 -m unittest discover -s roles/mc_notify_client/files/tests -v

Most cases run the real CLI in a subprocess (python3 -I mc_notify.py, as its shebang does), and every one of those
runs checks that no 8 consecutive characters of the token, or of its base64 at any alignment, are in stdout or
stderr. The rest call main() in-process (same check) to patch a constant (the 10 s deadline, the /etc path), stall
the name lookup, or inject an exception that quotes the token.
"""

from __future__ import annotations

import base64
import contextlib
import http.server
import importlib.util
import io
import json
import os
import pathlib
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "mc_notify.py"
SPEC = importlib.util.spec_from_file_location("mc_notify", SCRIPT)
mc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mc)


# ---- a fake Mission Control ------------------------------------------------------------------------------------
class FakeMC:
    """Records every request; answers from a queue of (status, body, headers, delay), else 202 {"status": "sent"}."""

    def __init__(self, tls: tuple[str, str] | None = None):
        self.requests: list[dict] = []
        self.answers: list[tuple] = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                fake.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers.items()),
                                      "body": body})
                status, out, headers, delay = fake.answers.pop(0) if fake.answers else (
                    202, b'{"status": "sent"}', {}, 0)
                if delay:
                    time.sleep(delay)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(out)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(out)

            do_POST = do_GET = do_PUT = _handle

            def log_message(self, *args):
                pass

        class Server(http.server.ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # a client that timed out or refused the certificate; nothing to report

        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*tls)
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, scheme="http", path="/api/notify"):
        return f"{scheme}://127.0.0.1:{self.port}{path}"

    def answer(self, status, body=b"", headers=None, delay=0):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.answers.append((status, raw, headers or {}, delay))

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class DripServer:
    """Answers the status line, then one header byte every `interval` seconds, forever: no single read waits long,
    so only a deadline for the whole exchange ends it."""

    def __init__(self, interval=0.1):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.interval = interval
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._drip, args=(conn,), daemon=True).start()

    def _drip(self, conn):
        try:
            conn.sendall(b"HTTP/1.1 202 Accepted\r\n")
            while not self.stop.is_set():
                conn.sendall(b"X")
                time.sleep(self.interval)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.stop.set()
        self.sock.close()


def secret_pieces(secret: str, n: int = 8) -> set[str]:
    """Every n consecutive characters of the secret and of its standard base64 at the 3 byte alignments."""
    raw = secret.encode()
    forms = [secret] + [base64.b64encode(raw[i:][: len(raw[i:]) // 3 * 3]).decode() for i in range(3)]
    return {f[i:i + n] for f in forms if f for i in range(max(1, len(f) - n + 1))}


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---- shared setup: a config + token pair per test ----------------------------------------------------------------
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="mc-notify-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.mc = FakeMC()
        self.addCleanup(self.mc.close)
        self.token = secrets.token_urlsafe(32)
        self.token_file = self.tmp / "token"
        self.write_token(self.token + "\n")
        self.config = self.tmp / "config.json"
        self.write_config({"url": self.mc.url(), "token_file": str(self.token_file)})
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.tmp / "home"), "LANG": "C.UTF-8",
                    "MC_NOTIFY_CONFIG": str(self.config), mc.INSECURE_ENV: "1"}
        mc.OUT.secret = ""

    def write_token(self, text, mode=0o600, path=None):
        path = pathlib.Path(path or self.token_file)
        path.write_bytes(text.encode() if isinstance(text, str) else text)
        os.chmod(path, mode)

    def write_config(self, cfg, mode=0o600, path=None):
        path = pathlib.Path(path or self.config)
        path.write_text(cfg if isinstance(cfg, str) else json.dumps(cfg))
        os.chmod(path, mode)

    def run_cli(self, *args, env=None, timeout=30):
        e = dict(self.env)
        e.update(env or {})
        e = {k: v for k, v in e.items() if v is not None}
        p = subprocess.run([sys.executable, "-I", str(SCRIPT), *args], env=e, capture_output=True, timeout=timeout,
                           check=False)
        p.stdout, p.stderr = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
        self.assertNoToken(p.stdout + p.stderr)
        return p

    def assertNoToken(self, text):
        for piece in secret_pieces(self.token):
            self.assertNotIn(piece, text, "8 characters of the token (or its base64) appeared in the output")

    def sent(self):
        self.assertEqual(len(self.mc.requests), 1, self.mc.requests)
        return json.loads(self.mc.requests[0]["body"])

    def in_process(self, *args, env=None):
        out, err = io.StringIO(), io.StringIO()
        e = dict(self.env)
        e.update(env or {})
        with mock.patch.dict(os.environ, {k: v for k, v in e.items() if v is not None}, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mc.main(list(args))
        self.assertNoToken(out.getvalue() + err.getvalue())
        return rc, out.getvalue(), err.getvalue()


# ---- exit codes ------------------------------------------------------------------------------------------------
class ExitCodes(Base):
    def test_202_exits_0_and_prints_the_status(self):
        for status in ("sent", "deduped", "held"):
            self.mc.requests.clear()
            self.mc.answer(202, {"status": status})
            p = self.run_cli("--title", "Backup done", "--tag", "backup")
            self.assertEqual((p.returncode, p.stdout, p.stderr), (0, status + "\n", ""))
            self.assertEqual(len(self.mc.requests), 1)

    def test_202_without_a_usable_status_prints_accepted(self):
        for body in (b"", b"not json", b'{"status": 5}', b'{"status": "SENT now"}', b"[1]"):
            self.mc.answer(202, body)
            p = self.run_cli("--title", "x", "--tag", "t")
            self.assertEqual((p.returncode, p.stdout), (0, "accepted\n"), body)

    def test_429_exits_3(self):
        self.mc.answer(429, {"error": "rate limited"}, {"Retry-After": "120"})
        p = self.run_cli("--title", "x", "--tag", "t")
        self.assertEqual(p.returncode, 3)
        self.assertIn("retry after 120 s", p.stderr)
        self.assertEqual(p.stdout, "")

    def test_429_with_a_strange_retry_after(self):
        self.mc.answer(429, b"", {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        p = self.run_cli("--title", "x", "--tag", "t")
        self.assertEqual(p.returncode, 3)
        self.assertNotIn("retry after", p.stderr)

    def test_every_other_answer_exits_1(self):
        for status in (200, 201, 204, 400, 401, 403, 404, 405, 413, 415, 500, 502, 503):
            self.mc.answer(status, {"error": "unauthorized"} if status == 401 else {"status": "sent"})
            p = self.run_cli("--title", "x", "--tag", "t")
            self.assertEqual(p.returncode, 1, status)
            self.assertIn(f"HTTP {status}", p.stderr)
            self.assertEqual(p.stdout, "", status)

    def test_400_names_the_field_only_when_it_is_one(self):
        self.mc.answer(400, {"error": "title"})
        self.assertIn("(field: title)", self.run_cli("--title", "x", "--tag", "t").stderr)
        self.mc.answer(400, {"error": "something else entirely"})
        self.assertNotIn("something", self.run_cli("--title", "x", "--tag", "t").stderr)

    def test_a_redirect_is_not_followed_and_the_token_goes_nowhere_else(self):
        other = FakeMC()
        self.addCleanup(other.close)
        for code in (301, 302, 303, 307, 308):
            self.mc.answer(code, b"", {"Location": other.url()})
            p = self.run_cli("--title", "x", "--tag", "t")
            self.assertEqual(p.returncode, 1, code)
            self.assertIn(f"HTTP {code}", p.stderr)
        self.assertEqual(other.requests, [])

    def test_connection_refused_exits_1(self):
        self.write_config({"url": f"http://127.0.0.1:{closed_port()}/api/notify", "token_file": str(self.token_file)})
        p = self.run_cli("--title", "x", "--tag", "t")
        self.assertEqual(p.returncode, 1)
        self.assertIn("couldn't reach Mission Control", p.stderr)

    def test_a_server_that_hangs_times_out_with_exit_1(self):
        self.mc.answer(202, {"status": "sent"}, delay=3)
        with mock.patch.object(mc, "TIMEOUT_S", 0.5):
            t0 = time.monotonic()
            rc, _, err = self.in_process("--title", "x", "--tag", "t")
        self.assertEqual(rc, 1)
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertIn("timed out", err)

    def test_a_slow_drip_answer_is_cut_by_the_overall_deadline(self):
        drip = DripServer(interval=0.1)
        self.addCleanup(drip.close)
        self.write_config({"url": f"http://127.0.0.1:{drip.port}/api/notify", "token_file": str(self.token_file)})
        with mock.patch.object(mc, "TIMEOUT_S", 1):
            t0 = time.monotonic()
            rc, out, err = self.in_process("--title", "x", "--tag", "t")
            took = time.monotonic() - t0
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("no answer within 1 s", err)
        self.assertLess(took, 2.0)  # every read gets a byte within 0.1 s: only the deadline can stop it

    def test_a_stuck_name_lookup_is_cut_by_the_deadline(self):
        # No real DNS: getaddrinfo is replaced by one that hangs (as a dead resolver does), then fails.
        def stuck(*args, **kwargs):
            time.sleep(2)
            raise OSError("resolver gave up")

        self.write_config({"url": "https://ai-box.example.ts.net:8443/api/notify", "token_file": str(self.token_file)})
        with mock.patch.object(mc, "TIMEOUT_S", 0.5), mock.patch("socket.getaddrinfo", stuck):
            t0 = time.monotonic()
            rc, _, err = self.in_process("--title", "x", "--tag", "t")
            took = time.monotonic() - t0
        self.assertEqual(rc, 1)
        self.assertIn("no answer within 0.5 s", err)
        self.assertLess(took, 1.5)

    def test_the_timeout_is_10_seconds(self):
        self.assertEqual(mc.TIMEOUT_S, 10)
        seen = {}

        class Opener:
            def open(self, req, timeout=None):
                seen["timeout"] = timeout
                raise mc.urllib.error.URLError(OSError(111, "Connection refused"))

        with mock.patch.object(mc, "_opener", lambda: Opener()):
            rc, _, err = self.in_process("--title", "x", "--tag", "t")
        self.assertEqual((rc, seen["timeout"]), (1, 10))
        self.assertIn("connection refused", err)

    def test_usage_errors_exit_2_and_send_nothing(self):
        cases = [
            [],
            ["--title", "x"],                                  # no tag
            ["--tag", "t"],                                    # no title
            ["--title", "   ", "--tag", "t"],                  # empty after strip
            ["--title", "x" * 81, "--tag", "t"],
            ["--title", "x", "--tag", "t", "--body", "b" * 301],
            ["--title", "x", "--tag", ""],
            ["--title", "x", "--tag", "-leading-dash"],
            ["--title", "x", "--tag", "Über"],                 # sanitizes to "-ber"
            ["--title", "x", "--tag", "t", "--url", "//evil.example/"],
            ["--unit", "a.service", "--tag", "t"],             # --unit sets the tag
            ["--unit", ""],
            ["--unit", "a b.service"],
            ["--title", "x", "--tag", "t", "--bogus"],
        ]
        for args in cases:
            p = self.run_cli(*args)
            self.assertEqual(p.returncode, 2, args)
            self.assertIn("mc-notify: ", p.stderr, args)
            self.assertEqual(p.stdout, "", args)
        self.assertEqual(self.mc.requests, [])

    def test_arguments_that_are_not_utf8_exit_2(self):
        for args in ([b"--title", b"x", b"--tag", b"t", b"--body", b"\xff"],
                     [b"--title", b"caf\xe9", b"--tag", b"t"],
                     [b"--title", b"x", b"--tag", b"t\xc3"],
                     [b"--title", b"x", b"--tag", b"t", b"--url", b"/\xff"],
                     [b"--unit", b"a\xff.service"]):
            p = self.run_cli(*args)
            self.assertEqual((p.returncode, p.stdout), (2, ""), args)
            self.assertIn("isn't valid UTF-8", p.stderr, args)
        self.assertEqual(self.mc.requests, [])

    def test_a_request_over_4096_bytes_is_a_usage_error(self):
        # Valid fields can't reach 4096 bytes (UTF-8, not escaped); the check is a guard, so shrink the limit.
        with mock.patch.object(mc, "MAX_REQUEST_BYTES", 60):
            rc, out, err = self.in_process("--title", "x" * 40, "--tag", "t")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("larger than 4096 bytes", err)
        self.assertEqual(self.mc.requests, [])

    def test_the_limits_themselves_are_accepted(self):
        p = self.run_cli("--title", "  " + "x" * 80 + "  ", "--tag", "t" * 64, "--body", "b" * 300)
        self.assertEqual(p.returncode, 0, p.stderr)
        msg = self.sent()
        self.assertEqual((msg["title"], msg["tag"], len(msg["body"])), ("x" * 80, "t" * 64, 300))

    def test_help_exits_0(self):
        p = self.run_cli("--help")
        self.assertEqual(p.returncode, 0)
        self.assertIn("--unit", p.stdout)


# ---- config ----------------------------------------------------------------------------------------------------
class Config(Base):
    def assertConfigError(self, needle=None, env=None):
        p = self.run_cli("--title", "x", "--tag", "t", env=env)
        self.assertEqual(p.returncode, 2, p.stderr)
        if needle:
            self.assertIn(needle, p.stderr)
        self.assertEqual(self.mc.requests, [])
        return p

    def test_mc_notify_config_that_is_missing_is_an_error_not_a_fallback(self):
        user = self.tmp / "home" / ".config" / "mc-notify"
        user.mkdir(parents=True)
        self.write_config({"url": self.mc.url(), "token_file": str(self.token_file)}, path=user / "config.json")
        self.assertConfigError("doesn't exist", env={"MC_NOTIFY_CONFIG": str(self.tmp / "nope.json")})

    def test_order_env_then_etc_then_home(self):
        system = self.tmp / "etc-config.json"
        user_dir = self.tmp / "home" / ".config" / "mc-notify"
        user_dir.mkdir(parents=True)
        other = FakeMC()
        self.addCleanup(other.close)
        self.write_config({"url": other.url(), "token_file": str(self.token_file)}, path=user_dir / "config.json")
        with mock.patch.object(mc, "SYSTEM_CONFIG", str(system)):
            # No /etc file: the user's.
            rc, _, err = self.in_process("--title", "x", "--tag", "t", env={"MC_NOTIFY_CONFIG": None})
            self.assertEqual((rc, len(other.requests), len(self.mc.requests)), (0, 1, 0), err)
            # An /etc file: it wins over the user's.
            self.write_config({"url": self.mc.url(), "token_file": str(self.token_file)}, path=system)
            rc, _, err = self.in_process("--title", "x", "--tag", "t", env={"MC_NOTIFY_CONFIG": None})
            self.assertEqual((rc, len(other.requests), len(self.mc.requests)), (0, 1, 1), err)
            # MC_NOTIFY_CONFIG wins over both.
            third = FakeMC()
            self.addCleanup(third.close)
            self.write_config({"url": third.url(), "token_file": str(self.token_file)}, path=self.tmp / "env.json")
            env = {"MC_NOTIFY_CONFIG": str(self.tmp / "env.json")}
            rc, _, err = self.in_process("--title", "x", "--tag", "t", env=env)
            self.assertEqual((rc, len(third.requests)), (0, 1), err)
            # None at all: exit 2.
            os.unlink(system)
            os.unlink(user_dir / "config.json")
            rc, _, err = self.in_process("--title", "x", "--tag", "t", env={"MC_NOTIFY_CONFIG": None})
            self.assertEqual(rc, 2)
            self.assertIn(str(user_dir / "config.json"), err)

    def test_the_real_default_paths(self):
        self.assertEqual(mc.SYSTEM_CONFIG, "/etc/mc-notify/config.json")
        with mock.patch.dict(os.environ, {"HOME": "/home/someone"}, clear=True), \
                mock.patch.object(mc, "SYSTEM_CONFIG", str(self.tmp / "absent")):
            self.assertEqual(mc.config_path(), "/home/someone/.config/mc-notify/config.json")

    def test_bad_configs(self):
        tf = str(self.token_file)
        for cfg, needle in [
            ("not json", "isn't valid JSON"),
            ('["a list"]', "must be a JSON object"),
            ({"url": self.mc.url()}, "missing token_file"),
            ({"token_file": tf}, "missing url"),
            ({"url": self.mc.url(), "token_file": tf, "token": "x"}, "; 1 unknown key"),
            ({"url": self.mc.url(), "token_file": tf, "token": "x", "secret": "y"}, "; 2 unknown keys"),
            ({"url": 5, "token_file": tf}, "must be strings"),
            ({"url": self.mc.url(), "token_file": "token"}, "absolute path"),
            ({"url": "ftp://127.0.0.1/api/notify", "token_file": tf}, "must be https://"),
            ({"url": "https://u:p@ai-box.example.ts.net:8443/api/notify", "token_file": tf}, "credentials"),
            ({"url": "https://ai-box.example.ts.net:8443/api/notify#x", "token_file": tf}, "fragment"),
            ({"url": "https://ai-box.example.ts.net:99999/api/notify", "token_file": tf}, "doesn't parse"),
            ({"url": "https://ai-box.example.ts.net/api notify", "token_file": tf}, "spaces"),
        ]:
            self.write_config(cfg)
            self.assertConfigError(needle)

    def test_a_config_others_can_write_is_refused(self):
        for mode in (0o620, 0o602, 0o666, 0o664):
            self.write_config({"url": self.mc.url(), "token_file": str(self.token_file)}, mode=mode)
            self.assertConfigError("not be writable by group or others")
        self.write_config({"url": self.mc.url(), "token_file": str(self.token_file)}, mode=0o644)
        self.assertEqual(self.run_cli("--title", "x", "--tag", "t").returncode, 0)

    def test_http_only_to_127_0_0_1_and_only_with_the_test_switch(self):
        url = self.mc.url()
        for value in (None, "", "0", "true", "yes", "1 "):
            self.assertConfigError("only with MC_NOTIFY_ALLOW_INSECURE_LOCAL=1", env={mc.INSECURE_ENV: value})
        for bad in (f"http://localhost:{self.mc.port}/api/notify", f"http://127.0.0.2:{self.mc.port}/api/notify",
                    f"http://[::1]:{self.mc.port}/api/notify", f"http://127.0.0.1.nip.io:{self.mc.port}/api/notify",
                    f"http://x@127.0.0.1:{self.mc.port}/api/notify", f"http://0x7f000001:{self.mc.port}/api/notify",
                    "http://ai-box.example.ts.net:8443/api/notify"):
            self.write_config({"url": bad, "token_file": str(self.token_file)})
            self.assertConfigError()
        self.write_config({"url": url, "token_file": str(self.token_file)})
        self.assertEqual(self.run_cli("--title", "x", "--tag", "t").returncode, 0)


# ---- the token file --------------------------------------------------------------------------------------------
class TokenFile(Base):
    def refused(self, needle):
        p = self.run_cli("--title", "x", "--tag", "t")
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn(needle, p.stderr)
        self.assertEqual(self.mc.requests, [])

    def test_readable_or_writable_by_group_or_others_is_refused(self):
        for mode in (0o640, 0o604, 0o644, 0o660, 0o606, 0o610, 0o601, 0o620, 0o602, 0o666):
            self.write_token(self.token + "\n", mode=mode)
            self.refused(f"mode {mode:04o}")

    def test_0600_and_tighter_are_accepted(self):
        for mode in (0o600, 0o400):
            self.mc.requests.clear()
            self.write_token(self.token + "\n", mode=mode)
            p = self.run_cli("--title", "x", "--tag", "t")
            self.assertEqual(p.returncode, 0, (mode, p.stderr))
            self.assertEqual(self.mc.requests[0]["headers"]["Authorization"], "Bearer " + self.token)

    def test_a_symlink_is_refused(self):
        real = self.tmp / "real-token"
        self.write_token(self.token, path=real)
        os.unlink(self.token_file)
        os.symlink(real, self.token_file)
        self.refused("symlink")

    def test_missing_directory_fifo(self):
        os.unlink(self.token_file)
        self.refused("doesn't exist")
        os.mkdir(self.token_file, 0o700)
        self.refused("isn't a regular file")
        os.rmdir(self.token_file)
        os.mkfifo(self.token_file, 0o600)
        self.refused("isn't a regular file")  # and doesn't hang (O_NONBLOCK)

    def test_contents(self):
        for content, needle in [
            ("", "one token"),
            ("  \n\n", "one token"),
            (self.token[:20] + " " + self.token[20:], "one token"),
            (self.token[:20] + "\n" + self.token[20:], "one token"),
            (self.token + "\x00", "one token"),
            ("tökén" + self.token, "non-ASCII"),
            (b"\xff" + self.token.encode(), "non-ASCII"),
            ("x" * 5000, "larger than 4 KiB"),
        ]:
            self.write_token(content)
            self.refused(needle)

    def test_surrounding_whitespace_is_dropped(self):
        self.write_token("\n  " + self.token + " \r\n")
        self.assertEqual(self.run_cli("--title", "x", "--tag", "t").returncode, 0)
        self.assertEqual(self.mc.requests[0]["headers"]["Authorization"], "Bearer " + self.token)


# ---- tags and --unit -------------------------------------------------------------------------------------------
class Tags(Base):
    def test_sanitize(self):
        for raw, want in [
            ("backup", "backup"),
            ("Backup FAILED (nightly)", "backup-failed--nightly-"),
            ("A_b.c-D9", "a_b.c-d9"),
            ("disk/sda1:full", "disk-sda1-full"),
            ("Über", "-ber"),
            ("x" * 100, "x" * 64),
            ("ab\ncd\t", "ab-cd-"),
        ]:
            self.assertEqual(mc.sanitize_tag(raw), want, raw)

    def test_the_sanitized_tag_is_what_is_sent(self):
        p = self.run_cli("--title", "x", "--tag", "Backup FAILED (nightly) on " + "Z" * 80)
        self.assertEqual(p.returncode, 0, p.stderr)
        tag = self.sent()["tag"]
        self.assertEqual(tag, ("backup-failed--nightly--on-" + "z" * 80)[:64])
        self.assertEqual(len(tag), 64)


class UnitDefaults(Base):
    def test_unit_sets_title_body_and_tag(self):
        p = self.run_cli("--unit", "ai-openwebui-backup.service")
        self.assertEqual((p.returncode, p.stdout), (0, "sent\n"), p.stderr)
        self.assertEqual(self.sent(), {"title": "ai-openwebui-backup.service failed",
                                       "body": "journalctl -u ai-openwebui-backup.service",
                                       "tag": "unit-ai-openwebui-backup.service"})

    def test_given_title_or_body_win_one_by_one(self):
        self.run_cli("--unit", "homelab-pull.service", "--title", "Backup pull failed")
        self.assertEqual(self.sent()["title"], "Backup pull failed")
        self.assertEqual(self.sent()["body"], "journalctl -u homelab-pull.service")
        self.mc.requests.clear()
        self.run_cli("--unit", "homelab-pull.service", "--body", "")
        self.assertEqual((self.sent()["title"], self.sent()["body"]), ("homelab-pull.service failed", ""))

    def test_an_escaped_or_templated_unit_name(self):
        unit = r"systemd-fsck@dev-disk-by\x2duuid-1234.service"
        self.run_cli("--unit", unit, "--urgent")
        msg = self.sent()
        self.assertEqual(msg["tag"], "unit-systemd-fsck-dev-disk-by-x2duuid-1234.service")
        self.assertEqual(msg["title"], unit + " failed")
        self.assertTrue(msg["urgent"])

    def test_a_unit_name_that_starts_with_a_dash(self):
        # The root mount is "-.mount": the units pass --unit=%i, so it can't be read as an option.
        p = self.run_cli("--unit=-.mount")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.sent(), {"title": "-.mount failed", "body": "journalctl -u -.mount", "tag": "unit--.mount"})

    def test_a_long_unit_still_sends_within_the_limits(self):
        unit = "a" * 100 + ".service"
        p = self.run_cli("--unit", unit)
        self.assertEqual(p.returncode, 0, p.stderr)
        msg = self.sent()
        self.assertEqual(len(msg["title"]), 80)
        self.assertTrue(msg["title"].endswith("… failed"))
        self.assertEqual(msg["tag"], ("unit-" + unit)[:64])
        self.assertEqual(msg["body"], "journalctl -u " + unit)


# ---- the request itself ----------------------------------------------------------------------------------------
class Request(Base):
    def test_shape(self):
        p = self.run_cli("--title", " Disk 91% ", "--body", "line 1\nline 2", "--tag", "disk", "--url", "/servers")
        self.assertEqual(p.returncode, 0, p.stderr)
        r = self.mc.requests[0]
        self.assertEqual((r["method"], r["path"]), ("POST", "/api/notify"))
        self.assertEqual(r["headers"]["Authorization"], "Bearer " + self.token)
        self.assertEqual(r["headers"]["Content-Type"], "application/json")
        self.assertEqual(json.loads(r["body"]), {"title": "Disk 91%", "body": "line 1\nline 2", "tag": "disk",
                                                 "url": "/servers"})
        self.mc.requests.clear()
        self.run_cli("--title", "x", "--tag", "t", "--urgent")
        self.assertEqual(self.sent(), {"title": "x", "tag": "t", "urgent": True})

    def test_only_given_fields_are_sent_never_null_or_an_unknown_key(self):
        # MC1 answers 400 to a null, a wrong type or a key outside title/body/tag/url/urgent (B2-CONTRACT.md §1).
        cases = [
            (["--title", "x", "--tag", "t"], {"title", "tag"}),
            (["--title", "x", "--tag", "t", "--body", ""], {"title", "tag", "body"}),
            (["--title", "x", "--tag", "t", "--body", "b", "--url", "/", "--urgent"],
             {"title", "tag", "body", "url", "urgent"}),
            (["--title", "x", "--tag", "t", "--url", "https://ai-box.example.ts.net:8443/"], {"title", "tag", "url"}),
            (["--unit", "a.service"], {"title", "tag", "body"}),
            (["--unit", "a.service", "--urgent", "--url", "/servers"], {"title", "tag", "body", "url", "urgent"}),
        ]
        for args, keys in cases:
            self.mc.requests.clear()
            p = self.run_cli(*args)
            self.assertEqual(p.returncode, 0, (args, p.stderr))
            msg = self.sent()
            self.assertEqual(set(msg), keys, args)
            self.assertLessEqual(set(msg), {"title", "body", "tag", "url", "urgent"})
            self.assertNotIn(None, msg.values(), args)
            self.assertNotIn(b"null", self.mc.requests[0]["body"], args)
            for k in ("title", "body", "tag", "url"):
                if k in msg:
                    self.assertIsInstance(msg[k], str, (args, k))
            if "urgent" in msg:
                self.assertIs(msg["urgent"], True, args)

    def test_the_title_is_one_line(self):
        for title in ("a\nb", "a\rb", "a\tb", "a\x7fb", "\x1b[31mred", "a\u2028b", "a\u2029b", "a\x85b",
                      # Cf, as MC1 refuses them: zero-width space and joiner, bidi override and isolate, BOM.
                      "a\u200bb", "a\u200db", "\u202eevil", "a\u2066b", "\ufeffa"):
            self.assertEqual(self.run_cli("--title", title, "--tag", "t").returncode, 2, repr(title))
        rc, _, err = self.in_process("--title", "a\ud800b", "--tag", "t")  # a lone surrogate: not UTF-8 either
        self.assertEqual(rc, 2)
        self.assertIn("isn't valid UTF-8", err)
        rc, _, err = self.in_process("--title", "a\u200bb", "--tag", "t")
        self.assertEqual(rc, 2)
        self.assertIn("one line", err)
        self.assertEqual(self.mc.requests, [])
        p = self.run_cli("--title", "Disk 91% \u2014 ai-box \U0001f525", "--tag", "t", "--body", "two\nlines")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.sent()["body"], "two\nlines")

    def test_url_rule(self):
        good = ["/", "/servers", "/a/b?c=d", "/#cards", "https://ai-box.example.ts.net:8443/",
                "https://homebk-edge.x.ts.net/a", "/" + "a" * 511]
        bad = ["", "servers", "//evil.example/", "/a//b", "/\\evil.example", "/a\\b", "http://ai-box.example.ts.net/",
               "HTTPS://ai-box.example.ts.net/", "https://evil.example/", "https://u@ai-box.example.ts.net/",
               "https://u:p@ai-box.example.ts.net/", "https://.ts.net/", "https://ts.net/", "https://-x.ts.net/",
               "https://x.ts.net.evil.example/", "https://x.ts.net//a", "https://x.ts.net/\\a", "javascript:alert(1)",
               "/a b", "/caf\u00e9", "https://x.ts.net:99999/", "/" + "a" * 512]
        for u in good:
            self.assertTrue(mc.url_ok(u), u)
        for u in bad:
            self.assertFalse(mc.url_ok(u), u)
            self.assertEqual(self.run_cli("--title", "x", "--tag", "t", "--url", u).returncode, 2, u)
        self.assertEqual(self.mc.requests, [])

    def test_proxy_variables_are_ignored(self):
        proxy = FakeMC()
        self.addCleanup(proxy.close)
        purl = f"http://127.0.0.1:{proxy.port}"
        env = {k: purl for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")}
        p = self.run_cli("--title", "x", "--tag", "t", env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual((len(self.mc.requests), proxy.requests), (1, []))


# ---- the token is never printed --------------------------------------------------------------------------------
class TokenNeverPrinted(Base):
    def test_a_server_that_echoes_the_token(self):
        for status, body in [(202, {"status": self.token}), (400, {"error": self.token}), (401, {"error": self.token}),
                             (500, self.token.encode()), (429, {"error": self.token})]:
            self.mc.answer(status, body, {"Retry-After": self.token} if status == 429 else {})
            self.run_cli("--title", "x", "--tag", "t")  # run_cli checks stdout and stderr

    def test_a_lowercase_token_echoed_as_a_status(self):
        self.token = "a" * 32
        self.write_token(self.token)
        self.mc.answer(202, {"status": self.token})
        p = self.run_cli("--title", "x", "--tag", "t")
        self.assertEqual(p.stdout, "<redacted>\n")

    def test_exceptions_that_quote_the_token(self):
        token = self.token

        class Opener:
            def __init__(self, exc):
                self.exc = exc

            def open(self, req, timeout=None):
                raise self.exc

        for exc in (ValueError(f"Invalid header value b'Bearer {token}\\n'"), RuntimeError(token),
                    OSError(f"boom {token}"), mc.urllib.error.URLError(f"reason {token}")):
            with mock.patch.object(mc, "_opener", lambda e=exc: Opener(e)):
                rc, _out, _err = self.in_process("--title", "x", "--tag", "t")  # in_process checks the output
            self.assertEqual(rc, 1, exc)

    def test_error_paths_never_print_it(self):
        self.write_token(self.token + "\n", mode=0o644)
        self.run_cli("--title", "x", "--tag", "t")
        self.write_token(self.token + " extra")
        self.run_cli("--title", "x", "--tag", "t")
        self.write_config('{"url": "' + self.token + '", "token_file": "' + str(self.token_file) + '"}')
        self.run_cli("--title", "x", "--tag", "t")
        self.write_config('{"' + self.token + '": 1}')
        p = self.run_cli("--title", "x", "--tag", "t")
        self.assertEqual(p.returncode, 2)
        self.assertIn("missing token_file, url; 1 unknown key", p.stderr)
        # A token pasted as the key next to the right ones: still only counted (the review's repro).
        self.write_config({"url": self.mc.url(), "token_file": str(self.token_file), self.token: "x"})
        p = self.run_cli("--title", "x", "--tag", "t")
        self.assertEqual(p.returncode, 2)
        self.assertIn("; 1 unknown key", p.stderr)

    def test_the_check_itself_sees_a_cut_token(self):
        # assertNoToken must fail on any 8 characters of the token or its base64, not only on the whole token.
        for leak in (self.token[:8], self.token[5:13], self.token[-8:], base64.b64encode(self.token.encode()).decode()[4:12]):
            with self.assertRaises(AssertionError):
                self.assertNoToken("mc-notify: config has an unknown key " + leak)
        self.assertNoToken("mc-notify: " + self.token[:7])

    def test_output_holding_part_of_the_token_is_redacted(self):
        # A server echoing a cut token as its status: the whole line goes, not just an exact match.
        self.token = "a" * 40
        self.write_token(self.token)
        self.mc.answer(202, {"status": "a" * 12})
        p = self.run_cli("--title", "x", "--tag", "t")
        self.assertEqual((p.returncode, p.stdout), (0, "<redacted>\n"))


# ---- HTTPS: verified by the default context --------------------------------------------------------------------
@unittest.skipUnless(shutil.which("openssl"), "needs the openssl CLI to make a throwaway certificate")
class Https(Base):
    def setUp(self):
        super().setUp()
        self.cert, self.key = str(self.tmp / "cert.pem"), str(self.tmp / "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj",
                        "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", self.key, "-out",
                        self.cert], check=True, capture_output=True, timeout=60)
        self.tls = FakeMC(tls=(self.cert, self.key))
        self.addCleanup(self.tls.close)
        self.write_config({"url": self.tls.url("https"), "token_file": str(self.token_file)})

    def test_an_unknown_certificate_is_refused_even_with_the_test_switch(self):
        for switch in ("1", None):
            p = self.run_cli("--title", "x", "--tag", "t", env={mc.INSECURE_ENV: switch, "SSL_CERT_FILE": None})
            self.assertEqual(p.returncode, 1)
            self.assertIn("TLS certificate verification failed", p.stderr)
        self.assertEqual(self.tls.requests, [])

    def test_a_trusted_certificate_works(self):
        p = self.run_cli("--title", "x", "--tag", "t", env={mc.INSECURE_ENV: None, "SSL_CERT_FILE": self.cert})
        self.assertEqual((p.returncode, p.stdout), (0, "sent\n"), p.stderr)
        self.assertEqual(self.tls.requests[0]["headers"]["Authorization"], "Bearer " + self.token)

    def test_the_wrong_name_is_refused(self):
        self.write_config({"url": f"https://localhost:{self.tls.port}/api/notify", "token_file": str(self.token_file)})
        p = self.run_cli("--title", "x", "--tag", "t", env={"SSL_CERT_FILE": self.cert})
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.tls.requests, [])


if __name__ == "__main__":
    unittest.main()
