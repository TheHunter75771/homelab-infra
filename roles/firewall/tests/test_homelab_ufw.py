"""Unit tests for the firewall role's rule rendering (no host needed).

    python3 -m unittest discover -s roles/firewall/tests -v

Covers the homelab_ufw_allow_rules filter (docs/CONTRACTS.md §12.2) and the DOCKER-USER guard script
rendered from the role's real template with and without wifi_iface. Addresses are documentation
ranges (RFC 5737), not real identifiers.
"""

import hashlib
import os
import shlex
import subprocess
import sys
import tempfile
import unittest

import jinja2

HERE = os.path.dirname(os.path.abspath(__file__))
ROLE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROLE, "filter_plugins"))

from homelab_ufw import homelab_ufw_allow_rules as render  # noqa: E402

LAN = "lan0"
WIFI = "wlan0"
LAN_CIDR = "192.0.2.0/24"
WORKSTATION = "192.0.2.10"
MEDIASERVER = "192.0.2.20"
# Inside the tailnet ranges (100.64.0.0/10, fd7a:115c:a1e0::/48); made up, not a real node.
TAILNET_IP = "100.64.0.5"
TAILNET_IP6 = "fd7a:115c:a1e0::5"

# The media server's rules before §12.2 (roles/firewall/vars/main.yml, firewall_rules[0:4]).
MEDIA_BEFORE = [
    "allow in on tailscale0 to any port 22 proto tcp comment 'homelab: ssh over tailnet'",
    "allow in on %s from %s to any port 22 proto tcp comment 'homelab: ssh from workstation'" % (LAN, WORKSTATION),
    "allow in on %s from %s to any port 8096 proto tcp comment 'homelab: jellyfin lan'" % (LAN, LAN_CIDR),
    "allow in on tailscale0 to any port 9999 proto tcp comment 'homelab: wol-relay over tailnet'",
]
# inventory/group_vars/media_servers firewall_allow_rules, with the identifiers filled in.
MEDIA_LIST = [
    {"port": 22, "proto": "tcp", "iface": "tailscale0", "comment": "ssh over tailnet"},
    {"port": 22, "proto": "tcp", "iface": LAN, "from": WORKSTATION, "comment": "ssh from workstation"},
    {"port": 8096, "proto": "tcp", "iface": LAN, "from": LAN_CIDR, "comment": "jellyfin lan"},
    {"port": 9999, "proto": "tcp", "iface": "tailscale0", "comment": "wol-relay over tailnet"},
]
# inventory/group_vars/ai_servers firewall_allow_rules, with the identifiers filled in.
AI_LIST = [
    {"port": 22, "proto": "tcp", "iface": "tailscale0", "comment": "ssh tailnet"},
    {"port": 22, "proto": "tcp", "iface": LAN, "from": WORKSTATION, "comment": "ssh gaming-pc lan"},
    {"port": 22, "proto": "tcp", "iface": LAN, "from": MEDIASERVER, "comment": "ssh mediaserver recovery"},
]


