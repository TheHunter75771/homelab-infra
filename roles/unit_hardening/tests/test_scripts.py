"""Direct tests for role unit_hardening's on-host scripts (files/ns_probe.py, files/scan_exec.py), run the way the
role runs them (python3 -I -B -c <source>, JSON on stdin), against scratch trees.
    .venv/bin/python roles/unit_hardening/tests/test_scripts.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

FILES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "files")
UNIX_ROOT = os.geteuid() == 0


def run(script, request):
    with open(os.path.join(FILES, script)) as fh:
        src = fh.read()
    res = subprocess.run([sys.executable, "-I", "-B", "-c", src], input=json.dumps(request), capture_output=True,
                         text=True, check=True)
    return json.loads(res.stdout)


def touch(path, text="", mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    os.chmod(path, mode)


class Probe(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(suffix=".uh-probe")
        p = f"{self.t}/proc"
        base = "21 1 0:20 / / rw - ext4 /dev/sda1 rw\n"
        # 10: hidden (a mount on /run/tailscale, no socket); 11: stale (the socket); 12: the dir unreadable;
        # 13: no /run/tailscale at all and no mount; 14: a mount at /run/tailscale-x only (not ours).
        for pid in (10, 11, 12, 13, 14):
            os.makedirs(f"{p}/{pid}/root/etc")
            os.makedirs(f"{p}/{pid}/ns")
            os.symlink("pid:[4026531836]", f"{p}/{pid}/ns/pid")
            touch(f"{p}/{pid}/comm", f"c{pid}\n")
            touch(f"{p}/{pid}/cgroup", f"0::/system.slice/u{pid}.service\n")
        # Uids: 10 and 11 run as svc (3001), 12 as other (3002) with only its saved uid 3001, 13/14 as root.
        touch(f"{p}/10/status", "Name:\tx\nUid:\t3001\t3001\t3001\t3001\n")
        touch(f"{p}/11/status", "Name:\tx\nUid:\t3001\t3001\t3001\t3001\n")
        touch(f"{p}/12/status", "Name:\tx\nUid:\t3002\t3002\t3001\t3002\n")
        touch(f"{p}/13/status", "Name:\tx\nUid:\t0\t0\t0\t0\n")
        touch(f"{p}/14/status", "Name:\tx\nUid:\t0\t0\t0\t0\n")
        # 15: a root service in its own PID namespace (PrivatePIDs), hidden.
        os.makedirs(f"{p}/15/root/run/tailscale")
        os.makedirs(f"{p}/15/ns")
        os.symlink("pid:[4026599999]", f"{p}/15/ns/pid")
        touch(f"{p}/15/status", "Name:\tx\nUid:\t0\t0\t0\t0\n")
        touch(f"{p}/15/mountinfo", base + "40 22 0:21 /x /run/tailscale ro - tmpfs tmpfs rw\n")
        os.makedirs(f"{p}/self/ns")
        os.symlink("pid:[4026531836]", f"{p}/self/ns/pid")
        touch(f"{self.t}/host/etc/passwd", "root:x:0:0::/root:/bin/sh\nsvc:x:3001:3001::/:/bin/false\n"
              "other:x:3002:3002::/:/bin/false\n")
        os.makedirs(f"{p}/10/root/run/tailscale")
        touch(f"{p}/10/mountinfo", base + "40 22 0:21 /systemd/inaccessible/dir /run/tailscale ro - tmpfs tmpfs rw\n")
        touch(f"{p}/11/root/run/tailscale/tailscaled.sock")
        touch(f"{p}/11/mountinfo", base)
        touch(f"{p}/12/root/run/tailscale/tailscaled.sock")
        os.chmod(f"{p}/12/root/run/tailscale", 0)
        touch(f"{p}/12/mountinfo", base + "40 22 0:21 /x /run/tailscale ro - tmpfs tmpfs rw\n")
        touch(f"{p}/13/mountinfo", base)
        os.makedirs(f"{p}/14/root/run/tailscale")
        touch(f"{p}/14/mountinfo", base + "40 22 0:21 /x /run/tailscale-x ro - tmpfs tmpfs rw\n")
        os.makedirs(f"{self.t}/host/run/tailscale", exist_ok=True)
        cwd = os.getcwd()
        os.chdir(f"{self.t}/host/run/tailscale")
        s = socket.socket(socket.AF_UNIX)
        s.bind("tailscaled.sock")
        s.close()
        os.chdir(cwd)

    def tearDown(self):
        os.chmod(f"{self.t}/proc/12/root/run/tailscale", 0o755)
        shutil.rmtree(self.t)

    def probe(self, root=None, users=()):
        return run("ns_probe.py", {"proc": f"{self.t}/proc", "root": root if root is not None else f"{self.t}/host",
                                   "dir": "/run/tailscale", "socket": "/run/tailscale/tailscaled.sock",
                                   "pids": {"hidden": 10, "stale": 11, "locked": 12, "bare": 13, "other": 14,
                                            "private": 15, "none": 0, "gone": 99},
                                   "users": list(users), "passwd": "/etc/passwd"})

    def test_own_pidns(self):
        u = self.probe()["units"]
        self.assertTrue(u["private"]["own_pidns"])
        self.assertEqual((u["private"]["socket"], u["private"]["mounted"]), ("absent", True))
        self.assertFalse(u["hidden"]["own_pidns"])

    def test_run_allowlist(self):
        p = f"{self.t}/proc"
        # 15 is a root unit with /run as a tmpfs: the bus socket and /run/systemd/system bound in.
        touch(f"{p}/15/root/run/dbus/system_bus_socket")
        os.makedirs(f"{p}/15/root/run/systemd/system")
        touch(f"{p}/15/root/run/systemd/system/inside-a-bound-dir")  # not descended into
        allow = ["/run/dbus/system_bus_socket", "/run/systemd/system"]
        req = {"proc": p, "root": f"{self.t}/host", "dir": "/run/tailscale", "socket": "/run/tailscale/tailscaled.sock",
               "pids": {"private": 15, "stale": 11}, "run_allow": {"private": allow, "stale": []}}
        u = run("ns_probe.py", req)["units"]
        # /run/tailscale (an empty dir in this fixture) is the only path that isn't on the allowlist's way.
        self.assertEqual(u["private"]["run_extra"], ["/run/tailscale"])
        self.assertFalse(u["private"]["run_truncated"])
        self.assertEqual(u["stale"]["run_extra"], ["/run/tailscale"])  # an extra directory, not its contents
        os.rmdir(f"{p}/15/root/run/tailscale")
        self.assertEqual(run("ns_probe.py", req)["units"]["private"]["run_extra"], [])
        touch(f"{p}/15/root/run/docker.sock")
        self.assertEqual(run("ns_probe.py", req)["units"]["private"]["run_extra"], ["/run/docker.sock"])
        self.assertNotIn("run_extra", run("ns_probe.py", dict(req, run_allow={}))["units"]["private"])

    def test_sweep(self):
        sw = self.probe(users=["svc", "3002", "nosuch"])["sweep"]
        # svc: 10 (hidden), 11 (stale) and 12 (its saved uid is svc's: still reachable, so swept).
        self.assertEqual(sw["svc"]["uid"], 3001)
        got = {p["pid"]: p["socket"] for p in sw["svc"]["procs"]}
        self.assertEqual(set(got), {10, 11, 12})
        self.assertEqual((got[10], got[11]), ("absent", "present"))
        self.assertEqual(sw["svc"]["procs"][0]["cgroup"], "/system.slice/u10.service")
        self.assertEqual([p["pid"] for p in sw["3002"]["procs"]], [12])
        self.assertTrue(sw["nosuch"]["error"].startswith("unknown user"))
        self.assertEqual(sw["nosuch"]["procs"], [])

    def test_sweep_skips_exited(self):
        shutil.rmtree(f"{self.t}/proc/11")
        sw = self.probe(users=["svc"])["sweep"]
        self.assertEqual({p["pid"] for p in sw["svc"]["procs"]}, {10, 12})

    def test_states(self):
        out = self.probe()
        u = out["units"]
        self.assertEqual((u["hidden"]["socket"], u["hidden"]["mounted"], u["hidden"]["dir_present"],
                          u["hidden"]["root_ok"]), ("absent", True, True, True))
        self.assertEqual((u["stale"]["socket"], u["stale"]["mounted"]), ("present", False))
        if not UNIX_ROOT:
            self.assertTrue(u["locked"]["socket"].startswith("unknown: "), u["locked"])
        self.assertEqual((u["bare"]["socket"], u["bare"]["mounted"], u["bare"]["dir_present"]), ("absent", False, False))
        self.assertFalse(u["other"]["mounted"])
        self.assertEqual(u["none"]["error"], "no process")
        self.assertFalse(u["gone"]["root_ok"])
        self.assertNotEqual(u["gone"]["socket"], "absent")
        self.assertEqual(out["host"], {"socket_is_socket": True, "dir_is_dir": True})

    def test_host_without_socket(self):
        out = self.probe(root=f"{self.t}/nohost")
        self.assertEqual(out["host"], {"socket_is_socket": False, "dir_is_dir": False})

    def test_host_socket_must_be_a_socket(self):
        os.makedirs(f"{self.t}/fake/run/tailscale")
        touch(f"{self.t}/fake/run/tailscale/tailscaled.sock")
        self.assertFalse(self.probe(root=f"{self.t}/fake")["host"]["socket_is_socket"])


class Scan(unittest.TestCase):
    RE = r"tailscaled\.sock|localapi|[\"'`]tailscale[\"'`\s]"

    def setUp(self):
        self.t = tempfile.mkdtemp(suffix=".uh-scan")
        touch(f"{self.t}/app/launcher", "#!/bin/sh\n", 0o755)
        touch(f"{self.t}/app/pkg/deep/mod.py", 'CMD = ["tailscale", "ip"]\n')
        touch(f"{self.t}/clean/launcher", "# After=tailscaled.service\n", 0o755)
        touch(f"{self.t}/data/x.txt", "tailscaled.sock\n")
        # A match split across the 1 MiB read boundary must still be found.
        touch(f"{self.t}/big/launcher", "a" * ((1 << 20) - 4) + "localapi\n", 0o755)
        os.makedirs(f"{self.t}/bin")
        os.symlink(f"{self.t}/app/launcher", f"{self.t}/bin/app")

    def tearDown(self):
        shutil.rmtree(self.t)

    PIDNS = r"(?<![\w.-])(?:systemctl|pgrep)(?![\w-])"

    def scan(self, units, **kw):
        req = {"res": {"uses": self.RE, "pidns": self.PIDNS}, "units": units, "broad": [], "max_files": 4000,
               "max_bytes": 1 << 28}
        req.update(kw)
        return run("scan_exec.py", req)

    def test_walks_the_app_dir_not_data_dirs(self):
        out = self.scan({"a": [f"{self.t}/app/launcher", f"{self.t}/data"], "c": [f"{self.t}/clean/launcher"]})
        self.assertEqual([h["file"] for h in out["a"]["hits"]["uses"]], [f"{self.t}/app/pkg/deep/mod.py"])
        self.assertIn(f"{self.t}/data: directory argument (not walked)", out["a"]["skipped"])
        self.assertEqual(out["c"]["hits"], {"uses": [], "pidns": []})

    def test_symlink_walks_the_real_dir(self):
        # /usr/bin/x -> /opt/x/x: the link's own dir is broad (not walked), the real one is the app's.
        out = self.scan({"l": [f"{self.t}/bin/app"]}, broad=[f"{self.t}/bin"])
        self.assertEqual([h["file"] for h in out["l"]["hits"]["uses"]], [f"{self.t}/app/pkg/deep/mod.py"])
        self.assertEqual(out["l"]["walked"], [f"{self.t}/app"])

    def test_boundary(self):
        self.assertEqual(self.scan({"b": [f"{self.t}/big/launcher"]})["b"]["hits"]["uses"][0]["matches"], ["localapi"])

    def test_every_distinct_match_per_regex(self):
        touch(f"{self.t}/ctl/launcher", "#!/bin/sh\nsystemctl is-active x; SYSTEMCTL stop y; pgrep -x z\n", 0o755)
        hits = self.scan({"c": [f"{self.t}/ctl/launcher"]})["c"]["hits"]
        self.assertEqual(hits["pidns"], [{"file": f"{self.t}/ctl/launcher", "matches": ["pgrep", "systemctl"]}])
        self.assertEqual(hits["uses"], [])

    def test_broad_dir_not_walked(self):
        out = self.scan({"a": [f"{self.t}/app/launcher"]}, broad=[f"{self.t}/app"])
        self.assertEqual(out["a"]["hits"]["uses"], [])
        self.assertEqual(out["a"]["walked"], [])

    def test_limits_and_skips(self):
        out = self.scan({"a": [f"{self.t}/app/launcher"]}, max_files=1)
        self.assertTrue(out["a"]["over_limit"])
        out = self.scan({"x": ["relative/path", "/proc/self/exe", f"{self.t}/missing"]})
        self.assertEqual(len(out["x"]["skipped"]), 3)
        self.assertEqual(out["x"]["scanned"], 0)

    @unittest.skipIf(UNIX_ROOT, "root reads mode-000 files")
    def test_unreadable(self):
        touch(f"{self.t}/app/secret", "x", 0)
        out = self.scan({"a": [f"{self.t}/app/launcher"]})
        self.assertEqual(len(out["a"]["unreadable"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
