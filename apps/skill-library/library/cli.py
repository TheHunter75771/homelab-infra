"""Command line: ``python3 -m library [--root R] [--cap SIZE] <command> ...``."""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from typing import TextIO

from . import naming
from .core import DEFAULT_CAP, Clock, Library, parse_topic_map
from .safefs import LibraryError

ROOT_ENV = "AI_LIBRARY_ROOT"
CAP_ENV = "AI_LIBRARY_CAP"

EPILOG = f"""\
The root comes from --root or ${ROOT_ENV}; there is no default. The cap comes from --cap or
${CAP_ENV} (default 500GB; decimal units, GiB etc. for binary). file, inbox and archive rebuild
INDEX.md afterwards.

exit codes: 0 ok; 1 check found problems, inbox left items behind, or cap is at >= 90 %;
2 refused or failed (bad input, unsafe path, cap reached, not a library, I/O error);
3 cap is at >= 100 %."""


def _common() -> argparse.ArgumentParser:
    # SUPPRESS lets the options appear before or after the command without clobbering.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS,
                        help=f"library root folder (or ${ROOT_ENV}); required")
    common.add_argument("--cap", default=argparse.SUPPRESS,
                        help=f"soft cap, e.g. 500GB, 750GiB, 10MB (or ${CAP_ENV}; default 500GB)")
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    parser = argparse.ArgumentParser(
        prog="python3 -m library", parents=[common],
        description="File, index, cap and check the AI Library (CONTRACTS §12.11).",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser("init", parents=[common], help="create the layout, README.md and INDEX.md (safe to repeat)")

    p = sub.add_parser("file", parents=[common], help="copy a file into 10-sources/<topic>/ with a header")
    p.add_argument("path", help="the file to copy in (left untouched)")
    p.add_argument("--topic", required=True, help="kebab-case topic, e.g. home-network")
    p.add_argument("--title", help="title (default: from the file name or its header)")
    p.add_argument("--source", help="where it came from (default: the original file name)")
    p.add_argument("--tags", help="comma-separated tags, e.g. gpu,rocm")
    p.add_argument("--summary", help="one or two sentences")

    p = sub.add_parser("inbox", parents=[common], help="move 00-inbox files into 10-sources/<topic>/ and log each move")
    p.add_argument("--topic-map", metavar="FILE",
                   help="lines of '<topic> <glob>'; first glob matching the file name (any case) wins")
    p.add_argument("--default-topic", metavar="TOPIC", help="topic for files no rule matched (e.g. unsorted)")

    sub.add_parser("index", parents=[common], help="rebuild INDEX.md (identical bytes when nothing changed)")
    sub.add_parser("cap", parents=[common], help="show usage against the cap")

    p = sub.add_parser("archive", parents=[common], help="move an item (with its sidecar) or folder to 90-archive/")
    p.add_argument("path", help="path relative to the library root, or absolute inside it")

    sub.add_parser("check", parents=[common], help="report naming/front-matter/layout problems; never changes anything")
    return parser


def _safe(text: str) -> str:
    """One printable line whatever the text holds (undecodable bytes, control characters)."""
    return naming.display(text)


def _reindex(lib: Library, out: TextIO) -> None:
    changed, count = lib.index()
    print(f"INDEX.md {'updated' if changed else 'unchanged'} ({count} items)", file=out)


def main(argv: Sequence[str] | None = None, *, clock: Clock | None = None,
         environ: Mapping[str, str] | None = None, stdout: TextIO | None = None,
         stderr: TextIO | None = None) -> int:
    env = os.environ if environ is None else environ
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    args = build_parser().parse_args(argv)

    root = getattr(args, "root", None) or env.get(ROOT_ENV)
    if not root:
        print(f"error: no library root: pass --root or set {ROOT_ENV}", file=err)
        return 2
    try:
        cap_text = getattr(args, "cap", None) or env.get(CAP_ENV)
        cap = naming.parse_size(cap_text) if cap_text else DEFAULT_CAP
    except ValueError as exc:
        print(f"error: {exc}", file=err)
        return 2

    try:
        if args.command == "init":
            lib, created = Library.init(root, clock=clock, cap_bytes=cap)
            for item in created:
                print(f"created {item}", file=out)
            print(f"library ready at {lib.root}" if created else f"library at {lib.root} already complete", file=out)
            return 0

        lib = Library.open(root, clock=clock, cap_bytes=cap)

        if args.command == "file":
            tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags is not None else None
            res = lib.file(args.path, args.topic, title=args.title, source=args.source,
                           tags=tags, summary=args.summary)
            print(f"filed {_safe(res.item)}" + (f" (sidecar {_safe(res.sidecar)})" if res.sidecar else ""), file=out)
            if res.warning:
                print(f"warning: {res.warning}", file=err)
            _reindex(lib, out)
            return 0

        if args.command == "inbox":
            if not args.topic_map and not args.default_topic:
                print("error: inbox needs --topic-map FILE and/or --default-topic TOPIC", file=err)
                return 2
            rules = []
            if args.topic_map:
                try:
                    with open(args.topic_map, encoding="utf-8") as fh:
                        rules = parse_topic_map(fh.read())
                except OSError as exc:
                    raise LibraryError(f"cannot read the topic map {args.topic_map}: {exc.strerror}") from None
            res = lib.inbox(topic_map=rules, default_topic=args.default_topic)
            for src, outcome in res.repaired:
                print(f"repaired {_safe(src)}: {_safe(outcome)}", file=out)
            for src, dest in res.moved:
                print(f"moved {_safe(src)} -> {_safe(dest)}", file=out)
            for src, reason in res.left:
                print(f"left {_safe(src)}: {_safe(reason)}", file=out)
            for problem in res.problems:
                print(f"problem: {_safe(problem)}", file=err)
            if res.warning:
                print(f"warning: {res.warning}", file=err)
            print(f"inbox: {len(res.moved)} moved, {len(res.left)} left", file=out)
            if res.moved or res.repaired:
                _reindex(lib, out)
            if res.refused:
                print("error: stopped at the cap; nothing more was moved", file=err)
                return 2
            return 1 if (res.left or res.problems) else 0

        if args.command == "index":
            _reindex(lib, out)
            return 0

        if args.command == "cap":
            status = lib.cap_status()
            print(f"usage {status.describe()}: {status.status}", file=out)
            return {"ok": 0, "warn": 1, "full": 3}[status.status]

        if args.command == "archive":
            for src, dest in lib.archive(args.path):
                print(f"archived {_safe(src)} -> {_safe(dest)}", file=out)
            _reindex(lib, out)
            return 0

        if args.command == "check":
            problems = lib.check()
            for problem in problems:
                print(problem, file=out)
            print(f"check: {len(problems)} problem(s)" if problems else "check: ok", file=out)
            return 1 if problems else 0
    except LibraryError as exc:
        print(f"error: {_safe(str(exc))}", file=err)
        return 2
    except OSError as exc:  # disk full, permissions, a vanished mount: report, don't trace back
        where = f": {_safe(str(exc.filename))}" if exc.filename else ""
        print(f"error: {_safe(str(exc.strerror or exc))}{where}", file=err)
        return 2
    except ValueError as exc:  # FrontMatterError, UnicodeError (undecodable names/text), bad values
        print(f"error: {_safe(str(exc))}", file=err)
        return 2
    raise AssertionError(f"unhandled command {args.command}")  # argparse guarantees one of the above
