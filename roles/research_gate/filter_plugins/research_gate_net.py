"""IP filter checks for role research_gate's unit (Python stdlib ipaddress only).

research_gate_ip_problems(allow, deny, required) -> list of problems, empty if fine:
  - an entry that systemd would not take as an address prefix (only CIDRs here; no `any`/`localhost`)
  - a deny entry that is a default route (/0): the public internet must stay allowed (§12.16.4)
  - an allow entry that is not a single host (/32 or /128), or that no deny entry covers (then it is pointless,
    and usually means the SearXNG address isn't where the role thinks it is)
  - a `required` range (the contract's minimum) that no deny entry covers

research_gate_in_network(address, cidr) -> bool
"""

import ipaddress


def _net(value):
    return ipaddress.ip_network(str(value).strip(), strict=True)


def research_gate_ip_problems(allow, deny, required):
    problems = []
    deny_nets = []
    for value in deny or []:
        try:
            net = _net(value)
        except ValueError as exc:
            problems.append("deny entry %r is not a network: %s" % (value, exc))
            continue
        if net.prefixlen == 0:
            problems.append("deny entry %s would block the public internet" % net)
        deny_nets.append(net)
    for value in allow or []:
        try:
            net = _net(value)
        except ValueError as exc:
            problems.append("allow entry %r is not a network: %s" % (value, exc))
            continue
        if net.num_addresses != 1:
            problems.append("allow entry %s must be a single address (/32 or /128)" % net)
        if not any(net.version == d.version and net.subnet_of(d) for d in deny_nets):
            problems.append("allow entry %s is not inside any deny entry" % net)
    for value in required or []:
        net = _net(value)
        if not any(net.version == d.version and net.subnet_of(d) for d in deny_nets):
            problems.append("required deny range %s is not covered" % net)
    return problems


def research_gate_in_network(address, cidr):
    try:
        return ipaddress.ip_address(str(address).strip()) in ipaddress.ip_network(str(cidr).strip(), strict=False)
    except ValueError:
        return False


class FilterModule:
    def filters(self):
        return {
            "research_gate_ip_problems": research_gate_ip_problems,
            "research_gate_in_network": research_gate_in_network,
        }
