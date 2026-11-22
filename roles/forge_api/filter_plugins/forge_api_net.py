"""Address checks for role forge_api (Python stdlib only: ipaddress).

forge_api_gateway_of(subnet) -> the first host of an IPv4 subnet (the address Docker gives a bridge network's
  gateway), or '' if subnet isn't an IPv4 network address
forge_api_ip_in(address, network) -> True if address is a dotted-quad IPv4 literal inside network and is
  neither its network nor its broadcast address
forge_api_ip_problems(allow, deny, bind) -> list of problems, empty if fine (§12.25: inbound from open-webui only):
  - the deny list is exactly ['any']
  - the allow list is exactly one entry, a single IPv4 host (/32), not loopback, unspecified, multicast or
    link-local, and not the bind address (the host must never be an allowed peer)
forge_api_ufw_problems(rule, webui, bind, port) -> list of problems with the UFW rule line, empty if fine:
  it must read exactly `allow from <webui> to <bind> port <port> proto tcp comment '<text>'`, so a rule from
  any, to any, on another port or protocol, or without a comment is refused
forge_api_ufw_port_rules(lines, port) -> the `ufw show added` lines that would let a connection in to port:
  inbound allow/limit rules whose port spec covers it (the port itself, a range or a list holding it), or that
  name no port at all (an address rule admits every port). deny/reject, outbound and route rules are skipped, and
  so are application profiles (a name, not a port)
"""

import ipaddress
import re


def _net(value):
    return ipaddress.ip_network(str(value).strip(), strict=True)


def _ipv4(text):
    text = str(text or "").strip()
    if text.count(".") != 3:
        raise ValueError(f"not a dotted-quad IPv4 address: {text!r}")
    return ipaddress.IPv4Address(text)


def forge_api_gateway_of(subnet):
    try:
        net = ipaddress.IPv4Network(str(subnet or "").strip(), strict=True)
    except ValueError:
        return ""
    if net.num_addresses < 4:
        return ""
    return str(net.network_address + 1)


def forge_api_ip_in(address, network):
    try:
        addr = _ipv4(address)
        net = ipaddress.IPv4Network(str(network or "").strip(), strict=True)
    except ValueError:
        return False
    return addr in net and addr not in (net.network_address, net.broadcast_address)


def forge_api_ip_problems(allow, deny, bind):
    problems = []
    if list(deny or []) != ["any"]:
        problems.append(f"the deny list must be exactly ['any'] (got {list(deny or [])})")
    allow = list(allow or [])
    if len(allow) != 1:
        problems.append(f"the allow list must be exactly open-webui's /32 (got {allow})")
    for value in allow:
        try:
            net = _net(value)
        except ValueError as exc:
            problems.append(f"allow entry {value!r} is not a network: {exc}")
            continue
        if net.version != 4 or net.prefixlen != 32:
            problems.append(f"allow entry {net} must be a single IPv4 host (/32)")
            continue
        addr = net.network_address
        if addr.is_loopback or addr.is_unspecified or addr.is_multicast or addr.is_link_local:
            problems.append(f"allow entry {net} is loopback, unspecified, multicast or link-local")
        if str(addr) == str(bind or "").strip():
            problems.append(f"allow entry {net} is the bind address: the host must never be an allowed peer")
    return problems


_UFW = re.compile(
    r"^allow from (?P<src>\S+) to (?P<dst>\S+) port (?P<port>[0-9]+) proto (?P<proto>\S+)"
    r" comment '(?P<comment>[A-Za-z0-9 _.,:;()/+=@-]+)'$"
)


def forge_api_ufw_problems(rule, webui, bind, port):
    text = " ".join(str(rule or "").split())
    m = _UFW.match(text)
    if not m:
        return [f"the UFW rule {text!r} is not `allow from <webui> to <bind> port <port> proto tcp comment '...'`"]
    problems = []
    if m.group("src") != str(webui or "").strip():
        problems.append("the UFW rule must come from open-webui's address {} only (got {})".format(webui, m.group("src")))
    if m.group("dst") != str(bind or "").strip():
        problems.append("the UFW rule must go to the bind address {} only (got {})".format(bind, m.group("dst")))
    if m.group("port") != str(port).strip():
        problems.append("the UFW rule must be for port {} (got {})".format(port, m.group("port")))
    if m.group("proto") != "tcp":
        problems.append("the UFW rule must be tcp (got {})".format(m.group("proto")))
    return problems


_PORTSPEC = re.compile(r"^[0-9]+([:,][0-9]+)*(/(tcp|udp))?$")


def _covers(spec, port):
    for part in spec.split("/")[0].split(","):
        if ":" in part:
            lo, hi = part.split(":", 1)
            if lo.isdigit() and hi.isdigit() and int(lo) <= port <= int(hi):
                return True
        elif part.isdigit() and int(part) == port:
            return True
    return False


def forge_api_ufw_port_rules(lines, port):
    port = int(port)
    hits = []
    for raw in lines or []:
        line = " ".join(str(raw).split())
        words = line.split(" ")
        if words[:1] == ["ufw"]:
            words = words[1:]
        if "comment" in words:
            words = words[: words.index("comment")]
        if not words or words[0] not in ("allow", "limit") or words[1:2] == ["out"]:
            continue  # deny/reject, route rules, outbound rules, the header
        rest = words[1:]
        m = re.search(r"(?:^| )port (\S+)", " ".join(rest))
        if m:
            spec = m.group(1)
        elif "from" in rest or "to" in rest:
            spec = None  # an address rule without a port: every port
        else:
            tail = [w for w in rest if w != "in"]
            if tail[:1] == ["on"]:
                tail = tail[2:]
            if len(tail) != 1 or not _PORTSPEC.match(tail[0]):
                continue  # an application profile
            spec = tail[0]
        if spec is None or _covers(spec, port):
            hits.append(line)
    return hits


class FilterModule:
    def filters(self):
        return {
            "forge_api_gateway_of": forge_api_gateway_of,
            "forge_api_ip_in": forge_api_ip_in,
            "forge_api_ip_problems": forge_api_ip_problems,
            "forge_api_ufw_problems": forge_api_ufw_problems,
            "forge_api_ufw_port_rules": forge_api_ufw_port_rules,
        }
