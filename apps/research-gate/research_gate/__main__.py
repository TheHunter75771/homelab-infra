"""``python3 -m research_gate [--config FILE] serve | run-once | status``.

* ``serve``: the poll loop (every ``poll_interval`` seconds) for ``research-gate.service``.
* ``run-once``: one pass, then exit (0 = no errors, 1 = some item failed, 2 = could not run,
  3 = the host's interface tables are unreadable).
* ``serve`` and ``run-once`` first check that ``/proc/self/net`` is readable (the address rules
  need it) and exit 3 with a clear message if not, so systemd shows the failure.
* ``status``: pending searches and fetches per request (targets printed as Python reprs).

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
from .gate import Gate

DEFAULT_CONFIG = "/etc/research-gate/config.json"
log = logging.getLogger("research_gate")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research-gate", description="Skill Forge research gate (CONTRACTS §12.16).")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"JSON config (default {DEFAULT_CONFIG})")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    sub.add_parser("serve", help="poll forever (the service)")
    sub.add_parser("run-once", help="one pass, then exit")
    sub.add_parser("status", help="pending searches and fetches per request")
    return parser


def serve(gate: Gate, interval: float, stop: threading.Event | None = None) -> int:
    stop = stop or threading.Event()

    def on_signal(signum: int, _frame: object) -> None:
        log.info("signal %d: stopping after this pass", signum)
        stop.set()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
    log.info("research gate serving (poll every %ss)", interval)
    while not stop.is_set():
        try:
            gate.run_once()
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
        gate = Gate(cfg)
    except (ConfigError, ValueError) as exc:
        print(f"research-gate: config: {exc}", file=sys.stderr)
        return 2
    if args.command in ("serve", "run-once"):
        problem = gate.preflight()
        if problem:
            print(f"research-gate: cannot start: {problem}. Every fetch would be refused; fix the unit "
                  "(no ProcSubset=pid) or the host, then restart.", file=sys.stderr)
            return 3
    if args.command == "serve":
        return serve(gate, cfg["poll_interval"])
    try:
        if args.command == "status":
            for line in gate.status():
                print(line)
            return 0
        report = gate.run_once()
    except (LibraryError, OSError) as exc:
        print(f"research-gate: {exc}", file=sys.stderr)
        return 2
    print(report.summary())
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
