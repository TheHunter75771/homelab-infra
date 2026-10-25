#!/usr/bin/env python3
"""Tests for templates/presets/jellyfin-network.xml.j2: the real template, rendered with the role's
defaults the way the template module does (trim_blocks), must give Jellyfin a network.xml it reads as
intended:
  - base URL /watch and the hl-public subnet as the only known proxy;
  - LAN networks that cover the tailnet (tailnet clients arrive through `tailscale serve`, so Jellyfin
    sees their 100.x address) but never a globally routable range, which would make internet clients
    "LAN" and bypass the admin's remote-access block;
  - the known proxy's subnet excluded from the LAN as the last entry (`!<subnet>`), so a tunnel request
    that reaches Jellyfin with cloudflared's own address (no usable X-Forwarded-For) counts as remote.
Element order follows Jellyfin's own serializer. A small model of Jellyfin's LAN check (loopback, or
inside an include entry and inside no `!` entry; exclusions win) checks the result address by address.

  python3 -m unittest discover -s roles/stacks/tests -v    # needs PyYAML and Jinja2
"""
import ipaddress
import pathlib
import unittest
import xml.etree.ElementTree as ET

import jinja2
import yaml

ROLE = pathlib.Path(__file__).resolve().parents[1]
DEFAULTS = ROLE / "defaults/main.yml"
TEMPLATE = ROLE / "templates/presets/jellyfin-network.xml.j2"

RFC1918 = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
TAILNET_V4 = "100.64.0.0/10"            # Tailscale's addresses (RFC 6598 shared address space)
TAILNET_V6 = "fd7a:115c:a1e0::/48"      # Tailscale's IPv6 range, inside the ULA block
# Everything a LAN entry may sit inside: private (RFC1918, IPv6 ULA), CGNAT-shared (the tailnet) and
# link-local. Nothing here is globally routable, so an entry inside one of them can't be.
NON_GLOBAL = [ipaddress.ip_network(n) for n in [
    *RFC1918, TAILNET_V4, "169.254.0.0/16", "fc00::/7", "fe80::/10",
]]
EXPECTED_ELEMENTS = ["BaseUrl", "LocalNetworkSubnets", "KnownProxies"]

# Who must land where, by the address Jellyfin ends up with for the request.
LAN = {
    "100.101.102.103": "a tailnet client, through serve (X-Forwarded-For honored)",
    "fd7a:115c:a1e0::1": "a tailnet client over IPv6",
    "192.168.1.20": "a TV on the home LAN (published port, source kept)",
    "172.18.0.5": "another container (Jellyseerr, Sonarr) on the media network",
    "127.0.0.1": "Jellyfin's own loopback (its healthcheck)",
}
REMOTE = {
    "172.30.10.2": "cloudflared itself: a tunnel request without a usable X-Forwarded-For",
    "172.30.10.1": "the hl-public gateway without X-Forwarded-For",
    "203.0.113.5": "an internet visitor through the tunnel",
    "2001:db8::1": "an internet visitor over IPv6",
}


def render(variables: dict) -> str:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
    return env.from_string(TEMPLATE.read_text()).render(variables)


def strings(element) -> list:
    return [child.text for child in element.findall("string")]


def is_lan(address: str, entries: list) -> bool:
    """Jellyfin's LAN check as modelled here: loopback, or inside an include entry and inside no
    exclusion (`!`-prefixed) entry. Exclusions win over includes."""
    ip = ipaddress.ip_address(address)
    if ip.is_loopback:
        return True
    includes = [ipaddress.ip_network(e) for e in entries if not e.startswith("!")]
    excludes = [ipaddress.ip_network(e[1:]) for e in entries if e.startswith("!")]

    def inside(nets):
        return any(ip.version == n.version and ip in n for n in nets)

    return inside(includes) and not inside(excludes)


