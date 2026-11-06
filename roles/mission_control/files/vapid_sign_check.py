"""Sign a VAPID token with Mission Control's real vapid.json, the way pywebpush does it (py_vapid Vapid.from_string
+ sign with the file's subject), to prove pushes can be signed (live bug 2026-10-28: a subject with a port made every
send fail). Read-only; nothing is sent anywhere.

Usage: <venv python> -I -B vapid_sign_check.py <vapid.json>
Prints exactly "OK", or "FAIL <exception type>". Never key material, never the token.
"""

import json
import sys


def main(argv):
    try:
        from py_vapid import Vapid

        with open(argv[1], encoding="ascii") as fh:
            data = json.load(fh)
        vapid = Vapid.from_string(private_key=data["privateKey"])
        headers = vapid.sign({"sub": data["subject"], "aud": "https://web.push.apple.com"})
        if not str(headers.get("Authorization", "")).startswith("vapid t="):
            print("FAIL no vapid Authorization header")
            return 0
    except Exception as exc:  # report the type only: messages can echo claims
        print("FAIL %s" % type(exc).__name__)
        return 0
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
