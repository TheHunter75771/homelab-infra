"""``finance-digest [--config FILE] run [--date YYYY-MM-DD] [--dry-run]``

* ``run``: one digest for today (in the configured time zone) or ``--date``; written to the AI
  Library's ``50-finance/log/YYYY-MM.md`` and, when configured, appended to Notion.
* ``--dry-run``: print the entry to stdout and write nothing (no library, no Notion).

Exit codes: 0 ok; 1 written, but some Money Hub read failed; 2 nothing written. Logs go to stderr.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from collections.abc import Sequence

from . import config, digest


def _date(value: str) -> dt.date:
    try:
        if len(value) != 10:
            raise ValueError
        return dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD, e.g. 2026-10-27") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finance-digest", description="Daily read-only Money Hub digest (CONTRACTS section 12.18).")
    parser.add_argument("--config", default=config.DEFAULT_PATH, help=f"JSON config (default {config.DEFAULT_PATH})")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    run = sub.add_parser("run", help="write today's digest")
    run.add_argument("--date", type=_date, help="the day to write (default: today in the configured time zone)")
    run.add_argument("--dry-run", action="store_true", help="print the entry; write nothing")
    return parser


def main(argv: Sequence[str] | None = None, *, notion_connect=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s", force=True)
    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        print(f"finance-digest: config: {e}", file=sys.stderr)
        return 2
    return digest.run(cfg, day=args.date, dry_run=args.dry_run, notion_connect=notion_connect)


if __name__ == "__main__":
    sys.exit(main())
