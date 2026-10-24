#!/usr/bin/env python3
"""Stand-in for the sqlite3 CLI, used by test_backup.sh only when sqlite3 isn't installed.

Implements exactly the subset homelab-backup uses, on the real SQLite library:
  sqlite3 [-init FILE] [-bail] [-cmd CMD]... DBFILE [CMD]...
  CMD: ".timeout MS" | ".backup 'PATH'" | ".backup PATH" | any SQL (rows printed '|'-joined)
`.backup` is the same sqlite3_backup online-backup API the CLI calls.
"""
import shlex
import sqlite3
import sys


def run(conn, cmd):
    if cmd.startswith("."):
        parts = shlex.split(cmd)
        if parts[0] == ".timeout":
            conn.execute(f"PRAGMA busy_timeout = {int(parts[1])}")
        elif parts[0] == ".backup":
            dst = sqlite3.connect(parts[-1])
            with dst:
                conn.backup(dst)
            dst.close()
        else:
            raise SystemExit(f"fake sqlite3: unsupported dot-command {parts[0]}")
        return
    for row in conn.execute(cmd):
        print("|".join("" if v is None else str(v) for v in row))


def main(argv):
    pre, args = [], []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-bail":
            pass
        elif a == "-init":
            i += 1  # init file: only ever /dev/null here
        elif a == "-cmd":
            i += 1
            pre.append(argv[i])
        else:
            args.append(a)
        i += 1
    db, cmds = args[0], args[1:]
    try:
        conn = sqlite3.connect(db)
        for c in pre + cmds:
            run(conn, c)
        conn.close()
    except sqlite3.Error as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
