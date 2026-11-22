"""Role unit_hardening, preflight: does the code a unit runs use tailscaled's local API?

Runs on the target as root (python3 -I -c <this file>), reads a JSON request on stdin and prints a JSON report.
Request: {"res": {<name>: <regex>, ...}, "units": {<unit>: [<absolute path>, ...]}, "broad": [<dir>, ...],
          "max_files": <int>, "max_bytes": <int>}
The role asks two questions at once: "uses" (tailscaled's socket or CLI) and "pidns" (code that can't run in a
private PID namespace; judged for root units only).
For each path (taken from the unit's Exec*= and EnvironmentFile= lines by the role's filter plugin):
  - a regular file (symlinks followed) is scanned, and so is every regular file under its directory and under its
    real directory (the app's own tree: /opt/forge-api/forge-api -> /opt/forge-api), unless that directory is
    broad (/, a top-level dir such as /opt or /etc, or one of `broad`: shared bin/lib dirs);
  - a directory argument (data: --dest, --volume, --out) is never walked;
  - a path under /proc, /sys, /dev or /run, or one that doesn't exist, is skipped and reported.
A walk past max_files or max_bytes stops and reports over_limit; an unreadable file is reported. Both make the
preflight refuse (nothing proven). Per regex, each matching file is reported with its distinct matches (lower
case, <= 60 printable chars each, at most 20). Only file names and matched text are printed, never file contents.
"""
import json
import os
import re
import stat
import sys

CHUNK = 1 << 20
OVERLAP = 512
PSEUDO = ("/proc", "/sys", "/dev", "/run")


def under(path, top):
    return path == top or path.startswith(top + "/")


def is_broad(d, broad):
    d = os.path.normpath(d)
    return d.count("/") < 2 or d in broad


def scan_file(path, rxs):
    """{name: sorted distinct matches} for every regex that matches the file. Raises OSError when unreadable.
    Blocks overlap by OVERLAP bytes, so a match across a read boundary is found (and may be seen twice: a set)."""
    found = {name: set() for name in rxs}
    tail = b""
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            buf = tail + block
            for name, rx in rxs.items():
                for m in rx.finditer(buf):
                    if len(found[name]) < 20:
                        text = m.group(0).decode("latin-1").lower()
                        found[name].add("".join(c if c.isprintable() else "?" for c in text)[:60])
            tail = buf[-OVERLAP:]
    return {name: sorted(v) for name, v in found.items() if v}


def scan(p, rxs, rep, seen):
    """Scan one file (symlinks followed, each real file once) into rep."""
    real = os.path.realpath(p)
    if real in seen:
        return
    seen.add(real)
    try:
        st = os.stat(real)
        if not stat.S_ISREG(st.st_mode):
            return
        rep["scanned"] += 1
        rep["bytes"] += st.st_size
        found = scan_file(real, rxs)
    except OSError as e:
        rep["unreadable"].append(f"{real}: {e.strerror}")
        return
    for name, matches in found.items():
        rep["hits"][name].append({"file": real, "matches": matches})


def main():
    req = json.load(sys.stdin)
    rxs = {name: re.compile(pattern.encode(), re.IGNORECASE) for name, pattern in sorted(req["res"].items())}
    broad = {os.path.normpath(b) for b in req.get("broad", [])}
    max_files = int(req.get("max_files", 4000))
    max_bytes = int(req.get("max_bytes", 256 << 20))
    out = {}
    for unit, paths in req["units"].items():
        rep = {"hits": {name: [] for name in rxs}, "unreadable": [], "skipped": [], "walked": [], "files": [],
               "scanned": 0, "bytes": 0, "over_limit": False}
        seen = set()
        walked = set()
        for p in paths:
            if not isinstance(p, str) or not p.startswith("/"):
                rep["skipped"].append(f"{p}: not absolute")
                continue
            if any(under(os.path.normpath(p), t) or under(os.path.realpath(p), t) for t in PSEUDO):
                rep["skipped"].append(f"{p}: pseudo or runtime filesystem")
                continue
            if not os.path.lexists(p):
                rep["skipped"].append(f"{p}: missing")
                continue
            if os.path.isdir(p):
                rep["skipped"].append(f"{p}: directory argument (not walked)")
                continue
            if not os.path.isfile(p):
                rep["skipped"].append(f"{p}: not a regular file")
                continue
            rep["files"].append(p)
            scan(p, rxs, rep, seen)
            for d in {os.path.dirname(os.path.normpath(p)), os.path.dirname(os.path.realpath(p))}:
                if d in walked or is_broad(d, broad):
                    continue
                walked.add(d)
                rep["walked"].append(d)
                for root, dirs, files in os.walk(d, followlinks=False):
                    dirs.sort()
                    for name in sorted(files):
                        if rep["scanned"] >= max_files or rep["bytes"] >= max_bytes:
                            rep["over_limit"] = True
                            break
                        scan(os.path.join(root, name), rxs, rep, seen)
                    if rep["over_limit"]:
                        break
        out[unit] = rep
    json.dump(out, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")


main()
