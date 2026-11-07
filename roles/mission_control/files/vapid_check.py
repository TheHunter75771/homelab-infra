"""Check Mission Control's VAPID key file without printing it.

Usage: <venv python> -I -B vapid_check.py <path> [--keys-only]

Prints exactly one line and exits 0 when the file is good: "ok". Otherwise "bad: <reason>" and exit 1. The
reason never holds key material. --keys-only checks the keys alone (the subject may be missing or wrong: the role
fixes it next with vapid_subject.py, never touching the keys). Good = a regular file (not a symlink), a JSON object with exactly the keys
privateKey (43 base64url chars: a 32-byte P-256 scalar), publicKey (87 base64url chars: the 65-byte uncompressed
point) and subject (a mailto: or https:// URL), and the public key is the one the private key derives. The same
layout MC1's load_vapid() reads.
"""

import base64
import json
import os
import re
import stat
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

B64U = re.compile(r"[A-Za-z0-9_-]+\Z")
# py_vapid's own rule (py_vapid/__init__.py _check_sub, strict mode: what Vapid.sign() enforces), verbatim...
PY_VAPID_SUB = re.compile(
    r"^(mailto:.+@((localhost|[%\w-]+(\.[%\w-]+)+|([0-9a-f]{1,4}):+([0-9a-f]{1,4})?)))|https:\/\/(localhost|[\w-]+\.[\w\.-]+|([0-9a-f]{1,4}:+)+([0-9a-f]{1,4})?)$",
    re.IGNORECASE,
)
# ...and, because its mailto branch isn't anchored at the end, the whole string must also be one of these: an https
# origin with no port and no path, or one mailto address.
STRICT_SUB = re.compile(r"(https://[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+|mailto:[^\s@/:]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+)\Z")


def subject_ok(sub):
    """True when py_vapid will sign with this subject (and it is a plain origin or address)."""
    return isinstance(sub, str) and PY_VAPID_SUB.match(sub) is not None and STRICT_SUB.match(sub) is not None


def unb64u(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def check(path, keys_only=False):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    if not stat.S_ISREG(st.st_mode):
        return "not a regular file"
    try:
        with open(path, encoding="ascii") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return "not readable JSON"
    allowed = (["privateKey", "publicKey"], ["privateKey", "publicKey", "subject"]) if keys_only else (
        ["privateKey", "publicKey", "subject"],)
    if not isinstance(data, dict) or sorted(data) not in allowed:
        return "the keys must be exactly privateKey, publicKey and subject"
    if not keys_only and not subject_ok(data["subject"]):
        return "subject is not one py_vapid signs with (https://<host> with no port or path, or mailto:<address>)"
    priv, pub = data["privateKey"], data["publicKey"]
    if not isinstance(priv, str) or len(priv) != 43 or not B64U.match(priv):
        return "privateKey is not 43 base64url characters"
    if not isinstance(pub, str) or len(pub) != 87 or not B64U.match(pub):
        return "publicKey is not 87 base64url characters"
    try:
        key = ec.derive_private_key(int.from_bytes(unb64u(priv), "big"), ec.SECP256R1())
    except ValueError:
        return "privateKey is not a valid P-256 scalar"
    derived = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    if derived != unb64u(pub):
        return "publicKey does not belong to privateKey"
    return "ok"


def main(argv):
    if len(argv) not in (2, 3) or (len(argv) == 3 and argv[2] != "--keys-only"):
        print("bad: usage: vapid_check.py <path> [--keys-only]")
        return 1
    verdict = check(argv[1], keys_only=len(argv) == 3)
    print(verdict if verdict == "ok" else "bad: " + verdict)
    return 0 if verdict == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
