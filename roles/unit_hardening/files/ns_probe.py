"""Role unit_hardening, apply and verify: what does each running unit's mount namespace show at /run/tailscale?

Runs on the target as root (python3 -I -c <this file>), reads a JSON request on stdin and prints a JSON report.
Request: {"proc": "/proc", "root": "", "dir": "/run/tailscale", "socket": "/run/tailscale/tailscaled.sock",
          "pids": {<unit>: <MainPID>, ...}, "users": [<user name or uid>, ...], "passwd": "/etc/passwd",
          "run_allow": {<root unit>: [<path under /run>, ...], ...}}
Per unit (pid > 0), read through /proc/<pid>/root (the process's own view of the filesystem):
  - root_ok:        /proc/<pid>/root/ is a readable directory (the positive control: without it, "no socket"
                    would prove nothing);
  - dir_present:    <dir> is a directory there;
  - socket:         <socket> there: "absent", "present" or "unknown: <error>" (only root sees into the
                    inaccessible directory; anyone else gets EACCES, which never reads as absent);
  - mounted:        /proc/<pid>/mountinfo has a mount whose mount point is <dir> (the InaccessiblePaths mount);
  - own_pidns:      its PID namespace differs from this probe's (PrivatePIDs=yes; root units);
  - run_extra:      (units in run_allow only) every path under its /run that is neither an allowlist entry, nor a
                    directory leading to one (Q8-1: the /run tmpfs holds only the allowlist). An extra directory is
                    listed, not its contents; allowlisted directories aren't descended into. At most 50 are listed;
                    run_truncated says the walk stopped at 500 entries.
Same-uid sweep (Reviewer Q, Q6-3), per user (resolved through <root><passwd>, then the pwd module): every process
whose real, effective, saved or filesystem uid is that user's, with what its /proc/<pid>/root shows at <socket>.
A process of that uid that still sees the socket lets any other process of the uid reach it through
/proc/<pid>/root (same uid: ptrace read access), whatever its own unit hides. A process that exits mid-sweep is
skipped; an unresolvable user is an error.
Host (under "root", "" on a real host): socket_is_socket, dir_is_dir.
Nothing is opened but directory entries and mountinfo; the socket is never connected to.
"""
import json
import os
import stat
import sys


def entry_state(path):
    """'absent', 'present', or 'unknown: <error>' (EACCES and the like: can't tell, so never "absent")."""
    try:
        os.lstat(path)
        return "present"
    except FileNotFoundError:
        return "absent"
    except OSError as e:
        return f"unknown: {e.strerror}"


def mount_points(mountinfo):
    points = set()
    with open(mountinfo, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            fields = line.split(" ")
            if len(fields) > 4:
                # Octal escapes (\040 for a space) as the kernel writes them.
                points.add(fields[4].encode().decode("unicode_escape"))
    return points


def run_extra(run_root, allow):
    """Paths under run_root (shown as /run/...) outside the allowlist, and whether the walk was cut short."""
    allow = {a.rstrip("/") for a in allow}
    extra, seen = [], 0
    for top, dirs, files in os.walk(run_root, followlinks=False):
        rel_top = "/run" + top[len(run_root):]
        keep = []
        for name in sorted(dirs) + sorted(files):
            path = f"{rel_top}/{name}"
            seen += 1
            if seen > 500:
                return extra, True
            if path in allow:
                continue  # an allowlist entry: not descended into
            if any(a.startswith(path + "/") for a in allow):
                if name in dirs:
                    keep.append(name)  # a directory on the way to an entry
                continue
            if len(extra) < 50:
                extra.append(path)
        dirs[:] = [d for d in dirs if d in keep]
    return extra, False


def ns_id(path):
    try:
        return os.readlink(path)
    except OSError as e:
        return f"unknown: {e.strerror}"


def resolve_uid(user, passwd):
    if str(user).isdigit():
        return int(user)
    try:
        with open(passwd, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                f = line.rstrip("\n").split(":")
                if len(f) > 2 and f[0] == user and f[2].isdigit():
                    return int(f[2])
    except OSError:
        pass
    import pwd
    return pwd.getpwnam(user).pw_uid


def proc_uids(base):
    """The process's real, effective, saved and filesystem uids, or None if it is gone."""
    try:
        with open(base + "/status", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("Uid:"):
                    return {int(x) for x in line.split()[1:5]}
    except (FileNotFoundError, ProcessLookupError):
        return None
    return set()


def read_small(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(4096).strip()
    except OSError:
        return ""


def sweep(proc, sock, users, passwd):
    out = {}
    pids = sorted(int(p) for p in os.listdir(proc) if p.isdigit())
    for user in users:
        rep = {"uid": None, "procs": [], "error": ""}
        try:
            rep["uid"] = resolve_uid(user, passwd)
        except (KeyError, OSError) as e:
            rep["error"] = f"unknown user {user!r}: {e}"
            out[str(user)] = rep
            continue
        for pid in pids:
            base = f"{proc}/{pid}"
            uids = proc_uids(base)
            if not uids or rep["uid"] not in uids:
                continue
            state = entry_state(base + "/root" + sock)
            if not os.path.isdir(base):
                continue  # exited while we looked
            cg = read_small(base + "/cgroup").splitlines()
            rep["procs"].append({"pid": pid, "comm": read_small(base + "/comm"), "socket": state,
                                 "cgroup": cg[-1].split(":", 2)[-1] if cg else ""})
        out[str(user)] = rep
    return out


def main():
    req = json.load(sys.stdin)
    proc = req["proc"].rstrip("/")
    tdir = req["dir"]
    sock = req["socket"]
    root = req.get("root", "")
    run_allow = req.get("run_allow", {})
    out = {"units": {}, "host": {}}
    for unit, pid in sorted(req["pids"].items()):
        pid = int(pid)
        rep = {"pid": pid, "root_ok": False, "dir_present": False, "socket": "unknown: not read", "mounted": False,
               "own_pidns": False, "error": ""}
        if pid <= 0:
            rep["error"] = "no process"
            out["units"][unit] = rep
            continue
        base = f"{proc}/{pid}"
        try:
            rep["root_ok"] = os.path.isdir(base + "/root/") and len(os.listdir(base + "/root/")) > 0
            if not rep["root_ok"]:
                rep["error"] = "namespace root not readable"
                out["units"][unit] = rep
                continue
            rep["dir_present"] = os.path.isdir(base + "/root" + tdir)
            rep["socket"] = entry_state(base + "/root" + sock)
            rep["mounted"] = tdir in mount_points(base + "/mountinfo")
            if unit in run_allow:
                rep["run_extra"], rep["run_truncated"] = run_extra(base + "/root/run", run_allow[unit])
            mine, theirs = ns_id(f"{proc}/self/ns/pid"), ns_id(base + "/ns/pid")
            rep["own_pidns"] = not mine.startswith("unknown") and not theirs.startswith("unknown") and mine != theirs
        except OSError as e:
            rep["error"] = f"{e.__class__.__name__}: {e.strerror}"
        out["units"][unit] = rep
    try:
        out["host"]["socket_is_socket"] = stat.S_ISSOCK(os.lstat(root + sock).st_mode)
    except OSError:
        out["host"]["socket_is_socket"] = False
    out["host"]["dir_is_dir"] = os.path.isdir(root + tdir) and not os.path.islink(root + tdir)
    out["sweep"] = sweep(proc, sock, req.get("users", []), root + req.get("passwd", "/etc/passwd"))
    json.dump(out, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")


main()
