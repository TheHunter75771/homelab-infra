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

B2/B1 (B2-CONTRACT.md §2, §4). None of these ever returns or prints a token:
mission_control_notify_token_check(tokens) -> {"names": [valid sender names, sorted], "problems": [...]}: tokens is a
  mapping {name: token} of at most 32 senders (MC1's limit); names ^[a-z0-9-]{1,32}$; tokens strings of 32-256 visible
  ASCII characters (no spaces), no two alike. A problem names the sender only when it is a valid name shorter than
  32 characters (the shortest token); any other key, and any value longer than 12 (18 for an allow_from entry), is
  shown by its length, so tokens and names swapped, or a token pasted anywhere, never print a token.
mission_control_notify_sender_list(hashes, allow_from) -> MC1's notify_senders: [{name, token_sha256[, allow_from]}]
  sorted by name, from {name: sha256 hex} (hashed by the caller with hash('sha256')); allow_from only when non-empty.
mission_control_notify_allow_problems(allow_from, names, tailnet_range) -> list of problems: a mapping whose keys are
  configured senders, each a non-empty list of at most 16 (MC1's limit) distinct IPv4 addresses (canonical form, no
  prefix) inside the range.
mission_control_notify_settings_problems(quiet_hours, dedupe, rate) -> list of problems: quiet_hours null or exactly
  {start, end} as "HH:MM" strings with start != end; dedupe null or an integer 0-86400; rate null or 1-1000.
mission_control_app_links_problems(links) -> list of problems: a list of at most 12 mappings of exactly {label, url};
  label 1-32 printable characters without surrounding spaces; url http(s)://host[:port][/path[?query]] (MC1's link
  rule), at most 300 characters, no user info, fragment, spaces or non-ASCII. Problems never quote the URL (it may
  carry a password).
