"""Static check: every task in role mission_control's task files that touches a secret carries `no_log: true`.
"Touches" = names the VAPID key file or its scripts (vapid_keygen.py, vapid_check.py), the journal scan (which reads
every secret), the Home Assistant token file, or the subscriptions file (push endpoints are capability URLs).
Usage: python3 check_no_log.py <tasks/*.yml>..."""
import re
import sys

import yaml

SECRET = re.compile(
    r"\b(mission_control_vapid_file|mission_control_ha_token_file|mission_control_subscriptions_file"
    r"|vapid_keygen[.]py|vapid_check[.]py|vapid_subject[.]py|vapid_sign_check[.]py|journal_scan[.]py)\b"
)
NESTED = ("block", "rescue", "always")
# Tasks that only print a verdict or path and never hold a secret (asserts and debug messages on registered
# verdicts). Listed by name so a new task is checked by default.
EXEMPT = {
    "VAPID | the generator's verdict (never the key)",
    "VAPID | the subject's verdict (never the keys)",
    "Venv | locks and helper scripts",
}


def tasks(items):
    for t in items or []:
        if not isinstance(t, dict):
            continue
        yield t
        for key in NESTED:
            yield from tasks(t.get(key))


def touches(task):
    body = {k: v for k, v in task.items() if k not in NESTED and k != "name"}
    if any(k.endswith("assert") for k in body):
        return False
    return bool(SECRET.search(yaml.safe_dump(body, default_flow_style=True, width=1 << 20)))


def main(paths):
    bad, seen = [], 0
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for task in tasks(yaml.safe_load(fh)):
                if touches(task) and task.get("name") not in EXEMPT:
                    seen += 1
                    if task.get("no_log") is not True:
                        bad.append(f"{path}: {task.get('name', '<unnamed>')}")
    for line in bad:
        print(f"FAIL: touches a secret without no_log: true: {line}")
    if not bad:
        print(f"PASS: all {seen} secret-touching tasks are no_log ({len(paths)} files)")
    return 1 if bad or seen < 8 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
