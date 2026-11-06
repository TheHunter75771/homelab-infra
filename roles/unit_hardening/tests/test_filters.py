"""Unit tests for role unit_hardening's filter plugin and its "uses tailscaled" regex (vars/main.yml).
    .venv/bin/python roles/unit_hardening/tests/test_filters.py
"""
import os
import sys
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "filter_plugins"))
import unit_hardening as f

with open(os.path.join(HERE, "..", "vars", "main.yml")) as fh:
    VARS = yaml.safe_load(fh)
USES_RE = VARS["unit_hardening_uses_re"]
PIDNS_RE = VARS["unit_hardening_pidns_re"]
OWN = "/etc/systemd/system/x.service.d/50-no-tailscaled.conf"

CAT = f"""# /etc/systemd/system/x.service
# Ansible managed
# /run read-only explicitly
# /etc/systemd/system/x.service.d/50-no-tailscaled.conf
[Unit]
After=network-online.target tailscaled.service

[Service]
ExecStartPre=-/bin/true
ExecStart=!!/usr/bin/python3 -I /opt/x/x \\
  --config=/etc/x/config.json serve
EnvironmentFile=-/etc/x/env
ReadOnlyPaths=/run
TemporaryFileSystem=/run/tailscale:ro
InaccessiblePaths=/dev/shm

# {OWN}
[Service]
InaccessiblePaths=-/run/tailscale -/var/run/tailscale

# /usr/lib/systemd/system/service.d/10-timeout-abort.conf
[Service]
TimeoutStopFailureMode=abort
"""


class Blocks(unittest.TestCase):
    def test_headers_only_at_start_or_after_blank(self):
        paths = [b["path"] for b in f.unit_hardening_blocks(CAT)]
        self.assertEqual(paths, ["/etc/systemd/system/x.service", OWN,
                                 "/usr/lib/systemd/system/service.d/10-timeout-abort.conf"])

    def test_lines_skip_own_dropin_comments_sections_and_hiding(self):
        lines = f.unit_hardening_lines(CAT, [OWN])
        self.assertIn("ExecStart=!!/usr/bin/python3 -I /opt/x/x --config=/etc/x/config.json serve", lines)
        self.assertNotIn("InaccessiblePaths=-/run/tailscale -/var/run/tailscale", lines)
        self.assertFalse([ln for ln in lines if ln.startswith(("#", "[", "TemporaryFileSystem", "InaccessiblePaths"))])
        self.assertIn("TimeoutStopFailureMode=abort", lines)

    def test_a_comment_cannot_hide_lines_as_a_fake_header(self):
        lines = f.unit_hardening_lines(CAT, ["/etc/systemd/system/x.service.d/50-no-tailscaled.conf"])
        self.assertIn("ExecStartPre=-/bin/true", lines)

    def test_another_dropin_using_the_socket_is_seen(self):
        cat = CAT + "\n# /etc/systemd/system/x.service.d/90-local.conf\n[Service]\nBindPaths=/run/tailscale\n"
        self.assertEqual(f.unit_hardening_uses(f.unit_hardening_lines(cat, [OWN]), USES_RE),
                         ["BindPaths=/run/tailscale"])

    def test_exec_paths(self):
        self.assertEqual(f.unit_hardening_exec_paths(f.unit_hardening_lines(CAT, [OWN])),
                         ["/bin/true", "/usr/bin/python3", "/opt/x/x", "/etc/x/config.json", "/etc/x/env"])

    def test_exec_prefixes(self):
        lines = ["ExecStart=@/usr/bin/a argv0 --x", "ExecStop=+/usr/bin/b", "ExecReload=:/usr/bin/c",
                 "ExecCondition=|/usr/bin/d", "Environment=FOO=/not/this", "ExecStartPost=/bin/sh -c 'echo /e/f'"]
        self.assertEqual(f.unit_hardening_exec_paths(lines),
                         ["/usr/bin/a", "/usr/bin/b", "/usr/bin/c", "/usr/bin/d", "/bin/sh"])

    def test_bind_sources(self):
        value = "-/run/a.sock:/run/a.sock:rbind /run/systemd/system:/run/systemd/system:rbind /etc/x:/etc/y"
        self.assertEqual(f.unit_hardening_bind_sources(value), ["/run/a.sock", "/run/systemd/system", "/etc/x"])
        self.assertEqual(f.unit_hardening_bind_sources(""), [])
        self.assertEqual(f.unit_hardening_bind_sources(None), [])

    def test_denied_ancestors(self):
        denied = VARS["unit_hardening_run_deny_paths"]
        bad = ["/run/systemd", "/run/udev", "/run/udev/control", "/run/credentials", "/run/snapd.socket",
               "/run/containerd", "/run/libvirt", "/run/user", "/run/tailscale", "/run/systemd/userdb"]
        good = ["/run/dbus/system_bus_socket", "/run/systemd/system", "/run/dbus", "/run/systemd/journal",
                "/run/udev/data", "/run/systemd-x", "/run/credentialsx"]
        self.assertEqual(f.unit_hardening_denied_ancestors(bad, denied), bad)
        self.assertEqual(f.unit_hardening_denied_ancestors(good, denied), [])

    def test_props(self):
        self.assertEqual(f.unit_hardening_props("A=1\nB=x=y\nA=2\n"), {"A": "1", "B": "x=y"})


