"""Static check (Reviewer D): every task in the role's task files that touches the agent token
carries `no_log: true`. "Touches" = mentions a variable that holds the token or its slurped bytes,
reads the token file with slurp, or generates a token. Covers tasks the offline test can't run
(tasks/verify.yml needs a live agent). Usage: python3 check_no_log.py <tasks/*.yml>..."""
import re
import sys

import yaml

SECRET = re.compile(
    r"\b(ai_host_agent_token|ai_control_agent_token|ai_host_agent_verify_auth|ai_host_agent_verify_slurp"
    r"|ai_host_agent_export_slurp)\b(?!_)|secrets\.token_hex")
TOKEN_FILE = re.compile(r"\b(ai_host_agent_token_file|ai_control_token_file)\b")
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
    return any(m in task for m in ("ansible.builtin.slurp", "slurp")) and bool(TOKEN_FILE.search(text))


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
        print(f"FAIL: touches the token without no_log: true: {line}")
    if not bad:
        print(f"PASS: all {seen} token-touching tasks are no_log ({len(paths)} files)")
    return 1 if bad or seen == 0 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
