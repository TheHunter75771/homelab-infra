"""Reviewer Q's fixes: Q-F1 (peer refused before a slot), Q-F2 (bounded write-lock wait),
Q-F3 (systemd socket activation)."""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from forge_api.activation import ActivationError, inherited_socket

from .helpers import APPROVER, OTHER, TOKEN, Env, Served

HERE = Path(__file__).resolve().parent.parent


def idle(port, source, n):
    """n connections from ``source`` that send nothing."""
    socks = []
    for _ in range(n):
        s = socket.create_connection(("127.0.0.1", port), timeout=5, source_address=(source, 0))
        socks.append(s)
    return socks


class PeerBeforeSlotTests(unittest.TestCase):
    """Q-F1: refused peers never take one of the service's connection slots."""

    def setUp(self):
        self.env = Env(self)

    def test_sixteen_refused_peers_do_not_block_a_legitimate_call(self):
        srv = Served(self, self.env, reject_wait=5, socket_timeout=10, request_deadline=30)
        held = idle(srv.port, OTHER, 16)
        self.addCleanup(lambda: [s.close() for s in held])
        time.sleep(0.2)
        t0 = time.monotonic()
        code, _, _ = srv.call("GET", "/requests")
        self.assertEqual(code, 200)
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_refused_peers_cannot_take_even_a_single_slot(self):
        srv = Served(self, self.env, max_concurrent=1, reject_wait=5)
        held = idle(srv.port, OTHER, 40)          # more than the reject pool: the rest closed at once
        self.addCleanup(lambda: [s.close() for s in held])
        time.sleep(0.2)
        self.assertEqual(srv.call("GET", "/requests")[0], 200)
        self.assertEqual(srv.call("GET", "/requests")[0], 200)

    def test_refused_peer_with_reject_pool_full_is_closed_at_once(self):
        srv = Served(self, self.env, max_rejecting=1, reject_wait=5)
        held = idle(srv.port, OTHER, 1)
        self.addCleanup(lambda: [s.close() for s in held])
        time.sleep(0.2)
        with socket.create_connection(("127.0.0.1", srv.port), timeout=5, source_address=(OTHER, 0)) as s:
            t0 = time.monotonic()
            try:
                data = s.recv(100)
            except ConnectionResetError:
                data = b""
            self.assertEqual(data, b"")
            self.assertLess(time.monotonic() - t0, 2.0)

    def test_refused_peer_still_told_403_and_the_reject_is_bounded(self):
        srv = Served(self, self.env, reject_wait=0.3)
        code, out, _ = srv.call("GET", "/requests", source=OTHER)
        self.assertEqual((code, out), (403, {"error": "forbidden"}))
        with socket.create_connection(("127.0.0.1", srv.port), timeout=5, source_address=(OTHER, 0)) as s:
            t0 = time.monotonic()
            data = b""
            while True:   # sends nothing: answered 403 after reject_wait anyway
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            self.assertTrue(data.startswith(b"HTTP/1.0 403 "), data[:60])
            self.assertLess(time.monotonic() - t0, 2.0)

    def test_token_not_needed_to_be_refused_and_never_read(self):
        srv = Served(self, self.env)
        out = srv.raw(f"GET /requests HTTP/1.0\r\nAuthorization: Bearer {TOKEN}\r\n\r\n".encode(), source=OTHER)
        self.assertTrue(out.startswith(b"HTTP/1.0 403 "))
        self.assertNotIn(b"requests", out)


class WriteWaitTests(unittest.TestCase):
    """Q-F2: the in-process write lock is waited for a bounded time, then 503 without the forge."""

    def test_bounded_wait_then_503(self):
        env = Env(self)
        env.service.write_wait = 0.3
        srv = Served(self, env)
        rid = env.new_request()
        env.service._write_lock.acquire()
        try:
            t0 = time.monotonic()
            code, out, _ = srv.call("POST", f"/requests/{rid}/tick-searches",
                                    {"queries": ["apa 7 citation rules"], "user": APPROVER})
            self.assertEqual((code, out), (503, {"error": "the forge is busy; try again in a moment"}))
            self.assertLess(time.monotonic() - t0, 2.0)
            self.assertEqual(srv.call("POST", "/requests", {"title": "x", "why": "y",
                                                            "queries": [{"query": "a", "why": "b"}]})[0], 503)
        finally:
            env.service._write_lock.release()
        self.assertNotIn("\n- [x] ", env.read(rid, "searches.md"))
        self.assertNotIn("approve-chat", env.read(rid, "state.md"))
        self.assertEqual(len(os.listdir(env.root / "35-forge")), 1)
        code, out, _ = srv.call("POST", f"/requests/{rid}/tick-searches",
                                {"queries": ["apa 7 citation rules"], "user": APPROVER})
        self.assertEqual((code, out["counts"]["ticked"]), (200, 1))

    def test_default_wait_is_bounded(self):
        from forge_api.service import WRITE_WAIT
        self.assertGreater(WRITE_WAIT, 0)
        self.assertLessEqual(WRITE_WAIT, 10)
        self.assertEqual(Env(self).service.write_wait, WRITE_WAIT)


