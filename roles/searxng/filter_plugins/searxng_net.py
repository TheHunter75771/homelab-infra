"""CIDR checks for role searxng's pinned network (Python stdlib ipaddress only).

searxng_net_problems(subnet, gateway, ipv4, container_range, pools, avoid) -> list of problems, empty if fine:
  - the subnet is not a strict IPv4 network with a /24../29 prefix, or is not inside `container_range`
  - it overlaps a Docker address pool (pools: [{base, size}]) or a network in `avoid`
  - the gateway or the container address is not a usable host address of the subnet, or they are equal

searxng_network_overlaps(subnet, lines, own_name) -> list of "<name> <subnet>" for every existing Docker
network that overlaps `subnet`, except the role's own network `own_name`. `lines` are
"<name> <subnet> [<subnet>...]" (docker network inspect --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}').
"""

import ipaddress


def _net(value, strict=False):
    return ipaddress.ip_network(str(value).strip(), strict=strict)


def searxng_net_problems(subnet, gateway, ipv4, container_range, pools, avoid):
    problems = []
    try:
        net = _net(subnet, strict=True)
    except ValueError as exc:
        return ["subnet %r is not a network: %s" % (subnet, exc)]
    if net.version != 4 or not 24 <= net.prefixlen <= 29:
        problems.append("subnet %s must be IPv4 with a prefix from /24 to /29" % net)
    try:
        container = _net(container_range)
    except ValueError as exc:
        problems.append("container range %r is not a network: %s" % (container_range, exc))
    else:
        if net.version != container.version or not net.subnet_of(container):
            problems.append("subnet %s is not inside the Docker container range %s" % (net, container))
    for pool in pools or []:
        base = pool.get("base", "") if isinstance(pool, dict) else pool
        try:
            other = _net(base)
        except ValueError as exc:
            problems.append("cannot parse Docker pool %r: %s" % (base, exc))
            continue
        if other.version == net.version and net.overlaps(other):
            problems.append("subnet %s overlaps Docker address pool %s (compose networks are allocated there)" % (net, other))
    for value in avoid or []:
        try:
            other = _net(value)
        except ValueError as exc:
            problems.append("cannot parse reserved network %r: %s" % (value, exc))
            continue
        if other.version == net.version and net.overlaps(other):
            problems.append("subnet %s overlaps reserved network %s" % (net, value))
    hosts = {}
    for label, value in (("gateway", gateway), ("container address", ipv4)):
        try:
            addr = ipaddress.ip_address(str(value).strip())
        except ValueError as exc:
            problems.append("%s %r is not an IP address: %s" % (label, value, exc))
            continue
        if addr not in net or addr in (net.network_address, net.broadcast_address):
            problems.append("%s %s is not a usable host address in %s" % (label, addr, net))
        hosts[label] = addr
    if len(hosts) == 2 and hosts["gateway"] == hosts["container address"]:
        problems.append("the gateway and the container address are both %s" % hosts["gateway"])
    return problems


def searxng_network_overlaps(subnet, lines, own_name):
    net = _net(subnet)
    found = []
    for line in lines or []:
        parts = str(line).split()
        if not parts or parts[0] == own_name:
            continue
        for value in parts[1:]:
            try:
                other = _net(value)
            except ValueError:
                continue
            if other.version == net.version and net.overlaps(other):
                found.append("%s %s" % (parts[0], other))
    return found


class FilterModule:
    def filters(self):
        return {
            "searxng_net_problems": searxng_net_problems,
            "searxng_network_overlaps": searxng_network_overlaps,
        }
