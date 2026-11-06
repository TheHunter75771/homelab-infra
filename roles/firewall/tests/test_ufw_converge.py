"""Two runs of the firewall role's UFW convergence against a model of UFW (no host, no root).

    python3 -m unittest discover -s roles/firewall/tests -v

The role decides "declared" vs "present" by comparing strings: the lines it renders (vars/main.yml,
filter_plugins/homelab_ufw.py) against what `ufw show added` prints. A rule that doesn't read back exactly
would fail the run, and one that isn't declared is deleted. These tests run the role's OWN expressions,
read from vars/main.yml, tasks/main.yml and tasks/ufw.yml (the add loop's changed_when, the set_fact,
the assertions, the delete loop), through Jinja's native environment, against FakeUfw below.

FakeUfw parses a rule command independently of the role's renderer and prints rules with a port of
ufw's own UFWCommandRule.get_command (src/parser.py), the function behind `ufw show added`; it adds,
skips, updates (comment only) and deletes rules the way ufw does. Addresses are documentation ranges
(RFC 5737 / RFC 3849) or Docker-internal, not real identifiers.
"""

import importlib.util
import ipaddress
import os
import re
import shlex
import sys
import unittest

import jinja2
import yaml
from jinja2.nativetypes import NativeEnvironment

HERE = os.path.dirname(os.path.abspath(__file__))
ROLE = os.path.dirname(HERE)
ROLES = os.path.dirname(ROLE)
sys.path.insert(0, os.path.join(ROLE, "filter_plugins"))

from homelab_ufw import homelab_ufw_allow_rules  # noqa: E402

LAN = "lan0"
WIFI = "wlan0"
DOCKER_DNS_IP = "172.17.0.1"
WEBUI_IP = "172.20.0.4"
AI_GATEWAY = "172.20.0.1"
FORGE_RULE = {"port": 8790, "proto": "tcp", "from": WEBUI_IP, "to": AI_GATEWAY, "comment": "forge-api from open-webui"}
# roles/forge_api/vars/main.yml forge_api_ufw_rule, filled in (roles/forge_api/tests/test_forge_api.yml asserts the same).
FORGE_LINE = "allow from 172.20.0.4 to 172.20.0.1 port 8790 proto tcp comment 'homelab: forge-api from open-webui'"

# Old-style rules (exactly as before `to`) mixed with every new form.
MIXED = [
    {"port": 22, "proto": "tcp", "iface": "tailscale0", "comment": "ssh over tailnet"},
    {"port": 22, "proto": "tcp", "iface": LAN, "from": "192.0.2.10", "comment": "ssh from workstation"},
    {"port": 8096, "proto": "tcp", "iface": LAN, "from": "192.0.2.0/24", "comment": "jellyfin lan"},
    FORGE_RULE,
    {"port": 8443, "proto": "tcp", "iface": LAN, "from": "192.0.2.0/24", "to": "192.0.2.5", "comment": "iface from to"},
    {"port": 9000, "proto": "udp", "iface": "tailscale0", "to": "100.64.0.5", "comment": "iface to only"},
    {"port": 9001, "proto": "tcp", "from": "2001:db8::/32", "to": "2001:db8::1", "comment": "v6 from to"},
]


# ---------------------------------------------------------------------------------------------- UFW model
ANY = {4: "0.0.0.0/0", 6: "::/0"}


def _ufw_addr(text):
    """How ufw stores an address: canonical, a host without its /32 or /128."""
    net = ipaddress.ip_network(text, strict=False)
    return str(net.network_address) if net.prefixlen == net.max_prefixlen else str(net)


