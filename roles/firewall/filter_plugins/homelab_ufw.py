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

ALLOWED_KEYS = {"port", "proto", "iface", "from", "comment"}
REQUIRED_KEYS = ("port", "proto", "iface", "comment")
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
    if "/" in text and canonical.prefixlen == canonical.max_prefixlen:
        _fail(index, rule, "from: drop the /%d (UFW stores a single host without a prefix)" % canonical.prefixlen)
    if str(canonical) != text:
        _fail(index, rule, "from: write %s (UFW stores it in that form)" % canonical)
    return text


def homelab_ufw_allow_rules(rules):
    """[{port, proto, iface, from?, comment}] -> ["allow in on IFACE [from FROM ]to any port P proto T comment '...'"]."""
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
        iface = str(rule["iface"] if rule["iface"] is not None else "").strip()
        if not IFACE.fullmatch(iface):
            _fail(index, rule, "iface must be one interface name ([A-Za-z0-9_.:-], at most 15 characters)")
        source = _source(index, rule) if "from" in rule else ""
        comment = str(rule["comment"] if rule["comment"] is not None else "").strip()
        if comment.startswith(COMMENT_PREFIX):
            comment = comment[len(COMMENT_PREFIX):]
        if not COMMENT.fullmatch(comment):
            _fail(index, rule, "comment must be plain text (letters, digits, spaces, _.,:;()/+=@-)")
        match = "allow in on %s %sto any port %s proto %s" % (
            iface, ("from %s " % source) if source else "", port, proto
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
