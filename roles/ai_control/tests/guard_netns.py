"""Load the rendered guard (templates/guard.nft.j2) in private network namespaces and check what it does
(tests/test_ai_control.yml, Reviewer Q). Run as
  unshare --map-auto --map-root-user --net --fork python3 -I -B guard_netns.py \
      <guard.nft> <guard_probe.py> <tailnet ip4> <tailnet ip6> <serve port> <server port> <iface>
"ai-box" is this namespace: lo up; the tailnet addresses on a veth named <iface> (as on the real node), whose peer
sits in a second namespace ("tailnet peer", 100.64.0.99 / fd7a:115c:a1e0::99); and a veth eth9 whose peer sits in a
third ("LAN peer", 192.0.2.2 / 2001:db8::2), with routes to the tailnet addresses through it. A stand-in for UFW's and
Docker's tables (`table inet other_table`), then the guard loaded TWICE (a reload must replace only its own table).
Root (uid 0 here) listens on <ip4>:<serve>, [<ip6>]:<serve>, 127.0.0.1:<server>, and as controls <ip4>:<serve+8000>
(another serve port, not this guard's) and 127.0.0.1:<server-2>. Clients (files/guard_probe.py tcp), first once without the guard (every path open), then with it:
  ai-box as uid 0, 1000 (rendered as allowed) and 1001 (not allowed); the tailnet peer as 0 and 1001; the LAN peer as 0.
Prints one JSON object; exit 0 unless the setup itself fails."""
import json
import os
import subprocess
import sys
import time

guard, probe, ip4, ip6, serve, server, iface = sys.argv[1:8]
serve, server = int(serve), int(server)
PEER4, PEER6, LAN4, LAN6, LANAB4, LANAB6 = "100.64.0.99", "fd7a:115c:a1e0::99", "192.0.2.2", "2001:db8::2", "192.0.2.1", "2001:db8::1"


def run(*a, ns=None, uid=None, inp=None):
    argv = list(a)
    if uid is not None:
        argv = ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups", "--"] + argv
    if ns is not None:
        argv = ["nsenter", "-t", str(ns), "-n", "--preserve-credentials", "--"] + argv
    return subprocess.run(argv, check=True, capture_output=True, text=True, input=inp)


def peer_ns():
    p = subprocess.Popen(["unshare", "--net", "sleep", "120"])
    for _ in range(50):  # wait until the child is in its own netns
        if os.readlink(f"/proc/{p.pid}/ns/net") != os.readlink("/proc/self/ns/net"):
            return p
        time.sleep(0.05)
    raise SystemExit("peer namespace didn't start")


tail_ns, lan_ns = peer_ns(), peer_ns()
try:
    run("ip", "link", "set", "lo", "up")
    for ns in (tail_ns.pid, lan_ns.pid):
        run("ip", "link", "set", "lo", "up", ns=ns)
    # ai-box <-> tailnet peer, over <iface>.
    run("ip", "link", "add", iface, "type", "veth", "peer", "name", "tpeer")
    run("ip", "link", "set", "tpeer", "netns", str(tail_ns.pid))
    run("ip", "addr", "add", ip4 + "/32", "dev", iface)
    run("ip", "-6", "addr", "add", ip6 + "/128", "dev", iface, "nodad")
    run("ip", "link", "set", iface, "up")
    run("ip", "route", "add", PEER4 + "/32", "dev", iface)
    run("ip", "-6", "route", "add", PEER6 + "/128", "dev", iface)
    run("ip", "addr", "add", PEER4 + "/32", "dev", "tpeer", ns=tail_ns.pid)
    run("ip", "-6", "addr", "add", PEER6 + "/128", "dev", "tpeer", "nodad", ns=tail_ns.pid)
    run("ip", "link", "set", "tpeer", "up", ns=tail_ns.pid)
    run("ip", "route", "add", ip4 + "/32", "dev", "tpeer", ns=tail_ns.pid)
    run("ip", "-6", "route", "add", ip6 + "/128", "dev", "tpeer", ns=tail_ns.pid)
    # ai-box <-> LAN peer, over eth9: the tailnet addresses reached from another interface.
    run("ip", "link", "add", "eth9", "type", "veth", "peer", "name", "epeer")
    run("ip", "link", "set", "epeer", "netns", str(lan_ns.pid))
    run("ip", "addr", "add", LANAB4 + "/24", "dev", "eth9")
    run("ip", "-6", "addr", "add", LANAB6 + "/64", "dev", "eth9", "nodad")
    run("ip", "link", "set", "eth9", "up")
    run("ip", "addr", "add", LAN4 + "/24", "dev", "epeer", ns=lan_ns.pid)
    run("ip", "-6", "addr", "add", LAN6 + "/64", "dev", "epeer", "nodad", ns=lan_ns.pid)
    run("ip", "link", "set", "epeer", "up", ns=lan_ns.pid)
    run("ip", "route", "add", ip4 + "/32", "via", LANAB4, ns=lan_ns.pid)
    run("ip", "-6", "route", "add", ip6 + "/128", "via", LANAB6, ns=lan_ns.pid)
    for key in ("all", "default", iface, "eth9"):
        run("sysctl", "-q", "-w", f"net.ipv4.conf.{key}.rp_filter=0")

    import socket
    listeners = []
    targets = {"serve4": (ip4, serve), "serve6": (ip6, serve), "server": ("127.0.0.1", server),
               "other_serve4": (ip4, serve + 8000), "control": ("127.0.0.1", server - 2)}
    for host, port in targets.values():
        s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen(16)
        listeners.append(s)
    spec = lambda names: json.dumps([{"name": n, "host": targets[n][0], "port": targets[n][1]} for n in names])
    local, remote = spec(targets), spec(["serve4", "serve6", "other_serve4"])
    with open(probe, encoding="utf-8") as fh:
        py = [sys.executable, "-I", "-B", "-c", fh.read(), "tcp"]
    # Before the guard: every path is open (so the refusals below are the guard's doing, not the setup's).
    unguarded = {"box_1001": json.loads(run(*py, local, uid=1001).stdout),
                 "lan_root": json.loads(run(*py, remote, ns=lan_ns.pid).stdout)}
    run("nft", "-f", "-", inp="table inet other_table {\n chain input { type filter hook input priority 0; }\n}\n")
    loads = [subprocess.run(["nft", "-f", guard], check=False, capture_output=True, text=True).returncode for _ in range(2)]
    out = {
        "unguarded": unguarded,
        "loads": loads,
        "box_root": json.loads(run(*py, local).stdout),
        "box_1000": json.loads(run(*py, local, uid=1000).stdout),
        "box_1001": json.loads(run(*py, local, uid=1001).stdout),
        "tailnet_root": json.loads(run(*py, remote, ns=tail_ns.pid).stdout),
        "tailnet_1001": json.loads(run(*py, remote, ns=tail_ns.pid, uid=1001).stdout),
        "lan_root": json.loads(run(*py, remote, ns=lan_ns.pid).stdout),
    }
    tables = run("nft", "list", "tables").stdout.split()
    out["other_table_kept"] = "other_table" in tables
    out["guard_tables"] = sum(1 for w in tables if w == "ai_control_guard")
    # After two loads the table holds each rule once (a reload replaces, never appends).
    out["guard_rules"] = run("nft", "list", "table", "inet", "ai_control_guard").stdout.count("reject with tcp reset")
    print(json.dumps(out))
finally:
    for p in (tail_ns, lan_ns):
        p.kill()