class Rule:
    def __init__(self, action, direction, iface, src, dst, dport, proto, comment):
        self.action, self.direction, self.interface_in = action, direction, iface
        self.src, self.dst, self.dport, self.protocol, self.comment = src, dst, dport, proto, comment
        self.interface_out, self.sport, self.sapp, self.dapp, self.logtype = "", "any", "", "", ""

    def key(self):
        # ufw treats rules that differ only in action or comment as the same rule (it updates them in place).
        return (self.direction, self.interface_in, self.src, self.dst, self.dport, self.protocol)

    def get_command(self):
        """ufw src/parser.py UFWCommandRule.get_command, the printer behind `ufw show added`."""
        r = self
        res = r.action
        if (r.dst in ("0.0.0.0/0", "::/0") and r.src in ("0.0.0.0/0", "::/0") and r.sport == "any"
                and r.sapp == "" and r.interface_in == "" and r.interface_out == "" and r.dport != "any"):
            if r.direction == "out":
                res += " %s" % r.direction
            res += " %s" % r.dport
            if r.protocol != "any":
                res += "/%s" % r.protocol
            if r.comment != "":
                res += " comment '%s'" % r.comment
            return res
        if r.interface_in != "":
            res += " in on %s" % r.interface_in
        if r.interface_out != "":
            res += " out on %s" % r.interface_out
        elif r.direction == "out":
            res += " %s" % r.direction
        for loc, port, word in ((r.src, r.sport, "from"), (r.dst, r.dport, "to")):
            if loc in ("0.0.0.0/0", "::/0"):
                loc = "any"
            if loc != "any" or port != "any":
                res += " %s %s" % (word, loc)
                if port != "any":
                    res += " port %s" % port
        if " to " not in res and " from " not in res and r.interface_in == "" and r.interface_out == "":
            res += " to any"
        if r.protocol != "any":
            res += " proto %s" % r.protocol
        if r.comment != "":
            res += " comment '%s'" % r.comment
        return res


def parse_rule(words):
    """A ufw rule command (argv after `ufw` / `ufw delete`) -> Rule. Raises ValueError like ufw's parser does."""
    words = list(words)
    comment = ""
    if "comment" in words:
        i = words.index("comment")
        comment, words = words[i + 1], words[:i]
    action = words.pop(0)
    if action not in ("allow", "deny", "reject", "limit"):
        raise ValueError("bad action %r" % action)
    direction, iface = "in", ""
    if words[:1] in (["in"], ["out"]):
        direction = words.pop(0)
    if words[:1] == ["on"]:
        words.pop(0)
        iface = words.pop(0)
    if len(words) == 1 and "/" in words[0]:          # short syntax: 53/udp
        port, proto = words[0].split("/")
        return Rule(action, direction, iface, "0.0.0.0/0", "0.0.0.0/0", port, proto, comment)
    src = dst = None
    dport, proto = "any", "any"
    while words:
        word = words.pop(0)
        if word == "from":
            src = words.pop(0)
        elif word == "to":
            dst = words.pop(0)
        elif word == "port":
            dport = words.pop(0)
        elif word == "proto":
            proto = words.pop(0)
        else:
            raise ValueError("unexpected %r" % word)
    versions = {ipaddress.ip_network(a, strict=False).version for a in (src, dst) if a not in (None, "any")}
    if len(versions) > 1:
        raise ValueError("Mixed IP versions for 'from' and 'to'")
    version = versions.pop() if versions else 4
    src = ANY[version] if src in (None, "any") else _ufw_addr(src)
    dst = ANY[version] if dst in (None, "any") else _ufw_addr(dst)
    return Rule(action, direction, iface, src, dst, dport, proto, comment)


class FakeUfw:
    HEADER = "Added user rules (see 'ufw status' for running firewall):"

    def __init__(self, lines=()):
        self.rules = [parse_rule(shlex.split(line)) for line in lines]

    def run(self, argv):
        assert argv[0] == "ufw", argv
        args = argv[1:]
        if args == ["show", "added"]:
            return "\n".join([self.HEADER] + ["ufw " + r.get_command() for r in self.rules])
        if args[:1] == ["delete"]:
            want = parse_rule(args[1:])
            for r in self.rules:
                if r.key() == want.key() and r.action == want.action:
                    self.rules.remove(r)
                    return "Rule deleted"
            return "Could not delete non-existent rule"
        new = parse_rule(args)
        for i, r in enumerate(self.rules):
            if r.key() == new.key():
                if (r.action, r.comment) == (new.action, new.comment):
                    return "Skipping adding existing rule"
                self.rules[i] = new
                return "Rule updated"
        self.rules.append(new)
        return "Rule added"

    def show_added(self):
        return ["ufw " + r.get_command() for r in self.rules]


