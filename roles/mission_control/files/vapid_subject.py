"""Set the `subject` of Mission Control's existing vapid.json, never touching the keys (live bug 2026-10-28: the role's
old default `https://<fqdn>:8443` has a port, which py_vapid refuses to sign with, so every push failed).

Usage: <venv python> -I -B vapid_subject.py <path> <subject>

The keys must already be checked (vapid_check.py --keys-only). If the subject already equals <subject>, nothing is
written and it prints "unchanged". Otherwise it writes {"privateKey", "publicKey", "subject"} with the two key strings
exactly as read, to a temporary file in the same directory (O_EXCL|O_NOFOLLOW, the original's owner, group and mode
set before any byte is written), fsyncs it, renames it over the original and fsyncs the directory, then re-reads the
result and checks the keys are the same strings. It prints "updated". Anything else prints "bad: <reason>" and exits 1,
leaving the file as it was. It never prints key material.
"""

import json
import os
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vapid_check import subject_ok  # noqa: E402  (the one subject rule, next to this script)


def read(path):
    with open(path, encoding="ascii") as fh:
        return json.load(fh)


def main(argv):
    if len(argv) != 3:
        print("bad: usage: vapid_subject.py <path> <subject>")
        return 1
    path, subject = argv[1], argv[2]
    if not subject_ok(subject):
        print("bad: the new subject is not one py_vapid signs with")
        return 1
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        print("bad: not a regular file")
        return 1
    data = read(path)
    if not isinstance(data, dict) or not {"privateKey", "publicKey"} <= set(data) <= {"privateKey", "publicKey", "subject"}:
        print("bad: unexpected keys")
        return 1
    if data.get("subject") == subject:
        print("unchanged")
        return 0
    keys = (data["privateKey"], data["publicKey"])
    body = json.dumps({"privateKey": keys[0], "publicKey": keys[1], "subject": subject}, indent=2, sort_keys=True)
    directory = os.path.dirname(os.path.abspath(path))
    tmp = os.path.join(directory, ".vapid-subject.%d.tmp" % os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            os.fchown(fd, st.st_uid, st.st_gid)
            os.fchmod(fd, stat.S_IMODE(st.st_mode))
            os.write(fd, (body + "\n").encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    after = read(path)
    if (after.get("privateKey"), after.get("publicKey")) != keys or after.get("subject") != subject:
        print("bad: the rewritten file doesn't hold the same keys")
        return 1
    print("updated")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
