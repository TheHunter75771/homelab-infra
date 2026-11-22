"""Unit tests for roles/ai_stack/filter_plugins/ai_stack_net.py (stdlib only).

  python3 -m unittest discover -s roles/ai_stack/tests -p 'test_*.py' -v
"""

import importlib.util
import os
import unittest

PLUGIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "filter_plugins", "ai_stack_net.py")
spec = importlib.util.spec_from_file_location("ai_stack_net", PLUGIN)
net = importlib.util.module_from_spec(spec)
spec.loader.exec_module(net)

# role docker's defaults
POOLS = [{"base": "172.20.0.0/14", "size": 24}, {"base": "172.24.0.0/14", "size": 24}]
AI, EXTRACT = "172.20.0.0/24", "172.23.255.240/28"


class NetProblemsTest(unittest.TestCase):
    def test_role_defaults_fit(self):
        self.assertEqual(net.ai_stack_net_problems(AI, EXTRACT, POOLS), [])

    def test_overlap(self):
        self.assertIn("overlap", " ".join(net.ai_stack_net_problems(AI, "172.20.0.16/28", POOLS)))

    def test_outside_the_pools(self):
        problems = net.ai_stack_net_problems(AI, "172.29.16.0/28", POOLS)
        self.assertEqual(len(problems), 1)
        self.assertIn("not inside a Docker address pool", problems[0])

    def test_not_a_network_and_too_small(self):
        self.assertIn("not a network address", " ".join(net.ai_stack_net_problems("172.20.0.1/24", EXTRACT, POOLS)))
        self.assertIn("smaller than a /28", " ".join(net.ai_stack_net_problems(AI, "172.23.255.248/29", POOLS)))
        self.assertIn("not IPv4", " ".join(net.ai_stack_net_problems("fd00::/64", EXTRACT, POOLS)))


class NetworkConflictsTest(unittest.TestCase):
    LIVE = ("bridge 172.17.0.0/16 ", "host ", "none ", "ai_ai 172.20.0.0/24 ", "research 172.29.16.0/28 ")

    def test_live_host_with_the_pin_is_clean(self):
        self.assertEqual(net.ai_stack_network_conflicts(AI, self.LIVE, "ai_ai"), [])
        self.assertEqual(net.ai_stack_network_conflicts(EXTRACT, self.LIVE, "ai_extract"), [])
        self.assertEqual(net.ai_stack_network_conflicts(EXTRACT, [*self.LIVE, f"ai_extract {EXTRACT} "], "ai_extract"), [])

    def test_own_network_with_another_subnet(self):
        found = net.ai_stack_network_conflicts(AI, ["ai_ai 172.21.0.0/24 "], "ai_ai")
        self.assertEqual(found, ["ai_ai exists with 172.21.0.0/24, not 172.20.0.0/24"])

    def test_another_network_overlaps(self):
        self.assertEqual(
            net.ai_stack_network_conflicts(EXTRACT, ["other_net 172.23.255.0/24 "], "ai_extract"),
            ["other_net 172.23.255.0/24"],
        )

    def test_absent_is_fine(self):
        self.assertEqual(net.ai_stack_network_conflicts(AI, ["bridge 172.17.0.0/16"], "ai_ai"), [])


class MountinfoStackTest(unittest.TestCase):
    DOCKER = "1500 1400 8:1 /var/lib/homelab-ai/skills /skills ro,relatime master:1 - ext4 /dev/sda1 rw"
    SKILLS = "1600 1500 0:60 /30-skills /skills ro,nosuid,nodev,noexec,relatime master:9 - cifs //pc/lib rw,vers=3.1.1"
    OTHER = "1400 1 0:50 / / rw,relatime - overlay overlay rw"

    def test_bottom_first_top_last(self):
        stack = net.ai_stack_mountinfo_stack([self.OTHER, self.DOCKER, self.SKILLS], "/skills")
        self.assertEqual([m["fstype"] for m in stack], ["ext4", "cifs"])
        self.assertEqual(stack[-1]["options"], ["ro", "nosuid", "nodev", "noexec", "relatime"])
        self.assertEqual(stack[-1]["source"], "//pc/lib")

    def test_no_optional_fields_and_absent(self):
        line = "1500 1400 8:1 /x /skills rw - ext4 /dev/sda1 rw"
        self.assertEqual(net.ai_stack_mountinfo_stack([line], "/skills")[0]["options"], ["rw"])
        self.assertEqual(net.ai_stack_mountinfo_stack([self.OTHER], "/skills"), [])
        self.assertEqual(net.ai_stack_mountinfo_stack(["garbage", ""], "/skills"), [])

    def test_exact_mount_point_only(self):
        sub = "1700 1600 0:60 /30-skills/a /skills/a rw - cifs //pc/lib rw"
        self.assertEqual(net.ai_stack_mountinfo_stack([self.DOCKER, sub], "/skills")[-1]["fstype"], "ext4")

    def test_escaped_mount_point(self):
        line = "1500 1400 8:1 /x /my\\040skills ro - ext4 /dev/sda1 rw"
        self.assertEqual(len(net.ai_stack_mountinfo_stack([line], "/my skills")), 1)


PINS = {"ollama-cpu": "172.20.0.2", "gpu-broker": "172.20.0.3", "open-webui": "172.20.0.4"}


class PinnedIpTest(unittest.TestCase):
    """§12.23: the pinned container addresses on `ai`."""

    def test_role_defaults_fit(self):
        self.assertEqual(net.ai_stack_pinned_ip_problems(AI, PINS), [])

    def test_refused(self):
        for pins, word in (
            ({"gpu-broker": "172.21.0.3"}, "not inside"),
            ({"gpu-broker": "172.20.0.1"}, "gateway"),
            ({"gpu-broker": "172.20.0.0"}, "network or broadcast"),
            ({"gpu-broker": "172.20.0.255"}, "network or broadcast"),
            ({"gpu-broker": "fd00::3"}, "not inside"),
            ({"gpu-broker": "broker"}, "not an IP address"),
            ({"gpu-broker": "172.20.0.3", "open-webui": "172.20.0.3"}, "are both"),
        ):
            problems = net.ai_stack_pinned_ip_problems(AI, pins)
            self.assertTrue(any(word in p for p in problems), (pins, problems))

    def test_bad_subnet(self):
        self.assertIn("not a network address", net.ai_stack_pinned_ip_problems("172.20.0.1/24", PINS)[0])

    def test_holders(self):
        live = ["ollama-cpu 172.20.0.2/24", "gpu-broker 172.20.0.3/24", "open-webui 172.20.0.4/24", ""]
        self.assertEqual(net.ai_stack_pinned_ip_holders(live, PINS), [])
        # Docker's own addresses before the pin (ai-box on 2026-10-27 had exactly the defaults).
        shuffled = ["ollama-cpu 172.20.0.3/24", "stray 172.20.0.4/24", "gpu-broker 172.20.0.9/24"]
        self.assertEqual(
            net.ai_stack_pinned_ip_holders(shuffled, PINS),
            ["ollama-cpu holds 172.20.0.3 (pinned for gpu-broker)", "stray holds 172.20.0.4 (pinned for open-webui)"],
        )


if __name__ == "__main__":
    unittest.main()
