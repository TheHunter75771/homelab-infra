"""``forge-api [--config FILE] serve | check-config [--require-library]``.

* ``serve``: the HTTP service for ``forge-api.service``. With systemd socket activation it serves
  the inherited socket (refused unless it is one listening TCP socket on the configured address
  and port); otherwise it binds itself. Exits 2 if the config, the token file or an inherited
  socket is refused, 3 if the address cannot be bound. An unmounted library does not stop it: each call
  answers 503 until the share is back.
* ``check-config``: validate the config and the token file (never printing the token) and say
  whether the library opens. Exit 0 if usable, 2 if not; with ``--require-library`` an
  unavailable library is also exit 2.

Logs go to stderr (the journal under systemd), through a formatter that scrubs the token.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Sequence

from library import Library, LibraryError

from .activation import ActivationError, inherited_socket
from .config import ConfigError, load, load_token
from .server import App, ForgeApiServer, RedactingFormatter

DEFAULT_CONFIG = "/etc/forge-api/config.json"
LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"
log = logging.getLogger("forge_api")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge-api", description="Skill Forge HTTP API for Open WebUI (CONTRACTS §12.25).")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"JSON config (default {DEFAULT_CONFIG})")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    sub.add_parser("serve", help="run the HTTP service")
    check = sub.add_parser("check-config", help="validate the config and the token file")
    check.add_argument("--require-library", action="store_true", help="also fail if the library does not open")
    return parser


def setup_logging(verbose: bool, secrets: list[str]) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, secrets))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def serve(server: ForgeApiServer, stop: threading.Event | None = None) -> int:
    stop = stop or threading.Event()

    def on_signal(signum: int, _frame: object) -> None:
        log.info("signal %d: stopping", signum)
        stop.set()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    log.info("forge-api listening on %s port %d", host, port)
    try:
        stop.wait()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load(args.config)
        token = load_token(cfg["token_file"])
    except ConfigError as exc:
        print(f"forge-api: config: {exc}", file=sys.stderr)
        return 2
    setup_logging(args.verbose, [token])
    if args.command == "check-config":
        try:
            Library.open(cfg["library_root"])
            lib_state = "opens"
        except (LibraryError, OSError) as exc:
            lib_state = f"NOT available ({exc})"
        print(f"config ok: bind {cfg['bind']} port {cfg['port']}; allowed peers "
              f"{', '.join(cfg['allowed_peers'])}; approver set; token file ok; library {lib_state}")
        return 2 if args.require_library and lib_state != "opens" else 0
    try:
        sock = inherited_socket(cfg["bind"], cfg["port"])
    except ActivationError as exc:
        print(f"forge-api: socket activation: {exc}", file=sys.stderr)
        return 2
    try:
        server = ForgeApiServer(App(cfg, token), cfg["bind"], cfg["port"], sock=sock)
    except OSError as exc:
        print(f"forge-api: cannot listen on {cfg['bind']} port {cfg['port']}: {exc.strerror or exc}",
              file=sys.stderr)
        return 3
    return serve(server)


if __name__ == "__main__":
    sys.exit(main())
