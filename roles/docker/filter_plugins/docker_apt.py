"""Pinned-package check for role docker's install path (docs/CONTRACTS.md §12.8; Python stdlib only).

docker_apt_pending(names, installed, pins) -> the names (in order) that are not installed at exactly
pins[name]. `installed` is ansible_facts.packages from ansible.builtin.package_facts (manager apt):
{name: [{"version": "5:29.5.2-1~ubuntu.26.04~resolute", ...}, ...]}. Versions compare as exact strings,
epoch included, the same string the apt module installs with `name=version`.
"""


def docker_apt_pending(names, installed, pins):
    installed = installed or {}
    pending = []
    for name in names or []:
        versions = {str(entry.get("version", "")) for entry in installed.get(name) or []}
        if str(pins[name]) not in versions:
            pending.append(name)
    return pending


class FilterModule:
    def filters(self):
        return {"docker_apt_pending": docker_apt_pending}
