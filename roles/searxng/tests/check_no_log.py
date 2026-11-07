"""Static check: every task in role searxng's task files that touches the SearXNG secret carries `no_log: true`.
"Touches" = generates a secret (secrets.token_*), or names SEARXNG_SECRET or a variable holding the secret's
creation result. Usage: python3 check_no_log.py <tasks/*.yml>..."""
import re
import sys

import yaml

SECRET = re.compile(r"secrets\.token_|SEARXNG_SECRET=|\bsearxng_secret_(value|script)\b")
NESTED = ("block", "rescue", "always")


class Loader(yaml.SafeLoader):
    """SafeLoader that reads Ansible's !unsafe tag as a plain string."""


Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


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
            for task in tasks(yaml.load(fh, Loader=Loader)):
                if touches(task):
                    seen += 1
                    if task.get("no_log") is not True:
                        bad.append(f"{path}: {task.get('name', '<unnamed>')}")
    for line in bad:
        print(f"FAIL: touches the secret without no_log: true: {line}")
    if not bad:
        print(f"PASS: all {seen} secret-touching tasks are no_log ({len(paths)} files)")
    return 1 if bad or seen == 0 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
