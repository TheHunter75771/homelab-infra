"""Address and lock-file checks for role mission_control (Python stdlib only).

mission_control_url_host(url) -> the URL's host ('' if none or unparsable)
mission_control_url_port(url) -> the URL's port, or the scheme's default (0 if unknown)
mission_control_url_problems(url, allowed_range, require_ip) -> list of problems, empty if fine:
  http(s)://host[:port][/], no user info, path, query or fragment; with require_ip the host must be an IPv4
  literal inside allowed_range (the unit's IP filter allows exactly that address).
mission_control_ip_verdict(address, allow, deny) -> 'allow' | 'deny' | 'pass'
  systemd's IPAddressAllow=/IPAddressDeny= semantics (systemd.resource-control(5)): an address matching an
  allow entry is let through even when a deny entry matches too ("allow wins"); otherwise a deny match drops
  it; an address in neither list passes. 'localhost' = 127.0.0.0/8 + ::1/128, 'any' = 0.0.0.0/0 + ::/0.
mission_control_ip_problems(allow, deny, required) -> list of problems, empty if fine:
  - every entry parses as a systemd address prefix ('localhost' allowed in the allow list only, never 'any');
  - allow entries other than 'localhost' are single hosts (/32 or /128), never wider;
  - no deny entry is a /0 (the push services on the public internet stay reachable);
  - every allow host sits inside a deny entry (otherwise it is pointless: it passes anyway);
  - every `required` range is covered by a deny entry.
mission_control_first_host(cidr) -> the network's first host address ('' if cidr is empty or not a network)
mission_control_probe_plan(allow, deny, denied, public, tcp, tcp_denied, runtime) -> the egress probe's targets, each
  {name, kind, host, port, role, verdict, want}: a UDP send to every allowed host (role allow), to every address in
  `denied` (role deny) and to `public` (role public), with the verdict of mission_control_ip_verdict and the result
  the sandboxed probe must report (EPERM when denied, else "sent"); then each [name, host, port] of `tcp`, or a bare
  host name meaning that push service on 443 (role tcp, want "ok"). Port 9 (discard) for UDP: nothing listens for it and no reply is needed.
mission_control_probe_problems(plan, results, crypto=True) -> list of problems, empty if fine: the plan's own roles agree with
  the verdicts (an address meant to be denied that the filter would let through is a problem even before probing),
  the crypto check is "ok", and every target's result equals its want.
mission_control_vapid_subject_ok(sub) -> True when py_vapid signs with it: py_vapid's own _check_sub rule (strict
  mode, verbatim) AND a whole-string https://<host> (no port, no path) or mailto:<address>. The same rule as
  files/vapid_check.py subject_ok (tests/subject_vectors.json pins both).
mission_control_lock_pins(text) -> {normalized name: version} for every `name==version` requirement line
mission_control_lock_problems(text) -> list of problems: each requirement is `name==version` with at least one
  --hash=sha256:<64 hex>, nothing else (no URLs, -e, -r/-c includes, extras, markers or options).
mission_control_dist_drift(installed, pins) -> sorted list of 'name: installed X, want Y' lines (missing,
  wrong version, or extra distributions besides pip); [] when the venv is exactly the lock.
"""

import ipaddress
import re
from urllib.parse import urlsplit

_LOCALHOST = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]
_ANY = [ipaddress.ip_network("0.0.0.0/0"), ipaddress.ip_network("::/0")]
_HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")
_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.+!_-]+)$")


def _norm(name):
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def _entries(value):
    v = str(value).strip()
    if v == "localhost":
        return list(_LOCALHOST)
    if v == "any":
        return list(_ANY)
    return [ipaddress.ip_network(v, strict=True)]


def mission_control_url_host(url):
    try:
        return urlsplit(str(url)).hostname or ""
    except ValueError:
        return ""


def mission_control_url_port(url):
    try:
        parts = urlsplit(str(url))
        port = parts.port
    except ValueError:
        return 0
    if port:
        return port
    return {"http": 80, "https": 443}.get(parts.scheme, 0)


