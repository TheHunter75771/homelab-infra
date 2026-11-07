# Renders firewall_allow_rules (docs/CONTRACTS.md §12.2) into UFW rule lines, written exactly the
# way `ufw show added` prints them (without the leading "ufw "), so the role can compare declared
# and present rules as strings. Unit tests: roles/firewall/tests/test_homelab_ufw.py.
import ipaddress
import re

try:
    from ansible.errors import AnsibleFilterError
except ImportError:  # unit tests run without Ansible
    class AnsibleFilterError(Exception):
        pass

ALLOWED_KEYS = {"port", "proto", "iface", "from", "to", "comment"}
# iface may be left out only when both from and to are given (an address-to-address rule, e.g. a container to
# the bridge gateway, where the bridge interface name isn't pinned).
REQUIRED_KEYS = ("port", "proto", "comment")
PROTOS = ("tcp", "udp")
# Linux interface names UFW keeps as given: ASCII letters, digits and _.:-, at most 15 characters (IFNAMSIZ - 1).
IFACE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")
# ASCII digits only (str.isdigit() also accepts e.g. superscripts, which int() then rejects).
PORT = re.compile(r"^[0-9]{1,5}$")
# ufw keeps comments in single quotes; printable text without quotes or backslashes.
COMMENT = re.compile(r"^[A-Za-z0-9 _.,:;()/+=@-]+$", re.ASCII)
COMMENT_PREFIX = "homelab: "


def _fail(index, rule, message):
    raise AnsibleFilterError(
        "firewall_allow_rules[%d] %s: %s" % (index, rule if isinstance(rule, dict) else repr(rule), message)
    )


def _port(index, rule):
    value = rule["port"]
    if isinstance(value, bool):
        _fail(index, rule, "port must be a number")
    text = str(value).strip()
    if not PORT.fullmatch(text) or not 1 <= int(text) <= 65535:
        _fail(index, rule, "port must be a number from 1 to 65535 (ASCII digits)")
    return str(int(text))


def _source(index, rule):
    """The rule's from, exactly as `ufw show added` prints it, or fail.

    UFW rewrites some sources (any = no from, a /32 or /128 host prefix is dropped, an IPv6 address is
    compressed, host bits are rejected or cleared), and a rewritten rule never reads back as declared,
    so the role would fail on every run. Only the canonical spelling is accepted.
    """
    text = str(rule["from"] if rule["from"] is not None else "").strip()
    if text.lower() == "any":
        _fail(index, rule, "from: any is UFW's default; leave from out instead")
    if "%" in text:
        _fail(index, rule, "from: no IPv6 zone (%iface); use iface for the interface")
    try:
        canonical = ipaddress.ip_network(text, strict=True) if "/" in text else ipaddress.ip_address(text)
    except ValueError as error:
        _fail(index, rule, "from must be one IP address or network address (%s)" % error)
    first = canonical.network_address if "/" in text else canonical
    # U-2: an IPv4-mapped IPv6 address (::ffff:a.b.c.d) is an IPv4 peer under an IPv6 name; write the IPv4 form.
    if _ipv4_mapped(first):
        _fail(index, rule, "from: no IPv4-mapped IPv6 address (::ffff:a.b.c.d); write the IPv4 address")
    # Reviewer U (U-1): a /0 is any. UFW then stores the rule without a from, so a from/to rule without iface
    # would be open on every interface (the Wi-Fi uplink too), and the add loop runs before the read-back check.
    # The unspecified address alone is refused for the same reason.
    if ("/" in text and canonical.prefixlen == 0) or ("/" not in text and canonical.is_unspecified):
        _fail(index, rule, "from: %s is any; leave from out" % text)
    if "/" in text and canonical.prefixlen == canonical.max_prefixlen:
        _fail(index, rule, "from: drop the /%d (UFW stores a single host without a prefix)" % canonical.prefixlen)
    if str(canonical) != text:
        _fail(index, rule, "from: write %s (UFW stores it in that form)" % canonical)
    return text


def _ipv4_mapped(address):
    return getattr(address, "ipv4_mapped", None) is not None