class Filter(unittest.TestCase):
    def test_media_list_reproduces_the_rules_ufw_already_has(self):
        self.assertEqual(render(MEDIA_LIST), MEDIA_BEFORE)

    def test_ai_list(self):
        self.assertEqual(render(AI_LIST), [
            "allow in on tailscale0 to any port 22 proto tcp comment 'homelab: ssh tailnet'",
            "allow in on lan0 from 192.0.2.10 to any port 22 proto tcp comment 'homelab: ssh gaming-pc lan'",
            "allow in on lan0 from 192.0.2.20 to any port 22 proto tcp comment 'homelab: ssh mediaserver recovery'",
        ])

    def test_string_port_and_existing_prefix_are_normalized(self):
        self.assertEqual(
            render([{"port": "0022", "proto": "udp", "iface": "tailscale0", "comment": "homelab: x"}]),
            ["allow in on tailscale0 to any port 22 proto udp comment 'homelab: x'"],
        )

    def test_empty_list(self):
        self.assertEqual(render([]), [])

    def assertRejected(self, rules, fragment):
        with self.assertRaises(Exception) as caught:
            render(rules)
        self.assertIn(fragment, str(caught.exception))

    def test_rejects_bad_items(self):
        base = {"port": 22, "proto": "tcp", "iface": "tailscale0", "comment": "ok"}
        self.assertRejected("nope", "must be a list")
        self.assertRejected(["x"], "mapping")
        self.assertRejected([dict(base, src="192.0.2.1")], "unknown keys")
        self.assertRejected([{"port": 22, "proto": "tcp", "iface": "tailscale0"}], "missing keys")
        for port in (0, 65536, "22/tcp", "8000:8010", True, None, "\u00b2", "\u0662\u0662", "\uff12\uff12"):
            self.assertRejected([dict(base, port=port)], "port")
        self.assertRejected([dict(base, proto="any")], "proto")
        for iface in ("", "lan0 to any", "wlan0'", "lan0/1", "x" * 16, "\u00ebth0", "lan0\nx"):
            self.assertRejected([dict(base, iface=iface)], "iface")
        self.assertRejected([dict(base, **{"from": ""})], "from")
        self.assertRejected([dict(base, **{"from": "192.0.2.1 port 1"})], "from")
        self.assertRejected([dict(base, comment="it's")], "comment")
        self.assertRejected([dict(base, comment="caf\u00e9")], "comment")
        self.assertRejected([dict(base, comment="")], "comment")
        self.assertRejected([base, dict(base, comment="other")], "same rule")

    def test_rejects_sources_ufw_would_rewrite(self):
        base = {"port": 22, "proto": "tcp", "iface": "lan0", "comment": "ok"}
        cases = {
            "any": "leave from out",
            "ANY": "leave from out",
            "192.0.2.1/32": "drop the /32",
            "2001:db8::1/128": "drop the /128",
            "192.0.2.5/24": "network address",       # host bits set
            "192.0.2.0/024": "write 192.0.2.0/24",
            "2001:DB8::/32": "write 2001:db8::/32",
            "2001:db8:0:0::1": "write 2001:db8::1",
            "fe80::1%lan0": "zone",
            "host.example": "network address",
            "192.0.2.01": "network address",
        }
        for source, fragment in cases.items():
            with self.subTest(source=source):
                self.assertRejected([dict(base, **{"from": source})], fragment)

    def test_accepts_canonical_sources_and_interface_names(self):
        rules = [
            {"port": 22, "proto": "tcp", "iface": "br-a1.10:x", "from": "10.0.0.0/8", "comment": "a"},
            {"port": 22, "proto": "tcp", "iface": "lan0", "from": "2001:db8::/32", "comment": "b"},
            {"port": 22, "proto": "tcp", "iface": "lan0", "from": "2001:db8::1", "comment": "c"},
            {"port": 22, "proto": "tcp", "iface": "x" * 15, "from": "192.0.2.1", "comment": "d"},
        ]
        self.assertEqual(
            [line.split(" comment ")[0] for line in render(rules)],
            [
                "allow in on br-a1.10:x from 10.0.0.0/8 to any port 22 proto tcp",
                "allow in on lan0 from 2001:db8::/32 to any port 22 proto tcp",
                "allow in on lan0 from 2001:db8::1 to any port 22 proto tcp",
                "allow in on xxxxxxxxxxxxxxx from 192.0.2.1 to any port 22 proto tcp",
            ],
        )

    def test_errors_are_filter_errors(self):
        from homelab_ufw import AnsibleFilterError
        for bad in ({"from": "any"}, {"from": "192.0.2.1/32"}, {"port": "\u00b2"}, {"iface": "x" * 16}):
            rule = dict({"port": 22, "proto": "tcp", "iface": "lan0", "comment": "ok"}, **bad)
            with self.subTest(bad=bad), self.assertRaises(AnsibleFilterError):
                render([rule])


class OutboundPolicy(unittest.TestCase):
    def test_only_dns_is_denied_out_so_ntp_works(self):
        # Default policy is allow out; the only outbound rules are the plaintext-DNS denies. NTP (udp 123)
        # must stay open: role base's IP-literal NTP sources are what recovers a badly wrong clock.
        import yaml
        with open(os.path.join(ROLE, "vars", "main.yml"), encoding="utf-8") as handle:
            role_vars = yaml.safe_load(handle)
        outbound = [rule for rule in role_vars["firewall_dns_rules"] if " out " in " %s " % rule]
        self.assertEqual(sorted(r.split(" comment ")[0] for r in outbound), ["deny out 53/tcp", "deny out 53/udp"])
        self.assertFalse([rule for rule in role_vars["firewall_dns_rules"] if "123" in rule])
        with open(os.path.join(ROLE, "tasks", "ufw.yml"), encoding="utf-8") as handle:
            self.assertIn("{ direction: outgoing, policy: allow }", handle.read())