"""

import ipaddress
import re
from collections.abc import Mapping, Sequence
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


_NOTIFY_NAME = re.compile(r"[a-z0-9-]{1,32}\Z")
_TOKEN_MIN = 32
_NOTIFY_TOKEN = re.compile(r"[!-~]{%d,256}\Z" % _TOKEN_MIN)
_HHMM = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]\Z")
# MC1's rule for a tap-through link (config.py _links), with the query allowed and the fragment not.
_APP_URL = re.compile(r"https?://[^\s/?#@]+(/[^\s#]*)?\Z")


def _is_list(value):
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and not isinstance(value, Mapping)


def _key_label(key):
    """A sender name for a message. Shown only when it is a valid name AND shorter than the shortest token (32): a
    key that could be a token (tokens and names swapped, a token pasted as a name) is shown by its length only."""
    if isinstance(key, str) and _NOTIFY_NAME.match(key) and len(key) < _TOKEN_MIN:
        return repr(key)
    return "<a key of %d characters>" % len(str(key))


def _value_label(value, short=12):
    """A value for a message: as is when it is short (no token fits), else by its length only."""
    text = str(value)
    if len(text) <= short and text.isprintable():
        return repr(text)
    return "<a value of %d characters>" % len(text)


def mission_control_notify_token_check(tokens):
    if not isinstance(tokens, Mapping):
        return {"names": [], "problems": ["mission_control_notify_tokens must be a mapping {sender name: token} (vault)"]}
    names, problems, owners = [], [], {}
    if len(tokens) > 32:
        problems.append("%d senders; MC1 takes at most 32" % len(tokens))
    for key in sorted(tokens, key=str):
        label = _key_label(key)
        if not isinstance(key, str) or not _NOTIFY_NAME.match(key):
            problems.append("sender name %s must be 1-32 characters of a-z 0-9 -" % label)
            continue
        names.append(key)
        token = tokens[key]
        if not isinstance(token, str) or not _NOTIFY_TOKEN.match(token):
            problems.append("sender %s: the token must be a string of 32-256 visible ASCII characters, no spaces" % label)
            continue
        owners.setdefault(token, []).append(key)
    for same in owners.values():
        if len(same) > 1:
            problems.append("senders %s have the same token (each sender needs its own)" % ", ".join(map(_key_label, same)))
    return {"names": names, "problems": problems}


def mission_control_notify_sender_list(hashes, allow_from=None):
    allow = allow_from if isinstance(allow_from, Mapping) else {}
    out = []
    for name in sorted(hashes or {}):
        entry = {"name": name, "token_sha256": hashes[name]}
        ips = allow.get(name)
        if ips:
            entry["allow_from"] = list(ips)
        out.append(entry)
    return out


def mission_control_notify_allow_problems(allow_from, names, tailnet_range):
    if not isinstance(allow_from, Mapping):
        return ["mission_control_notify_allow_from must be a mapping {sender name: [tailnet IPv4, ...]} ({} for none)"]
    net = ipaddress.ip_network(str(tailnet_range), strict=True)
    problems = []
    for key in sorted(allow_from, key=str):
        label = _key_label(key)
        if key not in (names or []):
            problems.append("allow_from names %s, which isn't a configured sender (mission_control_notify_tokens)" % label)
        ips = allow_from[key]
        if not _is_list(ips) or not ips:
            problems.append("allow_from[%s] must be a non-empty list of tailnet IPv4 addresses (leave the sender out for "
                            "no address check)" % label)
            continue
        if len(ips) > 16:
            problems.append("allow_from[%s] lists %d addresses; MC1 takes at most 16" % (label, len(ips)))
        seen = set()
        for ip in ips:
            text = ip if isinstance(ip, str) else repr(ip)
            try:
                address = ipaddress.IPv4Address(ip) if isinstance(ip, str) else None
            except ValueError:
                address = None
            if address is None or str(address) != ip:
                problems.append("allow_from[%s]: %s is not an IPv4 address (one host, no prefix)"
                                % (label, _value_label(text, 18)))
            elif address not in net:
                problems.append("allow_from[%s]: %s is outside the tailnet %s" % (label, ip, net))
            elif ip in seen:
                problems.append("allow_from[%s] lists %s twice" % (label, ip))
            seen.add(text)
    return problems


def mission_control_notify_settings_problems(quiet_hours, dedupe=None, rate=None):
    problems = []
    if quiet_hours is not None:
        if not isinstance(quiet_hours, Mapping) or sorted(str(k) for k in quiet_hours) != ["end", "start"]:
            problems.append('mission_control_quiet_hours must be null (off) or exactly {start: "HH:MM", end: "HH:MM"}')
        else:
            good = True
            for key in ("start", "end"):
                value = quiet_hours[key]
                if not isinstance(value, str) or not _HHMM.match(value):
                    good = False
                    problems.append('quiet_hours %s must be a quoted "HH:MM" from 00:00 to 23:59 (YAML reads an unquoted '
                                    "22:00 as the number 1320); it is %s" % (key, _value_label(value)))
            if good and quiet_hours["start"] == quiet_hours["end"]:
                problems.append("quiet_hours start and end are the same time (empty or all day is ambiguous; null is off)")
    for name, value, lo, hi in (("mission_control_notify_dedupe_seconds", dedupe, 0, 86400),
                                ("mission_control_notify_rate_per_hour", rate, 1, 1000)):
        if value is None:
            continue
        number = None
        if isinstance(value, int) and not isinstance(value, bool):
            number = value
        elif isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 6:
            number = int(value)
        if number is None or not lo <= number <= hi:
            problems.append("%s must be null (MC1's default) or an integer %d-%d; it is %s" % (name, lo, hi, _value_label(value)))
    return problems


def mission_control_app_links_problems(links):
    if not _is_list(links):
        return ["mission_control_app_links must be a list of {label, url} ([] for no Apps card)"]
    problems = []
    if len(links) > 12:
        problems.append("mission_control_app_links has %d entries; MC1 takes at most 12" % len(links))
    for i, link in enumerate(links):
        where = "app_links[%d]" % i
        if not isinstance(link, Mapping) or sorted(str(k) for k in link) != ["label", "url"]:
            problems.append("%s must be exactly {label, url}" % where)
            continue
        label, url = link["label"], link["url"]
        if not isinstance(label, str) or not 0 < len(label) <= 32 or not label.isprintable() or label != label.strip():
            problems.append("%s: the label must be 1-32 printable characters without leading or trailing spaces" % where)
        else:
            where += " (%s)" % label
        if not isinstance(url, str) or not url:
            problems.append("%s: the url is empty or not a string" % where)
            continue
        if len(url) > 300:
            problems.append("%s: the url is longer than 300 characters" % where)
        if any(ord(c) < 33 or ord(c) > 126 for c in url):
            problems.append("%s: the url has spaces, control or non-ASCII characters" % where)
            continue
        if not url.startswith(("http://", "https://")):
            problems.append("%s: the url must start with http:// or https://" % where)
            continue
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            problems.append("%s: the url doesn't parse (host or port)" % where)
            continue
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            problems.append("%s: the url must not carry credentials (user info)" % where)
        if "#" in url:
            problems.append("%s: the url must not have a fragment (#...)" % where)
        if not parts.hostname:
            problems.append("%s: the url has no host" % where)
        if port is not None and not 0 < port < 65536:
            problems.append("%s: the url's port is out of range" % where)
        if not _APP_URL.match(url):
            problems.append("%s: the url must be http(s)://host[:port] followed by nothing or a /path" % where)
    return problems


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
            "mission_control_notify_token_check": mission_control_notify_token_check,
            "mission_control_notify_sender_list": mission_control_notify_sender_list,
            "mission_control_notify_allow_problems": mission_control_notify_allow_problems,
            "mission_control_notify_settings_problems": mission_control_notify_settings_problems,
            "mission_control_app_links_problems": mission_control_app_links_problems,
        }
