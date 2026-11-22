"""Which addresses the gate may connect to, and the one DNS lookup per hop (CONTRACTS §12.16.3).

The gate resolves a host name **once**, checks every address the resolver returned, and then
connects to one of those addresses itself; nothing resolves the name again (DNS rebinding cannot
swap the address between the check and the connect).

Refused: loopback, private (RFC 1918 and the other special-purpose ranges Python marks
non-global), link-local, CGNAT / tailnet ``100.64.0.0/10``, ULA ``fc00::/7``, multicast,
unspecified, reserved, documentation and benchmarking ranges, and anything else that is not
globally routable. IPv6 forms that carry an IPv4 address are checked by their embedded address
too: IPv4-mapped ``::ffff:a.b.c.d``, NAT64 ``64:ff9b::/96`` (and the local-use ``64:ff9b:1::/48``),
6to4 ``2002::/16``; Teredo ``2001::/32`` is refused outright (its client address is obscured).

On top of the fixed classes, the host's **own** networks are refused, re-read before every hop
(``LocalNets``): every local interface address and the network it sits in (``/proc/net/if_inet6``
address + prefix length, which also covers ``noprefixroute`` addresses that have no route), every
non-default on-link IPv6 route (``/proc/net/ipv6_route``), every local IPv4 address
(``/proc/net/fib_trie``, ``/32 host LOCAL``) and every non-default on-link IPv4 route
(``/proc/net/route``). A global IPv6 on the LAN (ai-box's own, or a neighbour's in the same /64)
is therefore refused like a private address. IPv6 forms that embed an IPv4 address (mapped,
NAT64, 6to4, IPv4-compatible) are also checked by that address.

The files are read through ``/proc/self/net`` and are injectable for tests. systemd's
``ProcSubset=pid`` hides them (Reviewer E proved it), so the unit must not set it. If a table
cannot be read, ``LocalNets.problem`` raises ``TablesUnreadable``: the fetch is never made, but
it is a *retryable* condition, not a refusal, so it never uses up an approval; ``serve`` and
``run-once`` refuse to start at all while the tables are unreadable.

The "allow loopback" switch exists only so tests can talk to fake servers on 127.0.0.1. It is a
constructor argument, never a config key, and it allows plain IPv4 ``127.0.0.0/8`` only.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Callable

Resolver = Callable[..., list[str]]  # (host, port, timeout, family=AF_UNSPEC) -> addresses

_CGNAT = ipaddress.IPv4Network("100.64.0.0/10")
_ULA = ipaddress.IPv6Network("fc00::/7")
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.IPv6Network("64:ff9b:1::/48")
_TEREDO = ipaddress.IPv6Network("2001::/32")
_SIXTOFOUR = ipaddress.IPv6Network("2002::/16")
_LOOPBACK4 = ipaddress.IPv4Network("127.0.0.0/8")


class AddressRefused(Exception):
    """The destination address is not a public internet address."""


class TablesUnreadable(Exception):
    """The host's own interface tables could not be read, so no address can be judged
    (retryable: a deployment problem, not a property of the destination)."""


def embedded_v4(ip: ipaddress.IPv6Address, *, compat: bool = False) -> ipaddress.IPv4Address | None:
    """The IPv4 address carried by an IPv4-mapped, NAT64 (well-known or local-use) or 6to4
    address, and with ``compat`` by an IPv4-compatible ``::a.b.c.d`` one; else None."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64 or ip in _NAT64_LOCAL:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in _SIXTOFOUR:
        return ip.sixtofour
    if compat and 1 < int(ip) < 2**32:
        return ipaddress.IPv4Address(int(ip))
    return None


def _v4_problem(ip: ipaddress.IPv4Address) -> str | None:
    checks = (
        (ip.is_loopback, "loopback"),
        (ip.is_unspecified, "unspecified"),
        (ip in _CGNAT, "CGNAT/tailnet 100.64.0.0/10"),
        (ip.is_link_local, "link-local"),
        (ip.is_multicast, "multicast"),
        (ip.is_private, "private"),
        (ip.is_reserved, "reserved"),
        (not ip.is_global, "not globally routable"),
    )
    for hit, why in checks:
        if hit:
            return why
    return None


