"""Static check (the §12.15 Reviewer D lesson): every task in the role's task files that touches the pull key's
private half carries `no_log: true`. "Touches" = mentions a variable that holds it or its slurped bytes, runs
ssh-keygen, or reads/writes the private key file (slurp/fetch/copy). Covers the tasks the offline test can't run
the way the hosts do. Usage: python3 check_no_log.py <tasks/*.yml>..."""
import re
import sys

import yaml

SECRET = re.compile(r"\b(ai_backup_pull_private_key|ai_backup_export_slurp)\b(?!_)|ssh-keygen")
KEY_FILE = re.compile(r"\b(ai_backup_key_file|ai_backup_pull_key_file)\b")
NESTED = ("block", "rescue", "always")
READERS = ("ansible.builtin.slurp", "slurp", "ansible.builtin.fetch", "fetch", "ansible.builtin.copy", "copy")


def tasks(items):
    for t in items or []:
        if not isinstance(t, dict):
            continue
        yield t
        for key in NESTED:
            yield from tasks(t.get(key))


def touches(task):
    body = {k: v for k, v in task.items() if k not in NESTED and k != "name"}
    text = yaml.safe_dump(body, default_flow_style=True, width=1 << 20)
    if SECRET.search(text):
        return True
    for mod in READERS:
        if mod in task:
            for field in ("src", "dest"):
                path = str((task[mod] or {}).get(field, ""))
                if KEY_FILE.search(path) and not path.endswith(".pub"):
                    return True
    return False


def main(paths):
    bad, seen = [], 0
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for task in tasks(yaml.safe_load(fh)):
                if touches(task):
                    seen += 1
                    if task.get("no_log") is not True:
                        bad.append(f"{path}: {task.get('name', '<unnamed>')}")
    for line in bad:
        print(f"FAIL: touches the pull key's private half without no_log: true: {line}")
    if not bad:
        print(f"PASS: all {seen} private-key tasks are no_log ({len(paths)} files)")
    return 1 if bad or seen < 5 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
