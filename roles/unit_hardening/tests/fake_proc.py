"""Test helper: change the fake world between converges (the fake systemctl's make_proc builds the /proc entry).
    python3 fake_proc.py <tmp> stale <unit> <pid>    the unit's process restarted outside the role (sees the socket)
    python3 fake_proc.py <tmp> stray <user> <pid>    a process of <user> outside any unit (a login shell, say)
    python3 fake_proc.py <tmp> remove <pid>          that process exited
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_loader = importlib.machinery.SourceFileLoader("fake_systemctl", os.path.join(HERE, "bin", "systemctl"))
_spec = importlib.util.spec_from_loader("fake_systemctl", _loader)
fake = importlib.util.module_from_spec(_spec)
_loader.exec_module(fake)

tmp, action = sys.argv[1], sys.argv[2]
with open(f"{tmp}/state.json") as fh:
    st = json.load(fh)
if action == "stale":
    unit, pid = sys.argv[3], int(sys.argv[4])
    u = st["units"][unit]
    if int(u["props"].get("MainPID", "0")):
        shutil.rmtree(f"{st['proc']}/{u['props']['MainPID']}", ignore_errors=True)
    fake.make_proc(st, pid, False, unit, u["props"].get("User", ""), False)
    u["props"]["MainPID"] = str(pid)
elif action == "stray":
    fake.make_proc(st, int(sys.argv[4]), False, "", sys.argv[3], False)
elif action == "remove":
    shutil.rmtree(f"{st['proc']}/{int(sys.argv[3])}")
else:
    sys.exit(f"unknown action {action}")
with open(f"{tmp}/state.json", "w") as fh:
    json.dump(st, fh, indent=1, sort_keys=True)
