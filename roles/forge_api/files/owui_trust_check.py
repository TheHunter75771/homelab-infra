"""Open WebUI trust checks for role forge_api (docs/CONTRACTS.md §12.25; Reviewer Q, Q-1; Reviewer S, S-2/S-3/S-4).

Runs on the HOST (never inside the container): `python3 owui_trust_check.py <webui.db> <approver id>`, with the
path of open-webui's data volume on the host. The database must be a regular file (opened O_NOFOLLOW|O_NONBLOCK
first, so a symlink or a FIFO planted there is refused and nothing blocks); sqlite then opens that same file
READ-ONLY (`mode=ro`, PRAGMA query_only) and it never writes. The whole run is bounded by an alarm.

Open WebUI 0.12.4's layout (models/users.py, models/config.py, models/groups.py, models/tools.py,
models/access_grants.py, utils/access_control):
  - `user` (id, role, settings): settings is JSON, the Interface panel's values under "ui";
  - `config` (key, value), one JSON row per setting: "user.permissions" (DEFAULT_USER_PERMISSIONS, what every
    user gets), "ui.default_interface_settings" (the admin's Interface defaults), "ui.default_group_id" (the group
    every new user joins). Where there is no row, config.py's default applies: the compose file (role ai_stack)
    sets no USER_PERMISSIONS_* variable, so every workspace permission defaults to off;
  - `group` (id, name, permissions JSON, parent_group_id) and `group_member`: a user's permissions are the default
    ones combined with every group's they belong to (directly or inherited), the most permissive value winning;
  - `tool` (id, user_id): workspace tools and their owners;
  - `access_grant` (resource_type, resource_id, principal_type user|group|anyone, principal_id or "*", permission
    read|write).

Truthiness as Open WebUI's own (S-3): a permission is on when Python's bool() of it is true (has_permission
returns bool(value), so even the string "false" is on); iframeSandboxAllowSameOrigin is read by the frontend as
`value ?? false` in JavaScript, so it is on when JS-truthy (anything but null/undefined, false, 0, NaN and "").

The checks (each reported; Ansible decides whether a failure stops the play):
  1. admins: the approver is the only user with role admin (only Noah may approve, §12.25.1);
  2. default-permissions: the default user permissions give no workspace tools (tools, tools_import,
     tools_export) and nothing named like a function;
  3. group-permissions (S-2): no group grants those either (every group counts, members or not: new users join
     the default group, and groups inherit);
  4. tool-write-grants (S-2): no access grant gives anyone (a user, a group or "*") write access to a tool;
  5. tool-owners (S-2): every tool belongs to the approver (another user's tool could be edited by them);
  6. iframe-same-origin: iframeSandboxAllowSameOrigin is not on, neither in the admin's Interface defaults nor in
     any user's own settings (artifacts would reach the Open WebUI origin, and with it the tool's Valve).

Prints one JSON object: {"ok": bool, "checks": [{"name", "ok", "detail"}]}. Exit 0 when the database was read
(whatever the checks say), 2 when it can't be read (missing, not a regular file, locked, not Open WebUI's layout,
timed out).
"""
import json
import math
import os
import signal
import sqlite3
import stat
import sys

DB_TIMEOUT = 5
RUN_TIMEOUT = 30
TOOL_KEYS = ("tools", "tools_import", "tools_export")
CHECKS = ("admins", "default-permissions", "group-permissions", "tool-write-grants", "tool-owners", "iframe-same-origin")


class Unreadable(Exception):
    pass


def py_on(value):
    """Open WebUI's has_permission: bool() of the stored value."""
    return bool(value)


def js_on(value):
    """The frontend's `value ?? false`, then JavaScript truthiness."""
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return not (value == 0 or (isinstance(value, float) and math.isnan(value)))
    if isinstance(value, str):
        return value != ""
    return True  # true, non-empty or empty arrays and objects alike


def as_dict(value):
    if isinstance(value, (str, bytes)):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def tool_keys_on(perms):
    workspace = as_dict(as_dict(perms).get("workspace", {}))
    on = [k for k in TOOL_KEYS if py_on(workspace.get(k))]
    on += [k for k, v in workspace.items() if "function" in k.lower() and py_on(v) and k not in on]
    return sorted(on)


