"""Static wiring check for role mission_control (Reviewer Q, Q-5): what the task-file tests can't see because they
include the files one by one. Usage: python3 check_wiring.py <role dir>. Prints PASS or each FAIL; exit 1 on any."""
import sys

import yaml

role = sys.argv[1]
load = lambda rel: yaml.safe_load(open(f"{role}/{rel}", encoding="utf-8"))
fails = []


def want(cond, msg):
    if not cond:
        fails.append(msg)


def mod(task, *names):
    for n in names:
        for key in (n, "ansible.builtin." + n):
            if key in task:
                return task[key]
    return None


main = load("tasks/main.yml")
imports = [mod(t, "import_tasks") for t in main if mod(t, "import_tasks")]
want(imports == ["preflight.yml", "base.yml", "venv.yml", "install.yml", "config.yml", "vapid.yml", "config_file.yml",
                 "guard.yml", "service.yml", "serve.yml"], f"tasks/main.yml import order is {imports}")
last = main[-1]
want(mod(last, "include_tasks") == "verify.yml", "the last task of tasks/main.yml must include verify.yml")
want(last.get("when") == ["mission_control_verify | bool", "not ansible_check_mode"],
     f"verify.yml's include must be when [mission_control_verify | bool, not ansible_check_mode], is {last.get('when')}")

defaults = load("defaults/main.yml")
want(defaults.get("mission_control_verify") is True, "mission_control_verify must default to true")

service = load("tasks/service.yml")
started = [mod(t, "systemd_service") for t in service if mod(t, "systemd_service")]
started = [(s.get("name"), s.get("enabled"), s.get("state")) for s in started]
for unit in ("{{ mission_control_guard_unit }}", "{{ mission_control_service }}", "{{ mission_control_briefing_timer }}"):
    want((unit, True, "started") in started, f"tasks/service.yml must enable and start {unit}")
want(not any(n == "{{ mission_control_briefing_service }}" for n, _, _ in started),
     "the briefing service must not be started by a converge (only its timer)")

verify = load("tasks/verify.yml")
active = next((mod(t, "command") for t in verify if "is-active" in str(mod(t, "command"))), None)
want(active is not None, "tasks/verify.yml has no is-active check")
if active:
    argv = active["argv"]
    for unit in ("{{ mission_control_guard_unit }}", "{{ mission_control_service }}", "{{ mission_control_briefing_timer }}"):
        want(unit in argv, f"is-active must cover {unit}")
includes = [mod(t, "include_tasks") for t in verify if mod(t, "include_tasks")]
for f in ("verify_check_config.yml", "verify_vapid_sign.yml", "verify_http.yml", "verify_serve.yml", "verify_guard.yml", "verify_egress.yml",
          "verify_journal.yml"):
    want(f in includes, f"tasks/verify.yml must include {f}")
egress = load("tasks/verify_egress.yml")
eg_includes = [mod(t, "include_tasks") for t in egress if mod(t, "include_tasks")]
want(eg_includes == ["verify_egress_plan.yml", "verify_egress_assert.yml"], f"verify_egress.yml includes {eg_includes}")
want("mission_control_sandbox" in yaml.safe_dump(egress), "the egress probe must run under mission_control_sandbox")
want("mission_control_sandbox" in yaml.safe_dump(load("tasks/verify_check_config.yml")),
     "check-config must run under mission_control_sandbox")

vars_ = load("vars/main.yml")
pip = vars_["mission_control_pip_argv"]
want(pip[1:4] == ["-I", "-m", "pip"], f"pip must run as `python -I -m pip`, is {pip}")
want(pip[0] == "{{ mission_control_venv_new_python }}", "pip must install into venv.new, never the live venv")
want("--require-hashes" in vars_["mission_control_pip_common"], "pip must use --require-hashes")
want(vars_["mission_control_pip_build_args"] == ["--only-binary=:all:"], "build.lock: wheels only")
want("nftables" in vars_["mission_control_packages"], "nftables must be installed (the guard's nft; Q2-3)")
base = load("tasks/base.yml")
want(any(mod(t, "apt") and mod(t, "apt").get("name") == "{{ mission_control_packages }}" for t in base),
     "tasks/base.yml must apt-install mission_control_packages")

handlers = [h["name"] for h in load("handlers/main.yml")]
want(handlers[0] == "Reload systemd for mission-control", "the daemon-reload handler must run first")
want(handlers.index("Reload mission-control guard") < handlers.index("Restart mission-control"),
     "the guard must be reloaded before the server restarts")

for f in fails:
    print("FAIL:", f)
print("PASS: wiring" if not fails else f"{len(fails)} wiring problem(s)")
sys.exit(1 if fails else 0)
