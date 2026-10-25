# Keep identical in roles/base and roles/watchdog (each role must work on its own);
# roles/base/tests/test_homelab_sysctl.py fails if the two copies differ.
"""Filter: which assignments in one sysctl.d file would not take effect at boot.

Input is the output of `systemd-sysctl --cat-config`: every sysctl.d file, merged in the order
systemd applies them, each one preceded by a "# /path/to/file.conf" header.

The model follows systemd-sysctl (sysctl.d(5) and src/sysctl/sysctl.c):
  * Lines are read in file order into one table keyed by the normalized key. Assigning the
    same value again keeps the earlier entry (and its place). A different value replaces it
    and moves it to the end. A "-key" line (no "=") is an entry with no value: it cancels an
    earlier explicit assignment of that key and excludes the key from every glob.
  * Explicit keys are applied as they stand. A glob is applied, in table order, to every
    matching key that has no explicit (or "-key") entry of its own, so of two globs matching
    the same key, the one later in the table wins.
  * "." and "/" are interchangeable separators when the key's first separator is a dot, and a
    leading "/" is ignored.
"""

import fnmatch
import itertools
import re

_HEADER = re.compile(r"^# (/etc|/run|/usr/local/lib|/usr/lib|/lib)/sysctl\.d/\S+\.conf$")
_GLOB_CHARS = set("*?[")
# Path components a glob's "*" is tried with when checking whether two globs overlap.
_SAMPLES = ("all", "default", "lo", "eth0", "ipv4", "ipv6", "conf", "x")


def _path(key):
    """Normalize a sysctl key to /proc/sys path form (slash separators, no leading slash)."""
    key = key.strip().lstrip("/")
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
            entries.append({"n": number, "file": current, "key": _path(key), "value": value.strip()})
        elif dash:
            entries.append({"n": number, "file": current, "key": _path(body), "value": None})
    return entries


def _table(entries):
    """systemd-sysctl's option table: normalized key -> winning entry, in application order."""
    table = {}
    for entry in entries:
        old = table.get(entry["key"])
        if old is not None and old["value"] == entry["value"]:
            continue  # same value: the earlier entry stays where it is
        table.pop(entry["key"], None)
        table[entry["key"]] = entry  # new or changed: goes to the end
    return table


def _is_glob(key):
    return any(c in _GLOB_CHARS for c in key)


def _expand(pattern):
    """Concrete sample keys a glob pattern matches (each "*" tried with a few component names)."""
    parts = re.split(r"(\*|\?)", pattern)
    slots = [i for i, p in enumerate(parts) if p in ("*", "?")][:3]
    choices = [_SAMPLES if parts[i] == "*" else ("x",) for i in slots]
    for combo in itertools.product(*choices):
        filled = list(parts)
        for i, value in zip(slots, combo):
            filled[i] = value
        yield "".join(filled)


def _globs_overlap(a, b):
    if fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a):
        return True
    return any(fnmatch.fnmatchcase(k, b) for k in _expand(a)) or any(fnmatch.fnmatchcase(k, a) for k in _expand(b))


def _dotted(key):
    return key.replace("/", ".")


def _note(key):
    if key == "kernel/unprivileged_bpf_disabled":
        return (" Once 1 is set the value is locked until reboot, so a later file writing anything"
                " else makes systemd-sysctl.service fail at boot.")
    return ""


def homelab_sysctl_overrides(merged, our_file):
    """Return a list of problems (empty when every assignment in our_file takes effect at boot)."""
    entries = _parse(merged)
    ours = [e for e in entries if e["file"] == our_file and e["value"] is not None]
    if not ours:
        return ["%s is not in the merged sysctl config (or sets nothing)." % our_file]
    table = _table(entries)
    order = list(table)

    problems = []
    for own in ours:
        key, name = own["key"], _dotted(own["key"])
        final = table[key]
        if final["value"] is None:
            problems.append("%s = %s in %s is cancelled by '-%s' in %s.%s" % (
                name, own["value"], our_file, name, final["file"], _note(key)))
            continue
        if final["value"] != own["value"]:
            problems.append("%s = %s in %s is overridden by %s = %s in %s.%s" % (
                name, own["value"], our_file, name, final["value"], final["file"], _note(key)))
            continue
        if not _is_glob(key):
            continue
        # A glob skips every key that has its own entry (explicit, or "-key").
        for other_key, other in table.items():
            if _is_glob(other_key) or not fnmatch.fnmatchcase(other_key, key) or other["value"] == own["value"]:
                continue
            if other["value"] is None:
                problems.append("%s = %s in %s is skipped for %s, which %s excludes from globs." % (
                    name, own["value"], our_file, _dotted(other_key), other["file"]))
            else:
                problems.append("%s = %s in %s is skipped for %s, which %s sets to %s." % (
                    name, own["value"], our_file, _dotted(other_key), other["file"], other["value"]))
        # A different glob later in the table wins on every key both match.
        for other_key in order[order.index(key) + 1:]:
            other = table[other_key]
            if not _is_glob(other_key) or other["value"] in (None, own["value"]):
                continue
            if _globs_overlap(other_key, key):
                problems.append("%s = %s in %s is overridden where it overlaps %s = %s in %s." % (
                    name, own["value"], our_file, _dotted(other_key), other["value"], other["file"]))
    return problems


class FilterModule:
    def filters(self):
        return {"homelab_sysctl_overrides": homelab_sysctl_overrides}
