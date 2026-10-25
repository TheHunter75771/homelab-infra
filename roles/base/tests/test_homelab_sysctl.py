"""Unit tests for the homelab_sysctl_overrides filter (no host needed).

    python3 -m unittest discover -s roles/base/tests -v

The merged configs are the vendor-only fixture plus the homelab sysctl files rendered from the
roles' real templates, merged the way `systemd-sysctl --cat-config` prints them.
"""

import filecmp
import os
import sys
import unittest

import jinja2
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROLES = os.path.dirname(os.path.dirname(HERE))
BASE_PLUGIN = os.path.join(ROLES, "base", "filter_plugins", "homelab_sysctl.py")
WATCHDOG_PLUGIN = os.path.join(ROLES, "watchdog", "filter_plugins", "homelab_sysctl.py")
sys.path.insert(0, os.path.dirname(BASE_PLUGIN))

from homelab_sysctl import homelab_sysctl_overrides as check  # noqa: E402

HARDENING = "/etc/sysctl.d/99-zz-homelab-hardening.conf"
PANIC = "/etc/sysctl.d/99-zz-homelab-panic.conf"


def _render(role, template):
    with open(os.path.join(ROLES, role, "defaults", "main.yml"), encoding="utf-8") as handle:
        defaults = yaml.safe_load(handle)
    with open(os.path.join(ROLES, role, "templates", template), encoding="utf-8") as handle:
        source = handle.read()
    return jinja2.Template(source).render(ansible_managed="Ansible managed", **defaults)


with open(os.path.join(HERE, "fixtures", "vendor-cat-config.txt"), encoding="utf-8") as fixture:
    VENDOR = fixture.read()
HARD = _render("base", "sysctl-homelab-hardening.conf.j2")
PAN = _render("watchdog", "sysctl-homelab-panic.conf.j2")


def merged(*blocks):
    """Join (path, text) blocks the way --cat-config prints them; a bare string is used as is."""
    out = []
    for block in blocks:
        if isinstance(block, tuple):
            out.append("# %s\n%s" % block)
        else:
            out.append(block)
    return "\n".join(out) + "\n"


CLEAN = merged(VENDOR, (HARDENING, HARD), (PANIC, PAN))
LATER = "/usr/lib/sysctl.d/99-zzz-vendor.conf"
EARLIER = "/usr/lib/sysctl.d/30-vendor.conf"


class PluginCopies(unittest.TestCase):
    def test_base_and_watchdog_copies_are_byte_identical(self):
        self.assertTrue(filecmp.cmp(BASE_PLUGIN, WATCHDOG_PLUGIN, shallow=False),
                        "roles/base and roles/watchdog filter_plugins/homelab_sysctl.py differ")


class Rendered(unittest.TestCase):
    def test_templates_render_the_expected_keys(self):
        self.assertIn("kernel.unprivileged_bpf_disabled = 1", HARD)
        self.assertIn("net.ipv4.conf.*.send_redirects = 0", HARD)
        self.assertIn("kernel.panic = 10", PAN)


class CleanBox(unittest.TestCase):
    def test_hardening_wins(self):
        self.assertEqual(check(CLEAN, HARDENING), [])

    def test_panic_wins(self):
        self.assertEqual(check(CLEAN, PANIC), [])

    def test_vendor_comment_starting_with_a_path_is_not_a_file_header(self):
        # the fixture's "# /proc/kallsyms, ..." comment must not start a new file
        self.assertIn("# /proc/kallsyms", VENDOR)
        self.assertEqual(check(merged(VENDOR, (HARDENING, "# /proc/sys/fs is per-boot\n" + HARD)), HARDENING), [])

    def test_our_file_missing(self):
        self.assertEqual(len(check(VENDOR, HARDENING)), 1)


