# Keep identical in roles/base and roles/watchdog (each role must work on its own).
"""Filter: which assignments in one sysctl.d file would not take effect at boot.

Input is the output of `systemd-sysctl --cat-config`: every sysctl.d file, merged in the order
systemd applies them, each one preceded by a "# /path/to/file.conf" header. sysctl.d(5) rules:
  * files are applied in filename order, so the last assignment to a key wins;
  * a glob key applies to every matching key, except keys that have an explicit assignment
    anywhere, and keys excluded with a "-key" line (no "=");
  * "." and "/" are interchangeable separators when the key's first separator is a dot.
"""

import fnmatch
import re

_HEADER = re.compile(r"^# (/etc|/run|/usr/local/lib|/usr/lib|/lib)/sysctl\.d/\S+\.conf$")
_GLOB_CHARS = set("*?[")


def _path(key):
    """Normalize a sysctl key to /proc/sys path form (slash separators)."""
    key = key.strip()
    seps = [i for i in (key.find("."), key.find("/")) if i >= 0]
    if seps and key[min(seps)] == ".":
        # dotted form: dots and slashes are interchanged
        key = key.replace(".", "\0").replace("/", ".").replace("\0", "/")
    return key


def _parse(merged):
    entries = []
    current = None
    for number, raw in enumerate(merged.splitlines()):
        line = raw.strip()
        if _HEADER.match(line):
            current = line[2:]
            continue
        if not line or line[0] in "#;" or current is None:
            continue
        dash = line.startswith("-")
        body = line[1:] if dash else line
        if "=" in body:
            key, value = body.split("=", 1)
            entries.append({"n": number, "file": current, "kind": "assign", "key": _path(key), "value": value.strip()})
        elif dash:
            entries.append({"n": number, "file": current, "kind": "exclude", "key": _path(body), "value": None})
    return entries


def _is_glob(key):
    return any(c in _GLOB_CHARS for c in key)


def _note(key):
    if key == "kernel/unprivileged_bpf_disabled":
        return (" Once 1 is set the value is locked until reboot, so a later file writing anything"
                " else makes systemd-sysctl.service fail at boot.")
    return ""


def homelab_sysctl_overrides(merged, our_file):
    """Return a list of problems (empty when every assignment in our_file wins at boot)."""
    entries = _parse(merged)
    ours = [e for e in entries if e["file"] == our_file and e["kind"] == "assign"]
    if not ours:
        return ["%s is not in the merged sysctl config (or sets nothing)." % our_file]

    def last_explicit(key):
        found = [e for e in entries if e["kind"] == "assign" and e["key"] == key]
        return found[-1] if found else None

    problems = []
    for own in ours:
        dotted = own["key"].replace("/", ".")
        # The last assignment to this exact key (glob string or explicit key) must be ours.
        last = last_explicit(own["key"])
        if last is not own:
            problems.append("%s = %s in %s is overridden by %s = %s in %s.%s" % (
                dotted, own["value"], our_file, dotted, last["value"], last["file"], _note(own["key"])))
            continue
        if not _is_glob(own["key"]):
            continue
        # A glob never touches keys that are set explicitly or excluded; those must end up
        # with our value some other way, or the glob silently skips them.
        for e in entries:
            if _is_glob(e["key"]) or not fnmatch.fnmatchcase(e["key"], own["key"]):
                continue
            final = last_explicit(e["key"])
            if final is not None and final["value"] == own["value"]:
                continue
            if e["kind"] == "assign" and e is final:
                problems.append("%s = %s in %s is skipped for %s, which %s sets to %s." % (
                    dotted, own["value"], our_file, e["key"].replace("/", "."), e["file"], e["value"]))
            elif e["kind"] == "exclude" and final is None:
                problems.append("%s = %s in %s is skipped for %s, which %s excludes from globs." % (
                    dotted, own["value"], our_file, e["key"].replace("/", "."), e["file"]))
    return problems


class FilterModule:
    def filters(self):
        return {"homelab_sysctl_overrides": homelab_sysctl_overrides}
