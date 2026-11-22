"""Probe Mission Control's IP filter from inside its own sandbox (role mission_control, tasks/verify.yml).

Run by systemd-run with exactly the units' sandbox (User=mc, IPAddressDeny=/IPAddressAllow=, ...):
  <venv python> -I -B egress_probe.py '<targets JSON>' [--no-crypto]
Targets: [{"name": str, "kind": "udp"|"tcp"|"systemctl"|"dns"|"absent", "host": str, "port": int}, ...].
  udp: connect() + send() of one byte. systemd's IP filter is a cgroup egress program, so a denied destination
       fails at send() with EPERM at once, and an allowed or unfiltered one succeeds without any reply: a clean
       allow/deny signal that doesn't depend on the far host being up (a TCP SYN to a denied host just times out).
  tcp: a real connect (name resolution included) within 5 s: proves the path end to end.
  systemctl: `<port-less host>` is "<systemctl path> <unit>": `systemctl show -p ActiveState <unit>` must answer
       (D-Bus reachable through the /run allowlist) -> "ok" or "error: <rc or type>".
  dns: getaddrinfo(host) through the resolver stub -> "ok" or "error: resolve".
  absent: os.path.lexists(host) -> "absent" or "present" (sockets the /run allowlist must hide).
Also "crypto" (unless --no-crypto): imports pywebpush and py_vapid and signs with a throwaway P-256 key, which proves the pinned venv
works under the sandbox (MemoryDenyWriteExecute, the syscall filter).
Prints one JSON object {name: "sent" | "ok" | "EPERM" | "<errno name>" | "error: <type>"}. Exit 0.
"""

import errno
import json
import os
import socket
import subprocess
import sys


def probe_udp(host, port):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(5)
        try:
            sock.connect((host, port))
            sock.send(b"\0")
        except OSError as exc:
            return errno.errorcode.get(exc.errno, "error: %s" % exc.__class__.__name__)
    return "sent"


def probe_tcp(host, port):
    try:
        with socket.create_connection((host, port), timeout=5):
            return "ok"
    except socket.gaierror:
        return "error: resolve"
    except TimeoutError:
        return "ETIMEDOUT"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, "error: %s" % exc.__class__.__name__)


def probe_systemctl(spec):
    exe, unit = spec.split(" ", 1)
    try:
        proc = subprocess.run([exe, "show", "-p", "ActiveState", "--", unit], capture_output=True, text=True,
                              timeout=15, env={"PATH": "/usr/bin:/bin", "LANG": "C"}, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "error: %s" % exc.__class__.__name__
    if proc.returncode != 0 or not proc.stdout.startswith("ActiveState="):
        return "error: rc %d" % proc.returncode
    return "ok"


def probe_dns(host):
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return "error: resolve"
    return "ok"


def probe_absent(path):
    return "present" if os.path.lexists(path) else "absent"


def probe_crypto():
    try:
        import py_vapid  # noqa: F401
        import pywebpush  # noqa: F401
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1())
        signature = key.sign(b"mission-control egress probe", ec.ECDSA(hashes.SHA256()))
        key.public_key().verify(signature, b"mission-control egress probe", ec.ECDSA(hashes.SHA256()))
    except Exception as exc:  # report, never raise: the assert names it
        return "error: %s" % exc.__class__.__name__
    return "ok"


def main(argv):
    targets = json.loads(argv[1]) if len(argv) > 1 else []
    out = {} if "--no-crypto" in argv[2:] else {"crypto": probe_crypto()}
    for t in targets:
        kind, host = t.get("kind"), str(t["host"])
        if kind == "udp":
            out[t["name"]] = probe_udp(host, int(t["port"]))
        elif kind == "tcp":
            out[t["name"]] = probe_tcp(host, int(t["port"]))
        elif kind == "systemctl":
            out[t["name"]] = probe_systemctl(host)
        elif kind == "dns":
            out[t["name"]] = probe_dns(host)
        elif kind == "absent":
            out[t["name"]] = probe_absent(host)
        else:
            out[t["name"]] = "error: unknown kind"
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