class ActivationUnitTests(unittest.TestCase):
    def listening(self, kind=socket.SOCK_STREAM, listen=True, host="127.0.0.1"):
        s = socket.socket(socket.AF_INET, kind)
        self.addCleanup(s.close)
        s.bind((host, 0))
        if listen:
            s.listen()
        return s

    def env_for(self, fds="1"):
        return {"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": fds, "LISTEN_FDNAMES": "x", "OTHER": "1"}

    def test_not_activated(self):
        self.assertIsNone(inherited_socket("127.0.0.1", 1, environ={}))
        env = {"LISTEN_PID": "1", "LISTEN_FDS": "1"}
        self.assertIsNone(inherited_socket("127.0.0.1", 1, environ=env))
        self.assertEqual(env, {"LISTEN_PID": "1", "LISTEN_FDS": "1"})   # not ours: left alone

    def test_good_socket(self):
        s = self.listening()
        fd = os.dup(s.fileno())
        env = self.env_for()
        got = inherited_socket("127.0.0.1", s.getsockname()[1], environ=env, fd=fd)
        self.addCleanup(got.close)
        self.assertEqual(got.getsockname(), s.getsockname())
        self.assertEqual(env, {"OTHER": "1"})

    def test_refused(self):
        tcp = self.listening()
        port = tcp.getsockname()[1]
        cases = [
            ("wrong port", tcp, "127.0.0.1", port + 1, "1"),
            ("wrong address", tcp, "127.0.0.2", port, "1"),
            ("two fds", tcp, "127.0.0.1", port, "2"),
            ("zero fds", tcp, "127.0.0.1", port, "0"),
            ("not listening", self.listening(listen=False), "127.0.0.1", None, "1"),
            ("udp", self.listening(kind=socket.SOCK_DGRAM, listen=False), "127.0.0.1", None, "1"),
        ]
        for why, sock, bind, p, fds in cases:
            with self.subTest(why=why), self.assertRaises(ActivationError):
                inherited_socket(bind, p if p is not None else sock.getsockname()[1],
                                 environ=self.env_for(fds), fd=os.dup(sock.fileno()))

    def test_configured_privileged_port_must_match(self):
        s = self.listening()   # an ephemeral port: not 879
        with self.assertRaisesRegex(ActivationError, "not the configured 127.0.0.1 port 879"):
            inherited_socket("127.0.0.1", 879, environ=self.env_for(), fd=os.dup(s.fileno()))

    def test_fd_not_a_socket(self):
        r, w = os.pipe()
        self.addCleanup(os.close, w)
        with self.assertRaises(ActivationError):
            inherited_socket("127.0.0.1", 1, environ=self.env_for(), fd=r)

    def test_unix_socket_refused(self):
        with tempfile.TemporaryDirectory() as d:
            u = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(u.close)
            u.bind(os.path.join(d, "s"))
            u.listen()
            with self.assertRaises(ActivationError):
                inherited_socket("127.0.0.1", 1, environ=self.env_for(), fd=os.dup(u.fileno()))


class ActivationEndToEndTests(unittest.TestCase):
    """The launcher started the way systemd does it: the socket on fd 3, LISTEN_PID = its pid."""

    def setUp(self):
        self.env = Env(self)
        self.token_path = self.env.base / "token"
        self.token_path.write_text(TOKEN + "\n")
        os.chmod(self.token_path, 0o640)
        self.sock = socket.socket()
        self.addCleanup(self.sock.close)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]

    def start(self, port):
        cfg = self.env.base / "config.json"
        cfg.write_text(json.dumps({"library_root": str(self.env.root), "bind": "127.0.0.1", "port": port,
                                   "allowed_peers": ["172.20.0.4"], "token_file": str(self.token_path),
                                   "approver_user_id": APPROVER}))
        fd = self.sock.fileno()
        script = (f'exec 3<&{fd}; LISTEN_PID=$$ LISTEN_FDS=1 exec "$0" -I -B "$1" --config "$2" serve')
        proc = subprocess.Popen(["/bin/sh", "-c", script, sys.executable, str(HERE / "forge-api"), str(cfg)],
                                pass_fds=(fd,), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def test_serves_the_inherited_socket(self):
        proc = self.start(self.port)
        self.sock.close()   # only the child holds it now
        deadline = time.monotonic() + 10
        out = b""
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
                    s.sendall(f"GET /requests HTTP/1.0\r\nAuthorization: Bearer {TOKEN}\r\n\r\n".encode())
                    while chunk := s.recv(4096):
                        out += chunk
                break
            except OSError:
                time.sleep(0.1)
        # the configured peers are open-webui's, so the host is refused: proves it is serving
        self.assertTrue(out.startswith(b"HTTP/1.0 403 "), out[:80])
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn(f"listening on 127.0.0.1 port {self.port}", err)
        self.assertNotIn(TOKEN, err)

    def test_mismatch_is_refused(self):
        proc = self.start(self.port + 1 if self.port < 65535 else self.port - 1)
        _, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 2, err)
        self.assertIn("socket activation", err)
        self.assertIn("not the configured", err)


if __name__ == "__main__":
    unittest.main()
