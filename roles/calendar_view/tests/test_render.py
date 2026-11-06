"""Render every template with the role defaults, the committed group_vars and placeholder host vars, and check
them against docs/CONTRACTS.md §14.2/§14.3: the config is exactly what the app loads, the unit carries the
contract's directives and the dashboard's full hardening set, and the tasks keep their promises (no_log on
every use of a password, the listen address pinned, the code installed from the app's real tree).

    python3 -m unittest discover -s roles/calendar_view/tests -v
"""

from __future__ import annotations

import json
import re
import sys
import unittest

import view_render as vr

APP_KEYS = {"base_url", "user", "password", "timezone", "days", "listen_host", "listen_port", "calendars"}


def app_config_module():
    """The app's own loader (module R owns both the app and this role)."""
    if str(vr.APP) not in sys.path:
        sys.path.insert(0, str(vr.APP))
    from calendar_view import config

    return config


class Rendered(unittest.TestCase):
    def setUp(self) -> None:
        vr.require_tools()
        self.vars = vr.load_vars()

    # --- defaults --------------------------------------------------------------------------------------
    def test_defaults(self) -> None:
        v = self.vars
        self.assertEqual((v["calendar_view_listen_host"], v["calendar_view_port"]), ("127.0.0.1", 5233))
        self.assertEqual(v["calendar_view_user"], "calview")
        self.assertEqual(v["calendar_view_viewer"], "viewer")
        self.assertEqual((v["calendar_view_timezone"], v["calendar_view_days"]), ("America/New_York", 7))
        self.assertEqual(v["calendar_view_packages"], ["python3-icalendar", "python3-recurring-ical-events"])
        self.assertEqual((v["calendar_view_icalendar_major"], v["calendar_view_recurring_ical_events_major"]), (6, 3))
        self.assertEqual(v["calendar_view_app_dir"], "/opt/homelab-calendar-view")
        self.assertEqual(v["calendar_view_config_file"], "/etc/homelab-calendar-view/config.json")
        self.assertEqual(v["calendar_view_memory_max"], "128M")
        self.assertEqual(v["calendar_view_healthz_url"], "http://127.0.0.1:5233/healthz")
        defaults = vr.yaml.safe_load((vr.ROLE / "defaults/main.yml").read_text())
        self.assertEqual(defaults["calendar_view_calendars"], [], "the calendars are host_vars")

    def test_the_caldav_url_follows_role_calendar(self) -> None:
        self.assertEqual(self.vars["calendar_view_caldav_url"], "http://127.0.0.1:5232")
        self.assertEqual(self.vars["calendar_view_calendar_service"], "homelab-calendar.service")
        calendar_defaults = (vr.REPO / "roles/calendar/defaults/main.yml",)
        with_calendar = vr.load_vars(extra_files=calendar_defaults)
        self.assertEqual(with_calendar["calendar_view_caldav_url"], "http://127.0.0.1:5232")
        self.assertEqual(with_calendar["calendar_view_calendar_service"], with_calendar["calendar_service"])
        moved = vr.load_vars({"calendar_port": 5299}, extra_files=calendar_defaults)
        self.assertEqual(moved["calendar_view_caldav_url"], "http://127.0.0.1:5299")

    # --- config ----------------------------------------------------------------------------------------
    def test_the_config_is_what_the_app_loads(self) -> None:
        text = vr.render("config.json.j2", self.vars)
        raw = json.loads(text)
        self.assertEqual(set(raw), APP_KEYS)
        self.assertEqual(raw["password"], vr.VIEWER_PASSWORD)
        self.assertEqual((raw["base_url"], raw["user"], raw["timezone"], raw["days"], raw["listen_host"], raw["listen_port"]),
                         ("http://127.0.0.1:5232", "viewer", "America/New_York", 7, "127.0.0.1", 5233))
        self.assertEqual(raw["calendars"], [
            {"path": "household/shared", "label": "Shared", "color": "#4f8cff"},
            {"path": "household/school", "label": "School", "color": "#3ccf91"},
            {"path": "alice/routines", "label": "Routines", "color": "#f2a93b"},
        ])
        cfg = app_config_module().validate(raw)
        self.assertEqual([c.path for c in cfg.calendars], ["household/shared", "household/school", "alice/routines"])

    def test_any_password_survives_the_json(self) -> None:
        tricky = "te\"st\\pass %(user)s 'quoted' é-0123456789"
        v = vr.load_vars({"vault_calendar_passwords": {"viewer": tricky}})
        raw = json.loads(vr.render("config.json.j2", v))
        self.assertEqual(raw["password"], tricky)
        app_config_module().validate(raw)

    # --- unit ------------------------------------------------------------------------------------------
    def unit(self, overrides: dict | None = None) -> str:
        return vr.render("homelab-calendar-view.service.j2", vr.load_vars(overrides) if overrides else self.vars)

    def test_the_service_unit(self) -> None:
        text = self.unit()
        d = vr.directives(text)
        expect = {
            "Type": ["exec"],
            "User": ["calview"],
            "Group": ["calview"],
            "ExecStart": [("/usr/bin/python3 -I -B /opt/homelab-calendar-view/calendar-view "
                           "--config /etc/homelab-calendar-view/config.json")],
            "Restart": ["always"],
            "RestartSec": ["5s"],
            "RestartMode": ["direct"],
            "StartLimitIntervalSec": ["0"],
            "After": ["network.target homelab-calendar.service"],
            "Wants": ["homelab-calendar.service"],
            "IPAddressDeny": ["any"],
            "IPAddressAllow": ["localhost"],
            "MemoryMax": ["128M"],
            "MemoryDenyWriteExecute": ["yes"],
            "WantedBy": ["multi-user.target"],
        }
        for key, value in expect.items():
            with self.subTest(directive=key):
                self.assertEqual(d.get(key), value)
        # No start limit, in [Unit] where systemd reads it (a display keeps retrying).
        self.assertIn("\nStartLimitIntervalSec=0\n", vr.section(text, "Unit"))
        self.assertNotIn("StartLimitBurst", d)
        self.assertIn("\nRestartMode=direct\n", vr.section(text, "Service"))
        # Nothing writable, no push in v1, no secret in the unit.
        for key in ("ReadWritePaths", "OnFailure", "Environment", "EnvironmentFile", "StateDirectory"):
            self.assertNotIn(key, d)
        self.assertNotIn(vr.VIEWER_PASSWORD, text)

    def test_wants_the_calendar_but_never_requires_it(self) -> None:
        d = vr.directives(self.unit())
        for key in ("Requires", "Requisite", "BindsTo", "PartOf", "Upholds"):
            self.assertNotIn(key, d, "the view must keep serving its stale cache while the calendar is down")
        for key in ("After", "Before", "Requires", "Wants", "BindsTo", "Requisite", "PartOf"):
            for value in d.get(key, []):
                with self.subTest(directive=key):
                    self.assertNotRegex(value, r"docker|bootgate")

    def test_the_dashboards_full_hardening_set(self) -> None:
        dash = (vr.REPO / "roles/dashboard/templates/homelab-dashboard.service.j2").read_text()
        block = dash.split("# Hardening", 1)[1].split("[Install]", 1)[0]
        dash_dirs = vr.directives(block)
        self.assertGreater(len(dash_dirs), 20, "the dashboard's hardening block moved; update this test")
        view = vr.directives(self.unit())
        for key, values in dash_dirs.items():
            if key == "ReadWritePaths":  # the dashboard's own data dirs; this service writes nothing
                continue
            with self.subTest(directive=key):
                self.assertEqual(view.get(key), values)

    def test_the_measured_score_is_recorded(self) -> None:
        m = re.search(r"systemd-analyze security[^:]*: (\d+\.\d+)", self.unit())
        self.assertIsNotNone(m, "put the measured systemd-analyze security score in the header comment")
        self.assertLessEqual(float(m.group(1)), 2.0)

    def test_the_unit_follows_the_paths(self) -> None:
        d = vr.directives(self.unit({"calendar_view_app_dir": "/opt/other/homelab-calendar-view",
                                     "calendar_view_config_dir": "/etc/other"}))
        self.assertEqual(d["ExecStart"], [("/usr/bin/python3 -I -B /opt/other/homelab-calendar-view/calendar-view "
                                           "--config /etc/other/config.json")])


