"""(a) Render every template with the role defaults, the committed group_vars and placeholder host vars,
and check them against docs/CONTRACTS.md §13.1: the Radicale config parses and holds every option,
the units carry the contract's directives (and the dashboard's full hardening set), and the playbook
and tasks keep their promises (targeted deploy, no_log on every password).

    python3 -m unittest discover -s roles/calendar/tests -v
"""

from __future__ import annotations

import configparser
import importlib.util
import pathlib
import re
import shlex
import subprocess
import tempfile
import unittest

import role_render as rr

HOOK = 'git add -A && (git diff --cached --quiet || git commit -q -m "Changes by "%(user)s)'
CONTRACT_OPTIONS = {
    "server": {"hosts": "127.0.0.1:5232", "max_connections": "8", "max_content_length": "10000000", "timeout": "30"},
    "auth": {"type": "htpasswd", "htpasswd_filename": "/etc/homelab-calendar/users", "htpasswd_encryption": "bcrypt",
             "delay": "1", "realm": "Calendar"},
    # §14.1: from_file, with rules that give accounts exactly owner_only (test_rights.py), plus viewers.
    "rights": {"type": "from_file", "file": "/etc/homelab-calendar/rights",
               "permit_delete_collection": "False", "permit_overwrite_collection": "False"},
    "storage": {"type": "multifilesystem", "filesystem_folder": "/srv/homelab/data/calendar/collections", "hook": HOOK},
    "web": {"type": "none"},
    "logging": {"level": "info", "mask_passwords": "True"},
}
# Set beyond the contract's list, with the reason in config.j2.
EXTRA_OPTIONS = {("server", "max_resource_size"): "8000000"}
ALERT_PUSH = ('/usr/local/bin/homelab-notify --tag calendar --title "Calendar is down" '
              '--body "homelab-calendar.service failed. See: journalctl -u homelab-calendar"')


