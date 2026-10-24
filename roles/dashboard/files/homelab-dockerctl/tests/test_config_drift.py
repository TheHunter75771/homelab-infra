"""Drift guard: the helper's settings file, as role dashboard renders it, must load into the helper's
real Config. A stray or renamed key would make every request fail with "helper misconfigured".

Two layers:
  - static (stdlib + PyYAML): the keys written by templates/dockerctl.json.j2 plus the timeout
    defaults are all Config fields, and every required field is among them;
  - rendered (needs Jinja2): the real template rendered with the role defaults, the committed
    group_vars and placeholder host vars (documentation addresses), then loaded with
    Config.from_mapping. The unit templates are rendered too, to check that they point at the same
    config file and cache. Under CI (CI=true) a missing Jinja2 is a failure, not a skip.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import unittest

from fakes import dockerctl

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None
try:
    import jinja2
except ImportError:  # pragma: no cover
    jinja2 = None

ROLE = pathlib.Path(__file__).resolve().parents[3]
REPO = ROLE.parents[1]
TEMPLATES = ROLE / "templates"
IN_CI = os.environ.get("CI", "").lower() == "true"

CONFIG_FIELDS = {f.name: f for f in dataclasses.fields(dockerctl.Config)}
REQUIRED = {name for name, f in CONFIG_FIELDS.items()
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING}

# Precedence low -> high, as Ansible applies them to the media server.
VAR_FILES = [
    ROLE / "defaults/main.yml",
    REPO / "inventory/group_vars/all/main.yml",
    REPO / "inventory/group_vars/all/versions.yml",
    REPO / "inventory/group_vars/media_servers/main.yml",
]
# host_vars are git-ignored; representative placeholders only (RFC 5737 / example.com).
HOST_VARS = {
    "inventory_hostname": "mediaserver-test",
    "lan_ip": "192.0.2.10",
    "tailnet_fqdn": "box.example.ts.example",
    "public_watch_host": "watch.example.com",
    "media_uuid": "0f0e0d0c-1111-4222-8333-944445555666",
    "ansible_managed": "Ansible managed",
}


def load_vars() -> dict:
    merged: dict = {}
    for path in VAR_FILES:
        merged.update(yaml.safe_load(path.read_text()) or {})
    merged.update(HOST_VARS)
    return merged


def to_nice_json(value, indent=4, sort_keys=True):
    """Ansible's to_nice_json, as far as these templates use it."""
    return json.dumps(value, indent=indent, sort_keys=sort_keys, separators=(",", ": "), ensure_ascii=False)


def combine(*dicts):
    out: dict = {}
    for d in dicts:
        out.update(d)
    return out


def environment():
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True, trim_blocks=True)
    env.filters.update({"to_nice_json": to_nice_json, "combine": combine})
    return env


def resolve(env, variables: dict) -> dict:
    """Template the variables against each other until nothing changes, the way Ansible resolves
    them lazily. A variable that can't be resolved here (vault secrets, lookups, Ansible-only
    filters) stays raw; the render below fails if a template actually needs one of them."""
    current = dict(variables)

    def render(value):
        if isinstance(value, str) and "{{" in value:
            try:
                out = env.from_string(value).render(current)
            except Exception:  # noqa: BLE001 - unresolvable here; left raw on purpose
                return value
            # Ansible turns a whole-value expression back into its native type ("{{ x | int }}").
            try:
                return json.loads(out) if out.strip()[:1] in "[{" or out.strip().isdigit() else out
            except ValueError:
                return out
        if isinstance(value, list):
            return [render(v) for v in value]
        if isinstance(value, dict):
            return {k: render(v) for k, v in value.items()}
        return value

    for _ in range(10):
        nxt = {k: render(v) for k, v in current.items()}
        if nxt == current:
            break
        current = nxt
    return current


