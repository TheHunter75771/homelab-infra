"""Unit tests for role docker's apt key fingerprint filter (no host, no gpg needed).

    python3 -m unittest discover -s roles/docker/tests -v

Fixtures:
  - fixtures/docker.asc: Docker's public apt signing key, as served by https://download.docker.com/linux/ubuntu/gpg
    (sha256 1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570). Its fingerprint is the one
    the role pins (docker_apt_key_fingerprint).
  - fixtures/other.asc: a throwaway ed25519 key generated for these tests (uid at example.invalid). Its
    expected fingerprint below was read with `gpg --show-keys --with-colons`, independently of the filter.
"""

import base64
import os
import sys
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROLE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROLE, "filter_plugins"))

from docker_openpgp import _dearmor, docker_openpgp_problem as problem  # noqa: E402
from docker_openpgp import docker_openpgp_fingerprints as fingerprints  # noqa: E402

DOCKER_FPR = "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
OTHER_FPR = "64EAB018C0189506435B5C31ABCBAD969A94935E"


def fixture(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="ascii") as handle:
        return handle.read()


def armor(raw, crc=True):
    from docker_openpgp import _crc24

    b64 = base64.b64encode(raw).decode()
    lines = ["-----BEGIN PGP PUBLIC KEY BLOCK-----", ""]
    lines += [b64[i:i + 64] for i in range(0, len(b64), 64)]
    if crc:
        lines.append("=" + base64.b64encode(_crc24(raw).to_bytes(3, "big")).decode())
    lines.append("-----END PGP PUBLIC KEY BLOCK-----")
    return "\n".join(lines) + "\n"


class Fingerprints(unittest.TestCase):
    def test_docker_key_has_exactly_the_pinned_fingerprint(self):
        self.assertEqual(fingerprints(fixture("docker.asc")), [DOCKER_FPR])

    def test_role_default_pins_the_same_fingerprint(self):
        with open(os.path.join(ROLE, "defaults", "main.yml"), encoding="utf-8") as handle:
            defaults = yaml.safe_load(handle)
        self.assertEqual(defaults["docker_apt_key_fingerprint"], DOCKER_FPR)

    def test_independent_key_matches_gpg(self):
        self.assertEqual(fingerprints(fixture("other.asc")), [OTHER_FPR])

    def test_binary_and_bytes_input(self):
        raw = _dearmor(fixture("docker.asc"))
        self.assertEqual(fingerprints(raw), [DOCKER_FPR])
        self.assertEqual(fingerprints(fixture("docker.asc").encode()), [DOCKER_FPR])

    def test_crlf_and_missing_crc_are_accepted(self):
        self.assertEqual(fingerprints(fixture("docker.asc").replace("\n", "\r\n")), [DOCKER_FPR])
        self.assertEqual(fingerprints(armor(_dearmor(fixture("docker.asc")), crc=False)), [DOCKER_FPR])

    def test_two_keys_are_both_listed(self):
        both = _dearmor(fixture("docker.asc")) + _dearmor(fixture("other.asc"))
        self.assertEqual(fingerprints(armor(both)), [DOCKER_FPR, OTHER_FPR])

    def test_a_different_key_does_not_match(self):
        self.assertNotIn(DOCKER_FPR, fingerprints(fixture("other.asc")))


class Malformed(unittest.TestCase):
    def test_bad_crc_is_rejected(self):
        raw = bytearray(_dearmor(fixture("docker.asc")))
        good = armor(bytes(raw))
        raw[40] ^= 0x01  # flip one bit inside the key material, keep the old CRC
        b64_bad = armor(bytes(raw)).split("\n")
        b64_good = good.split("\n")
        tampered = "\n".join(b64_bad[:-3] + b64_good[-3:])
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            fingerprints(tampered)

    def test_truncated_key_is_rejected(self):
        raw = _dearmor(fixture("docker.asc"))
        with self.assertRaisesRegex(ValueError, "truncated"):
            fingerprints(armor(raw[:200]))

    def test_not_a_key(self):
        for junk in ("", "<html>404</html>", "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n!!!!\n-----END PGP PUBLIC KEY BLOCK-----"):
            with self.subTest(junk=junk[:20]), self.assertRaises(ValueError):
                fingerprints(junk)
        with self.assertRaises(ValueError):
            fingerprints(b"")
        with self.assertRaisesRegex(ValueError, "packet header"):
            fingerprints(b"\x00\x01\x02")

    def test_two_armor_blocks_are_rejected(self):
        # gpg and apt load every block, so an appended key must not ride along with the pinned one.
        for both in (fixture("docker.asc") + fixture("other.asc"), fixture("other.asc") + fixture("docker.asc")):
            with self.subTest(first=both[40:60]), self.assertRaisesRegex(ValueError, "armor block|PUBLIC KEY BLOCK"):
                fingerprints(both)
            with self.assertRaises(ValueError):
                fingerprints(both.encode())

    def test_junk_outside_the_block_is_rejected(self):
        key = fixture("docker.asc")
        for doc in ("junk\n" + key, key + "junk\n", key + "\n\n-----END PGP PUBLIC KEY BLOCK-----\n",
                    "Comment: x\n" + key, key.rstrip("\n") + " trailing"):
            with self.subTest(doc=doc[:12] + "..." + doc[-12:]), self.assertRaises(ValueError):
                fingerprints(doc)
            with self.assertRaises(ValueError):
                fingerprints(doc.encode())

    def test_whitespace_around_the_block_is_fine(self):
        self.assertEqual(fingerprints("\n  \n" + fixture("docker.asc") + "\n\n  \n"), [DOCKER_FPR])

    def test_unknown_version_fails_closed(self):
        # A v3 public key packet (tag 6, new format): reported, never equal to a real fingerprint.
        body = bytes([3]) + bytes(20)
        packet = bytes([0xC6, len(body)]) + body
        self.assertEqual(fingerprints(packet), ["unsupported-v3"])


class Problem(unittest.TestCase):
    """docker_openpgp_problem: the assert's input. Never raises; "" only for an exact single-key match."""

    def b64(self, text):
        return base64.b64encode(text.encode() if isinstance(text, str) else text).decode()

    def test_match(self):
        self.assertEqual(problem(fixture("docker.asc"), DOCKER_FPR), "")
        self.assertEqual(problem(self.b64(fixture("docker.asc")), DOCKER_FPR.lower(), base64_input=True), "")

    def test_other_key(self):
        msg = problem(self.b64(fixture("other.asc")), DOCKER_FPR, base64_input=True)
        self.assertIn(OTHER_FPR, msg)
        self.assertIn("expected exactly " + DOCKER_FPR, msg)

    def test_extra_key_in_one_block(self):
        both = armor(_dearmor(fixture("docker.asc")) + _dearmor(fixture("other.asc")))
        self.assertIn("expected exactly", problem(self.b64(both), DOCKER_FPR, base64_input=True))

    def test_corrupt_files_are_reported_not_raised(self):
        cases = {
            "two blocks": fixture("docker.asc") + fixture("other.asc"),
            "truncated": fixture("docker.asc")[:900],
            "html": "<html>502 Bad Gateway</html>",
            "empty": "",
            "binary junk": bytes(range(256)),
        }
        for label, data in cases.items():
            with self.subTest(label=label):
                msg = problem(self.b64(data), DOCKER_FPR, base64_input=True)
                self.assertTrue(msg.startswith("it is not a valid OpenPGP public key file"), msg)
        self.assertTrue(problem("not base64 !!", DOCKER_FPR, base64_input=True).startswith("it is not a valid"))


if __name__ == "__main__":
    unittest.main()
