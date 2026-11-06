"""Strict smb.conf validation (docs/CONTRACTS.md §12.17.2: "testparm -s must pass").

testparm exits 0 even when it ignores an unknown or misspelt parameter, so a typo would silently
drop a security setting. It first fails any line that is not blank, a comment, a [section] or
`key = value` (testparm skips those silently too), then runs `testparm -s --suppress-prompt FILE` and fails (exit 1) on a
non-zero exit or any line that says a parameter was unknown, ignored or invalid, or any warning or error.
Used as the template task's `validate:` (on the rendered temp file, before it is installed) and again
by the verify. Usage: python3 -I -B testparm-strict.py [--testparm PROG] FILE
"""
import re
import subprocess
import sys

BAD = re.compile(r"unknown parameter|ignoring|invalid|error|warning|failed|can.t|cannot|not a valid", re.I)


def bare_lines(path):
    """Lines that are neither blank, a comment, a [section] nor `key = value` (testparm ignores them
    silently, e.g. a value lost its `=`). Continuation lines (trailing backslash) are joined first."""
    bad, buf = [], ""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for no, raw in enumerate(fh, 1):
            line = buf + raw.rstrip("\n")
            if line.endswith("\\"):
                buf = line[:-1]
                continue
            buf = ""
            text = line.strip()
            if not text or text[0] in "#;" or (text.startswith("[") and text.endswith("]")):
                continue
            if "=" not in text:
                bad.append(f"line {no}: no '=' in {text!r}")
    return bad


def main(argv):
    prog = "testparm"
    if len(argv) == 3 and argv[0] == "--testparm":
        prog, argv = argv[1], argv[2:]
    if len(argv) != 1:
        print("usage: testparm-strict.py [--testparm PROG] FILE", file=sys.stderr)
        return 2
    bare = bare_lines(argv[0])
    if bare:
        for line in bare:
            print(line, file=sys.stderr)
        return 1
    res = subprocess.run([prog, "-s", "--suppress-prompt", argv[0]], capture_output=True, text=True, check=False)
    bad = [line for line in res.stderr.splitlines() if BAD.search(line)]
    if res.returncode != 0 or bad or "[global]" not in res.stdout:
        print(f"testparm rc={res.returncode}", file=sys.stderr)
        for line in bad or res.stderr.splitlines()[-5:]:
            print(line, file=sys.stderr)
        return 1
    print(res.stdout, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
