"""``python3 -m skill_drafter [--config FILE] serve | run-once | status``.

* ``serve``: the poll loop (every ``poll_interval`` seconds) for ``skill-drafter.service``.
* ``run-once``: one pass, then exit (0 = no errors, 1 = some attempt failed, 2 = could not run).
* ``status``: per request, whether a draft is due, waiting (and why), done or failed.

Logs go to stderr (the journal under systemd).
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Sequence

from library import LibraryError

from .config import ConfigError, load
from .drafter import Drafter

DEFAULT_CONFIG = "/etc/skill-drafter/config.json"
log = logging.getLogger("skill_drafter")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skill-drafter", description="Skill Forge drafter (CONTRACTS §12.19).")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"JSON config (default {DEFAULT_CONFIG})")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    sub.add_parser("serve", help="poll forever (the service)")
    sub.add_parser("run-once", help="one pass, then exit")
    sub.add_parser("status", help="what is due, waiting, done or failed per request")
    return parser


def serve(drafter: Drafter, interval: float, stop: threading.Event | None = None) -> int:
    stop = stop or threading.Event()

    def on_signal(signum: int, _frame: object) -> None:
        log.info("signal %d: stopping after this pass", signum)
        stop.set()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
    log.info("skill drafter serving (poll every %ss)", interval)
    while not stop.is_set():
        try:
            drafter.run_once()
        except Exception:  # noqa: BLE001 - e.g. the library is not mounted; try again next poll
            log.exception("pass failed")
        stop.wait(interval)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        cfg = load(args.config)
        drafter = Drafter(cfg)
    except (ConfigError, ValueError) as exc:
        print(f"skill-drafter: config: {exc}", file=sys.stderr)
        return 2
    if args.command == "serve":
        return serve(drafter, cfg["poll_interval"])
    try:
        if args.command == "status":
            for line in drafter.status():
                print(line)
            return 0
        report = drafter.run_once()
    except (LibraryError, OSError) as exc:
        print(f"skill-drafter: {exc}", file=sys.stderr)
        return 2
    print(report.summary())
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
