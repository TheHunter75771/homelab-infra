"""Write a copy of the role's requirements.lock with pywebpush moved to 2.5.1 and every hash of that entry replaced by
64 zeros (Reviewer Q, Q-4: a "flipped hash"; tests/bin/fake-pip refuses such a lock as pip --require-hashes would).
Usage: python3 flip_lock.py <lock> <out>"""
import re
import sys

out, inside = [], False
for line in open(sys.argv[1], encoding="utf-8").read().splitlines():
    if line.startswith("pywebpush=="):
        line, inside = "pywebpush==2.5.1 \\", True
    elif inside and line.lstrip().startswith("--hash="):
        line = re.sub(r"sha256:[0-9a-f]{64}", "sha256:" + "0" * 64, line)
    else:
        inside = False
    out.append(line)
open(sys.argv[2], "w", encoding="utf-8").write("\n".join(out) + "\n")