# --- no_log guard ---------------------------------------------------------------------------------------
SECRET_ROOTS = {"calendar_passwords"}
# Derived from the password, but a yes/no only: the preflight's assert reads it.
BOOL_ONLY = {"calendar_view_password_ok"}
# Keys whose whole value is a Jinja expression (no braces), at any depth of a task.
RAW_EXPRESSION_KEYS = {"when", "that", "until", "failed_when", "changed_when", "var"}


def role_tasks() -> list[tuple[str, dict]]:
    """Every task of the role (tasks/ and handlers/, blocks flattened) as (name, task)."""
    def walk(items):
        for task in items or []:
            yield task
            for key in ("block", "rescue", "always"):
                yield from walk(task.get(key))

    return [(t.get("name", "?"), t) for d in ("tasks", "handlers") for path in sorted((vr.ROLE / d).glob("*.yml"))
            for t in walk(vr.yaml.safe_load(path.read_text()))]


def expressions(value, key: str = "") -> list[str]:
    """The Jinja a task evaluates: {{ }} and {% %} inside strings, plus whole raw-expression values."""
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


def template_uses_secret(task: dict) -> bool:
    """A template task whose source renders a password."""
    args = task.get("ansible.builtin.template") or {}
    src = args.get("src")
    return bool(src) and any(name in (vr.TEMPLATES / src).read_text() for name in SECRET_ROOTS)


