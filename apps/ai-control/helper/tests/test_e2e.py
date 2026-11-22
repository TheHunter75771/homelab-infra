"""End to end: the real script, started the way systemd's Accept=yes starts it (the connection on
stdin, or on fd 3 with LISTEN_FDS=1), loading a real config file, against the stub executables."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import unittest

from harness import SCRIPT, Env, make_session

# Sets LISTEN_PID to its own pid (which exec keeps), then execs the helper: what systemd does.
SD_EXEC = ("import os, sys; fd = int(sys.argv[2]); os.dup2(fd, 3, inheritable=True); "
           "fd != 3 and os.close(fd); os.environ['LISTEN_PID'] = str(os.getpid()); "
           "os.execv(sys.executable, [sys.executable, sys.argv[1]])")


class EndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()
        self.config = self.env.root / "helper.json"
        self.config.write_text(json.dumps(self.env.cfg_raw))
        os.chmod(self.config, 0o644)
        make_session(self.env.iolog, "claude", "00/00/01", epoch=1793000000, command=["/usr/bin/id"],
                     streams={"ttyout": b"\x1b[1muid=0(root)\x1b[0m token=abc\r\n"}, timing=[(4, 36)])

    def tearDown(self) -> None:
        self.env.close()

    def run_helper(self, request: bytes, *, fd3: bool = False, config_mode: int = 0o644) -> tuple[list[dict], str, int]:
        os.chmod(self.config, config_mode)
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        env = {"PATH": "/usr/bin:/bin", "AICTL_HELPER_CONFIG": str(self.config)}
        with server, client:
            if fd3:
                env["LISTEN_FDS"] = "1"
                proc = subprocess.Popen([sys.executable, "-c", SD_EXEC, str(SCRIPT), str(server.fileno())],
                                        stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
                                        pass_fds=(server.fileno(),))
            else:
                proc = subprocess.Popen([str(SCRIPT)], stdin=server, stderr=subprocess.PIPE, env=env)
            server.close()
            client.sendall(request)
            client.settimeout(15)
            data = b""
            while chunk := client.recv(65536):
                data += chunk
            _, err = proc.communicate(timeout=15)
        return [json.loads(line) for line in data.decode().splitlines()], err.decode(), proc.returncode

    def test_stdin_connection(self) -> None:
        lines, log, rc = self.run_helper(b'{"op": "sudo-replay", "user": "claude", "id": "000001", "output": true}\n')
        self.assertEqual(rc, 0, log)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0]["ok"], lines)
        self.assertEqual(lines[0]["data"]["output"], "uid=0(root) token=[REDACTED]\n")
        self.assertIn("<6>request peer_uid=", log)
        self.assertIn("op=sudo-replay user=claude id=00/00/01 max_bytes=65536 output=true", log)
        self.assertNotIn("uid=0(root)", log, "output never goes to the journal")

    def test_fd3_connection(self) -> None:
        lines, log, rc = self.run_helper(b'{"op": "containers"}\n', fd3=True)
        self.assertEqual(rc, 0, log)
        self.assertEqual(lines[0]["data"]["docker"], "active")
        self.assertEqual([c[0] for c in self.env.calls], ["systemctl", "systemctl", "docker"])

    def test_a_writable_config_is_refused(self) -> None:
        lines, log, rc = self.run_helper(b'{"op": "timers"}\n', config_mode=0o666)
        self.assertEqual(rc, 1)
        self.assertEqual(lines, [{"ok": False, "error": {"code": "internal", "message": "helper misconfigured"}}])
        self.assertIn("cannot load the config", log)
        self.assertEqual(self.env.calls, [])

    def test_not_a_socket(self) -> None:
        proc = subprocess.run([str(SCRIPT)], stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
                              env={"PATH": "/usr/bin:/bin", "AICTL_HELPER_CONFIG": str(self.config)})
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"aictl-helper.socket", proc.stderr)


if __name__ == "__main__":
    unittest.main()