def mission_control_url_problems(url, allowed_range, require_ip=True):
    if not isinstance(url, str) or not url:
        return ["the URL is empty or not a string"]
    if any(ord(c) < 33 or ord(c) > 126 for c in url):
        return ["the URL has spaces, control or non-ASCII characters"]
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        return ["the URL doesn't parse: %s" % exc]
    problems = []
    if parts.scheme not in ("http", "https"):
        problems.append("the scheme must be http or https")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        problems.append("the URL must not carry user info")
    if parts.path not in ("", "/") or parts.query or parts.fragment or url.endswith(("?", "#")):
        problems.append("the URL must be the bare origin (no path, query or fragment)")
    if port is not None and not 0 < port < 65536:
        problems.append("the port is out of range")
    host = parts.hostname or ""
    if not host:
        problems.append("the URL has no host")
        return problems
    if not require_ip:
        return problems
    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        problems.append("the host %r is not an IPv4 literal (the IP filter needs the address)" % host)
        return problems
    try:
        if address not in ipaddress.ip_network(str(allowed_range), strict=True):
            problems.append("%s is outside %s" % (address, allowed_range))
    except ValueError as exc:
        problems.append("the allowed range %r is not a network: %s" % (allowed_range, exc))
    return problems


def _matches(address, entries):
    for value in entries or []:
        for net in _entries(value):
            if net.version == address.version and address in net:
                return True
    return False


def mission_control_ip_verdict(address, allow, deny):
    addr = ipaddress.ip_address(str(address).strip())
    if _matches(addr, allow):
        return "allow"
    if _matches(addr, deny):
        return "deny"
    return "pass"


def mission_control_ip_problems(allow, deny, required):
    problems = []
    deny_nets = []
    for value in deny or []:
        if str(value).strip() in ("any", "localhost"):
            problems.append("deny entry %r: list the ranges, not a keyword" % value)
            continue
        try:
            net = _entries(value)[0]
        except ValueError as exc:
            problems.append("deny entry %r is not a network: %s" % (value, exc))
            continue
        if net.prefixlen == 0:
            problems.append("deny entry %s would block the public internet the push services need" % net)
        deny_nets.append(net)
    for value in required or []:
        net = ipaddress.ip_network(str(value), strict=True)
        if not any(net.version == d.version and net.subnet_of(d) for d in deny_nets):
            problems.append("required deny range %s is not covered" % net)
    if not allow:
        problems.append("the allow list is empty")
    for value in allow or []:
        v = str(value).strip()
        if v == "any":
            problems.append("allow entry 'any' allows everything")
            continue
        try:
            nets = _entries(v)
        except ValueError as exc:
            problems.append("allow entry %r is not a network: %s" % (value, exc))
            continue
        for net in nets:
            if v != "localhost" and net.prefixlen != net.max_prefixlen:
                problems.append("allow entry %s is wider than one host" % net)
            if not any(net.version == d.version and net.subnet_of(d) for d in deny_nets):
                problems.append("allow entry %s is not inside any deny entry (it passes anyway)" % net)
    return problems


_PY_VAPID_SUB = re.compile(
    r"^(mailto:.+@((localhost|[%\w-]+(\.[%\w-]+)+|([0-9a-f]{1,4}):+([0-9a-f]{1,4})?)))|https:\/\/(localhost|[\w-]+\.[\w\.-]+|([0-9a-f]{1,4}:+)+([0-9a-f]{1,4})?)$",
    re.IGNORECASE,
)
_STRICT_SUB = re.compile(r"(https://[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+|mailto:[^\s@/:]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+)\Z")


def mission_control_vapid_subject_ok(sub):
    return isinstance(sub, str) and _PY_VAPID_SUB.match(sub) is not None and _STRICT_SUB.match(sub) is not None


def mission_control_first_host(cidr):
    try:
        net = ipaddress.ip_network(str(cidr).strip(), strict=False)
    except ValueError:
        return ""
    return str(next(net.hosts(), net.network_address))


BLOCKED = ("EPERM", "ETIMEDOUT", "EHOSTUNREACH", "ENETUNREACH", "EACCES")


def mission_control_probe_plan(allow, deny, denied, public, tcp, tcp_denied=None, runtime=None):
    plan, seen = [], set()

    def add(name, kind, host, port, role, want):
        if name in seen:
            return
        seen.add(name)
        verdict = mission_control_ip_verdict(host, allow, deny) if kind == "udp" else ""
        if kind == "udp":
            want = "EPERM" if verdict == "deny" else "sent"
        plan.append({"name": name, "kind": kind, "host": host, "port": int(port), "role": role,
                     "verdict": verdict, "want": want})

    for value in allow or []:
        v = str(value).strip()
        host = "127.0.0.1" if v == "localhost" else re.sub(r"/(32|128)$", "", v)
        add("udp allow " + host, "udp", host, 9, "allow", "")
    for value in denied or []:
        v = str(value).strip()
        if v:
            add("udp deny " + v, "udp", v, 9, "deny", "")
    if str(public or "").strip():
        add("udp public " + str(public).strip(), "udp", str(public).strip(), 9, "public", "")
    for item in tcp or []:
        if isinstance(item, str):
            add("tcp push " + item, "tcp", item, 443, "tcp", "ok")
        else:
            name, host, port = item[0], item[1], item[2]
            add("tcp " + str(name), "tcp", str(host), port, "tcp", item[3] if len(item) > 3 else "ok")
    for name, host, port in tcp_denied or []:
        add("tcp deny " + str(name), "tcp", str(host), port, "deny", "blocked")
    rt = runtime or {}
    if rt.get("systemctl"):
        add("runtime systemctl", "systemctl", str(rt["systemctl"]), 0, "runtime", "ok")
    for host in rt.get("dns") or []:
        add("runtime dns " + host, "dns", host, 0, "runtime", "ok")
    for path in rt.get("absent") or []:
        add("runtime absent " + path, "absent", path, 0, "runtime", "absent")
    return plan


