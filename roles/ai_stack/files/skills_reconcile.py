"""Read-only share mount reconcile for role ai_stack (docs/CONTRACTS.md §12.19, §12.21; Reviewer I R5-1).
Run by homelab-ai-skills-reconcile.service from its timer (root, oneshot). Stdlib only. One probe of the
gaming PC, then the same decision for every mount given with --where (all are shares of that PC).

A soft CIFS mount whose server is gone stays mounted, and Docker stats a bind source at every container
start, so a stale /skills would block open-webui's (re)start while the gaming PC is off. So:
  PC unreachable (TCP connect to <host>:445 fails within --timeout) and the mount active -> stop it
      (the unit has LazyUnmount=yes: the unmount propagates into open-webui, which sees the marker again);
  PC reachable and the mount not active -> start it;
  otherwise nothing.
It never stats, opens or lists a mount path: only a TCP connect and `systemctl`. Prints one line per mount.
Exit 0, or 1 when a systemctl call it needed failed (the others are still done; the service shows failed).
"""

import argparse
import socket
import subprocess
import sys

SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:_.")


def mount_unit(where):
    """systemd-escape --path --suffix=mount, for an absolute path."""
    parts = [p for p in where.split("/") if p]
    if not where.startswith("/") or not parts or any(p in (".", "..") for p in parts):
        raise ValueError(f"not a clean absolute path: {where!r}")
    out = []
    for part in parts:
        out.append("".join(c if c in SAFE else "".join(f"\\x{b:02x}" for b in c.encode()) for c in part))
    name = "-".join(out)
    if name.startswith("."):  # systemd escapes only a leading dot of the whole name
        name = "\\x2e" + name[1:]
    return name + ".mount"


def reachable(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# Each systemctl call returns at once (is-active; start/stop with --no-block only queue the job), so this
# bound only matters if systemd itself is stuck. Worst case per pass: 2 calls x shares x this, well under the
# service's TimeoutStartSec (120 s) for the two shares.
SYSTEMCTL_TIMEOUT = 10


def systemctl(*args):
    return subprocess.run(["systemctl", *args], check=False, capture_output=True, timeout=SYSTEMCTL_TIMEOUT).returncode


def main(argv=None, probe=reachable, run=systemctl):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=445)
    parser.add_argument("--timeout", type=float, default=4.0)
    parser.add_argument(
        "--where", required=True, action="append", help="a mount point (the unit name is derived from it); repeatable"
    )
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 10:
        parser.error("--timeout must be in (0, 10] seconds")
    units = [mount_unit(where) for where in args.where]
    if len(set(units)) != len(units):
        parser.error("--where given twice for the same mount point")

    up = probe(args.host, args.port, args.timeout)
    state = "reachable" if up else "unreachable"
    failed = False
    for unit in units:
        # start/stop --no-block: systemd runs the mount jobs in parallel, each bounded by the mount unit's own
        # TimeoutSec, and this pass returns in seconds (a mount that then fails shows in its own unit). A
        # call that still hangs or can't run is logged, and the next mount is handled anyway.
        active, action = None, "none"
        try:
            active = run("is-active", "--quiet", unit) == 0
            # The action is named before run(), so a call that raises is still logged as that action.
            if not up and active:
                action = "stop"
                rc = run("stop", "--no-block", unit)
            elif up and not active:
                action = "start"
                rc = run("start", "--no-block", unit)
            else:
                rc = 0
            result = "" if rc == 0 else f" FAILED (rc {rc})"
        except (subprocess.TimeoutExpired, OSError) as exc:
            rc, result = -1, f" FAILED ({type(exc).__name__}: {exc})"
        failed = failed or rc != 0
        status = "unknown" if active is None else ("active" if active else "inactive")
        print(f"skills-reconcile: {args.host}:{args.port} {state}, {unit} {status} -> {action}{result}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
