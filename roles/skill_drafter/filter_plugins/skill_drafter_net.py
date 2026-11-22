"""Address checks for role skill_drafter (Python stdlib only: ipaddress). The same rules as role
finance_digest's filters (Reviewer H, H5-1), for the drafter's broker-only filter.

skill_drafter_ip_in(address, network) -> True if address is an IPv4 literal (dotted quad) inside network
skill_drafter_exclude(network, excludes) -> network minus each of excludes, as a sorted list of CIDRs
skill_drafter_overlapping(ranges, network) -> the entries of ranges that overlap network (a malformed entry
  counts as overlapping: it can't be proven clear); [] when network is empty
skill_drafter_ip_problems(allow, deny) -> list of problems, empty if fine:
  - the deny list is exactly ['any'] (everything not allowed is dropped)
  - the allow list is not empty, every entry is a CIDR systemd takes as such, and none is a /0
"""

import ipaddress


def _net(value):
    return ipaddress.ip_network(str(value).strip(), strict=True)


def skill_drafter_ip_in(address, network):
    text = str(address or "")
    if text.count(".") != 3:
        return False  # refuse short forms such as 100.64.2
    try:
        return ipaddress.IPv4Address(text) in ipaddress.ip_network(str(network), strict=True)
    except ValueError:
        return False


def skill_drafter_exclude(network, excludes):
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


def skill_drafter_overlapping(ranges, network):
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


def skill_drafter_ip_problems(allow, deny):
    problems = []
    if list(deny or []) != ["any"]:
        problems.append(f"the deny list must be exactly ['any'] (got {list(deny or [])})")
    if not allow:
        problems.append("the allow list is empty")
    for value in allow or []:
        try:
            net = _net(value)
        except ValueError as exc:
            problems.append(f"allow entry {value!r} is not a network: {exc}")
            continue
        if net.prefixlen == 0:
            problems.append(f"allow entry {net} allows everything")
    return problems


class FilterModule:
    def filters(self):
        return {
            "skill_drafter_ip_in": skill_drafter_ip_in,
            "skill_drafter_exclude": skill_drafter_exclude,
            "skill_drafter_overlapping": skill_drafter_overlapping,
            "skill_drafter_ip_problems": skill_drafter_ip_problems,
        }