def tasks_touching_secrets(tasks: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """Tasks that use calendar_passwords, render a template that does, or use anything derived from it
    (set_fact keys, a register, task vars), followed to a fixpoint. BOOL_ONLY variables don't carry it on."""
    secret = set(SECRET_ROOTS)
    while True:
        derived = set(secret)
        for _, task in tasks:
            if uses(task, secret) or template_uses_secret(task):
                derived |= {task["register"]} if "register" in task else set()
                derived |= set(task.get("vars") or {})
                facts = task.get("ansible.builtin.set_fact") or task.get("set_fact") or {}
                derived |= {k for k in facts if k != "cacheable"} - BOOL_ONLY
        if derived == secret:
            return [(name, task) for name, task in tasks
                    if uses(task, secret) or template_uses_secret(task) or set(task.get("vars") or {}) & secret]
        secret = derived


def no_log_violations(tasks: list[tuple[str, dict]]) -> list[str]:
    return [name for name, task in tasks_touching_secrets(tasks) if task.get("no_log") is not True]


class Static(unittest.TestCase):
    """The tasks (PyYAML only)."""

    def setUp(self) -> None:
        if vr.yaml is None:
            vr.require_tools()

    def test_every_task_that_touches_the_password_has_no_log(self) -> None:
        tasks = role_tasks()
        self.assertEqual(no_log_violations(tasks), [])
        touching = sorted(name for name, _ in tasks_touching_secrets(tasks))
        self.assertEqual(touching, ["Config | settings (root:calview 0640, with the viewer's password)",
                                    "Preflight | the viewer has a usable password (value never printed)"],
                         "review this list if it changed")

    def test_the_derived_yes_no_is_a_yes_no(self) -> None:
        for name, task in role_tasks():
            facts = task.get("ansible.builtin.set_fact") or {}
            for var in BOOL_ONLY & set(facts):
                with self.subTest(var):
                    self.assertIs(task.get("no_log"), True)
                    # Ends in a test: the value is a yes/no, never the password itself.
                    self.assertRegex(facts[var].strip(), r"is match\('\^\[ -~\]\{16,72\}\$'\) \}\}$")

    def test_the_no_log_guard_catches_every_form(self) -> None:
        planted = [
            ("get", {"name": "get", "ansible.builtin.debug": {"msg": "{{ calendar_passwords.get('viewer') }}"}}),
            ("loop", {"name": "loop", "ansible.builtin.debug": {"msg": "{{ item.value }}"},
                      "loop": "{{ calendar_passwords | dict2items }}"}),
            ("in a condition", {"name": "in a condition", "ansible.builtin.debug": {"msg": "x"},
                                "when": "calendar_passwords['viewer'] | length > 3"}),
            ("derive", {"name": "derive", "no_log": True,
                        "ansible.builtin.set_fact": {"pw_copy": "{{ calendar_passwords }}"}}),
            ("intermediate", {"name": "intermediate", "ansible.builtin.debug": {"msg": "{{ pw_copy }}"}}),
            ("register", {"name": "register", "no_log": True, "register": "pw_result",
                          "ansible.builtin.command": {"argv": ["true", "{{ calendar_passwords.viewer }}"]}}),
            ("from a result", {"name": "from a result", "ansible.builtin.debug": {"msg": "{{ pw_result.cmd }}"}}),
            ("task vars", {"name": "task vars", "vars": {"pw": "{{ calendar_passwords.viewer }}"},
                           "ansible.builtin.debug": {"msg": "hi"}}),
            ("a template", {"name": "a template", "ansible.builtin.template": {"src": "config.json.j2", "dest": "/x"}}),
            ("a mention", {"name": "a mention", "ansible.builtin.fail": {"msg": "Set calendar_passwords in the vault"}}),
        ]
        self.assertEqual(no_log_violations(planted),
                         ["get", "loop", "in a condition", "intermediate", "from a result", "task vars", "a template"])

    def test_the_preflight_pins_the_listen_address(self) -> None:
        first = vr.load_tasks("preflight.yml")[0]["ansible.builtin.assert"]
        self.assertIn("calendar_view_listen_host == '127.0.0.1'", first["that"])
        self.assertIn("must be exactly 127.0.0.1", first["fail_msg"])
        serve = vr.yaml.safe_load((vr.REPO / "roles/tailscale_serve/defaults/main.yml").read_text())
        view = [h for h in serve["tailscale_serve_handlers"] if h["port"] == 8446]
        if not view:
            self.skipTest("module C's 8446 handler isn't in roles/tailscale_serve yet (CONTRACTS §14.4)")
        self.assertEqual(view, [{"port": 8446, "mount": "/", "backend": "http://127.0.0.1:5233"}])

    def test_the_preflight_checks_the_calendars(self) -> None:
        tasks = {t["name"]: t for t in vr.load_tasks("preflight.yml")}
        that = " ".join(tasks["Preflight | the calendars to show (calendar_view_calendars in host_vars)"]
                        ["ansible.builtin.assert"]["that"])
        self.assertIn("calendar_view_calendars | length > 0", that)
        self.assertIn("'^#[0-9A-Fa-f]{6}$'", that)
        self.assertIn("'^[a-z][a-z0-9_-]{1,31}/[a-z0-9-]{1,40}$'", that)
        viewers = tasks["Preflight | the viewer can read every calendar shown (role calendar's calendar_viewers)"]
        self.assertEqual(viewers["when"], "calendar_viewers is defined")

    def test_the_main_import_order(self) -> None:
        names = [t["ansible.builtin.import_tasks"] for t in vr.load_tasks("main.yml")]
        self.assertEqual(names, ["preflight.yml", "user.yml", "packages.yml", "install.yml", "config.yml", "service.yml"])

    def test_the_installed_tree_is_the_apps(self) -> None:
        defaults = vr.yaml.safe_load((vr.ROLE / "defaults/main.yml").read_text())
        self.assertEqual(sorted(defaults["calendar_view_web_files"]), sorted(p.name for p in (vr.APP / "web").iterdir()))
        launcher = vr.APP / defaults["calendar_view_launcher"]
        self.assertTrue(launcher.is_file() and launcher.stat().st_mode & 0o111, "the launcher is executable")
        self.assertTrue((vr.APP / defaults["calendar_view_package"] / "__main__.py").is_file())

    def test_the_config_task(self) -> None:
        task = next(t for t in vr.load_tasks("config.yml") if "ansible.builtin.template" in t)
        args = task["ansible.builtin.template"]
        self.assertIs(task["no_log"], True)
        self.assertEqual((args["owner"], args["group"], args["mode"]),
                         ("{{ calendar_view_root_owner }}", "{{ calendar_view_user }}", "0640"))
        self.assertEqual(args["validate"], "{{ calendar_view_python }} -I -B {{ calendar_view_app_dir }}/"
                                           "{{ calendar_view_launcher }} --check-config %s")

    def test_the_health_check(self) -> None:
        tasks = vr.load_tasks("service.yml")
        probe = next(t for t in tasks if "ansible.builtin.uri" in t)
        self.assertEqual(probe["ansible.builtin.uri"]["url"], "{{ calendar_view_healthz_url }}")
        self.assertEqual((probe["until"], probe["retries"], probe["when"]),
                         ("calendar_view_healthz.status == 200", 10, "not ansible_check_mode"))
        check = tasks[tasks.index(probe) + 1]
        self.assertEqual(check["ansible.builtin.assert"]["that"], "calendar_view_healthz.status == 200")
        self.assertEqual(check["when"], "not ansible_check_mode")

    def test_handler_names_are_unique_in_the_repo(self) -> None:
        mine = {h["name"] for h in vr.yaml.safe_load((vr.ROLE / "handlers/main.yml").read_text())}
        for path in sorted((vr.REPO / "roles").glob("*/handlers/main.yml")):
            if path.parent.parent == vr.ROLE:
                continue
            others = {h.get("name") for h in vr.yaml.safe_load(path.read_text()) or []}
            with self.subTest(role=path.parent.parent.name):
                self.assertEqual(mine & others, set())


if __name__ == "__main__":
    unittest.main()
