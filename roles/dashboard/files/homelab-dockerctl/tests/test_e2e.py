"""End to end: the real script, started the way systemd's Accept=yes starts it (the connection on
stdin), with the real subprocess handling, against fake docker/systemctl/journalctl executables.
Covers main(), the config file, process-group cleanup on a client close, and the inactive guard."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from fakes import FIXTURES

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "homelab_dockerctl.py"
CID = "c" * 64

FAKE_SYSTEMCTL = """#!/bin/sh
cat "$FAKE_ROOT/docker-state"
[ "$(cat "$FAKE_ROOT/docker-state")" = active ]
"""

# Records every call; serves ps/inspect/logs. "logs --follow" prints one line and then keeps a
# child process running, so the test can check that the whole process group is stopped.
FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE_ROOT/docker-calls"
case "$1" in
  ps) echo "$FAKE_CID" ;;
  inspect) cat "$FAKE_ROOT/inspect.json" ;;
  logs)
    echo "2026-10-24T06:00:00.000000000Z hello from fake"
    echo "fake warning" >&2
    case " $* " in
      *" --follow "*) sleep 300 & echo $! > "$FAKE_ROOT/child.pid"; wait ;;
    esac ;;
  *) exit 9 ;;
esac
"""

FAKE_JOURNALCTL = """#!/bin/sh
echo "$*" >> "$FAKE_ROOT/journalctl-calls"
echo '[]'
"""


class EndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dockerctl-e2e-")
        self.root = pathlib.Path(self.tmp.name)
        os.chmod(self.root, 0o755)
        stacks = self.root / "stacks"
        shutil.copytree(FIXTURES / "stacks", stacks)
        for p in [stacks, *stacks.rglob("*")]:
            os.chmod(p, 0o755 if p.is_dir() else 0o644)
        bins = self.root / "bin"
        bins.mkdir()
        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("docker", FAKE_DOCKER), ("journalctl", FAKE_JOURNALCTL)):
            (bins / name).write_text(body)
            os.chmod(bins / name, 0o755)
        (self.root / "docker-state").write_text("inactive\n")
        inspect = [{"Id": CID, "Name": "/sonarr", "RestartCount": 0,
                    "State": {"Status": "running", "StartedAt": "2026-10-24T06:00:00Z", "Health": {"Status": "healthy"}},
                    "Config": {"Image": "x:1", "Labels": {"homelab.watch": "true",
                                                          "com.docker.compose.project": "media"}}}]
        (self.root / "inspect.json").write_text(json.dumps(inspect))
        (self.root / "status").write_text("ok\n")
        self.config = self.root / "dockerctl.json"
        self.config.write_text(json.dumps({
            "allowed_uid": os.getuid(),
            "trusted_owner_uid": os.getuid(),
            "stacks_dir": str(stacks),
            "media_mount": str(self.root / "media"),
            "bootgate_status_file": str(self.root / "status"),
            "cache_dir": str(self.root / "cache"),
            "docker": str(bins / "docker"),
            "systemctl": str(bins / "systemctl"),
            "journalctl": str(bins / "journalctl"),
            "poll_seconds": 0.05,
        }))

    def tearDown(self) -> None:
        pid_file = self.root / "child.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 9)
            except (ProcessLookupError, ValueError):
                pass
        self.tmp.cleanup()

    def start(self, request: dict) -> tuple[socket.socket, subprocess.Popen]:
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        env = {"PATH": "/usr/bin:/bin", "HOMELAB_DOCKERCTL_CONFIG": str(self.config), "FAKE_ROOT": str(self.root),
               "FAKE_CID": CID}
        proc = subprocess.Popen([sys.executable, "-I", str(SCRIPT)], stdin=server, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, env=env)
        server.close()
        client.sendall((json.dumps(request) + "\n").encode())
        return client, proc

    def roundtrip(self, request: dict, rc: int = 0) -> tuple[list[dict], str]:
        client, proc = self.start(request)
        with client:
            client.settimeout(10)
            data = b""
            while chunk := client.recv(65536):
                data += chunk
        _, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, rc)
        return [json.loads(line) for line in data.decode().splitlines()], err.decode()

    def docker_calls(self) -> list[str]:
        f = self.root / "docker-calls"
        return f.read_text().splitlines() if f.exists() else []

    def test_inactive_docker_is_never_called(self) -> None:
        for req in ({"op": "ps"}, {"op": "logs", "name": "sonarr"}, {"op": "restart", "name": "sonarr"},
                    {"op": "update", "name": "sonarr"}, {"op": "docker-state"}):
            with self.subTest(op=req["op"]):
                lines, err = self.roundtrip(req)
                self.assertIn("<6>request", err)
        self.assertEqual(self.docker_calls(), [])
        lines, _ = self.roundtrip({"op": "ps"})
        self.assertEqual((lines[0]["data"]["docker"], lines[0]["data"]["containers"]), ("inactive", []))
        self.assertIn("sonarr", [d["name"] for d in lines[0]["data"]["declared"]])
        lines, _ = self.roundtrip({"op": "docker-state"})
        self.assertEqual(lines[0]["data"], {"state": "inactive", "unit_state": "inactive"})

    def test_active_ps_and_logs(self) -> None:
        (self.root / "docker-state").write_text("active\n")
        lines, _ = self.roundtrip({"op": "ps"})
        self.assertEqual([c["name"] for c in lines[0]["data"]["containers"]], ["sonarr"])
        lines, err = self.roundtrip({"op": "logs", "name": "sonarr", "tail": 5})
        self.assertTrue(lines[0]["stream"])
        self.assertIn({"type": "line", "ts": "2026-10-24T06:00:00.000000000Z", "text": "hello from fake",
                       "stream": "stdout"}, lines)
        self.assertIn({"type": "line", "ts": None, "text": "fake warning", "stream": "stderr"}, lines)
        self.assertEqual(lines[-1], {"type": "end", "ok": True, "reason": "eof"})
        self.assertIn(f"logs --timestamps --tail 5 {CID}", self.docker_calls())
        self.assertIn("done op=logs name=sonarr tail=5 follow=false result=ok", err)

    def test_closing_a_follow_stops_the_whole_process_group(self) -> None:
        (self.root / "docker-state").write_text("active\n")
        client, proc = self.start({"op": "logs", "name": "sonarr", "follow": True})
        client.settimeout(10)
        buf = b""
        while buf.count(b"\n") < 2:
            buf += client.recv(65536)
        pid_file = self.root / "child.pid"
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            time.sleep(0.05)
        child = int(pid_file.read_text())
        client.close()
        _, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("result=client-closed", err.decode())
        for _ in range(100):  # the sleep child goes with its process group
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("the follow's child process survived the client close")

    def test_wrong_peer_and_bad_config(self) -> None:
        cfg = json.loads(self.config.read_text())
        cfg["allowed_uid"] = os.getuid() + 1
        self.config.write_text(json.dumps(cfg))
        lines, err = self.roundtrip({"op": "ps"})
        self.assertEqual(lines[0]["error"]["code"], "forbidden")
        self.assertIn("<4>rejected peer", err)
        self.config.write_text('{"allowed_uid": 0}')
        lines, err = self.roundtrip({"op": "ps"}, rc=1)
        self.assertEqual(lines[0]["error"], {"code": "internal", "message": "helper misconfigured"})
        self.assertIn("<3>cannot load the config", err)

    def test_not_a_socket_on_stdin(self) -> None:
        proc = subprocess.run([sys.executable, "-I", str(SCRIPT)], stdin=subprocess.DEVNULL, capture_output=True,
                              env={"HOMELAB_DOCKERCTL_CONFIG": str(self.config)}, timeout=10, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"not a socket", proc.stderr)


if __name__ == "__main__":
    unittest.main()
