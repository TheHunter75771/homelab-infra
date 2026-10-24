"""CIDR checks for role docker's daemon.json consistency assert (Python stdlib ipaddress only).

docker_address_pool_problems(pools, container, avoid) -> list of human-readable problems, empty if fine:
  - a pool base that is not a valid network, or not inside `container`
  - a pool size that is not an int between the base's prefix length and 30
  - a pool that overlaps another pool
  - a pool that overlaps any network in `avoid` (docker0's bip, hl-public, ...)
"""

import ipaddress


def _net(value):
    return ipaddress.ip_network(str(value).strip(), strict=False)


def docker_address_pool_problems(pools, container, avoid):
    problems = []
    container_net = _net(container)
    avoid_nets = []
    for value in avoid or []:
        try:
            avoid_nets.append((str(value), _net(value)))
        except ValueError as exc:
            problems.append("cannot parse reserved network %r: %s" % (value, exc))

    bases = []
    for pool in pools or []:
        base = str(pool.get("base", ""))
        try:
            net = _net(base)
        except ValueError as exc:
            problems.append("pool base %r is not a network: %s" % (base, exc))
            continue
        if net.version != container_net.version or not net.subnet_of(container_net):
            problems.append("pool %s is not inside %s" % (net, container_net))
        size = pool.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or not net.prefixlen <= size <= 30:
            problems.append("pool %s has size %r; it must be an int from %d to 30" % (net, size, net.prefixlen))
        for other in bases:
            if net.overlaps(other):
                problems.append("pool %s overlaps pool %s" % (net, other))
        for label, other in avoid_nets:
            if net.version == other.version and net.overlaps(other):
                problems.append("pool %s overlaps reserved network %s" % (net, label))
        bases.append(net)

    if not bases:
        problems.append("no address pools are defined")
    return problems


class FilterModule:
    def filters(self):
        return {"docker_address_pool_problems": docker_address_pool_problems}
