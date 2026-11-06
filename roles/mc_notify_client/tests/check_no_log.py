"""Static check: every task in role mc_notify_client's task files that names the token (mc_notify_client_token) carries
`no_log: true`. Asserts are exempt from that (they never print their condition), but an assert may neither check the
token itself (only the no_log verdict) nor template it in its fail_msg, success_msg or msg (a message is printed).
Text outside {{ }} / {% %} in a message is prose and is not checked.
Usage: python3 check_no_log.py <tasks/*.yml>..."""
import re
import sys

import yaml

SECRET = re.compile(r"\bmc_notify_client_token\b")
JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
MESSAGES = ("fail_msg", "success_msg", "msg")
NESTED = ("block", "rescue", "always")


def tasks(items):
    for t in items or []:
        if isinstance(t, dict):
            yield t
            for key in NESTED:
                yield from tasks(t.get(key))


def assert_problems(args):
    if not isinstance(args, dict):
        return []
    out = []
    if SECRET.search(yaml.safe_dump(args.get("that", ""))):
        out.append("an assert must check the verdict, not the token")
    for m in MESSAGES:
        if any(SECRET.search(block) for block in JINJA.findall(str(args.get(m) or ""))):
            out.append(f"its {m} templates the token")
    return out


def main(paths):
    bad, seen = [], 0
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for task in tasks(yaml.safe_load(fh)):
                body = {k: v for k, v in task.items() if k not in NESTED and k != "name"}
                asserts = [v for k, v in body.items() if k.endswith("assert")]
                if asserts:
                    for problem in assert_problems(asserts[0]):
                        bad.append(f"{path}: {task.get('name')} ({problem})")
                    continue
                if SECRET.search(yaml.safe_dump(body, default_flow_style=True, width=1 << 20)):
                    seen += 1
                    if task.get("no_log") is not True:
                        bad.append(f"{path}: {task.get('name', '<unnamed>')}")
    for line in bad:
        print(f"FAIL: touches the token without no_log: true: {line}")
    if not bad:
        print(f"PASS: all {seen} token-touching tasks are no_log ({len(paths)} files)")
    return 1 if bad or seen < 2 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
