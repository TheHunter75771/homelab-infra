"""Static check: every task in role finance_digest's task files that touches the Notion token carries
`no_log: true`. "Touches" = names finance_digest_notion_token, finance_digest_notion_json (the file content) or
finance_digest_notion_file (a task reading the file would print it: slurp returns it as base64).
Usage: python3 check_no_log.py <tasks/*.yml>..."""
import re
import sys

import yaml

SECRET = re.compile(r"\bfinance_digest_notion_(token|json|file)\b")
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
    return bool(SECRET.search(yaml.safe_dump(body, default_flow_style=True, width=1 << 20)))


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
        print(f"FAIL: touches the Notion token without no_log: true: {line}")
    if not bad:
        print(f"PASS: all {seen} token-touching tasks are no_log ({len(paths)} files)")
    return 1 if bad or seen == 0 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