class LaterFileOverrides(unittest.TestCase):
    def test_different_value_fails(self):
        problems = check(merged(CLEAN, (LATER, "fs.protected_fifos = 1")), HARDENING)
        self.assertEqual(len(problems), 1)
        self.assertIn("overridden by fs.protected_fifos = 1", problems[0])

    def test_same_value_is_fine(self):
        self.assertEqual(check(merged(CLEAN, (LATER, "fs.protected_fifos = 2\nkernel.panic=10")), HARDENING), [])
        self.assertEqual(check(merged(CLEAN, (LATER, "kernel.panic = 10")), PANIC), [])

    def test_bpf_lock_is_explained(self):
        problems = check(merged(CLEAN, ("/run/sysctl.d/99-zzz.conf", "kernel.unprivileged_bpf_disabled = 2")), HARDENING)
        self.assertEqual(len(problems), 1)
        self.assertIn("locked until reboot", problems[0])
        self.assertIn("systemd-sysctl.service fail at boot", problems[0])

    def test_slash_form_key_counts(self):
        self.assertEqual(len(check(merged(CLEAN, (LATER, "kernel/panic = 0")), PANIC)), 1)

    def test_leading_slash_is_stripped(self):
        self.assertEqual(len(check(merged(CLEAN, (LATER, "/kernel/panic = 0")), PANIC)), 1)
        self.assertEqual(check(merged(CLEAN, (LATER, "/kernel/panic = 10")), PANIC), [])

    def test_same_glob_with_another_value_fails(self):
        self.assertEqual(len(check(merged(CLEAN, (LATER, "net.ipv4.conf.*.send_redirects = 1")), HARDENING)), 1)


class LaterDashKeyCancels(unittest.TestCase):
    def test_dash_key_cancels_an_explicit_setting(self):
        problems = check(merged(CLEAN, (LATER, "-kernel.panic")), PANIC)
        self.assertEqual(len(problems), 1)
        self.assertIn("cancelled by '-kernel.panic'", problems[0])

    def test_dash_key_cancels_our_glob(self):
        problems = check(merged(CLEAN, (LATER, "-net.ipv4.conf.*.send_redirects")), HARDENING)
        self.assertEqual(len(problems), 1)
        self.assertIn("cancelled", problems[0])

    def test_earlier_dash_key_is_overridden_by_ours(self):
        self.assertEqual(check(merged(VENDOR, (EARLIER, "-kernel.panic"), (HARDENING, HARD), (PANIC, PAN)), PANIC), [])


class GlobsAndExplicitKeys(unittest.TestCase):
    def test_explicit_interface_key_anywhere_is_skipped_by_our_glob(self):
        # an explicit key beats any glob regardless of file order
        problems = check(merged(VENDOR, (EARLIER, "net.ipv4.conf.eth0.send_redirects = 1"), (HARDENING, HARD)), HARDENING)
        self.assertEqual(len(problems), 1)
        self.assertIn("skipped for net.ipv4.conf.eth0.send_redirects", problems[0])

    def test_explicit_interface_key_with_our_value_is_fine(self):
        self.assertEqual(check(merged(VENDOR, (EARLIER, "net.ipv4.conf.eth0.send_redirects = 0"), (HARDENING, HARD)), HARDENING), [])

    def test_excluded_interface_fails(self):
        problems = check(merged(VENDOR, (EARLIER, "-net.ipv4.conf.lo.send_redirects"), (HARDENING, HARD)), HARDENING)
        self.assertEqual(len(problems), 1)
        self.assertIn("excludes from globs", problems[0])

    def test_excluded_all_is_covered_by_our_explicit_all(self):
        self.assertEqual(check(merged(VENDOR, (EARLIER, "-net.ipv4.conf.all.send_redirects"), (HARDENING, HARD)), HARDENING), [])

    def test_later_different_glob_overlapping_ours_fails(self):
        for glob in ("net.ipv4.conf.eth*.send_redirects = 1", "net.ipv4.conf.*.send_* = 1", "net/ipv4/conf/*/send_redirects = 1"):
            with self.subTest(glob=glob):
                self.assertGreaterEqual(len(check(merged(CLEAN, (LATER, glob)), HARDENING)), 1)

    def test_later_different_glob_with_our_value_is_fine(self):
        self.assertEqual(check(merged(CLEAN, (LATER, "net.ipv4.conf.eth*.send_redirects = 0")), HARDENING), [])

    def test_later_glob_that_does_not_overlap_is_fine(self):
        self.assertEqual(check(merged(CLEAN, (LATER, "net.ipv6.conf.*.accept_redirects = 0")), HARDENING), [])

    def test_earlier_different_glob_is_applied_before_ours(self):
        self.assertEqual(check(merged(VENDOR, (EARLIER, "net.ipv4.conf.eth*.send_redirects = 1"), (HARDENING, HARD)), HARDENING), [])


class FileOrder(unittest.TestCase):
    def test_old_name_loses_to_a_vendor_9x_file(self):
        old = "/etc/sysctl.d/60-homelab-hardening.conf"
        problems = check(merged(VENDOR, (old, HARD), ("/usr/lib/sysctl.d/90-vendor.conf", "kernel.kptr_restrict = 1")), old)
        self.assertEqual(len(problems), 1)
        self.assertIn("kernel.kptr_restrict", problems[0])


if __name__ == "__main__":
    unittest.main()