class UsesRegex(unittest.TestCase):
    USES = (
        "ExecStart=/usr/bin/tailscale ip -4",
        'subprocess.run(["tailscale", "ping", "-c", "1"])',
        "cmd = ['tailscale', 'status', '--json']",
        "out=$(tailscale ip -4)",
        "TS=`tailscale ip`",
        "curl --unix-socket /var/run/tailscale/tailscaled.sock http://local-tailscaled.sock/localapi/v0/status",
        "BindReadOnlyPaths=/run/tailscale",
        "sock = '/run/tailscale/tailscaled.sock'",
        'import "tailscale.com/client/local"',
        "/usr/sbin/tailscale serve status",
        "Tailscale Serve --> TAILSCALE STATUS",
        "conn.request('GET', '/localapi/v0/whois?addr=x')",
    )
    CLEAN = (
        "After=network-online.target tailscaled.service",
        "Wants=tailscaled.service",
        "# IP_FREEBIND: bind the tailnet address even if tailscaled hasn't assigned it yet (boot).",
        "Binds use `IP_FREEBIND`, so the tailnet address can be bound before tailscaled has it at boot.",
        "ExecStart=/usr/local/bin/ollama serve",
        "the tailscale0 interface is in zone trusted",
        "/run/tailscale-other/x",
        "my-tailscale ip",
        "IPAddressAllow=100.64.0.10/32",
    )

    def test_uses(self):
        for s in self.USES:
            with self.subTest(s=s):
                self.assertTrue(f.unit_hardening_uses([s], USES_RE), s)

    def test_clean(self):
        for s in self.CLEAN:
            with self.subTest(s=s):
                self.assertFalse(f.unit_hardening_uses([s], USES_RE), s)


class PidnsRegex(unittest.TestCase):
    """What can't run in a private PID namespace (Q6-1): judged for root units only."""

    BLOCKS = (
        ('return subprocess.run(["systemctl", *args], check=False)', ["systemctl"]),
        ("ExecStartPre=/usr/bin/systemctl is-active x.mount", ["systemctl"]),
        ("ExecStart=/usr/bin/systemd-run --wait true", ["systemd-run"]),
        ("pid=$(pgrep -x dockerd); kill -HUP $pid", ["pgrep"]),
        ("pkill -f foo || pidof bar || killall baz", ["killall", "pidof", "pkill"]),
        ("nsenter -t 1 -m true", ["nsenter"]),
        ("open('/proc/1/root/etc/hostname')", ["/proc/1/"]),
        ("os.kill(pid, signal.SIGTERM)", ["os.kill("]),
        ("import psutil", ["psutil"]),
    )
    CLEAN = (
        "subprocess.run([prog, '-n', '-r', '-o', columns, '--mountpoint', path])",   # findmnt (ai_backup)
        "open('/proc/self/mountinfo')",
        "sqlite3.connect(uri, uri=True)",
        "rsync --list-only --no-h -e ssh",
        "SyslogIdentifier=ai-openwebui-backup",
        "my-systemctl-wrapper",
        "os.killpg_alias = None",
    )

    def test_blocks(self):
        for line, want in self.BLOCKS:
            with self.subTest(line=line):
                self.assertEqual(f.unit_hardening_matches([line], PIDNS_RE), want)

    def test_clean(self):
        for line in self.CLEAN:
            with self.subTest(line=line):
                self.assertEqual(f.unit_hardening_matches([line], PIDNS_RE), [])

    def test_matches_are_lower_case_and_distinct(self):
        self.assertEqual(f.unit_hardening_matches(["SYSTEMCTL a", "systemctl b"], PIDNS_RE), ["systemctl"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