# ---------------------------------------------------------------------------------------- the role's own Jinja
def _difference(a, b):
    return [x for i, x in enumerate(a) if x not in b and x not in a[:i]]


def _symmetric_difference(a, b):
    return _difference(a, b) + _difference(b, a)


def _regex_replace(value, pattern, replacement=""):
    return re.sub(pattern, replacement, value)


def environment():
    env = NativeEnvironment(undefined=jinja2.StrictUndefined)
    env.filters.update({
        "homelab_ufw_allow_rules": homelab_ufw_allow_rules,
        "regex_escape": re.escape,
        "difference": _difference,
        "symmetric_difference": _symmetric_difference,
        "regex_replace": _regex_replace,
    })
    env.tests.update({
        "match": lambda value, pattern: re.match(pattern, value) is not None,
        "search": lambda value, pattern: re.search(pattern, value) is not None,
    })
    return env


ENV = environment()


def _load(*parts):
    with open(os.path.join(ROLE, *parts), encoding="utf-8") as handle:
        return yaml.safe_load(handle)


ROLE_VARS = _load("vars", "main.yml")
MAIN_TASKS = _load("tasks", "main.yml")
UFW_TASKS = _load("tasks", "ufw.yml")


def _task(tasks, name):
    for task in tasks:
        if task.get("name") == name:
            return task
        for inner in task.get("block", []):
            if inner.get("name") == name:
                return inner
    raise KeyError(name)


def template(text, context):
    """Ansible-style: a string holding {{ }} is rendered natively; anything else is returned as is."""
    if isinstance(text, str) and "{{" in text:
        return ENV.from_string(text).render(**context)
    if isinstance(text, list):
        return [template(item, context) for item in text]
    return text


def condition(expression, context):
    return bool(ENV.from_string("{{ %s }}" % expression).render(**context))


def resolve(inventory):
    """The role's vars/main.yml resolved over the given inventory values (repeat until nothing changes)."""
    context = dict(inventory)
    pending = dict(ROLE_VARS)
    while pending:
        progressed = False
        for name, value in list(pending.items()):
            try:
                context[name] = template(value, context)
            except jinja2.UndefinedError:
                continue
            del pending[name]
            progressed = True
        if not progressed:
            raise AssertionError("unresolvable role vars: %s" % sorted(pending))
    return context


def inventory(allow_rules, **extra):
    values = {"firewall_allow_rules": allow_rules, "docker_dns_ip": DOCKER_DNS_IP, "lan_iface": LAN}
    values.update(extra)
    return values


def preflight(context):
    """tasks/main.yml: the allow-rule assertion (lockout guard present, nothing on wifi_iface)."""
    task = _task(MAIN_TASKS, "Declared allow rules keep SSH over the tailnet and stay off the Wi-Fi uplink")
    return all(condition(that, context) for that in task["ansible.builtin.assert"]["that"])


class RoleFailed(Exception):
    pass


