"""Shared by the calendar_view role tests: render the role's real templates the way Ansible would for the
media server (role defaults < committed group_vars < placeholder host vars), with PyYAML and Jinja2 only.

Placeholders only: generic logins (alice, household, viewer), test-only passwords. Under CI (CI=true) a
missing PyYAML or Jinja2 is a failure, not a skip.
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
APP = REPO / "apps/calendar-view"
TEMPLATES = ROLE / "templates"
IN_CI = os.environ.get("CI", "").lower() == "true"
VIEWER_PASSWORD = "test-only-viewer-password-0001"

# Precedence low -> high, as Ansible applies them to the media server.
VAR_FILES = [
    ROLE / "defaults/main.yml",
    REPO / "inventory/group_vars/all/main.yml",
    REPO / "inventory/group_vars/all/versions.yml",
    REPO / "inventory/group_vars/media_servers/main.yml",
]
# host_vars and the vault are git-ignored; representative placeholders only.
HOST_VARS = {
    "inventory_hostname": "mediaserver-test",
    "ansible_managed": "Ansible managed",
    "role_path": str(ROLE),
    "vault_calendar_passwords": {"alice": "test-only-alice-password-0001", "viewer": VIEWER_PASSWORD},
    "calendar_view_calendars": [
        {"path": "household/shared", "label": "Shared", "color": "#4F8CFF"},
        {"path": "household/school", "label": " School ", "color": "#3ccf91"},
        {"path": "alice/routines", "label": "Routines", "color": "#f2a93b"},
    ],
}


def require_tools() -> None:
    missing = [name for name, mod in (("PyYAML", yaml), ("Jinja2", jinja2)) if mod is None]
    if missing:
        if IN_CI:
            raise AssertionError(f"{', '.join(missing)} required in CI for the calendar_view tests")
        raise unittest.SkipTest(f"{', '.join(missing)} not installed")


def environment():
    """Ansible's template settings and the filters these templates use."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True, trim_blocks=True)
    env.filters["to_nice_json"] = lambda value, indent=4: json.dumps(value, indent=indent, sort_keys=True,
                                                                     separators=(",", ": "))
    return env


def resolve(variables: dict) -> dict:
    """Template the variables against each other until nothing changes (Ansible resolves them lazily, and
    keeps native types: "{{ vault_calendar_passwords }}" is a dict, not its text)."""
    from jinja2.nativetypes import NativeEnvironment

    env = NativeEnvironment(undefined=jinja2.StrictUndefined)
    current = dict(variables)

    def render(value):
        if isinstance(value, str) and "{{" in value:
            try:
                return env.from_string(value).render(current)
            except Exception:  # noqa: BLE001 - unresolvable here; left raw on purpose
                return value
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


def load_vars(overrides: dict | None = None, extra_files: tuple = ()) -> dict:
    merged: dict = {}
    for path in [VAR_FILES[0], *extra_files, *VAR_FILES[1:]]:
        merged.update(yaml.safe_load(path.read_text()) or {})
    merged.update(HOST_VARS)
    merged.update(overrides or {})
    return resolve(merged)


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


def section(text: str, name: str) -> str:
    """The lines of one [Section] (a header is a whole line; a comment mentioning it isn't)."""
    m = re.search(rf"^\[{name}\]$(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    return m.group(1)


def load_tasks(name: str) -> list[dict]:
    return yaml.safe_load((ROLE / "tasks" / name).read_text()) or []