def mission_control_probe_problems(plan, results, crypto=True):
    problems = []
    results = results or {}
    expect = {"allow": "allow", "deny": "deny", "public": "pass"}
    for p in plan or []:
        # UDP targets only: a TCP deny target may be blocked after Docker's DNAT (127.0.0.1:8888 -> SearXNG's container),
        # so its pre-DNAT address can be an allowed one; the live result decides it.
        if p["role"] in expect and p["kind"] == "udp" and p["verdict"] != expect[p["role"]]:
            problems.append("%s: the IP filter's verdict is %s, it must be %s" % (p["name"], p["verdict"], expect[p["role"]]))
    if crypto and results.get("crypto") != "ok":
        problems.append("crypto (pywebpush import + P-256 sign under the sandbox): %s" % results.get("crypto", "missing"))
    for p in plan or []:
        got = results.get(p["name"], "missing")
        ok = got in BLOCKED if p["want"] == "blocked" else got == p["want"]
        if not ok:
            problems.append("%s:%s %s: got %s, want %s" % (p["host"], p["port"], p["name"], got, p["want"]))
    return problems


def mission_control_lock_requirements(text):
    """Join backslash continuations; drop comments and blank lines."""
    out, cur = [], ""
    for raw in str(text).splitlines():
        line = raw.split("#", 1)[0].rstrip() if not raw.lstrip().startswith("--hash") else raw.rstrip()
        if not line.strip():
            if cur:
                out.append(cur.strip())
                cur = ""
            continue
        if line.endswith("\\"):
            cur += " " + line[:-1].strip()
            continue
        cur += " " + line.strip()
        out.append(cur.strip())
        cur = ""
    if cur.strip():
        out.append(cur.strip())
    return out


def mission_control_lock_pins(text):
    pins = {}
    for req in mission_control_lock_requirements(text):
        m = _REQ.match(req.split(" ", 1)[0])
        if m:
            pins[_norm(m.group(1))] = m.group(2)
    return pins


def mission_control_lock_problems(text):
    problems = []
    reqs = mission_control_lock_requirements(text)
    if not reqs:
        return ["the lock has no requirements"]
    seen = set()
    for req in reqs:
        head, _, rest = req.partition(" ")
        m = _REQ.match(head)
        if not m:
            problems.append("%r is not a plain name==version pin" % head[:80])
            continue
        name = _norm(m.group(1))
        if name in seen:
            problems.append("%s is pinned twice" % name)
        seen.add(name)
        tokens = rest.split()
        if not tokens or any(not _HASH.fullmatch(t) for t in tokens):
            problems.append("%s must carry only --hash=sha256:<64 hex> options (at least one)" % name)
    return problems


def mission_control_dist_drift(installed, pins):
    have = {_norm(k): str(v) for k, v in (installed or {}).items()}
    want = {_norm(k): str(v) for k, v in (pins or {}).items()}
    lines = []
    for name in sorted(set(have) | set(want)):
        if name == "pip":
            continue
        if have.get(name) != want.get(name):
            lines.append("%s: installed %s, want %s" % (name, have.get(name, "none"), want.get(name, "none")))
    return lines


class FilterModule:
    def filters(self):
        return {
            "mission_control_url_host": mission_control_url_host,
            "mission_control_url_port": mission_control_url_port,
            "mission_control_url_problems": mission_control_url_problems,
            "mission_control_ip_verdict": mission_control_ip_verdict,
            "mission_control_ip_problems": mission_control_ip_problems,
            "mission_control_first_host": mission_control_first_host,
            "mission_control_vapid_subject_ok": mission_control_vapid_subject_ok,
            "mission_control_probe_plan": mission_control_probe_plan,
            "mission_control_probe_problems": mission_control_probe_problems,
            "mission_control_lock_pins": mission_control_lock_pins,
            "mission_control_lock_problems": mission_control_lock_problems,
            "mission_control_dist_drift": mission_control_dist_drift,
        }