def _env():
    env = jinja2.Environment(trim_blocks=True, undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    env.filters["quote"] = shlex.quote
    env.filters["hash"] = lambda value, algo: hashlib.new(algo, value.encode()).hexdigest()
    return env


def render_script(**extra):
    with open(os.path.join(ROLE, "templates", "homelab-docker-user.sh.j2"), encoding="utf-8") as handle:
        source = handle.read()
    values = {
        "ansible_managed": "Ansible managed",
        "firewall_tailnet_iface": "tailscale0",
        "firewall_tailnet_v4": "100.64.0.0/10",
        "firewall_tailnet_v6": "fd7a:115c:a1e0::/48",
        "docker_dns_ip": "172.17.0.1",
    }
    values.update(extra)
    return _env().from_string(source).render(**values)


# Stand-ins for iptables/ip6tables, defined as shell functions ahead of the rendered script (a function
# wins over PATH, so the script's own PATH= line doesn't matter). Each keeps DOCKER-USER as the lines
# `iptables -S DOCKER-USER` would print ("-A DOCKER-USER <spec>") in the file named by $STATE4/$STATE6;
# no state file = no chain. -I always inserts at the top, as the script asks.
FAKE_IPTABLES = r"""
fake_ipt() {
	f=$1
	shift
	[ "$1" = -w ] && shift
	op=$1
	chain=$2
	shift 2
	case $op in
		-S) [ -e "$f" ] || return 1; echo "-N $chain"; cat "$f" ;;
		-C) grep -qxF -- "-A $chain $*" "$f" ;;
		-I) shift; { echo "-A $chain $*"; cat "$f"; } > "$f.new"; mv "$f.new" "$f" ;;
		-D) grep -vxF -- "-A $chain $*" "$f" > "$f.new" || true; mv "$f.new" "$f" ;;
		*) echo "fake iptables: unexpected $op" >&2; return 2 ;;
	esac
}
iptables() { fake_ipt "$STATE4" "$@"; }
ip6tables() { fake_ipt "$STATE6" "$@"; }
"""

DOCKER_DEFAULT = "-A DOCKER-USER -j RETURN"
FOREIGN = "-A DOCKER-USER -s 198.51.100.0/24 -j DROP"


class GuardScript(unittest.TestCase):
    def run_guard(self, script, v4, v6):
        """Runs the rendered script against the fake chains; returns (stdout, v4 lines, v6 lines)."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = {}
            for name, lines in (("v4", v4), ("v6", v6)):
                paths[name] = os.path.join(tmp, name)
                with open(paths[name], "w", encoding="utf-8") as handle:
                    handle.write("".join(line + "\n" for line in lines))
            guard = os.path.join(tmp, "guard.sh")
            with open(guard, "w", encoding="utf-8") as handle:
                handle.write(FAKE_IPTABLES + script)
            done = subprocess.run(
                ["sh", guard], capture_output=True, text=True, check=True,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "STATE4": paths["v4"], "STATE6": paths["v6"]},
            )
            result = []
            for name in ("v4", "v6"):
                with open(paths[name], encoding="utf-8") as handle:
                    result.append(handle.read().splitlines())
        return done.stdout, result[0], result[1]

    @staticmethod
    def tags(lines):
        return sorted(line.split("--comment homelab-du-")[1].split()[0] for line in lines if "homelab-du-" in line)

    def test_with_wifi_inserts_every_rule_once_and_a_rerun_changes_nothing(self):
        script = render_script(wifi_iface=WIFI)
        out, v4, v6 = self.run_guard(script, [DOCKER_DEFAULT], [DOCKER_DEFAULT])
        self.assertEqual([t.split("-")[0] for t in self.tags(v4)], ["dns", "dns", "tailnet4", "wifi"])
        self.assertEqual([t.split("-")[0] for t in self.tags(v6)], ["dns", "dns", "tailnet6", "wifi"])
        self.assertIn("-i wlan0 -m conntrack --ctstate NEW,INVALID -m comment", "\n".join(v4))
        self.assertEqual(v4[-1], DOCKER_DEFAULT)
        self.assertEqual(out.count("inserted"), 8)
        out2, v4_again, v6_again = self.run_guard(script, v4, v6)
        self.assertEqual(out2, "")
        self.assertEqual((v4_again, v6_again), (v4, v6))

    def test_without_wifi_prunes_the_stale_wifi_rule_and_keeps_the_rest(self):
        # The state a media-server-style host is left in: every rule of the Wi-Fi variant, plus rules
        # that aren't ours. Rendering without wifi_iface must delete only the tagged Wi-Fi drop.
        _, v4, v6 = self.run_guard(render_script(wifi_iface=WIFI), [FOREIGN, DOCKER_DEFAULT], [DOCKER_DEFAULT])
        stale_v4 = [line for line in v4 if "homelab-du-wifi-" in line]
        stale_v6 = [line for line in v6 if "homelab-du-wifi-" in line]
        self.assertEqual((len(stale_v4), len(stale_v6)), (1, 1))

        script = render_script()
        self.assertNotIn("wifi-", script)
        self.assertNotIn("Wi-Fi", script)
        out, v4_after, v6_after = self.run_guard(script, v4, v6)
        self.assertEqual(v4_after, [line for line in v4 if line not in stale_v4])
        self.assertEqual(v6_after, [line for line in v6 if line not in stale_v6])
        self.assertIn(FOREIGN, v4_after)
        self.assertEqual(out.count("removed stale homelab-du-wifi-"), 2)
        self.assertNotIn("inserted", out)

    def test_without_wifi_on_a_fresh_chain(self):
        out, v4, v6 = self.run_guard(render_script(), [DOCKER_DEFAULT], [DOCKER_DEFAULT])
        self.assertEqual([t.split("-")[0] for t in self.tags(v4)], ["dns", "dns", "tailnet4"])
        self.assertEqual([t.split("-")[0] for t in self.tags(v6)], ["dns", "dns", "tailnet6"])
        self.assertEqual(out.count("inserted"), 6)

    def test_tailnet_destination_drop_is_inserted_once(self):
        script = render_script(wifi_iface=WIFI, tailnet_ip=TAILNET_IP)
        out, v4, v6 = self.run_guard(script, [DOCKER_DEFAULT], [DOCKER_DEFAULT])
        tsdst = [line for line in v4 if "homelab-du-tsdst4-" in line]
        self.assertEqual(len(tsdst), 1)
        self.assertTrue(tsdst[0].startswith(
            "-A DOCKER-USER ! -i tailscale0 -m conntrack --ctstate NEW --ctorigdst %s -m comment" % TAILNET_IP))
        self.assertTrue(tsdst[0].endswith(" -j DROP"))
        # No IPv6 tailnet address given: nothing tailnet-destination in ip6tables.
        self.assertFalse([line for line in v6 if "tsdst" in line])
        self.assertEqual(out.count("inserted"), 9)
        out2, v4_again, v6_again = self.run_guard(script, v4, v6)
        self.assertEqual(out2, "")
        self.assertEqual((v4_again, v6_again), (v4, v6))

    def test_tailnet_destination_drop_ipv6(self):
        script = render_script(tailnet_ip=TAILNET_IP, tailnet_ip6=TAILNET_IP6)
        _, v4, v6 = self.run_guard(script, [DOCKER_DEFAULT], [DOCKER_DEFAULT])
        self.assertEqual(len([line for line in v4 if "homelab-du-tsdst4-" in line]), 1)
        tsdst6 = [line for line in v6 if "homelab-du-tsdst6-" in line]
        self.assertEqual(len(tsdst6), 1)
        self.assertIn("--ctorigdst %s -m comment" % TAILNET_IP6, tsdst6[0])
        self.assertFalse([line for line in v4 if TAILNET_IP6 in line])

    def test_tailnet_destination_drop_is_pruned_when_undeclared_or_changed(self):
        _, v4, v6 = self.run_guard(render_script(tailnet_ip=TAILNET_IP, tailnet_ip6=TAILNET_IP6),
                                   [FOREIGN, DOCKER_DEFAULT], [DOCKER_DEFAULT])
        # A changed address: the old rule's tag no longer matches, so it's replaced.
        out, v4_new, v6_new = self.run_guard(render_script(tailnet_ip="100.64.0.9", tailnet_ip6=TAILNET_IP6), v4, v6)
        self.assertEqual(out.count("removed stale homelab-du-tsdst4-"), 1)
        self.assertEqual([line for line in v4_new if "tsdst4" in line and TAILNET_IP + " " in line], [])
        self.assertEqual(len([line for line in v4_new if "--ctorigdst 100.64.0.9 " in line]), 1)
        self.assertEqual(v6_new, v6)
        # No tailnet address at all: both rules go, nothing else does.
        out, v4_none, v6_none = self.run_guard(render_script(), v4_new, v6_new)
        self.assertEqual(out.count("removed stale homelab-du-tsdst"), 2)
        self.assertNotIn("inserted", out)
        self.assertEqual(v4_none, [line for line in v4_new if "tsdst" not in line])
        self.assertEqual(v6_none, [line for line in v6_new if "tsdst" not in line])
        self.assertIn(FOREIGN, v4_none)

    def test_missing_chain_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = os.path.join(tmp, "guard.sh")
            with open(guard, "w", encoding="utf-8") as handle:
                handle.write(FAKE_IPTABLES + render_script())
            done = subprocess.run(["sh", guard], capture_output=True, text=True,
                                  env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                       "STATE4": os.path.join(tmp, "none4"), "STATE6": os.path.join(tmp, "none6")})
        self.assertEqual(done.returncode, 1)
        self.assertIn("DOCKER-USER chain is missing", done.stderr)

    def test_all_variants_are_valid_sh(self):
        for script in (render_script(wifi_iface=WIFI), render_script(),
                       render_script(wifi_iface=WIFI, tailnet_ip=TAILNET_IP, tailnet_ip6=TAILNET_IP6)):
            subprocess.run(["sh", "-n"], input=script.encode(), check=True)


if __name__ == "__main__":
    unittest.main()