def converge(ufw, context):
    """tasks/ufw.yml's add / read / compare / prune steps; returns (changed tasks, deleted command lines)."""
    changed = 0
    add = _task(UFW_TASKS, "Declared rules are present (SSH over tailnet first)")
    for item in template(add["loop"], context):
        out = ufw.run(shlex.split(template(add["ansible.builtin.command"]["cmd"], dict(context, item=item))))
        if condition(add["changed_when"], dict(context, firewall_add={"stdout": out})):
            changed += 1

    ctx = dict(context, firewall_show_added={"stdout_lines": ufw.run(["ufw", "show", "added"]).splitlines()})
    facts = _task(UFW_TASKS, "Work out which rules are not declared")
    fact_ctx = dict(ctx, **{k: template(v, ctx) for k, v in facts["vars"].items()})
    for name, value in facts["ansible.builtin.set_fact"].items():
        ctx[name] = template(value, fact_ctx)

    guard = _task(UFW_TASKS, "Every declared rule reads back exactly as declared, and the lockout guard is never pruned")
    for that in guard["ansible.builtin.assert"]["that"]:
        if not condition(that, ctx):
            raise RoleFailed("%s: declared not read back %s" % (that, _difference(ctx["firewall_rules"], ctx["firewall_present"])))

    delete = _task(UFW_TASKS, "Rules that are not declared are deleted")
    deleted = []
    for item in template(delete["loop"], ctx):
        cmd = template(delete["ansible.builtin.command"]["cmd"], dict(ctx, item=item))
        out = ufw.run(shlex.split(cmd))
        if "Rule deleted" not in out:
            raise RoleFailed("%s: %s" % (cmd, out))
        deleted.append(cmd)
        changed += 1

    if ctx["firewall_unmanaged"]:
        final = _task(UFW_TASKS, "UFW holds exactly the declared rules")
        final_ctx = dict(ctx, firewall_show_final={"stdout_lines": ufw.run(["ufw", "show", "added"]).splitlines()})
        final_ctx.update({k: template(v, final_ctx) for k, v in final["vars"].items()})
        for that in final["ansible.builtin.assert"]["that"]:
            if not condition(that, final_ctx):
                raise RoleFailed(that)
    return changed, deleted


