"""Role ai_stack filters (stdlib only; tests/test_ai_stack_net.py): address-plan checks for the two pinned
networks (docs/CONTRACTS.md §12.18b) and the mountinfo reader for the /skills check (§12.19)."""

import ipaddress


def _net(value):
    return ipaddress.ip_network(str(value), strict=True)


def ai_stack_net_problems(ai_subnet, extract_subnet, pools):
    """Problems with the two pinned subnets against role docker's address pools (list of {base, size})."""
    problems, nets = [], {}
    for label, value in (("ai_stack_network_subnet", ai_subnet), ("ai_stack_extract_subnet", extract_subnet)):
        try:
            net = _net(value)
        except ValueError as exc:
            problems.append(f"{label} {value!r} is not a network address: {exc}")
            continue
        if net.version != 4:
            problems.append(f"{label} {net} is not IPv4")
            continue
        if net.prefixlen > 28:
            problems.append(f"{label} {net} is smaller than a /28")
        bases = []
        for pool in pools or []:
            try:
                bases.append(_net(pool["base"]))
            except (KeyError, TypeError, ValueError):
                problems.append(f"bad Docker address pool {pool!r}")
        if not any(net.subnet_of(base) for base in bases if base.version == 4):
            problems.append(f"{label} {net} is not inside a Docker address pool ({', '.join(map(str, bases))})")
        nets[label] = net
    if len(nets) == 2 and nets["ai_stack_network_subnet"].overlaps(nets["ai_stack_extract_subnet"]):
        problems.append(f"the ai and extract subnets overlap ({ai_subnet}, {extract_subnet})")
    return problems


def ai_stack_network_conflicts(subnet, lines, own_name):
    """From `<name> <subnet>...` lines (docker network inspect): other networks overlapping `subnet`, and
    `own_name` itself if it exists with a different subnet list."""
    net = _net(subnet)
    found = []
    for line in lines or []:
        parts = str(line).split()
        if not parts:
            continue
        name, subnets = parts[0], []
        for value in parts[1:]:
            try:
                subnets.append(_net(value))
            except ValueError:
                continue
        if name == own_name:
            if [s for s in subnets if s.version == 4] != [net]:
                found.append(f"{name} exists with {' '.join(map(str, subnets)) or 'no subnet'}, not {net}")
            continue
        found.extend(f"{name} {other}" for other in subnets if other.version == net.version and net.overlaps(other))
    return found


def ai_stack_mountinfo_stack(lines, target):
    """The mounts at `target` in /proc/<pid>/mountinfo lines, bottom first: [{"options": [...], "source":
    ..., "fstype": ...}]. The last one is what a process sees at `target`. Mount points are octal-escaped by
    the kernel (\\040 for a space); `target` is compared escaped the same way."""
    want = str(target).replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011").replace("\n", "\\012")
    stack = []
    for line in lines or []:
        fields = str(line).split()
        if " - " not in str(line) or len(fields) < 10 or fields[4] != want:
            continue
        sep = fields.index("-")
        stack.append({"options": fields[5].split(","), "fstype": fields[sep + 1], "source": fields[sep + 2]})
    return stack


def ai_stack_pinned_ip_problems(subnet, pins):
    """Problems with the pinned container addresses {name: ip} on the `ai` network (§12.23): each a plain IPv4
    address inside `subnet`, not its network or broadcast address, not the first host (Docker's gateway),
    and no two alike."""
    problems = []
    try:
        net = _net(subnet)
    except ValueError as exc:
        return [f"ai_stack_network_subnet {subnet!r} is not a network address: {exc}"]
    seen = {}
    for name, value in (pins or {}).items():
        try:
            addr = ipaddress.ip_address(str(value))
        except ValueError:
            problems.append(f"{name} {value!r} is not an IP address")
            continue
        if addr.version != 4 or addr not in net:
            problems.append(f"{name} {addr} is not inside {net}")
        elif addr in (net.network_address, net.broadcast_address):
            problems.append(f"{name} {addr} is the network or broadcast address of {net}")
        elif addr == net.network_address + 1:
            problems.append(f"{name} {addr} is {net}'s gateway")
        if str(addr) in seen:
            problems.append(f"{name} and {seen[str(addr)]} are both {addr}")
        seen.setdefault(str(addr), name)
    return problems


def ai_stack_pinned_ip_holders(lines, pins):
    """From `<container> <ip>/<len>` lines (docker network inspect of `ai`): containers that hold one of the
    pinned addresses but aren't the service it is pinned for. pins: {container: ip}."""
    owner = {str(ip): name for name, ip in (pins or {}).items()}
    found = []
    for line in lines or []:
        parts = str(line).split()
        if len(parts) != 2:
            continue
        name, cidr = parts
        ip = cidr.split("/")[0]
        if ip in owner and owner[ip] != name:
            found.append(f"{name} holds {ip} (pinned for {owner[ip]})")
    return found


class FilterModule:
    def filters(self):
        return {
            "ai_stack_net_problems": ai_stack_net_problems,
            "ai_stack_network_conflicts": ai_stack_network_conflicts,
            "ai_stack_mountinfo_stack": ai_stack_mountinfo_stack,
            "ai_stack_pinned_ip_problems": ai_stack_pinned_ip_problems,
            "ai_stack_pinned_ip_holders": ai_stack_pinned_ip_holders,
        }
