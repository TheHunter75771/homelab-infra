"""Build the offline test's world under <tmp> (synthetic units modelled on ai-box's and the gaming PC's real ones;
no real names, addresses or tokens): <tmp>/host (the role's root: unit dir, tmpfiles.d, etc/passwd, run/tailscale
with a real unix socket), <tmp>/opt/* (the apps the units run), <tmp>/proc (the fake /proc, built with the fake
systemctl's own make_proc), <tmp>/state.json (the fake systemctl's state), <tmp>/www/api/ps (Ollama's /api/ps for
the busy check, served by run.sh). Running units start "stale": their namespace still shows tailscaled.sock.
    python3 make_fixture.py <tmp>
"""
import importlib.machinery
import importlib.util
import json
import os
import socket
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_loader = importlib.machinery.SourceFileLoader("fake_systemctl", os.path.join(HERE, "bin", "systemctl"))
_spec = importlib.util.spec_from_loader("fake_systemctl", _loader)
fake = importlib.util.module_from_spec(_spec)
_loader.exec_module(fake)

tmp = os.path.abspath(sys.argv[1])
host = os.path.join(tmp, "host")
UNIT_DIR = "/etc/systemd/system"


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    os.chmod(path, mode)


# The host: unit dir, tmpfiles.d, /run/tailscale with a real socket (bound by relative name: no sun_path limit).
os.makedirs(host + UNIT_DIR, exist_ok=True)
os.makedirs(host + "/etc/tmpfiles.d", exist_ok=True)
os.makedirs(host + "/run/tailscale", exist_ok=True)
cwd = os.getcwd()
os.chdir(host + "/run/tailscale")
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind("tailscaled.sock")
s.close()
os.chdir(cwd)
assert stat.S_ISSOCK(os.lstat(host + "/run/tailscale/tailscaled.sock").st_mode)

# Apps. clean-app: comments that mention tailscaled the way the real gpu-gate/ai-host-agent code does (must not
# match). ts-app: the launcher is clean, a helper module two levels down calls the CLI (the walk must find it).
write(f"{tmp}/opt/clean-app/clean-app", "#!/usr/bin/python3\nfrom clean import main\nmain()\n", 0o755)
write(f"{tmp}/opt/clean-app/clean/__init__.py",
      "# IP_FREEBIND: bind the tailnet address even if tailscaled hasn't assigned it yet (boot).\n"
      "# Ordering: After=tailscaled.service (tailscaled starts first).\n"
      "def main():\n    return 0\n")
write(f"{tmp}/etc/clean-app/config.json", '{"bind": "100.64.0.10"}\n')
write(f"{tmp}/opt/ts-app/ts-app", "#!/usr/bin/python3\nfrom tsapp.net import addr\nprint(addr())\n", 0o755)
write(f"{tmp}/opt/ts-app/tsapp/net/__init__.py",
      "import subprocess\n\ndef addr():\n    return subprocess.run([\"tailscale\", \"ip\", \"-4\"],"
      " capture_output=True, text=True).stdout\n")
write(f"{tmp}/opt/sock-app/sock-app", "#!/bin/sh\nexec curl --unix-socket /var/run/tailscale/tailscaled.sock "
      "http://local-tailscaled.sock/localapi/v0/status\n", 0o755)
# ctl-app: a root oneshot whose helper calls systemctl (Q6-1: can't take PrivatePIDs over the private socket).
write(f"{tmp}/opt/ctl-app/ctl-app", "#!/usr/bin/python3\nfrom ctl import main\nmain()\n", 0o755)
write(f"{tmp}/opt/ctl-app/ctl/__init__.py",
      "import subprocess\n\ndef main():\n    subprocess.run([\"systemctl\", \"is-active\", \"x.mount\"])\n")
write(f"{tmp}/opt/big-app/big-app", "#!/bin/sh\nexit 0\n", 0o755)
for i in range(12):
    write(f"{tmp}/opt/big-app/data/{i:02d}.txt", "x\n")
write(f"{tmp}/opt/locked-app/locked-app", "#!/bin/sh\nexit 0\n", 0o755)
write(f"{tmp}/opt/locked-app/secret.txt", "unreadable\n", 0o000)
os.makedirs(f"{tmp}/data/volume", exist_ok=True)
write(f"{tmp}/data/volume/tailscale-notes.txt", "tailscale ip -4\n")  # a data dir argument: never walked


