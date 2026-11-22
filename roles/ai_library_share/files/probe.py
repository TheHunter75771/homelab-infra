"""Read-mostly check of an AI Library root (docs/CONTRACTS.md §12.17.2/3 verify). Prints one JSON object.

Always: the sentinel is a regular file and Library.open(root) succeeds.
--write: writes a probe file under 00-inbox/ (the library's own inbox; a hidden name, so `inbox`
         never files a leftover), reads it back and deletes it.
--symlink: tries to create a symlink under 00-inbox/; reports whether that worked (and removes it).
Exit 0 when it ran (the caller asserts the fields), 2 on bad usage.
Usage: python3 -I -B probe.py --lib DIR --root ROOT [--sentinel NAME] [--write] [--symlink]
"""
import argparse
import json
import os
import secrets
import sys


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--sentinel", default=".ai-library-sentinel")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--symlink", action="store_true")
    args = ap.parse_args(argv)
    out = {"sentinel": False, "open": False, "write": None, "symlink_created": None, "errors": []}

    sent = os.path.join(args.root, args.sentinel)
    out["sentinel"] = os.path.isfile(sent) and not os.path.islink(sent)
    sys.path.insert(0, args.lib)
    try:
        from library import Library  # noqa: PLC0415
        lib = Library.open(args.root)
        out["open"] = True
    except Exception as exc:  # report, never trace back: the caller prints only these strings
        out["errors"].append(f"open: {type(exc).__name__}: {exc}")
        print(json.dumps(out))
        return 0
    inbox = os.path.join(str(lib.root), "00-inbox")
    tag = secrets.token_hex(8)

    if args.write:
        path = os.path.join(inbox, f".probe-ai-library-share-{tag}")
        data = f"ai-library-share probe {tag}\n".encode()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            with open(path, "rb") as fh:
                back = fh.read()
            os.unlink(path)
            out["write"] = back == data and not os.path.lexists(path)
            if back != data:
                out["errors"].append("write: read back different bytes")
        except OSError as exc:
            out["write"] = False
            out["errors"].append(f"write: {exc.strerror or exc}")
            if os.path.lexists(path):
                try:
                    os.unlink(path)
                except OSError:
                    out["errors"].append(f"write: could not remove {path}")

    if args.symlink:
        link = os.path.join(inbox, f".probe-ai-library-share-{tag}.link")
        try:
            os.symlink("../README.md", link)
        except OSError:
            out["symlink_created"] = False
        else:
            out["symlink_created"] = True
            try:
                os.unlink(link)
            except OSError:
                out["errors"].append(f"symlink: could not remove {link}")

    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
