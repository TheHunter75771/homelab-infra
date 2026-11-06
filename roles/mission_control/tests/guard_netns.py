"""Load the rendered guard (templates/guard.nft.j2) in a private network namespace and check what it does
(tests/test_mission_control.yml; Reviewer Q, Q-1 and Q2-1). Run as
  unshare --map-auto --map-root-user -n python3 -I -B guard_netns.py <guard.nft> <ip4> <ip6> <server port> <serve port> <tailscale iface>
The "box" namespace: loopback up, the tailnet addresses on lo, a stand-in for another table (`table inet other_table`).
Root listens on 127.0.0.1:<server>, <ip4>:<serve>, [<ip6>]:<serve> and, as a control, 127.0.0.1:<server - 2>.
Two child namespaces are joined by veth pairs:
  ctr  a stand-in container; the box side of its veth is `vctr0` (like a Docker bridge port);
  ts   a stand-in tailnet peer; the box side of its veth is named <tailscale iface>, so traffic from it arrives on
       the interface the guard trusts for serve's port.
Then: connects from the box as root and as uid 1000, and from ctr and ts; the guard loaded twice; a UFW-style
`iptables-nft-restore` of a *filter table and a Docker-style nat chain add/flush/delete must leave the guard table
unchanged (counters aside); the connects are repeated after all that. Prints one JSON object."""
import json
import os
import re
import socket
import subprocess
import sys
import time

guard, ip4, ip6 = sys.argv[1], sys.argv[2], sys.argv[3]
server, serve, tsif = int(sys.argv[4]), int(sys.argv[5]), sys.argv[6]


def run(*a, **kw):
    return subprocess.run(list(a), check=True, capture_output=True, text=True, **kw)


def guard_rules():
    out = run("nft", "list", "table", "inet", "mission_control_guard").stdout
    return re.sub(r"counter packets \d+ bytes \d+", "counter", out)


def child(name, box_if, n):
    """A child netns joined to the box by a veth (box side `box_if`), 10.99.n.0/24 and fd99:n::/64."""
    proc = subprocess.Popen(["unshare", "--net", "sleep", "120"])
    mine = os.readlink("/proc/self/ns/net")
    for _ in range(100):
        try:
            if os.readlink("/proc/%d/ns/net" % proc.pid) != mine:
                break
        except OSError:
            pass
        time.sleep(0.05)
    pid = str(proc.pid)
    run("ip", "link", "add", box_if, "type", "veth", "peer", "name", "eth0", "netns", pid)
    run("ip", "addr", "add", "10.99.%d.1/24" % n, "dev", box_if)
    run("ip", "-6", "addr", "add", "fd99:%d::1/64" % n, "dev", box_if, "nodad")
    run("ip", "link", "set", box_if, "up")
    ns = ["nsenter", "--target", pid, "--net"]
    run(*ns, "ip", "link", "set", "lo", "up")
    run(*ns, "ip", "addr", "add", "10.99.%d.2/24" % n, "dev", "eth0")
    run(*ns, "ip", "-6", "addr", "add", "fd99:%d::2/64" % n, "dev", "eth0", "nodad")
    run(*ns, "ip", "link", "set", "eth0", "up")
    run(*ns, "ip", "route", "add", "default", "via", "10.99.%d.1" % n)
    run(*ns, "ip", "-6", "route", "add", "default", "via", "fd99:%d::1" % n)
    return proc, ns


PROBE = r"""
import errno, json, socket, sys
out = {}
for name, host, port in json.loads(sys.argv[1]):
    try:
        socket.create_connection((host, port), timeout=3).close()
        out[name] = "ok"
    except OSError as exc:
        out[name] = errno.errorcode.get(exc.errno, type(exc).__name__)
print(json.dumps(out))
"""
box_targets = {"server": ("127.0.0.1", server), "serve4": (ip4, serve), "serve6": (ip6, serve),
               "control": ("127.0.0.1", server - 2)}
peer_targets = {"serve4": (ip4, serve), "serve6": (ip6, serve)}


def probe(prefix, targets):
    spec = json.dumps([[n, h, p] for n, (h, p) in targets.items()])
    return json.loads(run(*prefix, sys.executable, "-I", "-c", PROBE, spec).stdout)


run("ip", "link", "set", "lo", "up")
run("ip", "addr", "add", ip4 + "/32", "dev", "lo")
run("ip", "-6", "addr", "add", ip6 + "/128", "dev", "lo", "nodad")
subprocess.run(["nft", "-f", "-"], input="table inet other_table {\n chain input { type filter hook input priority 0; }\n}\n",
               check=True, text=True)
ctr, ctr_ns = child("ctr", "vctr0", 1)
ts, ts_ns = child("ts", tsif, 2)
loads = [subprocess.run(["nft", "-f", guard], capture_output=True, text=True).returncode for _ in range(2)]
listeners = []
for host, port in box_targets.values():
    s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(16)
    listeners.append(s)
user = ["setpriv", "--reuid=1000", "--regid=1000", "--clear-groups", "--"]


def all_probes():
    return {"root": probe([], box_targets), "user": probe(user, box_targets),
            "ctr": probe(ctr_ns, peer_targets), "ts": probe(ts_ns, peer_targets)}


first = all_probes()
rules = guard_rules()
# UFW reloads by restoring its whole *filter table through iptables-nft.
run("iptables-nft-restore", input="*filter\n:INPUT ACCEPT [0:0]\n:FORWARD ACCEPT [0:0]\n:OUTPUT ACCEPT [0:0]\n"
    ":ufw-before-input - [0:0]\n-A INPUT -j ufw-before-input\n-A ufw-before-input -i lo -j ACCEPT\nCOMMIT\n")
after_ufw = guard_rules()
# Docker (re)creates its nat chain, adds DNAT rules, and flushes/deletes them on restart.
run("iptables-nft", "-t", "nat", "-N", "DOCKER")
run("iptables-nft", "-t", "nat", "-A", "DOCKER", "-p", "tcp", "--dport", "8888", "-j", "DNAT", "--to-destination", "10.99.1.2:8080")
run("iptables-nft", "-t", "nat", "-F")
run("iptables-nft", "-t", "nat", "-X", "DOCKER")
run("nft", "flush", "table", "ip", "nat")
after_docker = guard_rules()
second = all_probes()
tables = run("nft", "list", "tables").stdout.split()
for p in (ctr, ts):
    p.kill()
print(json.dumps({"loads": loads, "first": first, "second": second,
                  "rules_have_iif": ('iifname != "%s"' % tsif) in rules,
                  "unchanged_after_ufw": after_ufw == rules, "unchanged_after_docker": after_docker == rules,
                  "other_table_kept": "other_table" in tables,
                  "guard_tables": sum(1 for w in tables if w == "mission_control_guard")}))