def template_keys(text: str) -> set[str]:
    block = re.search(r"\{%-?\s*set settings = \{(.*?)\}\s*-?%\}", text, re.DOTALL)
    assert block, "templates/dockerctl.json.j2 no longer has its `set settings = {...}` block"
    return set(re.findall(r"'([A-Za-z_][A-Za-z0-9_]*)'\s*:", block.group(1)))


@unittest.skipIf(yaml is None, "PyYAML not installed")
class StaticKeys(unittest.TestCase):
    def test_template_and_timeout_keys_are_config_fields(self) -> None:
        keys = template_keys((TEMPLATES / "dockerctl.json.j2").read_text())
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        timeouts = set(defaults["dashboard_dockerctl_timeouts"])
        self.assertFalse((keys | timeouts) - set(CONFIG_FIELDS), "keys the helper's Config doesn't know")
        self.assertFalse(keys & timeouts, "a timeout default would silently override a template key")
        self.assertLessEqual(REQUIRED, keys, "a required Config field is not rendered")
        for key in timeouts:
            with self.subTest(key=key):
                self.assertIn(CONFIG_FIELDS[key].type, ("float", "int"))
                self.assertIsInstance(defaults["dashboard_dockerctl_timeouts"][key], (int, float))

    def test_the_guard_catches_a_stray_key(self) -> None:
        text = (TEMPLATES / "dockerctl.json.j2").read_text().replace("'stacks_dir':", "'stack_dir':")
        self.assertIn("stack_dir", template_keys(text) - set(CONFIG_FIELDS))


class RunIsReadOnly(unittest.TestCase):
    """Static (stdlib only): /run is read-only in both units; only /run/bootgate is writable, nested."""

    def test_the_dashboard_unit(self) -> None:
        text = (TEMPLATES / "homelab-dashboard.service.j2").read_text()
        self.assertRegex(text, r"(?m)^ReadOnlyPaths=/run$")
        rw = re.search(r"(?m)^ReadWritePaths=(.*)$", text).group(1)
        self.assertTrue(rw.endswith(" -{{ dashboard_bootgate_run_dir }}"), rw)
        self.assertEqual(len(re.findall(r"(?m)^ReadWritePaths=", text)), 1)

    def test_the_helper_unit(self) -> None:
        text = (TEMPLATES / "homelab-dockerctl@.service.j2").read_text()
        self.assertRegex(text, r"(?m)^ReadOnlyPaths=/run$")
        self.assertNotRegex(text, r"(?m)^ReadWritePaths=")


# The real worst case on the helper socket in one 2 s window: 8 log streams opened together
# (docker-state + logs each), one update (docker-state + update), a summary refresh (up to 3) and
# docker-state about once a second (2).
PEAK_CONNECTIONS_PER_2S = 8 * 2 + 2 + 3 + 2


def directives(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z]+)=(.*)$", line)
        if m:
            out.setdefault(m.group(1), []).append(m.group(2))
    return out


