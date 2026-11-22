"""``python3 -m voice_profile [--config FILE] build [--dry-run] [--no-ai] | status``.

* ``build``: extract, clean, stats and (unless ``--no-ai``) draft the profile. ``--dry-run`` writes
  nothing and prints the summary (and a valid draft) instead. Exit 0 = done, 1 = the samples were
  processed but the draft failed or was skipped for a reason, 2 = could not run.
* ``status``: what is built, the profile's status, where the next draft goes, the broker.

Logs go to stderr (the journal under systemd).
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from library import LibraryError

from .build import Builder
from .config import ConfigError, load

DEFAULT_CONFIG = "/etc/voice-profile/config.json"
log = logging.getLogger("voice_profile")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voice-profile", description="Noah's voice profile (CONTRACTS §12.21).")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"JSON config (default {DEFAULT_CONFIG})")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    b = sub.add_parser("build", help="clean the samples, compute the stats, draft the profile")
    b.add_argument("--dry-run", action="store_true", help="write nothing; print what would happen")
    b.add_argument("--no-ai", action="store_true", help="skip the model (samples-text and stats only)")
    sub.add_parser("status", help="what is built and the profile's status")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        cfg = load(args.config)
        builder = Builder(cfg)
    except (ConfigError, ValueError) as exc:
        print(f"voice-profile: config: {exc}", file=sys.stderr)
        return 2
    try:
        if args.command == "status":
            for line in builder.status():
                print(line)
            return 0
        report = builder.build(dry_run=args.dry_run, no_ai=args.no_ai, out=sys.stdout)
    except (LibraryError, OSError) as exc:
        print(f"voice-profile: {exc}", file=sys.stderr)
        return 2
    for name, why in report.excluded:
        print(f"excluded: {name}: {why}")
    for dropped, kept, score in report.merged:
        print(f"merged: {dropped} -> {kept} (Jaccard {score})")
    print(("dry run: " if report.dry_run else "") + report.summary())
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