class Rendered(unittest.TestCase):
    def setUp(self) -> None:
        rr.require_tools()
        self.vars = rr.load_vars()

    # --- defaults --------------------------------------------------------------------------------------
    def test_defaults(self) -> None:
        v = self.vars
        self.assertEqual(v["calendar_radicale_series"], "3.6")  # versions.yml pins 3.6.0
        self.assertEqual(v["calendar_port"], 5232)
        self.assertEqual(v["calendar_user"], "calendar")
        self.assertEqual(v["calendar_config_dir"], "/etc/homelab-calendar")
        self.assertEqual(v["calendar_data_dir"], "/srv/homelab/data/calendar")
        self.assertEqual(v["calendar_notify_bin"], "/usr/local/bin/homelab-notify")
        self.assertEqual(v["calendar_packages"], ["python3-radicale", "python3-bcrypt", "python3-passlib", "git"])
        defaults = rr.yaml.safe_load((rr.ROLE / "defaults/main.yml").read_text())
        self.assertEqual(defaults["calendar_accounts"], [])
        self.assertNotIn("@", defaults["calendar_git_name"])
        self.assertTrue(defaults["calendar_git_email"].endswith(".invalid"), "placeholder identity only")

    def test_refused_names_match_what_the_backup_skips(self) -> None:
        backups = rr.yaml.safe_load((rr.REPO / "roles/backups/defaults/main.yml").read_text())
        self.assertEqual(self.vars["calendar_backup_excluded_names"], backups["backups_exclude_dirs"])

    def test_the_series_follows_the_pin(self) -> None:
        v = rr.load_vars({"python_packages": {"radicale": "3.7.2"}})
        self.assertEqual(v["calendar_radicale_series"], "3.7")

    # --- config ----------------------------------------------------------------------------------------
    def parse_config(self) -> configparser.RawConfigParser:
        parser = configparser.RawConfigParser(strict=True)  # what Radicale uses: no interpolation
        parser.read_string(rr.render("config.j2", self.vars))
        return parser

    def test_the_config_holds_every_contract_option(self) -> None:
        parser = self.parse_config()
        self.assertEqual(sorted(parser.sections()), sorted(CONTRACT_OPTIONS))
        for section, options in CONTRACT_OPTIONS.items():
            for option, value in options.items():
                with self.subTest(option=f"[{section}] {option}"):
                    self.assertEqual(parser.get(section, option), value)
        rendered = {(s, o) for s in parser.sections() for o in parser.options(s)}
        wanted = {(s, o) for s, opts in CONTRACT_OPTIONS.items() for o in opts}
        self.assertEqual(rendered - wanted, set(EXTRA_OPTIONS), "options set beyond the contract")
        for (section, option), value in EXTRA_OPTIONS.items():
            self.assertEqual(parser.get(section, option), value)

    def test_the_rights_file(self) -> None:
        """Owner rules for exactly the accounts, then one r rule per viewer (none by default)."""
        def sections(variables: dict) -> dict:
            parser = configparser.ConfigParser()  # what Radicale's from_file uses
            parser.read_string(rr.render("rights.j2", variables))
            return {name: dict(parser[name]) for name in parser.sections()}

        owners = {
            "owner-root": {"user": "(?:alice|bob|household)", "collection": "", "permissions": "R"},
            "owner-home": {"user": "(?:alice|bob|household)", "collection": "{user}", "permissions": "RW"},
            "owner-calendars": {"user": "(?:alice|bob|household)", "collection": "{user}/[^/]+", "permissions": "rw"},
        }
        self.assertEqual(sections(self.vars), {**owners, "viewer-viewer": {
            "user": "viewer", "collection": "(?:household/shared|household/school|alice/routines)", "permissions": "r"}})
        self.assertEqual(sections(rr.load_vars({"calendar_viewers": []})), owners)
        self.assertEqual(rr.yaml.safe_load((rr.ROLE / "defaults/main.yml").read_text())["calendar_viewers"], [])
        # A viewer never gets more than r, and the owner rules never name a viewer.
        for name, rule in sections(self.vars).items():
            if name.startswith("viewer-"):
                self.assertEqual(rule["permissions"], "r")
            else:
                self.assertNotIn("viewer", rule["user"])

    def test_the_rights_validate_step(self) -> None:
        """The template task's own validate command: accepts the rendered file, refuses broken ones
        (from_file itself would only fail later, with a 500 per request that reaches the rule)."""
        import sys

        task = next(t for t in rr.load_tasks("config.yml") if t["name"].startswith("Config | rights"))
        command = task["ansible.builtin.template"]["validate"]
        good = rr.render("rights.j2", self.vars)
        broken = {
            "an unclosed group": good.replace("alice/routines)", "alice/routines"),
            "an unknown placeholder": good.replace("{user}/[^/]+", "{login}/[^/]+"),
            "no permissions": good.replace("permissions: r\n", ""),
        }
        for what, text in broken.items():
            self.assertNotEqual(text, good, f"the {what} mutation changed nothing")
        with tempfile.TemporaryDirectory() as tmp:
            for what, text in {"the rendered file": good, **broken}.items():
                with self.subTest(case=what):
                    path = pathlib.Path(tmp, "rights")
                    path.write_text(text)
                    argv = shlex.split(command % path)
                    self.assertEqual(argv[0], "/usr/bin/python3")
                    r = subprocess.run([sys.executable, *argv[1:]], capture_output=True, text=True)
                    self.assertEqual(r.returncode == 0, what == "the rendered file", r.stderr[-300:])

    def test_the_hook_reaches_radicale_unescaped_and_fills_in_the_login(self) -> None:
        hook = self.parse_config().get("storage", "hook")
        self.assertIn("%(user)s", hook)
        # Radicale's own substitution (storage/multifilesystem/lock.py): %-format, shell-quoted values.
        values = {k: shlex.quote(v) for k, v in
                  {"path": "/x", "to_path": "", "cwd": "/x", "request": "PUT", "user": "alice"}.items()}
        self.assertEqual(hook % values,
                         'git add -A && (git diff --cached --quiet || git commit -q -m "Changes by "alice)')

    def test_the_hook_keeps_any_login_one_argument(self) -> None:
        """Defence in depth: the hook run through /bin/sh as Radicale runs it (%-filled with
        shell-quoted values), with a stub git that records the commit's argv."""
        hook = self.parse_config().get("storage", "hook")
        with tempfile.TemporaryDirectory() as tmp:
            stub = pathlib.Path(tmp, "git")
            stub.write_text('#!/bin/sh\n[ "$1" = diff ] && exit 1\n[ "$1" = commit ] && printf "%s\\n" "$@" > argv\nexit 0\n')
            stub.chmod(0o755)
            for login in ("alice", 'a"b', "x; touch pwned", "$(touch pwned)", "it's", "a b"):
                with self.subTest(login=login):
                    values = {k: shlex.quote(v) for k, v in
                              {"path": tmp, "to_path": "", "cwd": tmp, "request": "PUT", "user": login}.items()}
                    subprocess.run(["/bin/sh", "-c", hook % values], cwd=tmp, check=True,
                                   env={"PATH": f"{tmp}:/usr/bin:/bin"})
                    self.assertEqual(pathlib.Path(tmp, "argv").read_text().splitlines(),
                                     ["commit", "-q", "-m", f"Changes by {login}"])
                    self.assertFalse(pathlib.Path(tmp, "pwned").exists())

    def test_the_config_follows_the_port_and_paths(self) -> None:
        v = rr.load_vars({"calendar_port": 5299, "homelab_data_dir": "/srv/other/data"})
        parser = configparser.RawConfigParser()
        parser.read_string(rr.render("config.j2", v))
        self.assertEqual(parser.get("server", "hosts"), "127.0.0.1:5299")
        self.assertEqual(parser.get("storage", "filesystem_folder"), "/srv/other/data/calendar/collections")

    @unittest.skipUnless(importlib.util.find_spec("radicale"), "radicale not installed")
    def test_radicale_loads_the_config(self) -> None:
        import tempfile

        from radicale import config

        with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
            f.write(rr.render("config.j2", self.vars))
            f.flush()
            cfg = config.load([(f.name, False)])
        self.assertEqual(cfg.get("server", "hosts"), [("127.0.0.1", 5232)])
        self.assertIs(cfg.get("rights", "permit_delete_collection"), False)
        self.assertIs(cfg.get("rights", "permit_overwrite_collection"), False)
        self.assertEqual(cfg.get("storage", "hook"), HOOK)
        self.assertEqual(cfg.get("web", "type"), "none")
        self.assertIs(cfg.get("logging", "mask_passwords"), True)

    # --- units -----------------------------------------------------------------------------------------
    def test_the_service_unit(self) -> None:
        text = rr.render("homelab-calendar.service.j2", self.vars)
        d = rr.directives(text)
        expect = {
            "Type": ["exec"],
            "User": ["calendar"],
            "Group": ["calendar"],
            "ExecStart": ["/usr/bin/python3 -m radicale --config /etc/homelab-calendar/config"],
            # "+" root (the calendar account can't start units), "-" a refused queueing never fails
            # this start, --no-block never waits (the recovered unit is ordered after this one).
            "ExecStartPost": ["-+/usr/bin/systemctl start --no-block homelab-calendar-recovered.service"],
            "Restart": ["always"],
            "RestartSec": ["5s"],
            # Without it (RestartMode=normal) every failed start passes through "failed" and fires
            # OnFailure=: 5 "Calendar is down" calls per incident, and one for a crash that heals.
            "RestartMode": ["direct"],
            "StartLimitIntervalSec": ["600"],
            "StartLimitBurst": ["5"],
            "After": ["network.target"],
            "OnFailure": ["homelab-calendar-alert.service"],
            "ReadWritePaths": ["/srv/homelab/data/calendar"],
            "IPAddressDeny": ["any"],
            "IPAddressAllow": ["localhost"],
            "MemoryDenyWriteExecute": ["yes"],
            "UMask": ["0027"],
            "MemoryMax": ["192M"],
            "TasksMax": ["64"],
            "WantedBy": ["multi-user.target"],
        }
        for key, value in expect.items():
            with self.subTest(directive=key):
                self.assertEqual(d.get(key), value)
        # The start limit lives in [Unit] (systemd ignores it in [Service]).
        unit_section = text.split("[Service]")[0]
        self.assertIn("\nStartLimitIntervalSec=600\n", unit_section)
        self.assertIn("\nStartLimitBurst=5\n", unit_section)
        self.assertIn("\nRestartMode=direct\n", text.split("[Service]")[1].split("[Install]")[0])
        # The push no longer runs here (this unit's IP filter would drop it): no script settings either.
        self.assertNotIn("Environment", d)
        self.assertNotIn("/usr/local/sbin/homelab-calendar-recovered", text)

    def test_no_ordering_on_docker_or_the_gate(self) -> None:
        d = rr.directives(rr.render("homelab-calendar.service.j2", self.vars))
        for key in ("After", "Before", "Requires", "Wants", "BindsTo", "Requisite", "PartOf"):
            for value in d.get(key, []):
                with self.subTest(directive=key):
                    self.assertNotRegex(value, r"docker|bootgate")

    def test_the_dashboards_full_hardening_set(self) -> None:
        dash = (rr.REPO / "roles/dashboard/templates/homelab-dashboard.service.j2").read_text()
        block = dash.split("# Hardening", 1)[1].split("[Install]", 1)[0]
        dash_dirs = rr.directives(block)
        self.assertGreater(len(dash_dirs), 20, "the dashboard's hardening block moved; update this test")
        cal = rr.directives(rr.render("homelab-calendar.service.j2", self.vars))
        for key, values in dash_dirs.items():
            if key == "ReadWritePaths":
                continue
            with self.subTest(directive=key):
                self.assertEqual(cal.get(key), values)

    def test_the_measured_scores_are_recorded(self) -> None:
        for name in ("homelab-calendar.service.j2", "homelab-calendar-recovered.service.j2"):
            with self.subTest(unit=name):
                m = re.search(r"systemd-analyze security[^:]*: (\d+\.\d+)", rr.render(name, self.vars))
                self.assertIsNotNone(m, "put the measured systemd-analyze security score in the header comment")
                self.assertLessEqual(float(m.group(1)), 2.0)

    def test_the_recovered_unit(self) -> None:
        """CONTRACTS §13.1 (amended): the push runs in its own unit, outside the calendar's IP filter."""
        text = rr.render("homelab-calendar-recovered.service.j2", self.vars)
        d = rr.directives(text)
        alert = rr.directives(rr.render("homelab-calendar-alert.service.j2", self.vars))
        self.assertEqual(d["Type"], ["oneshot"])
        self.assertEqual(d["ExecStart"], ["/usr/local/sbin/homelab-calendar-recovered"])
        self.assertEqual(d["StateDirectory"], alert["StateDirectory"], "the marker dir the alert unit writes")
        self.assertEqual(d["SyslogIdentifier"], ["homelab-calendar-recovered"])
        # Network up for the push, and after the calendar's own start job: queued with --no-block, so
        # it runs once the calendar is active and the calendar never waits for it.
        self.assertEqual(d["After"], ["network-online.target homelab-calendar.service"])
        self.assertEqual(d["Wants"], ["network-online.target"])
        for key in ("Requires", "BindsTo", "Requisite", "PartOf", "Before"):
            self.assertNotIn(key, d, "it must never be able to stop or hold up the calendar")
        self.assertEqual(d["StartLimitIntervalSec"], ["0"], "a rate-limited start would swallow the push")
        self.assertNotIn("WantedBy", d, "never enabled: only the calendar's ExecStartPost= starts it")
        # The internet stays reachable: no IP filter, no private network, IP sockets allowed.
        for key in ("IPAddressDeny", "IPAddressAllow", "PrivateNetwork"):
            self.assertNotIn(key, d)
        self.assertEqual(d["RestrictAddressFamilies"], ["AF_UNIX AF_INET AF_INET6"])
        # Root with no capabilities: the push store through its group, writes only the notifier's state.
        self.assertEqual(d["CapabilityBoundingSet"], [""])
        self.assertEqual(d["AmbientCapabilities"], [""])
        self.assertEqual(d["SupplementaryGroups"], ["dashboard"])
        self.assertEqual(d["ProtectSystem"], ["strict"])
        self.assertEqual(d["ReadWritePaths"], ["-/var/lib/homelab-notify"])
        self.assertNotIn("User", d)
        for key in ("NoNewPrivileges", "ProtectHome", "PrivateTmp", "PrivateDevices", "MemoryDenyWriteExecute"):
            with self.subTest(directive=key):
                self.assertEqual(d.get(key), ["yes"])

    def test_the_alert_unit(self) -> None:
        d = rr.directives(rr.render("homelab-calendar-alert.service.j2", self.vars))
        self.assertEqual(d["Type"], ["oneshot"])
        self.assertEqual(d["StateDirectory"], ["homelab-calendar-alert"])
        # Marker first, so a failed push still leaves it; then the contract's exact push.
        self.assertEqual(d["ExecStart"], ["/usr/bin/touch /var/lib/homelab-calendar-alert/down", ALERT_PUSH])
        self.assertEqual(d["SuccessExitStatus"], ["2"])
        self.assertEqual(self.vars["calendar_alert_marker"], "/var/lib/homelab-calendar-alert/down")

    def test_root_without_capabilities_can_read_the_store_and_write_its_state(self) -> None:
        """The recovered unit's design rests on other modules' modes; a change there fails here."""
        dash = rr.yaml.safe_load((rr.REPO / "roles/dashboard/defaults/main.yml").read_text())
        self.assertEqual(self.vars["calendar_push_group"], dash["dashboard_user"], "the push store's group")
        store = {t["name"]: t for t in rr.yaml.safe_load((rr.REPO / "roles/dashboard/tasks/push_store.yml").read_text())}
        modes = {}
        for task in store.values():
            args = task.get("ansible.builtin.file") or task.get("ansible.builtin.copy")
            target = args.get("path") or args.get("dest")
            modes[target] = (args["group"], args["mode"])
        # Group-readable by dashboard_gid (the dashboard account's own group): dir 0750, files 0640.
        self.assertEqual(modes["{{ homelab_push_dir }}"], ("{{ dashboard_gid }}", "0750"))
        self.assertEqual(modes["{{ homelab_push_dir }}/vapid.json"], ("{{ dashboard_gid }}", "0640"))
        self.assertEqual(modes["{{ homelab_push_dir }}/subscriptions.json"], ("{{ dashboard_gid }}", "0640"))
        # The dashboard rewrites subscriptions.json itself, and keeps 0640 whatever its UMask.
        js = (rr.REPO / "apps/dashboard/server/src/push/store.js").read_text()
        self.assertRegex(js, r"FILE_MODE = 0o640;")
        self.assertIn("fs.fchmodSync(fd, FILE_MODE)", js)
        # Everything homelab-notify writes is root's own (role notifier): no DAC override needed.
        notifier = rr.yaml.safe_load((rr.REPO / "roles/notifier/tasks/main.yml").read_text())
        owned = {t["ansible.builtin.file"]["path"]: (t["ansible.builtin.file"]["owner"], t["ansible.builtin.file"]["mode"])
                 for t in notifier if "ansible.builtin.file" in t and "owner" in t["ansible.builtin.file"]}
        self.assertEqual(owned["{{ notifier_state_dir }}"], ("root", "0755"))
        self.assertEqual(owned["{{ notifier_state_dir }}/history.jsonl"], ("root", "0644"))
        defaults = rr.yaml.safe_load((rr.REPO / "roles/notifier/defaults/main.yml").read_text())
        self.assertEqual(defaults["notifier_state_dir"], self.vars["calendar_notify_state_dir"])

    def test_the_recovery_title_closes_the_alert_on_the_dashboard(self) -> None:
        """The dashboard closes "Calendar is down" only with a later same-tag push that isRecovery() accepts."""
        is_recovery = dashboard_is_recovery()
        title = unit_environment(rr.render("homelab-calendar-recovered.service.j2", self.vars))["CALENDAR_RECOVERED_TITLE"]
        self.assertEqual(title, self.vars["calendar_recovered_title"])
        down = shlex.split(rr.directives(rr.render("homelab-calendar-alert.service.j2", self.vars))["ExecStart"][1])
        down_title, down_tag = down[down.index("--title") + 1], down[down.index("--tag") + 1]
        self.assertEqual(down_tag, "calendar", "the recovery must use the down alert's tag")
        self.assertTrue(is_recovery(title), f"{title!r} wouldn't close the alert on the dashboard")
        self.assertTrue(is_recovery("[REHEARSAL] " + title), "the notifier's rehearsal prefix keeps it a recovery")
        self.assertFalse(is_recovery(down_title), f"{down_title!r} would close itself")
        # The translation itself: the live bug, and the dashboard's own documented examples.
        self.assertFalse(is_recovery("Calendar back up"))
        for example in ("Media drive back", "jellyfin recovered", "mediaserver back online", "x healthy again"):
            self.assertTrue(is_recovery(example), example)
        self.assertFalse(is_recovery("Calendar still not back online"))

    def test_the_recovered_script_reads_what_the_unit_sets(self) -> None:
        script = (rr.ROLE / "files/homelab-calendar-recovered.sh").read_text()
        reads = dict(re.findall(r"\$\{(CALENDAR_[A-Z_]+):-([^}]*)\}", script))
        env = unit_environment(rr.render("homelab-calendar-recovered.service.j2", self.vars))
        self.assertEqual(set(reads), set(env), "every setting the script reads comes from the unit")
        for key, default in reads.items():
            with self.subTest(env=key):
                self.assertEqual(env[key], default, "the script's fallback matches the role's default")

    def test_the_gitignore(self) -> None:
        lines = [x for x in rr.render("collections.gitignore.j2", self.vars).splitlines() if x and not x.startswith("#")]
        self.assertEqual(lines, [".Radicale.cache", ".Radicale.lock", ".Radicale.tmp-*"])