class JellyfinNetworkPreset(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        defaults = yaml.safe_load(DEFAULTS.read_text())
        cls.public_subnet = defaults["stacks_public_subnet"]
        cls.lan_subnets = defaults["stacks_jellyfin_lan_subnets"]
        # Only what the template needs: StrictUndefined fails the render if it ever needs more.
        cls.rendered = render({
            "stacks_public_subnet": cls.public_subnet,
            "stacks_jellyfin_lan_subnets": cls.lan_subnets,
        })
        cls.root = ET.fromstring(cls.rendered.encode("utf-8"))

    def child(self, tag):
        found = self.root.findall(tag)
        self.assertEqual(len(found), 1, f"exactly one <{tag}>")
        return found[0]

    def lan_entries(self) -> list:
        return strings(self.child("LocalNetworkSubnets"))

    def includes(self) -> list:
        return [e for e in self.lan_entries() if not e.startswith("!")]

    # --- the document

    def test_root_element(self):
        self.assertEqual(self.root.tag, "NetworkConfiguration")

    def test_exactly_the_preset_elements_in_jellyfin_order(self):
        # Nothing else is preset (e.g. no EnableRemoteAccess): Jellyfin fills in its own defaults.
        self.assertEqual([child.tag for child in self.root], EXPECTED_ELEMENTS)

    def test_base_url(self):
        self.assertEqual(self.child("BaseUrl").text, "/watch")

    def test_no_blank_lines(self):
        # trim_blocks (the template module's default) keeps the loop from leaving empty lines.
        self.assertNotIn("\n\n", self.rendered)

    # --- known proxy

    def test_known_proxies_is_the_public_subnet_only(self):
        self.assertEqual(strings(self.child("KnownProxies")), [self.public_subnet])

    def test_known_proxy_is_a_small_private_ipv4_subnet_off_the_tailnet(self):
        net = ipaddress.ip_network(self.public_subnet)  # strict: host bits set is an error too
        self.assertEqual(net.version, 4)
        self.assertGreaterEqual(net.prefixlen, 16, "a known proxy wider than /16 trusts too many senders")
        self.assertFalse(net.is_global, f"{net} is globally routable")
        self.assertTrue(any(net.subnet_of(n) for n in NON_GLOBAL if n.version == 4))
        self.assertFalse(net.overlaps(ipaddress.ip_network(TAILNET_V4)),
                         "a known proxy inside the tailnet would trust X-Forwarded-For from tailnet devices")

    # --- LAN networks

    def test_lan_list_comes_from_defaults_plus_the_exclusion(self):
        self.assertIsInstance(self.lan_subnets, list)
        self.assertFalse([e for e in self.lan_subnets if str(e).startswith("!")],
                         "the exclusion is derived from stacks_public_subnet in the template, not listed")
        self.assertEqual(self.lan_entries(), [*self.lan_subnets, "!" + self.public_subnet])

    def test_exclusion_is_last_and_is_the_known_proxy(self):
        entries = self.lan_entries()
        exclusions = [e for e in entries if e.startswith("!")]
        self.assertEqual(exclusions, ["!" + self.public_subnet])
        self.assertEqual(entries[-1], "!" + self.public_subnet)

    def test_exclusion_and_proxy_follow_the_public_subnet(self):
        # Rendered with another subnet: both entries are derived from the variable, never hardcoded.
        other = "10.99.0.0/24"
        root = ET.fromstring(render({
            "stacks_public_subnet": other,
            "stacks_jellyfin_lan_subnets": self.lan_subnets,
        }).encode("utf-8"))
        self.assertEqual(strings(root.find("KnownProxies")), [other])
        self.assertEqual(strings(root.find("LocalNetworkSubnets"))[-1], "!" + other)

    def test_lan_includes_rfc1918_and_the_tailnet(self):
        for net in [*RFC1918, TAILNET_V4]:
            self.assertIn(net, self.includes())

    def test_lan_covers_tailscale_ipv6(self):
        tailnet = ipaddress.ip_network(TAILNET_V6)
        lan = [ipaddress.ip_network(n) for n in self.includes()]
        self.assertTrue(any(n.version == 6 and tailnet.subnet_of(n) for n in lan), lan)

    def test_lan_entries_are_never_globally_routable(self):
        includes = self.includes()
        self.assertTrue(includes)
        for text in includes:
            with self.subTest(entry=text):
                net = ipaddress.ip_network(text)  # strict: host bits set is an error too
                self.assertTrue(
                    any(net.version == c.version and net.subnet_of(c) for c in NON_GLOBAL),
                    f"{text} is not inside a private, CGNAT-shared or link-local range")
                self.assertFalse(net.is_global, f"{text} is globally routable")

    # --- the result, address by address

    def test_classification(self):
        entries = self.lan_entries()
        for address, who in LAN.items():
            with self.subTest(address=address, expect="LAN"):
                self.assertTrue(is_lan(address, entries), f"{address} ({who}) must be LAN")
        for address, who in REMOTE.items():
            with self.subTest(address=address, expect="remote"):
                self.assertFalse(is_lan(address, entries), f"{address} ({who}) must be remote")

    def test_model_exclusion_wins(self):
        # The model itself: an exclusion beats a wider include, and order doesn't matter.
        self.assertFalse(is_lan("10.1.2.3", ["10.0.0.0/8", "!10.1.0.0/16"]))
        self.assertFalse(is_lan("10.1.2.3", ["!10.1.0.0/16", "10.0.0.0/8"]))
        self.assertTrue(is_lan("10.2.0.1", ["10.0.0.0/8", "!10.1.0.0/16"]))
        self.assertTrue(is_lan("::1", []))


if __name__ == "__main__":
    unittest.main()