def _forge_api_filters():
    path = os.path.join(ROLES, "forge_api", "filter_plugins", "forge_api_net.py")
    spec = importlib.util.spec_from_file_location("forge_api_net_for_firewall_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------------------------------------- tests
class Model(unittest.TestCase):
    def test_model_prints_the_dns_rules_the_role_already_converges_on_real_hosts(self):
        # vars/main.yml firewall_dns_rules have been read back from real UFW (from + to, no iface; deny out
        # short syntax): the model must print them byte for byte.
        context = resolve(inventory([]))
        for line in context["firewall_dns_rules"]:
            self.assertEqual(FakeUfw([line]).show_added(), ["ufw " + line])

    def test_model_prints_like_ufw(self):
        cases = {
            "allow in on lan0 from 192.0.2.10/32 to any port 22 proto tcp": "allow in on lan0 from 192.0.2.10 to any port 22 proto tcp",
            "allow from 192.0.2.1 to 192.0.2.2 port 1 proto tcp": "allow from 192.0.2.1 to 192.0.2.2 port 1 proto tcp",
            "allow in from 192.0.2.1 to 192.0.2.2 port 1 proto tcp": "allow from 192.0.2.1 to 192.0.2.2 port 1 proto tcp",
            "allow to 2001:db8:0::1 port 1 proto udp": "allow to 2001:db8::1 port 1 proto udp",
            "allow in on lan0 to any port 22 proto tcp": "allow in on lan0 to any port 22 proto tcp",
        }
        for given, printed in cases.items():
            with self.subTest(given=given):
                self.assertEqual(FakeUfw([given]).show_added(), ["ufw " + printed])
        with self.assertRaises(ValueError):
            FakeUfw(["allow from 192.0.2.1 to 2001:db8::1 port 1 proto tcp"])


class ForgeApiRule(unittest.TestCase):
    def test_renders_the_exact_line_forge_api_verifies(self):
        self.assertEqual(homelab_ufw_allow_rules([FORGE_RULE]), [FORGE_LINE])

    def test_forge_api_verify_and_preflight_accept_it_after_a_converge(self):
        ufw = FakeUfw()
        converge(ufw, resolve(inventory(MIXED)))
        forge = _forge_api_filters()
        # verify.yml: port_rules == ['ufw ' ~ forge_api_ufw_rule], port_rules from `ufw show added`.
        self.assertEqual(forge.forge_api_ufw_port_rules(ufw.run(["ufw", "show", "added"]).splitlines(), 8790),
                         ["ufw " + FORGE_LINE])
        self.assertEqual(forge.forge_api_ufw_problems(FORGE_LINE, WEBUI_IP, AI_GATEWAY, 8790), [])


class Converge(unittest.TestCase):
    STALE = [
        "allow 80/tcp comment 'not ours'",
        "allow from 192.0.2.99 to 172.20.0.1 port 8790 proto tcp comment 'homelab: old forge'",
        "allow in on tailscale0 to any port 22 proto tcp comment 'homelab: ssh over tailnet'",
    ]

    def test_mixed_rule_set_converges_then_a_second_run_changes_nothing(self):
        context = resolve(inventory(MIXED, wifi_iface=WIFI))
        self.assertTrue(preflight(context))
        ufw = FakeUfw(self.STALE)
        changed, deleted = converge(ufw, context)
        self.assertGreater(changed, 0)
        self.assertEqual(deleted, [
            "ufw delete allow 80/tcp",
            "ufw delete allow from 192.0.2.99 to 172.20.0.1 port 8790 proto tcp",
        ])
        self.assertEqual(ufw.show_added()[0], "ufw allow in on tailscale0 to any port 22 proto tcp comment 'homelab: ssh over tailnet'")
        self.assertEqual(sorted(ufw.show_added()), sorted("ufw " + line for line in context["firewall_rules"]))
        self.assertIn("ufw " + FORGE_LINE, ufw.show_added())

        before = ufw.show_added()
        self.assertEqual(converge(ufw, context), (0, []))      # changed=0, nothing deleted
        self.assertEqual(ufw.show_added(), before)

    def test_a_dropped_to_rule_is_pruned(self):
        ufw = FakeUfw()
        converge(ufw, resolve(inventory(MIXED)))
        _, deleted = converge(ufw, resolve(inventory([rule for rule in MIXED if rule is not FORGE_RULE])))
        self.assertEqual(deleted, ["ufw delete allow from 172.20.0.4 to 172.20.0.1 port 8790 proto tcp"])
        self.assertNotIn("ufw " + FORGE_LINE, ufw.show_added())

    def test_rules_without_to_converge_exactly_as_before(self):
        # The pre-`to` rule set on a host that already holds it: nothing added, nothing deleted.
        old = [rule for rule in MIXED if "to" not in rule]
        context = resolve(inventory(old, wifi_iface=WIFI))
        ufw = FakeUfw(context["firewall_rules"])
        self.assertEqual(converge(ufw, context), (0, []))

    def test_a_comment_change_on_a_to_rule_updates_in_place(self):
        ufw = FakeUfw()
        converge(ufw, resolve(inventory(MIXED)))
        renamed = [dict(rule, comment="forge api") if rule is FORGE_RULE else rule for rule in MIXED]
        changed, deleted = converge(ufw, resolve(inventory(renamed)))
        self.assertEqual((changed, deleted), (1, []))
        self.assertIn("ufw allow from 172.20.0.4 to 172.20.0.1 port 8790 proto tcp comment 'homelab: forge api'",
                      ufw.show_added())


class Preflight(unittest.TestCase):
    def test_a_rule_without_iface_passes_the_wifi_check(self):
        self.assertTrue(preflight(resolve(inventory(MIXED, wifi_iface=WIFI))))
        self.assertTrue(preflight(resolve(inventory(MIXED))))

    def test_a_rule_on_wifi_is_still_refused(self):
        rules = MIXED + [{"port": 1, "proto": "tcp", "iface": WIFI, "from": "192.0.2.1", "to": "192.0.2.2", "comment": "w"}]
        self.assertFalse(preflight(resolve(inventory(rules, wifi_iface=WIFI))))

    def test_the_lockout_guard_is_still_required(self):
        self.assertFalse(preflight(resolve(inventory(MIXED[1:]))))


if __name__ == "__main__":
    unittest.main()