class SocketFloodProtection(unittest.TestCase):
    """Static: the socket can't latch off from a burst, and if it ever does, it recovers without Docker."""

    def test_the_socket_template(self) -> None:
        d = directives((TEMPLATES / "homelab-dockerctl.socket.j2").read_text())
        self.assertEqual(d["OnFailure"], ["homelab-dockerctl-recover.service"])
        for key in ("PollLimitIntervalSec", "PollLimitBurst", "TriggerLimitIntervalSec", "TriggerLimitBurst"):
            with self.subTest(key=key):
                self.assertEqual(len(d.get(key, [])), 1, f"{key} must be set exactly once")

    def test_the_recovery_never_touches_docker_and_is_rate_limited(self) -> None:
        text = (TEMPLATES / "homelab-dockerctl-recover.service.j2").read_text()
        d = directives(text)
        self.assertEqual(d["Type"], ["oneshot"])
        self.assertEqual(d["ExecCondition"], ["/usr/bin/systemctl is-failed --quiet homelab-dockerctl.socket"])
        self.assertEqual(d["ExecStart"], ["/usr/bin/systemctl restart homelab-dockerctl.socket"])
        self.assertEqual(len(d["ExecStartPre"]), 1)
        self.assertTrue(d["ExecStartPre"][0].startswith("/usr/bin/sleep "))
        self.assertIn("StartLimitIntervalSec", d)
        self.assertIn("StartLimitBurst", d)
        commands = " ".join(d.get(k, [""])[0] for k in ("ExecCondition", "ExecStartPre", "ExecStart"))
        self.assertNotRegex(commands, r"docker\.(service|socket)|/usr/bin/docker|\bstart\b")
        timer = directives((TEMPLATES / "homelab-dockerctl-recover.timer.j2").read_text())
        self.assertEqual(len(timer["OnCalendar"]), 1)
        self.assertNotIn("OnUnitActiveSec", timer, "a refused start must not make the timer re-fire at once")

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_the_numbers(self) -> None:
        dv = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        poll, trigger = dv["dashboard_dockerctl_poll_limit_burst"], dv["dashboard_dockerctl_trigger_limit_burst"]
        self.assertEqual(dv["dashboard_dockerctl_poll_limit_interval"], "2s")
        self.assertEqual(dv["dashboard_dockerctl_trigger_limit_interval"], "2s")
        self.assertGreaterEqual(poll, 2 * PEAK_CONNECTIONS_PER_2S, "the poll limit needs 2x headroom over the real peak")
        self.assertGreater(trigger, poll, "the latching trigger limit must sit above the transient poll limit")
        self.assertGreaterEqual(trigger, dv["dashboard_dockerctl_max_connections"])
        self.assertLessEqual(trigger, 200, "still caps a runaway loop (systemd's own default is 200 per 2 s)")
        self.assertGreaterEqual(dv["dashboard_dockerctl_recover_delay_seconds"], 10)
        self.assertGreaterEqual(dv["dashboard_dockerctl_recover_start_limit_burst"], 1)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class Rendered(unittest.TestCase):
    def setUp(self) -> None:
        if jinja2 is None:
            if IN_CI:
                self.fail("Jinja2 is required in CI for the rendered drift test (pip install jinja2)")
            self.skipTest("Jinja2 not installed")
        self.env = environment()
        self.vars = resolve(self.env, load_vars())
        # The role reads this from the server's package.json with an Ansible lookup; do the same.
        if "{{" in str(self.vars["dashboard_server_entry"]):
            pkg = REPO / "apps/dashboard/server/package.json"
            main = json.loads(pkg.read_text()).get("main") if pkg.exists() else None
            self.vars["dashboard_server_entry"] = main or "src/server.js"

    def render(self, name: str) -> str:
        text = self.env.from_string((TEMPLATES / name).read_text()).render(self.vars)
        self.assertNotIn("{{", text, f"{name} rendered with an unresolved variable")
        return text

    def test_the_rendered_settings_load_into_config(self) -> None:
        raw = json.loads(self.render("dockerctl.json.j2"))
        self.assertFalse(set(raw) - set(CONFIG_FIELDS), "unknown keys")
        self.assertLessEqual(REQUIRED, set(raw), "required keys missing")
        cfg = dockerctl.Config.from_mapping(raw)  # the helper's own loader and validation
        self.assertEqual(cfg.allowed_uid, 10101)
        self.assertEqual(cfg.stacks_dir, "/srv/homelab/stacks")
        self.assertEqual(cfg.media_mount, "/media")
        self.assertEqual(cfg.media_dirs, ("tv", "movies", "downloads"))
        self.assertEqual(cfg.held_stacks, ("media",))
        self.assertEqual(cfg.bootgate_status_file, "/var/lib/bootgate/status")
        self.assertEqual(cfg.backup_pull_user, "backuppull")
        self.assertEqual(cfg.cache_dir, "/var/cache/homelab-dockerctl")
        self.assertEqual(cfg.follow_max_seconds, 1800)

    def test_a_renamed_key_would_be_caught(self) -> None:
        text = (TEMPLATES / "dockerctl.json.j2").read_text().replace("'allowed_uid':", "'allowed_user':")
        raw = json.loads(self.env.from_string(text).render(self.vars))
        with self.assertRaises(ValueError):
            dockerctl.Config.from_mapping(raw)

    def test_the_units_point_at_the_same_config_cache_and_group(self) -> None:
        service = self.render("homelab-dockerctl@.service.j2")
        socket_unit = self.render("homelab-dockerctl.socket.j2")
        self.assertIn(f"Environment=HOMELAB_DOCKERCTL_CONFIG={self.vars['dashboard_dockerctl_config']}\n", service)
        self.assertIn(f"ExecStart=/usr/bin/python3 -I {self.vars['dashboard_dockerctl_script']}\n", service)
        cache = json.loads(self.render("dockerctl.json.j2"))["cache_dir"]
        self.assertIn(f"CacheDirectory={pathlib.PurePosixPath(cache).name}\n", service)
        self.assertIn("ListenStream=/run/homelab-dockerctl.sock\n", socket_unit)
        self.assertIn("SocketGroup=dashboard\nSocketMode=0660\nAccept=yes\n", socket_unit)
        max_conn = int(re.search(r"^MaxConnections=(\d+)$", socket_unit, re.MULTILINE).group(1))
        self.assertGreaterEqual(max_conn, 32, "CONTRACTS §11.2: 8 log streams + an update + refreshes")
        self.assertRegex(service, r"(?m)^ReadOnlyPaths=/run$")

    def test_the_rendered_flood_protection_and_recovery(self) -> None:
        sock = directives(self.render("homelab-dockerctl.socket.j2"))
        self.assertEqual(sock["OnFailure"], ["homelab-dockerctl-recover.service"])
        self.assertEqual((sock["PollLimitIntervalSec"], sock["PollLimitBurst"]), (["2s"], ["50"]))
        self.assertEqual((sock["TriggerLimitIntervalSec"], sock["TriggerLimitBurst"]), (["2s"], ["100"]))
        self.assertEqual(sock["MaxConnections"], ["32"])
        recover = directives(self.render("homelab-dockerctl-recover.service.j2"))
        self.assertEqual(recover["ExecStartPre"], ["/usr/bin/sleep 30"])
        self.assertEqual((recover["StartLimitIntervalSec"], recover["StartLimitBurst"]), (["30min"], ["6"]))
        self.assertEqual(recover["TimeoutStartSec"], ["90"])
        timer = directives(self.render("homelab-dockerctl-recover.timer.j2"))
        self.assertEqual(timer["OnCalendar"], ["*:0/15"])
        self.assertEqual(timer["WantedBy"], ["timers.target"])

    def test_the_dashboard_unit_keeps_run_read_only_but_bootgate_writable(self) -> None:
        unit = self.render("homelab-dashboard.service.j2")
        self.assertRegex(unit, r"(?m)^ProtectSystem=strict$")
        self.assertRegex(unit, r"(?m)^ReadOnlyPaths=/run$")
        rw = re.search(r"(?m)^ReadWritePaths=(.*)$", unit).group(1).split()
        self.assertEqual(rw, ["/var/lib/homelab-dashboard", "/srv/homelab/push", "-/run/bootgate"])
        # Every writable path under /run is nested inside the read-only /run (the nesting rule
        # applies), and nothing else under /run is writable.
        under_run = [p.lstrip("-") for p in rw if p.lstrip("-").startswith("/run/")]
        self.assertEqual(under_run, ["/run/bootgate"])
        self.assertIn(f"ExecStart=/usr/bin/node /opt/homelab-dashboard/server/{self.vars['dashboard_server_entry']}\n",
                      unit)
        self.assertIn("Environment=DASHBOARD_CONFIG=/etc/homelab-dashboard/config.json\n", unit)


if __name__ == "__main__":
    unittest.main()
