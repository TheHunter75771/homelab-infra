#!/usr/bin/env python3
"""Offline checks for role edge_ssh, driven by test_edge_ssh.sh. Needs ansible-core (the repo
venv). It renders the role's templates the way Ansible does and evaluates the role's own
assert expressions (read from tasks/*.yml, not copied) against fixtures.

  check_edge_ssh.py render OUTDIR
  check_edge_ssh.py layout SSHD_CONFIG DROPIN_NAME...      -> exit 0 if the preflight passes
  check_edge_ssh.py effective SSHD_T_OUTPUT                -> prints keys that fail, exit 1 if any
  check_edge_ssh.py jail STATUS_FILE ACTIONS_FILE IGNOREIP_FILE
  check_edge_ssh.py ini JAIL_FILE F2B_FILE                 -> parse check of the fail2ban files
"""
import base64
import configparser
import os
import re
import sys

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ROLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def trusted(value):
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, list):
        return [trusted(v) for v in value]
    if isinstance(value, dict):
        return {k: trusted(v) for k, v in value.items()}
    return value


def role_vars():
    v = {"ansible_managed": "Ansible managed"}
    for f in ("defaults/main.yml", "vars/main.yml"):
        with open(os.path.join(ROLE, f)) as fh:
            v.update(trusted(yaml.safe_load(fh)))
    return v


def task(file, name):
    with open(os.path.join(ROLE, "tasks", file)) as fh:
        for t in yaml.safe_load(fh):
            if t.get("name") == name:
                return t
            for sub in t.get("block", []):
                if sub.get("name") == name:
                    return sub
    raise SystemExit(f"task not found: {name}")


def conditions_hold(t, variables):
    """Evaluate a task's vars + assert `that` list the way Ansible does."""
    local = dict(variables)
    for k, v in (t.get("vars") or {}).items():
        local[k] = trusted(v)
    templar = Templar(loader=DataLoader(), variables=local)
    return all(templar.evaluate_conditional(trust_as_template(c)) for c in t["ansible.builtin.assert"]["that"])


def cmd_render(outdir):
    v = role_vars()
    templar = Templar(loader=DataLoader(), variables=v)
    os.makedirs(outdir, exist_ok=True)
    for name in sorted(os.listdir(os.path.join(ROLE, "templates"))):
        with open(os.path.join(ROLE, "templates", name)) as fh:
            src = fh.read()
        # Ansible's template module renders with trim_blocks=True
        out = templar.template(trust_as_template(src), overrides={"trim_blocks": True})
        with open(os.path.join(outdir, name[:-3]), "w") as fh:
            fh.write(out)
        print("rendered", name[:-3])
    return 0


def cmd_layout(config_path, *dropins):
    v = role_vars()
    with open(config_path, "rb") as fh:
        v["edge_ssh_main_config"] = {"content": base64.b64encode(fh.read()).decode()}
    v["edge_ssh_dropins"] = {"files": [{"path": f"/etc/ssh/sshd_config.d/{d}"} for d in dropins]}
    t = task("sshd.yml", "The drop-in directory is included before any line that sets one of our keywords")
    return 0 if conditions_hold(t, v) else 1


def cmd_effective(path):
    v = role_vars()
    with open(path) as fh:
        v["edge_ssh_effective"] = {"stdout_lines": fh.read().splitlines()}
    t = task("sshd.yml", "Every hardened setting is the effective one")
    bad = []
    for key, value in v["edge_ssh_settings"].items():
        if not conditions_hold(t, dict(v, item={"key": key, "value": value})):
            bad.append(key)
    if bad:
        print("not effective:", " ".join(bad))
    return 1 if bad else 0


def cmd_jail(status, actions, ignoreip):
    v = role_vars()
    with open(status) as fh:
        v["edge_ssh_jail_status"] = {"stdout": fh.read()}
    results = []
    for f in (actions, ignoreip):
        with open(f) as fh:
            text = fh.read()
        results.append({"stdout": text, "stdout_lines": text.splitlines()})
    v["edge_ssh_jail_get"] = {"results": results}
    t = task("fail2ban.yml", "The jail bans with the nftables action and ignores loopback and the tailnet")
    return 0 if conditions_hold(t, v) else 1


TIME_RE = re.compile(r"^\d+(s|m|h|d|w)$")
UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def seconds(t):
    return int(t[:-1]) * UNIT_S[t[-1]]


def cmd_ini(jail_file, f2b_file):
    errors = []
    jail = configparser.RawConfigParser(strict=True)
    jail.read(jail_file)
    if jail.sections() != ["sshd"]:
        errors.append(f"jail sections {jail.sections()} != ['sshd']")
    s = jail["sshd"] if jail.has_section("sshd") else {}
    want = {"enabled": "true", "backend": "systemd", "banaction": "nftables", "maxretry": "5",
            "findtime": "10m", "bantime": "1h", "bantime.increment": "true", "bantime.maxtime": "1w"}
    for k, val in want.items():
        if s.get(k) != val:
            errors.append(f"{k} = {s.get(k)!r}, want {val!r}")
    if "[" in s.get("banaction", ""):
        errors.append("banaction must be a plain name (jail.conf appends [port=..., ...])")
    for k in ("findtime", "bantime", "bantime.maxtime"):
        if not TIME_RE.match(s.get(k, "")):
            errors.append(f"{k} is not a fail2ban time: {s.get(k)!r}")
    ignore = s.get("ignoreip", "").split()
    for net in ("100.64.0.0/10", "fd7a:115c:a1e0::/48", "::1"):
        if net not in ignore:
            errors.append(f"ignoreip lacks {net}")
    if not any(n.startswith("127.") for n in ignore):
        errors.append("ignoreip lacks loopback")
    f2b = configparser.RawConfigParser(strict=True)
    f2b.read(f2b_file)
    purge = f2b.get("DEFAULT", "dbpurgeage", fallback="")
    if not TIME_RE.match(purge) or seconds(purge) < seconds(s.get("bantime.maxtime", "0s")):
        errors.append(f"dbpurgeage {purge!r} must be a time >= bantime.maxtime")
    for e in errors:
        print("ini:", e)
    return 1 if errors else 0


if __name__ == "__main__":
    # exit 1 = the check said no; anything unexpected is 3, so a crash never passes for a "no".
    cmd, args = sys.argv[1], sys.argv[2:]
    try:
        rc = {"render": cmd_render, "layout": cmd_layout, "effective": cmd_effective,
              "jail": cmd_jail, "ini": cmd_ini}[cmd](*args)
    except Exception as e:  # noqa: BLE001
        print(f"check_edge_ssh: error: {type(e).__name__}: {e}", file=sys.stderr)
        rc = 3
    sys.exit(rc)