def unit_environment(unit_text: str) -> dict[str, str]:
    """Environment= lines as systemd reads them: whitespace-separated assignments, double quotes kept together."""
    return dict(a.split("=", 1) for v in rr.directives(unit_text).get("Environment", []) for a in shlex.split(v))


def dashboard_is_recovery():
    """The dashboard's isRecovery(), translated from apps/dashboard/server/src/collectors/alerts.js:
    RECOVERY_END_RE and QUALIFIER_RE as Python re (the JS patterns are compatible), same logic."""
    js = (rr.REPO / "apps/dashboard/server/src/collectors/alerts.js").read_text()

    def pattern(name: str) -> re.Pattern:
        m = re.search(rf"const {name}\s*=\s*/(.+?)/([a-z]*);", js, re.DOTALL)
        assert m, f"{name} is gone from alerts.js"
        assert set(m.group(2)) <= {"i"}, f"{name} gained flags {m.group(2)!r}; translate them"
        return re.compile(m.group(1), re.IGNORECASE if "i" in m.group(2) else 0)

    body = re.search(r"export function isRecovery\(title\) \{(.*?)\n\}", js, re.DOTALL)
    assert body and "return RECOVERY_END_RE.test(t) && !QUALIFIER_RE.test(t);" in body.group(1), \
        "isRecovery's logic changed; update this translation"
    recovery, qualifier = pattern("RECOVERY_END_RE"), pattern("QUALIFIER_RE")
    return lambda title: bool(recovery.search(title.strip())) and not qualifier.search(title.strip())


