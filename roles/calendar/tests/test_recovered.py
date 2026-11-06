"""(c) files/homelab-calendar-recovered.sh (installed as /usr/local/sbin/homelab-calendar-recovered), run
the way homelab-calendar-recovered.service runs it (its settings in the environment), with a stub
homelab-notify and a stub systemctl on PATH that record their arguments:
  - no marker: no push, exit 0;
  - marker, a server whose unauthenticated PROPFIND / answers 401, and the same active invocation
    after the settle period: exactly one push ("Calendar back online", tag calendar), marker gone;
  - the notifier failing, or missing, never fails the unit (exit 0), and the marker still goes;
  - no "back online", the marker kept, exit 0, when: nothing listens; the port answers but not with 401
    (another server, or Radicale failing); it accepts and closes (bound, then died); the service
    restarted (new InvocationID) or stopped within the settle period; or it has no invocation.
Stdlib only (bash and, if installed, shellcheck). test_server.py runs it against a real Radicale.
"""

from __future__ import annotations

import http.server
import json
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import textwrap
import threading
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "files/homelab-calendar-recovered.sh"
NOTIFY_STUB = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys
    with open(os.environ["STUB_LOG"], "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")
    sys.exit(int(os.environ.get("STUB_RC", "0")))
""")
# show --property=InvocationID --value <unit>: the Nth call prints the Nth of STUB_INVOCATIONS (the
# last one repeats); is-active --quiet <unit>: STUB_ACTIVE (1 = active).
SYSTEMCTL_STUB = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os, sys
    log = os.environ["STUB_SYSTEMCTL_LOG"]
    with open(log, "a") as f:
        f.write(" ".join(sys.argv[1:]) + "\\n")
    if sys.argv[1] == "is-active":
        sys.exit(0 if os.environ.get("STUB_ACTIVE", "1") == "1" else 3)
    if sys.argv[1] == "show":
        n = sum(1 for line in open(log) if line.startswith("show "))
        ids = os.environ.get("STUB_INVOCATIONS", "inv-1").split(",")
        print(ids[min(n, len(ids)) - 1])
        sys.exit(0)
    sys.exit(1)
""")
PUSH = ["--tag", "calendar", "--title", "Calendar back online", "--body", "homelab-calendar.service is running again."]


class Propfind(http.server.BaseHTTPRequestHandler):
    status = 401  # what an unauthenticated PROPFIND / gets (Radicale: 401)

    def do_PROPFIND(self) -> None:  # noqa: N802 - http.server's naming
        self.send_response(self.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


class Recovered(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="calendar-recovered-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bindir = self.tmp / "bin"
        bindir.mkdir()
        for name, text in (("homelab-notify", NOTIFY_STUB), ("systemctl", SYSTEMCTL_STUB)):
            (bindir / name).write_text(text)
            (bindir / name).chmod(0o755)
        self.marker = self.tmp / "state/down"
        self.marker.parent.mkdir()
        self.log = self.tmp / "pushes.jsonl"
        self.systemctl_log = self.tmp / "systemctl.log"
        self.env = {
            "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "STUB_LOG": str(self.log),
            "STUB_SYSTEMCTL_LOG": str(self.systemctl_log),
            "CALENDAR_ALERT_MARKER": str(self.marker),
            "CALENDAR_NOTIFY_BIN": "homelab-notify",  # resolved from PATH: the stub
            "CALENDAR_SERVICE": "homelab-calendar.service",
            "CALENDAR_HOST": "127.0.0.1",
            "CALENDAR_PORT": str(self.serve(401)),
            "CALENDAR_WAIT_SECONDS": "5",
            "CALENDAR_SETTLE_SECONDS": "0",
        }

    def serve(self, status: int) -> int:
        """A loopback HTTP server answering PROPFIND with `status`; returns its port."""
        handler = type("Handler", (Propfind,), {"status": status})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def run_script(self, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(SCRIPT)], env={**self.env, **env}, capture_output=True, text=True, timeout=60)

    def pushes(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def assert_kept_open(self, r: subprocess.CompletedProcess, why: str) -> None:
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.pushes(), [])
        self.assertTrue(self.marker.exists())
        self.assertIn(why, r.stdout)
        self.assertIn("keeping the alert open", r.stdout)

    def test_no_marker_no_push(self) -> None:
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.pushes(), [])
        self.assertFalse(self.systemctl_log.exists(), "nothing to do: not even a systemctl call")

    def test_marker_gives_one_push_and_goes(self) -> None:
        self.marker.touch()
        r = self.run_script(CALENDAR_SETTLE_SECONDS="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.pushes(), [PUSH])
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.systemctl_log.read_text().splitlines(), [
            "show --property=InvocationID --value homelab-calendar.service",
            "is-active --quiet homelab-calendar.service",
            "show --property=InvocationID --value homelab-calendar.service",
        ])
        # A second start with no marker left pushes nothing.
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(len(self.pushes()), 1)

    def test_a_failed_or_missing_notifier_never_fails_the_unit(self) -> None:
        for what, env in (("no devices (2)", {"STUB_RC": "2"}), ("error (1)", {"STUB_RC": "1"}),
                          ("not installed", {"CALENDAR_NOTIFY_BIN": str(self.tmp / "missing/homelab-notify")})):
            with self.subTest(notifier=what):
                self.marker.touch()
                r = self.run_script(**env)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertFalse(self.marker.exists())

    def test_nothing_listening_keeps_the_alert_open(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]  # closed again: nothing listens there
        self.marker.touch()
        self.assert_kept_open(self.run_script(CALENDAR_PORT=str(port), CALENDAR_WAIT_SECONDS="1"), "not answering")

    def test_an_answer_that_is_not_401_keeps_the_alert_open(self) -> None:
        for status in (200, 207, 500, 503):
            with self.subTest(status=status):
                self.marker.touch()
                self.assert_kept_open(self.run_script(CALENDAR_PORT=str(self.serve(status)), CALENDAR_WAIT_SECONDS="1"),
                                      "not answering")

    def test_a_server_that_accepts_and_closes_keeps_the_alert_open(self) -> None:
        """Bound, then died: the TCP connect succeeds, but no HTTP reply comes."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        self.addCleanup(listener.close)

        def accept_and_close() -> None:
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                conn.close()

        threading.Thread(target=accept_and_close, daemon=True).start()
        self.marker.touch()
        self.assert_kept_open(self.run_script(CALENDAR_PORT=str(listener.getsockname()[1]), CALENDAR_WAIT_SECONDS="1"),
                              "not answering")

    def test_a_restart_within_the_settle_period_keeps_the_alert_open(self) -> None:
        self.marker.touch()
        self.assert_kept_open(self.run_script(STUB_INVOCATIONS="inv-1,inv-2"), "restarted within")

    def test_a_stop_within_the_settle_period_keeps_the_alert_open(self) -> None:
        self.marker.touch()
        self.assert_kept_open(self.run_script(STUB_ACTIVE="0"), "stopped within")

    def test_no_invocation_keeps_the_alert_open(self) -> None:
        self.marker.touch()
        self.assert_kept_open(self.run_script(STUB_INVOCATIONS=""), "no invocation")

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed (CI runs it on roles/*/files/*.sh)")
    def test_shellcheck_clean(self) -> None:
        r = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
