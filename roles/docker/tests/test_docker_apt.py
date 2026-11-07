"""Unit tests for role docker's pinned-package check (no host needed).

    python3 -m unittest discover -s roles/docker/tests -v
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "filter_plugins"))

from docker_apt import docker_apt_pending as pending  # noqa: E402

NAMES = ["docker-ce", "containerd.io"]
PINS = {"docker-ce": "5:29.5.2-1~ubuntu.26.04~resolute", "containerd.io": "2.2.4-1~ubuntu.26.04~resolute"}


def facts(**versions):
    return {name.replace("_", "-").replace("-io", ".io"): [{"version": v, "arch": "amd64"}] for name, v in versions.items()}


class Pending(unittest.TestCase):
    def test_nothing_installed(self):
        self.assertEqual(pending(NAMES, {}, PINS), NAMES)
        self.assertEqual(pending(NAMES, None, PINS), NAMES)

    def test_all_at_pin(self):
        installed = facts(docker_ce=PINS["docker-ce"], containerd_io=PINS["containerd.io"])
        self.assertEqual(pending(NAMES, installed, PINS), [])

    def test_version_bump_or_drift_is_pending(self):
        installed = facts(docker_ce="5:29.5.3-1~ubuntu.26.04~resolute", containerd_io=PINS["containerd.io"])
        self.assertEqual(pending(NAMES, installed, PINS), ["docker-ce"])

    def test_epoch_is_part_of_the_version(self):
        installed = facts(docker_ce="29.5.2-1~ubuntu.26.04~resolute", containerd_io=PINS["containerd.io"])
        self.assertEqual(pending(NAMES, installed, PINS), ["docker-ce"])

    def test_one_of_several_installed_versions_matches(self):
        installed = facts(containerd_io=PINS["containerd.io"])
        installed["docker-ce"] = [{"version": "5:29.4.0-1"}, {"version": PINS["docker-ce"]}]
        self.assertEqual(pending(NAMES, installed, PINS), [])

    def test_missing_pin_raises(self):
        with self.assertRaises(KeyError):
            pending(["docker-ce-cli"], {}, PINS)


if __name__ == "__main__":
    unittest.main()