# --- no_log guard -----------------------------------------------------------------------------------
SECRET_ROOTS = {"calendar_passwords"}
# Derived from the passwords but account names only (dict keys): preflight prints it in its fail_msg.
NAMES_ONLY = {"calendar_password_ok"}
# Keys whose whole value is a Jinja expression (no braces), at any depth of a task.
RAW_EXPRESSION_KEYS = {"when", "that", "until", "failed_when", "changed_when", "var"}


def role_tasks() -> list[tuple[str, dict]]:
    """Every task of the role (tasks/ and handlers/, blocks flattened) as (name, task)."""
    def walk(items):
        for task in items or []:
            yield task
            for key in ("block", "rescue", "always"):
                yield from walk(task.get(key))

    return [(t.get("name", "?"), t) for d in ("tasks", "handlers") for path in sorted((rr.ROLE / d).glob("*.yml"))
            for t in walk(rr.yaml.safe_load(path.read_text()))]


def expressions(value, key: str = "") -> list[str]:
    """The Jinja a task evaluates: {{ }} and {% %} inside strings, plus whole raw-expression values.
    A name in plain text (a message, a task name) is a mention, not a use."""
    if isinstance(value, dict):
        return [e for k, v in value.items() for e in expressions(v, k)]
    if isinstance(value, list):
        return [e for v in value for e in expressions(v, key)]
    if not isinstance(value, str):
        return []
    if key in RAW_EXPRESSION_KEYS:
        return [value]
    return re.findall(r"\{\{.*?\}\}|\{%.*?%\}", value, re.DOTALL)


