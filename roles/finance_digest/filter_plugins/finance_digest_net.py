"""Address checks for role finance_digest (Python stdlib only: urllib.parse, ipaddress).

finance_digest_url_problems(url, allowed_range) -> list of problems, empty if fine:
  the Money Hub URL must be http(s)://<IPv4 literal>[:port][/], with no user info, path, query or fragment,
  and the address inside allowed_range (the tailnet). A hostname is refused: the unit's IP filter allows one
  address, so the URL must name it (and a name would need DNS the role can't pin).
finance_digest_url_host(url) -> the URL's host ('' if it has none)
finance_digest_exclude(network, excludes) -> network minus each of excludes, as a sorted list of CIDRs
finance_digest_overlapping(ranges, network) -> the entries of ranges that overlap network (a malformed entry
  counts as overlapping: it can't be proven clear); [] when network is empty
finance_digest_ip_problems(allow, deny, required, public) -> list of problems, empty if fine:
  - every entry is an address prefix systemd takes as such (only CIDRs; `any` only as the whole deny list)
  - no allow entry is a /0
  - public false: the deny list is exactly ['any'] (everything not allowed is dropped)
  - public true: no deny entry is a /0 (the public internet stays reachable), every allow entry sits inside a
    deny entry (else it is pointless), and every `required` range is covered by a deny entry
"""

import ipaddress
from urllib.parse import urlsplit


def _net(value):
    return ipaddress.ip_network(str(value).strip(), strict=True)


def finance_digest_url_host(url):
    try:
        return urlsplit(str(url)).hostname or ""
    except ValueError:
        return ""


def finance_digest_url_problems(url, allowed_range):
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
    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        problems.append("the host %r is not an IPv4 literal (a hostname can't be pinned in the IP filter)" % host)
        return problems
    try:
        if address not in ipaddress.ip_network(str(allowed_range), strict=True):
            problems.append("%s is outside %s" % (address, allowed_range))
    except ValueError as exc:
        problems.append("the allowed range %r is not a network: %s" % (allowed_range, exc))
    return problems


def finance_digest_exclude(network, excludes):
    nets = [_net(network)]
    for value in excludes or []:
        cut = _net(value)
        out = []
        for net in nets:
            if net.version == cut.version and cut.subnet_of(net):
                out.extend(net.address_exclude(cut))
            elif net.version == cut.version and net.subnet_of(cut):
                continue
            else:
                out.append(net)
        nets = out
    return [str(n) for n in sorted(nets, key=lambda n: (n.version, n.network_address, n.prefixlen))]


def finance_digest_overlapping(ranges, network):
    if not str(network or "").strip():
        return []
    cut = _net(network)
    hits = []
    for value in ranges or []:
        try:
            net = _net(value)
        except ValueError:
            hits.append(str(value))
            continue
        if net.version == cut.version and net.overlaps(cut):
            hits.append(str(net))
    return hits


def finance_digest_ip_problems(allow, deny, required, public):
    problems = []
    deny = list(deny or [])
    deny_nets = []
    if not public:
        if deny != ["any"]:
            problems.append("without Notion the deny list must be exactly ['any'] (got %s)" % deny)
    else:
        for value in deny:
            try:
                net = _net(value)
            except ValueError as exc:
                problems.append("deny entry %r is not a network: %s" % (value, exc))
                continue
            if net.prefixlen == 0:
                problems.append("deny entry %s would block the public internet Notion needs" % net)
            deny_nets.append(net)
        for value in required or []:
            net = _net(value)
            if not any(net.version == d.version and net.subnet_of(d) for d in deny_nets):
                problems.append("required deny range %s is not covered" % net)
    if not allow:
        problems.append("the allow list is empty")
    for value in allow or []:
        try:
            net = _net(value)
        except ValueError as exc:
            problems.append("allow entry %r is not a network: %s" % (value, exc))
            continue
        if net.prefixlen == 0:
            problems.append("allow entry %s allows everything" % net)
        if public and not any(net.version == d.version and net.subnet_of(d) for d in deny_nets):
            problems.append("allow entry %s is not inside any deny entry" % net)
    return problems


class FilterModule:
    def filters(self):
        return {
            "finance_digest_url_problems": finance_digest_url_problems,
            "finance_digest_url_host": finance_digest_url_host,
            "finance_digest_exclude": finance_digest_exclude,
            "finance_digest_overlapping": finance_digest_overlapping,
            "finance_digest_ip_problems": finance_digest_ip_problems,
        }
