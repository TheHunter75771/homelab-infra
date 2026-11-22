"""Render tests for the chrony drop-ins (no host needed).

    python3 -m unittest discover -s roles/base/tests -v

The clock-recovery fix (docs/CONTRACTS.md §12.9): exactly two IP-literal NTP sources, one per operator,
so chrony needs no DNS; `authselectmode ignore`, so they can be selected while NTS is failing (chrony's
default "mix" makes every NTS source required); and `makestep 1 -1`, so a clock that comes up months off
is stepped whenever a source answers. Both files are rendered from the role's real templates and defaults.
tests/test_chrony_selectdata.yml covers the live check of the same thing.
"""

import ipaddress
import os
import unittest

import jinja2
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROLE = os.path.dirname(HERE)

OPERATORS = {
    "Cloudflare": ipaddress.ip_network("162.159.200.0/24"),
    "Google": ipaddress.ip_network("216.239.35.0/24"),
}


def _defaults():
    with open(os.path.join(ROLE, "defaults", "main.yml"), encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def render(template, **overrides):
    values = dict(_defaults(), ansible_managed="Ansible managed", **overrides)
    with open(os.path.join(ROLE, "templates", template), encoding="utf-8") as handle:
        source = handle.read()
    env = jinja2.Environment(trim_blocks=True, undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    return env.from_string(source).render(**values)


def directives(text):
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


class FallbackSources(unittest.TestCase):
    def setUp(self):
        self.text = render("chrony-homelab-fallback.sources.j2")
        self.lines = directives(self.text)

    def test_every_line_is_an_ip_literal_server_with_iburst(self):
        self.assertTrue(self.lines)
        for line in self.lines:
            words = line.split()
            self.assertEqual((words[0], words[2:]), ("server", ["iburst"]), line)
            ipaddress.ip_address(words[1])  # raises on a hostname

    def test_exactly_two_sources_one_per_operator(self):
        # Two, so Ubuntu's five NTS pools keep the majority in the falseticker vote.
        addresses = [ipaddress.ip_address(line.split()[1]) for line in self.lines]
        self.assertEqual(len(addresses), 2)
        operators = sorted(name for addr in addresses for name, net in OPERATORS.items() if addr in net)
        self.assertEqual(operators, sorted(OPERATORS))

    def test_render_is_stable(self):
        self.assertEqual(render("chrony-homelab-fallback.sources.j2"), self.text)


class HomelabConf(unittest.TestCase):
    def test_fallback_selectable_and_steps_at_any_time(self):
        self.assertEqual(sorted(directives(render("chrony-homelab.conf.j2"))),
                         ["authselectmode ignore", "makestep 1 -1"])

    def test_no_nocerttimecheck(self):
        # Decided: NTS certificate time checks stay on (docs/CONTRACTS.md §12.9).
        self.assertNotIn("nocerttimecheck", render("chrony-homelab.conf.j2"))
        self.assertNotIn("nocerttimecheck", render("chrony-homelab-fallback.sources.j2"))

    def test_render_is_stable(self):
        self.assertEqual(render("chrony-homelab.conf.j2"), render("chrony-homelab.conf.j2"))

    def test_ubuntu_reads_conf_d_after_its_own_makestep(self):
        # Our makestep only wins if confdir comes after Ubuntu's "makestep 1 3" (the last one read wins).
        with open(os.path.join(HERE, "fixtures", "ubuntu-chrony.conf"), encoding="utf-8") as handle:
            ubuntu = directives(handle.read())
        keys = [line.split()[0] for line in ubuntu]
        self.assertIn("makestep 1 3", ubuntu)
        self.assertIn("confdir /etc/chrony/conf.d", ubuntu)
        self.assertIn("sourcedir /etc/chrony/sources.d", ubuntu)
        self.assertGreater(keys.index("confdir"), keys.index("makestep"))
        self.assertEqual(keys.count("makestep"), 1)


if __name__ == "__main__":
    unittest.main()