def _destination(index, rule):
    """The rule's to: one host address (IPv4 or IPv6), exactly as `ufw show added` prints it, or fail.

    Not a range or network, not any, and never the unspecified address (0.0.0.0 or ::), which would open the
    port on every address the host has; no IPv4-mapped, loopback, broadcast (255.255.255.255), multicast or
    link-local address. Only the canonical spelling is accepted, as for from.
    """
    text = str(rule["to"] if rule["to"] is not None else "").strip()
    if text.lower() == "any":
        _fail(index, rule, "to: any is UFW's default; leave to out instead")
    if "/" in text or "-" in text:
        _fail(index, rule, "to must be one address, not a range or network")
    if "%" in text:
        _fail(index, rule, "to: no IPv6 zone (%iface); use iface for the interface")
    try:
        address = ipaddress.ip_address(text)
    except ValueError as error:
        _fail(index, rule, "to must be one IP address (%s)" % error)
    if address.is_unspecified:
        _fail(index, rule, "to: never %s (that is every address on the host)" % text)
    # Reviewer U (U-2, U-3): one ordinary unicast address of this host, nothing else.
    if _ipv4_mapped(address):
        _fail(index, rule, "to: no IPv4-mapped IPv6 address (::ffff:a.b.c.d); write the IPv4 address")
    if address.is_loopback:
        _fail(index, rule, "to: no loopback address (UFW's before.rules already accept loopback)")
    if address.version == 4 and address == ipaddress.IPv4Address("255.255.255.255"):
        _fail(index, rule, "to: no broadcast address")
    if address.is_multicast:
        _fail(index, rule, "to: no multicast address")
    if address.is_link_local:
        _fail(index, rule, "to: no link-local address")
    if str(address) != text:
        _fail(index, rule, "to: write %s (UFW stores it in that form)" % address)
    return text


def homelab_ufw_allow_rules(rules):
    """[{port, proto, iface?, from?, to?, comment}] -> UFW rule lines.

    iface given:  "allow in on IFACE [from FROM ]to TO|any port P proto T comment 'homelab: ...'"
    iface left out (only with both from and to):
                  "allow from FROM to TO port P proto T comment 'homelab: ...'"
    """
    if not isinstance(rules, list):
        raise AnsibleFilterError("firewall_allow_rules must be a list, got %s" % type(rules).__name__)
    out = []
    seen = set()
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            _fail(index, rule, "each rule must be a mapping")
        unknown = sorted(set(rule) - ALLOWED_KEYS)
        if unknown:
            _fail(index, rule, "unknown keys %s (allowed: %s)" % (unknown, sorted(ALLOWED_KEYS)))
        missing = [key for key in REQUIRED_KEYS if key not in rule]
        if missing:
            _fail(index, rule, "missing keys %s" % missing)
        port = _port(index, rule)
        proto = str(rule["proto"]).strip()
        if proto not in PROTOS:
            _fail(index, rule, "proto must be one of %s" % (PROTOS,))
        if "iface" in rule:
            iface = str(rule["iface"] if rule["iface"] is not None else "").strip()
            if not IFACE.fullmatch(iface):
                _fail(index, rule, "iface must be one interface name ([A-Za-z0-9_.:-], at most 15 characters)")
        elif "from" in rule and "to" in rule:
            iface = ""
        else:
            _fail(index, rule, "missing keys ['iface'] (iface may be left out only when both from and to are given)")
        source = _source(index, rule) if "from" in rule else ""
        destination = _destination(index, rule) if "to" in rule else ""
        # UFW refuses a rule whose from and to are different IP versions.
        if source and destination and ipaddress.ip_network(source).version != ipaddress.ip_address(destination).version:
            _fail(index, rule, "from and to must both be IPv4 or both IPv6")
        comment = str(rule["comment"] if rule["comment"] is not None else "").strip()
        if comment.startswith(COMMENT_PREFIX):
            comment = comment[len(COMMENT_PREFIX):]
        if not COMMENT.fullmatch(comment):
            _fail(index, rule, "comment must be plain text (letters, digits, spaces, _.,:;()/+=@-)")
        # The way ufw's get_command prints a rule: "in on IFACE" only with an interface (then no bare "in"),
        # "from" only when not any, "to" always (any when not given).
        match = "allow %s%sto %s port %s proto %s" % (
            ("in on %s " % iface) if iface else "",
            ("from %s " % source) if source else "",
            destination or "any",
            port,
            proto,
        )
        # UFW treats rules that differ only in comment as one rule (it updates the comment).
        if match in seen:
            _fail(index, rule, "same rule as an earlier one (only the comment may differ)")
        seen.add(match)
        out.append("%s comment '%s%s'" % (match, COMMENT_PREFIX, comment))
    return out


class FilterModule:
    def filters(self):
        return {"homelab_ufw_allow_rules": homelab_ufw_allow_rules}