def address_problem(addr: str) -> str | None:
    """Why the gate must not connect to ``addr`` (an IP literal), or None if it is public."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])  # drop an IPv6 zone id
    except ValueError:
        return "not an IP address"
    if isinstance(ip, ipaddress.IPv4Address):
        return _v4_problem(ip)
    embedded: ipaddress.IPv4Address | None = None
    if ip.ipv4_mapped is not None:
        embedded = ip.ipv4_mapped
        label = "IPv4-mapped"
    elif ip in _NAT64 or ip in _NAT64_LOCAL:
        embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        label = "NAT64"
    elif ip in _SIXTOFOUR:
        embedded = ip.sixtofour
        label = "6to4"
    elif ip in _TEREDO:
        return "Teredo"
    if embedded is not None:
        inner = _v4_problem(embedded)
        if inner:
            return f"{label} {inner} ({embedded})"
        if ip in _NAT64_LOCAL:
            return "local-use NAT64"
        # A public IPv4 address in a mapped/NAT64/6to4 wrapper is judged by that address
        # (the wrappers themselves sit in ranges Python calls reserved or private).
        return None
    checks = (
        (ip.is_loopback, "loopback"),
        (ip.is_unspecified, "unspecified"),
        (ip.is_link_local, "link-local"),
        (ip.is_site_local, "site-local"),
        (ip in _ULA, "ULA fc00::/7"),
        (ip.is_multicast, "multicast"),
        (ip.is_reserved, "reserved"),
        (ip.is_private, "private"),
        (not ip.is_global, "not globally routable"),
    )
    for hit, why in checks:
        if hit:
            return why
    return None


PROC_PATHS = {
    "if_inet6": "/proc/self/net/if_inet6",
    "ipv6_route": "/proc/self/net/ipv6_route",
    "fib_trie": "/proc/self/net/fib_trie",
    "route": "/proc/self/net/route",
}
_RTF_GATEWAY = 0x0002
_ZERO128 = "0" * 32

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _v6(hex32: str) -> ipaddress.IPv6Address:
    if len(hex32) != 32:
        raise ValueError(f"bad IPv6 field {hex32!r}")
    return ipaddress.IPv6Address(int(hex32, 16))


def _v4_le(hex8: str) -> ipaddress.IPv4Address:
    """/proc/net/route stores IPv4 in host (little-endian) byte order."""
    return ipaddress.IPv4Address(int.from_bytes(int(hex8, 16).to_bytes(4, "big"), "little"))


class LocalNets:
    """The host's own addresses and on-link networks, read fresh by ``load()``."""

    def __init__(self, paths: dict[str, str] | None = None) -> None:
        self.paths = dict(PROC_PATHS)
        self.paths.update(paths or {})
        self.nets: list[Network] = []
        self.v6_ok = False
        self.v4_ok = False

    def _read(self, key: str) -> str:
        with open(self.paths[key], encoding="ascii", errors="replace") as fh:
            return fh.read()

    def load(self) -> LocalNets:
        nets: list[Network] = []
        try:
            for line in self._read("if_inet6").splitlines():
                f = line.split()
                if len(f) < 6:
                    continue
                addr, plen = _v6(f[0]), int(f[2], 16)
                nets.append(ipaddress.IPv6Network((addr, 128)))
                if plen:
                    nets.append(ipaddress.IPv6Network((addr, plen), strict=False))
            for line in self._read("ipv6_route").splitlines():
                f = line.split()
                if len(f) < 10:
                    continue
                plen, nexthop, flags = int(f[1], 16), f[4], int(f[8], 16)
                if plen and nexthop == _ZERO128 and not flags & _RTF_GATEWAY:
                    nets.append(ipaddress.IPv6Network((_v6(f[0]), plen), strict=False))
            v6_ok = True
        except (OSError, ValueError):
            v6_ok = False
        try:
            last: str | None = None
            for line in self._read("fib_trie").splitlines():
                text = line.strip()
                if text.startswith("|-- "):
                    last = text[4:].strip()
                elif text == "/32 host LOCAL" and last:
                    nets.append(ipaddress.IPv4Network(last))
            for line in self._read("route").splitlines()[1:]:
                f = line.split()
                if len(f) < 8:
                    continue
                dest, gw, flags, mask = _v4_le(f[1]), _v4_le(f[2]), int(f[3], 16), _v4_le(f[7])
                if int(mask) and int(gw) == 0 and not flags & _RTF_GATEWAY:
                    nets.append(ipaddress.IPv4Network(f"{dest}/{mask}", strict=False))
            v4_ok = True
        except (OSError, ValueError):
            v4_ok = False
        self.nets, self.v6_ok, self.v4_ok = nets, v6_ok, v4_ok
        return self

    def unreadable(self, *, need_v6: bool = True) -> str | None:
        """Why the tables needed for judging addresses cannot be used, or None."""
        if not self.v4_ok:
            return "the host's IPv4 interface tables could not be read (/proc/self/net hidden or missing?)"
        if need_v6 and not self.v6_ok:
            return ("the host's IPv6 interface tables could not be read (/proc/self/net hidden, or IPv6 "
                    "disabled: then set ipv4_only)")
        return None

    def problem(self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
        """Why ``ip`` is on this host's own networks, or None. Raises TablesUnreadable."""
        if isinstance(ip, ipaddress.IPv4Address) and not self.v4_ok:
            raise TablesUnreadable("the host's IPv4 interface tables could not be read")
        if isinstance(ip, ipaddress.IPv6Address) and not self.v6_ok:
            raise TablesUnreadable("the host's IPv6 interface tables could not be read")
        for net in self.nets:
            if ip.version == net.version and ip in net:
                return f"on a local network of this host ({net})"
        if isinstance(ip, ipaddress.IPv6Address):
            inner = embedded_v4(ip, compat=True)
            if inner is not None:
                why = self.problem(inner)
                return f"{why} (embedded in {ip})" if why else None
        return None


class AddressPolicy:
    """The refusal rules, plus the test-only loopback allowance."""

    def __init__(self, *, _test_allow_loopback: bool = False,
                 proc_paths: dict[str, str] | None = None) -> None:
        self._allow_loopback = _test_allow_loopback
        self.local = LocalNets(proc_paths)

    def refresh(self) -> None:
        """Re-read the host's own addresses and networks (called before every hop)."""
        self.local.load()

    def problem(self, addr: str) -> str | None:
        try:
            ip = ipaddress.ip_address(addr.split("%", 1)[0])
        except ValueError:
            return "not an IP address"
        if self._allow_loopback and isinstance(ip, ipaddress.IPv4Address) and ip in _LOOPBACK4:
            return None
        return address_problem(addr) or self.local.problem(ip)

    def check_all(self, host: str, addrs: list[str]) -> list[str]:
        """Every address must be acceptable (a mix of public and private is refused)."""
        if not addrs:
            raise AddressRefused(f"{host} did not resolve to any address")
        for addr in addrs:
            why = self.problem(addr)
            if why:
                raise AddressRefused(f"{host} resolves to a refused address {addr} ({why})")
        return addrs


def system_resolver(host: str, port: int, timeout: float, family: int = socket.AF_UNSPEC) -> list[str]:
    """One getaddrinfo, bounded by ``timeout`` (it runs in a helper thread; a lookup still
    running at the deadline is abandoned and counts as a failure)."""
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["infos"] = socket.getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM,
                                              proto=socket.IPPROTO_TCP)
        except OSError as exc:
            box["error"] = exc

    t = threading.Thread(target=run, name="rgate-dns", daemon=True)
    t.start()
    t.join(max(0.0, timeout))
    if t.is_alive():
        raise TimeoutError(f"DNS lookup of {host} timed out")
    if "error" in box:
        raise OSError(f"DNS lookup of {host} failed: {box['error']}")
    out: list[str] = []
    for family, _, _, _, sockaddr in box["infos"]:  # type: ignore[union-attr]
        if family in (socket.AF_INET, socket.AF_INET6):
            addr = str(sockaddr[0])
            if addr not in out:
                out.append(addr)
    return out
