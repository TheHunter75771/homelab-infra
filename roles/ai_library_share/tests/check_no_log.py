"""Static check (the §12.15 Reviewer D lesson, reused from roles/ai_host_agent): every task in the
role's task files that touches the ailib password carries `no_log: true`. "Touches" = mentions a
variable that holds the password or its slurped bytes, generates it (secrets.token_*), runs smbpasswd,
or reads the password or credentials file with slurp. Covers tasks the offline test can't run (the
real smbpasswd, the client half on ai-box). Usage: python3 check_no_log.py <tasks/*.yml>..."""
import re
import sys

import yaml

SECRET = re.compile(
    r"\b(ai_library_share_password|ai_library_share_client_password|ai_library_share_export_slurp"
    r"|ai_library_share_smbpasswd)\b(?!_)|secrets\.token_|smbpasswd")
SECRET_FILE = re.compile(r"\b(ai_library_share_password_file|ai_library_share_credentials_file)\b")
NESTED = ("block", "rescue", "always")


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
    reads = any(m in task for m in ("ansible.builtin.slurp", "slurp", "ansible.builtin.fetch", "fetch"))
    return reads and bool(SECRET_FILE.search(text))


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
        print(f"FAIL: touches the ailib password without no_log: true: {line}")
    if not bad:
        print(f"PASS: all {seen} password-touching tasks are no_log ({len(paths)} files)")
    return 1 if bad or seen == 0 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
