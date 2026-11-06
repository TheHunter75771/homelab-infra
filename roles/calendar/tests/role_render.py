"""Shared by the calendar tests: render the role's real templates the way Ansible would for the media
server (role defaults < committed group_vars < placeholder host vars), with PyYAML and Jinja2 only.

Placeholders only: generic logins (alice, bob, household), documentation names. Under CI (CI=true)
a missing PyYAML or Jinja2 is a failure, not a skip (same rule as the dashboard's drift test).
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None
try:
    import jinja2
except ImportError:  # pragma: no cover
    jinja2 = None

ROLE = pathlib.Path(__file__).resolve().parents[1]
REPO = ROLE.parents[1]
TEMPLATES = ROLE / "templates"
IN_CI = os.environ.get("CI", "").lower() == "true"

# Precedence low -> high, as Ansible applies them to the media server.
VAR_FILES = [
    ROLE / "defaults/main.yml",
    REPO / "inventory/group_vars/all/main.yml",
    REPO / "inventory/group_vars/all/versions.yml",
    REPO / "inventory/group_vars/media_servers/main.yml",
]
# host_vars are git-ignored; representative placeholders only.
HOST_VARS = {
    "inventory_hostname": "mediaserver-test",
    "ansible_managed": "Ansible managed",
    "calendar_accounts": [
        {"user": "alice", "calendars": [{"id": "personal", "name": "Personal"}, {"id": "routines", "name": "Routines"}]},
        {"user": "bob", "calendars": [{"id": "personal", "name": "Personal"}]},
        {"user": "household", "calendars": [{"id": "shared", "name": "Shared"}, {"id": "school", "name": "School"}]},
    ],
    # The wall-screen view's read-only login (CONTRACTS §14.1); "viewer" is the shared placeholder name.
    "calendar_viewers": [{"user": "viewer", "read": ["household/shared", "household/school", "alice/routines"]}],
}


def require_tools(jinja: bool = True) -> None:
    """Skip (or fail under CI) when the rendering tools are missing."""
    missing = [name for name, mod in (("PyYAML", yaml), ("Jinja2", jinja2 if jinja else True)) if mod is None]
    if missing:
        if IN_CI:
            raise AssertionError(f"{', '.join(missing)} required in CI for the calendar tests")
        raise unittest.SkipTest(f"{', '.join(missing)} not installed")


def environment():
    """Ansible's template settings and filters, as far as these templates need them."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True, trim_blocks=True)
    env.filters["regex_escape"] = re.escape  # ansible.builtin.regex_escape (re_type python)
    env.filters["regex_replace"] = lambda value, pattern, replacement="": re.sub(pattern, replacement, value)
    return env


def resolve(env, variables: dict) -> dict:
    """Template the variables against each other until nothing changes (Ansible resolves them lazily).
    One that can't be resolved here (vault secrets) stays raw; a template that needs it then fails."""
    current = dict(variables)

    def render(value):
        if isinstance(value, str) and "{{" in value:
            try:
                out = env.from_string(value).render(current)
            except Exception:  # noqa: BLE001 - unresolvable here; left raw on purpose
                return value
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


def load_vars(overrides: dict | None = None) -> dict:
    merged: dict = {}
    for path in VAR_FILES:
        merged.update(yaml.safe_load(path.read_text()) or {})
    merged.update(HOST_VARS)
    merged.update(overrides or {})
    return resolve(environment(), merged)


def render(name: str, variables: dict) -> str:
    text = environment().from_string((TEMPLATES / name).read_text()).render(variables)
    if "{{" in text or "{%" in text:
        raise AssertionError(f"{name} rendered with an unresolved expression")
    return text


def directives(text: str) -> dict[str, list[str]]:
    """systemd unit lines as {Directive: [values in order]} (comments and sections skipped)."""
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z]+)=(.*)$", line)
        if m:
            out.setdefault(m.group(1), []).append(m.group(2))
    return out


def load_tasks(name: str) -> list[dict]:
    return yaml.safe_load((ROLE / "tasks" / name).read_text()) or []