def uses(task: dict, names: set[str]) -> bool:
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, sorted(names))) + r")\b")
    return any(pattern.search(e) for e in expressions(task))


def tasks_touching_secrets(tasks: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """Tasks that use calendar_passwords, or any variable derived from it (set_fact keys, a register,
    task vars), followed to a fixpoint. NAMES_ONLY variables don't carry the secret on."""
    secret = set(SECRET_ROOTS)
    while True:
        derived = set(secret)
        for _, task in tasks:
            if uses(task, secret):
                derived |= {task["register"]} if "register" in task else set()
                derived |= set(task.get("vars") or {})
                facts = task.get("ansible.builtin.set_fact") or task.get("set_fact") or {}
                derived |= {k for k in facts if k != "cacheable"} - NAMES_ONLY
        if derived == secret:
            return [(name, task) for name, task in tasks if uses(task, secret) or set(task.get("vars") or {}) & secret]
        secret = derived


def no_log_violations(tasks: list[tuple[str, dict]]) -> list[str]:
    return [name for name, task in tasks_touching_secrets(tasks) if task.get("no_log") is not True]


class Static(unittest.TestCase):
    """The playbook and tasks (PyYAML only)."""

    def setUp(self) -> None:
        rr.require_tools(jinja=False)

    def test_the_playbook_is_the_targeted_deploy(self) -> None:
        text = (rr.REPO / "playbooks/calendar.yml").read_text()
        plays = rr.yaml.safe_load(text)
        self.assertEqual(len(plays), 1)
        self.assertEqual(plays[0]["hosts"], "media_servers")
        self.assertIs(plays[0]["become"], True)
        # §14.3: the view (module R) after the calendar it reads, before the serve config that publishes both.
        self.assertEqual([r["role"] for r in plays[0]["roles"]], ["calendar", "calendar_view", "tailscale_serve"])
        self.assertIn("touches nothing else", text)

    def test_every_task_that_touches_a_password_has_no_log(self) -> None:
        tasks = role_tasks()
        self.assertEqual(no_log_violations(tasks), [])
        touching = [name for name, _ in tasks_touching_secrets(tasks)]
        self.assertEqual(len(touching), 5, f"review this list if it changed: {touching}")
        # The one derived name the preflight may print: provably account names only (dict keys).
        for name, task in tasks:
            facts = task.get("ansible.builtin.set_fact") or {}
            for var in NAMES_ONLY & set(facts):
                with self.subTest(names_only=var):
                    self.assertIs(task.get("no_log"), True)
                    self.assertRegex(facts[var].strip(), r"\| map\(attribute='key'\) \| list \}\}$")

    def test_the_no_log_guard_catches_every_form(self) -> None:
        planted = [
            ("get", {"name": "get", "ansible.builtin.debug": {"msg": "{{ calendar_passwords.get('alice') }}"}}),
            ("loop", {"name": "loop", "ansible.builtin.debug": {"msg": "{{ item.value }}"},
                      "loop": "{{ calendar_passwords | dict2items }}"}),
            ("in a condition", {"name": "in a condition", "ansible.builtin.debug": {"msg": "x"},
                                "when": "calendar_passwords['alice'] | length > 3"}),
            ("derive", {"name": "derive", "no_log": True,
                        "ansible.builtin.set_fact": {"pw_copy": "{{ calendar_passwords }}"}}),
            ("intermediate", {"name": "intermediate", "ansible.builtin.debug": {"msg": "{{ pw_copy }}"}}),
            ("register", {"name": "register", "no_log": True, "register": "pw_result",
                          "ansible.builtin.command": {"argv": ["true", "{{ calendar_passwords.alice }}"]}}),
            ("from a result", {"name": "from a result", "ansible.builtin.debug": {"msg": "{{ pw_result.cmd }}"}}),
            ("task vars", {"name": "task vars", "vars": {"pw": "{{ calendar_passwords.alice }}"},
                           "ansible.builtin.debug": {"msg": "hi"}}),
            ("a mention", {"name": "a mention", "ansible.builtin.fail": {"msg": "Set calendar_passwords in the vault"}}),
        ]
        self.assertEqual(no_log_violations(planted),
                         ["get", "loop", "in a condition", "intermediate", "from a result", "task vars"])

    def test_the_preflight_requires_the_push_group(self) -> None:
        """Without it the recovered unit fails at GROUP (216) and the "down" alert never closes."""
        tasks = rr.load_tasks("preflight.yml")
        lookup = next(t for t in tasks if "ansible.builtin.getent" in t)
        self.assertEqual(lookup["ansible.builtin.getent"],
                         {"database": "group", "key": "{{ calendar_push_group }}", "fail_key": False})
        self.assertIs(lookup["check_mode"], False)
        check = tasks[tasks.index(lookup) + 1]["ansible.builtin.assert"]
        self.assertEqual(check["that"], "ansible_facts.getent_group[calendar_push_group] is not none")
        self.assertIn("deploy role dashboard first", check["fail_msg"])

    def test_the_preflight_pins_the_listen_address(self) -> None:
        """tailscale_serve proxies 8445 to 127.0.0.1 only; any other address deploys behind a dead 8445."""
        first = rr.load_tasks("preflight.yml")[0]["ansible.builtin.assert"]
        self.assertIn("calendar_listen_host == '127.0.0.1'", first["that"])
        self.assertIn("must be exactly 127.0.0.1", first["fail_msg"])
        serve = rr.yaml.safe_load((rr.REPO / "roles/tailscale_serve/defaults/main.yml").read_text())
        calendar = [h for h in serve["tailscale_serve_handlers"] if h["port"] == 8445]
        self.assertEqual(calendar, [{"port": 8445, "mount": "/", "backend": "http://127.0.0.1:5232"}])

    def test_the_main_import_order(self) -> None:
        names = [t["ansible.builtin.import_tasks"] for t in rr.load_tasks("main.yml")]
        self.assertEqual(names, ["preflight.yml", "user.yml", "app.yml", "config.yml", "history.yml",
                                 "service.yml", "calendars.yml"])


if __name__ == "__main__":
    unittest.main()