def open_regular(path):
    """Refuse anything but a regular file at path (no symlink, no FIFO or device), without blocking."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise Unreadable(f"cannot open {path}: {exc.strerror}") from None
    try:
        st = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(st.st_mode):
        raise Unreadable(f"{path} is not a regular file")
    return st


def read(db_path):
    before = open_regular(db_path)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=DB_TIMEOUT)
    try:
        con.execute("PRAGMA query_only = ON")
        # The file sqlite opened is the one checked above (not swapped in between).
        after = os.stat(db_path, follow_symlinks=False)
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino) or not stat.S_ISREG(after.st_mode):
            raise Unreadable(f"{db_path} changed while it was opened")
        users = [(uid, role, as_dict(settings) if settings else {})
                 for uid, role, settings in con.execute("SELECT id, role, settings FROM user")]
        rows = {}
        for key, value in con.execute("SELECT key, value FROM config WHERE key IN "
                                      "('user.permissions', 'ui.default_interface_settings', 'ui.default_group_id')"):
            rows[key] = json.loads(value) if isinstance(value, (str, bytes)) else value
        groups = [(gid, name, as_dict(perms) if perms else {})
                  for gid, name, perms in con.execute('SELECT id, name, permissions FROM "group"')]
        tools = list(con.execute("SELECT id, user_id FROM tool"))
        grants = list(con.execute("SELECT resource_id, principal_type, principal_id FROM access_grant "
                                  "WHERE resource_type = 'tool' AND permission = 'write'"))
        return users, rows, groups, tools, grants
    finally:
        con.close()


def check(users, rows, groups, tools, grants, approver):
    checks = []

    admins = sorted(uid for uid, role, _ in users if role == "admin")
    others = [a for a in admins if a != approver]
    checks.append({"name": "admins", "ok": admins == [approver],
                   "detail": f"{len(admins)} admin(s); the approver is {'an' if approver in admins else 'NOT an'} admin"
                             + (f"; other admins: {', '.join(others)}" if others else "")})

    perms = rows.get("user.permissions")
    on = tool_keys_on(perms)
    source = "database" if perms is not None else "default (no row: off)"
    checks.append({"name": "default-permissions", "ok": not on,
                   "detail": (f"workspace permissions on for every user: {', '.join(on)}" if on
                              else f"no workspace tools or functions for users ({source})")})

    default_group = rows.get("ui.default_group_id")
    granting = []
    for gid, name, gperms in groups:
        keys = tool_keys_on(gperms)
        if keys:
            mark = " (the default group for new users)" if gid == default_group else ""
            granting.append(f"{name!s} [{gid}]{mark}: {', '.join(keys)}")
    checks.append({"name": "group-permissions", "ok": not granting,
                   "detail": ("groups granting workspace tools: " + "; ".join(granting)) if granting
                   else f"no group grants workspace tools ({len(groups)} group(s))"})

    writes = [f"tool {rid} -> {ptype}:{pid}" for rid, ptype, pid in grants]
    checks.append({"name": "tool-write-grants", "ok": not writes,
                   "detail": ("write access to tools granted: " + "; ".join(writes)) if writes
                   else "no write grants on tools"})

    foreign = [f"{tid} (owner {uid})" for tid, uid in tools if uid != approver]
    checks.append({"name": "tool-owners", "ok": not foreign,
                   "detail": ("tools owned by someone else: " + "; ".join(foreign)) if foreign
                   else f"{len(tools)} tool(s), all the approver's"})

    defaults = as_dict(rows.get("ui.default_interface_settings") or {})
    default_on = js_on(defaults.get("iframeSandboxAllowSameOrigin"))
    users_on = sum(1 for _, _, s in users if js_on(as_dict(s.get("ui", {})).get("iframeSandboxAllowSameOrigin")))
    checks.append({"name": "iframe-same-origin", "ok": not default_on and users_on == 0,
                   "detail": f"Interface default {'ON' if default_on else 'off'}; {users_on} user(s) with it on"})
    return checks


def on_alarm(_signum, _frame):
    raise Unreadable(f"timed out after {RUN_TIMEOUT} s")


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if len(argv) != 3 or not argv[2]:
        print(json.dumps({"ok": False, "error": "usage: owui_trust_check.py <webui.db> <approver id>"}))
        return 2
    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(RUN_TIMEOUT)
    try:
        data = read(argv[1])
    except (Unreadable, sqlite3.Error, ValueError) as exc:  # not a regular file, locked, no such table, bad JSON
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}))
        return 2
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    checks = check(*data, argv[2])
    print(json.dumps({"ok": all(c["ok"] for c in checks), "checks": checks}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
