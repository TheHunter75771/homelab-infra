"""Scan Mission Control's journal for its secrets without printing them (role mission_control, tasks/verify.yml).

Usage: python3 -I -B journal_scan.py <journalctl> <vapid.json> <ha-token> <subscriptions.json> <unit>...

Collects the secrets that exist: the VAPID private key (as stored, in standard base64 and as hex of the raw
scalar), the Home Assistant token (when its file exists) and every subscription's endpoint, auth and p256dh
(push endpoints are capability URLs). Then reads the units' whole journal (`<journalctl> -q --no-pager -o cat
-u <unit>...`) and prints one JSON line: {"lines": N, "secrets": K, "leaks": [labels], "error": "..."}.
Labels name the kind of secret, never its value. Exit 0 unless the arguments are wrong.
"""

import base64
import json
import os
import subprocess
import sys


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _walk(node, out):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("endpoint", "auth", "p256dh") and isinstance(value, str) and len(value) >= 8:
                out.append(("subscription " + key, value))
            else:
                _walk(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk(item, out)


def secrets(vapid_path, ha_path, subs_path):
    found = []
    vapid = _read(vapid_path)
    if vapid is not None:
        try:
            priv = json.loads(vapid).get("privateKey")
        except (ValueError, AttributeError):
            priv = None
        if isinstance(priv, str) and priv:
            found.append(("VAPID private key", priv))
            raw = base64.urlsafe_b64decode(priv + "=" * (-len(priv) % 4))
            found.append(("VAPID private key (base64)", base64.b64encode(raw).decode().rstrip("=")))
            found.append(("VAPID private key (hex)", raw.hex()))
    ha = _read(ha_path)
    if ha is not None and ha.strip():
        found.append(("Home Assistant token", ha.strip()))
    subs = _read(subs_path)
    if subs is not None:
        try:
            _walk(json.loads(subs), found)
        except ValueError:
            pass
    return found


def main(argv):
    if len(argv) < 6:
        print(json.dumps({"error": "usage"}))
        return 2
    journalctl, vapid_path, ha_path, subs_path, units = argv[1], argv[2], argv[3], argv[4], argv[5:]
    found = secrets(vapid_path, ha_path, subs_path)
    cmd = [journalctl, "-q", "--no-pager", "-o", "cat"]
    for unit in units:
        cmd += ["-u", unit]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"lines": 0, "secrets": len(found), "leaks": [], "error": exc.__class__.__name__}))
        return 0
    text = proc.stdout
    leaks = sorted({label for label, value in found if value in text})
    error = "" if proc.returncode == 0 else "journalctl exited %d" % proc.returncode
    print(json.dumps({"lines": len(text.splitlines()), "secrets": len(found), "leaks": leaks, "error": error}))
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main(sys.argv))