def unit(desc, service, after="network-online.target"):
    return (f"# Ansible managed\n# /run read-only explicitly (a comment that looks like a path)\n[Unit]\n"
            f"Description={desc}\nAfter={after}\n\n[Service]\n{service}\n\n[Install]\nWantedBy=multi-user.target\n")


def props(active, typ="exec", pid=0, nrestarts="0", dropins="", user=""):
    return {"User": user, "DynamicUser": "no", "LoadState": "loaded", "ActiveState": "active" if active is True else (active or "inactive"),
            "SubState": {True: "running", "activating": "start"}.get(active, "dead"), "Type": typ,
            "MainPID": str(pid), "NRestarts": nrestarts, "DropInPaths": dropins, "InaccessiblePaths": "",
            "RuntimeDirectoryPreserve": "no"}


PY = "/usr/bin/python3"
CLEAN = f"{PY} -I -B {tmp}/opt/clean-app/clean-app --config {tmp}/etc/clean-app/config.json"
FEDORA = "/usr/lib/systemd/system/service.d/10-timeout-abort.conf"
units = {
    # The happy path: two long-running services (one with prefixed Exec lines), an idle oneshot, a oneshot caught
    # mid-run, a stopped service, and three root units (Q6-1): a oneshot, a long-running service, and a oneshot
    # whose code calls systemctl (accepted with unit_hardening_systemctl_via_bus).
    "svc-a.service": {"text": unit("A", f"Type=exec\nUser=svca\nExecStart={CLEAN} serve\nReadOnlyPaths=/run\n"
                                        "IPAddressDeny=any\nIPAddressAllow=172.20.0.5/32"),
                      "props": props(True, pid=1001, user="svca")},
    "svc-b.service": {"text": unit("B", "Type=simple\nUser=svcb\nExecStartPre=-/bin/true\n"
                                        f"ExecStart=!!{PY} -I {tmp}/opt/clean-app/clean-app \\\n  --config="
                                        f"{tmp}/etc/clean-app/config.json\nEnvironmentFile=-{tmp}/etc/clean-app/env",
                                   after="tailscaled.service network-online.target"),
                      "props": props(True, typ="simple", pid=1002, dropins=FEDORA, user="svcb")},
    "job.service": {"text": unit("job", f"Type=oneshot\nUser=jobu\nExecStart={CLEAN} run --out {tmp}/data/volume"),
                    "props": props(False, typ="oneshot", user="jobu")},
    "busy-job.service": {"text": unit("busy", f"Type=oneshot\nUser=busyu\nExecStart={CLEAN} run"),
                         "props": props("activating", typ="oneshot", pid=1003, user="busyu")},
    "off.service": {"text": unit("off", f"Type=exec\nUser=offu\nExecStart={CLEAN} serve"),
                    "props": props(False, user="offu")},
    "rootjob.service": {"text": unit("rootjob", f"Type=oneshot\nExecStart={CLEAN} backup\nPrivateNetwork=yes"),
                        "props": props(False, typ="oneshot")},
    "rootsvc.service": {"text": unit("rootsvc", f"Type=exec\nUser=root\nExecStart={CLEAN} serve"),
                        "props": props(True, pid=1401, user="root")},
    "rootctl.service": {"text": unit("rootctl", f"Type=oneshot\nExecStart={PY} -I {tmp}/opt/ctl-app/ctl-app"),
                        "props": dict(props(False, typ="oneshot"),
                                      CapabilityBoundingSet="cap_chown cap_net_admin cap_sys_admin")},
    # A root oneshot with every capability and no bus: advisory for CAP_SYS_ADMIN alone.
    "rootcap.service": {"text": unit("rootcap", f"Type=oneshot\nExecStart={CLEAN} sync"),
                        "props": dict(props(False, typ="oneshot"),
                                      CapabilityBoundingSet="cap_chown cap_dac_override cap_sys_admin")},
    "tailscaled.service": {"text": unit("Tailscale node agent",
                                        "ExecStart=/usr/sbin/tailscaled --socket=/run/tailscale/tailscaled.sock\n"
                                        "RuntimeDirectory=tailscale\nRuntimeDirectoryMode=0755\nType=notify",
                                        after="network-pre.target"),
                           "props": props(True, typ="notify", pid=900, dropins=FEDORA)},
    # Q6-4: Ollama, asked through /api/ps before a restart.
    "ollama.service": {"text": unit("ollama", "Type=exec\nUser=ollama\nExecStart=/bin/sh "
                                              f"{tmp}/opt/clean-app/clean-app"),
                       "props": props(True, pid=1301, user="ollama")},
    # Refusals.
    "ts-cli.service": {"text": unit("cli", f"ExecStart={PY} -I {tmp}/opt/ts-app/ts-app"),
                       "props": props(True, pid=1101, user="refu")},
    "ts-bind.service": {"text": unit("bind", f"ExecStart={CLEAN}\nBindReadOnlyPaths=/run/tailscale/tailscaled.sock"),
                        "props": props(True, pid=1102, user="refu")},
    "ts-pre.service": {"text": unit("pre", f"ExecStartPre=/usr/bin/tailscale wait\nExecStart={CLEAN}"),
                       "props": props(True, pid=1103, user="refu")},
    "ts-sock.service": {"text": unit("sock", f"ExecStart=/bin/sh {tmp}/opt/sock-app/sock-app"),
                        "props": props(True, pid=1104, user="refu")},
    "masked.service": {"text": unit("masked", f"ExecStart={CLEAN}"),
                       "props": dict(props(False, user="refu"), LoadState="masked")},
    "oneshot-restart.service": {"text": unit("os", f"Type=oneshot\nExecStart={CLEAN}"),
                                "props": props(False, typ="oneshot", user="refu")},
    "big.service": {"text": unit("big", f"ExecStart=/bin/sh {tmp}/opt/big-app/big-app"), "props": props(False, user="refu")},
    "locked.service": {"text": unit("locked", f"ExecStart=/bin/sh {tmp}/opt/locked-app/locked-app"),
                       "props": props(False, user="refu")},
    "dyn.service": {"text": unit("dyn", f"DynamicUser=yes\nExecStart={CLEAN}"),
                    "props": dict(props(False, user="dyn"), DynamicUser="yes")},
    "rootfork.service": {"text": unit("fork", f"Type=forking\nExecStart={CLEAN} --daemon"),
                         "props": props(False, typ="forking")},
    "rootbind.service": {"text": unit("bind", f"Type=oneshot\nBindReadOnlyPaths=/run/docker.sock\nExecStart={CLEAN}"),
                         "props": dict(props(False, typ="oneshot"),
                                       BindReadOnlyPaths="/run/docker.sock:/run/docker.sock:rbind")},
    "rootpgrep.service": {"text": unit("pgrep", f"Type=oneshot\nExecStartPre=/usr/bin/pgrep -x foo\nExecStart={CLEAN}"),
                          "props": props(False, typ="oneshot")},
    # Breaks once hardened (verify must catch it).
    "fragile.service": {"text": unit("fragile", f"User=svcf\nExecStart={CLEAN} serve"),
                        "props": props(True, pid=1201, user="svcf"), "fails_when_hardened": True},
}
UIDS = {"svca": 2001, "svcb": 2002, "jobu": 2003, "busyu": 2004, "offu": 2005, "ollama": 2010, "svcf": 2011,
        "dyn": 2012, "refu": 2013}
write(f"{host}/etc/passwd", "root:x:0:0:root:/root:/bin/bash\n"
      + "".join(f"{n}:x:{u}:{u}::/nonexistent:/usr/sbin/nologin\n" for n, u in UIDS.items()))
write(f"{tmp}/www/api/ps", json.dumps({"models": []}) + "\n")
for name, u in units.items():
    u["fragment"] = f"{UNIT_DIR}/{name}"

state = {"root": host, "unit_dir": UNIT_DIR, "proc": f"{tmp}/proc", "units": units, "loaded": {}, "next_pid": 5000,
         "uids": UIDS}
with open(f"{tmp}/state.json", "w") as fh:
    json.dump(state, fh, indent=1, sort_keys=True)

# The fake /proc: this probe's own PID namespace, and every running unit's process, stale (the socket visible,
# no mount on /run/tailscale, the host's PID namespace).
os.makedirs(f"{tmp}/proc/self/ns", exist_ok=True)
os.symlink(fake.HOST_PIDNS, f"{tmp}/proc/self/ns/pid")
for name, u in units.items():
    pid = int(u["props"]["MainPID"])
    if pid:
        fake.make_proc(state, pid, False, name, u["props"]["User"], False)
print(f"fixture: {len(units)} units under {tmp}")
