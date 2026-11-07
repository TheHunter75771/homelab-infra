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


def _walk(node, labels, values):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("endpoint", "auth", "p256dh") and isinstance(value, str) and len(value) >= 8:
                labels.append("subscription " + key)
                values.append(value)
            else:
                _walk(value, labels, values)
    elif isinstance(node, list):
        for item in node:
            _walk(item, labels, values)


def collect(vapid_path, ha_path, subs_path):
    """Two parallel lists: labels[i] names the kind of values[i]. Only the labels (and how many) are ever printed;
    the values serve the membership test alone."""
    labels, values = [], []
    vapid = _read(vapid_path)
    if vapid is not None:
        try:
            priv = json.loads(vapid).get("privateKey")
        except (ValueError, AttributeError):
            priv = None
        if isinstance(priv, str) and priv:
            raw = base64.urlsafe_b64decode(priv + "=" * (-len(priv) % 4))
            labels += ["VAPID private key", "VAPID private key (base64)", "VAPID private key (hex)"]
            values += [priv, base64.b64encode(raw).decode().rstrip("="), raw.hex()]
    ha = _read(ha_path)
    if ha is not None and ha.strip():
        labels.append("Home Assistant token")
        values.append(ha.strip())
    subs = _read(subs_path)
    if subs is not None:
        try:
            _walk(json.loads(subs), labels, values)
        except ValueError:
            pass
    return labels, values


def main(argv):
    if len(argv) < 6:
        print(json.dumps({"error": "usage"}))
        return 2
    journalctl, vapid_path, ha_path, subs_path, units = argv[1], argv[2], argv[3], argv[4], argv[5:]
    labels, values = collect(vapid_path, ha_path, subs_path)
    cmd = [journalctl, "-q", "--no-pager", "-o", "cat"]
    for unit in units:
        cmd += ["-u", unit]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"lines": 0, "secrets": len(labels), "leaks": [], "error": exc.__class__.__name__}))
        return 0
    text = proc.stdout
    leaks = sorted({label for i, label in enumerate(labels) if values[i] in text})
    error = "" if proc.returncode == 0 else "journalctl exited %d" % proc.returncode
    print(json.dumps({"lines": len(text.splitlines()), "secrets": len(labels), "leaks": leaks, "error": error}))
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main(sys.argv))
