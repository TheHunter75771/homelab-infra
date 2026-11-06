"""calendar-view [--config PATH] | --check-config PATH

Serves the wall-screen page on the configured loopback address and refreshes every calendar once a
minute. The config path comes from --config, else $CALENDAR_VIEW_CONFIG, else
/etc/homelab-calendar-view/config.json. --check-config validates a file and exits: 0 when good, 2 with
a one-line reason (never a value from the file) when not; role calendar_view runs it before writing.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading

from . import __version__
from .config import DEFAULT_PATH, ENV_VAR, ConfigError, load

log = logging.getLogger("calendar_view")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="calendar-view", description="Wall-screen calendar view (read-only).")
    ap.add_argument("--config", default=os.environ.get(ENV_VAR) or DEFAULT_PATH, help="JSON settings file")
    ap.add_argument("--check-config", metavar="PATH", help="validate a settings file and exit")
    ap.add_argument("--version", action="version", version=f"calendar-view {__version__}")
    args = ap.parse_args(argv)

    if args.check_config:
        try:
            load(args.check_config)
        except ConfigError as e:
            print(f"calendar-view: {e}", file=sys.stderr)
            return 2
        return 0

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    try:
        cfg = load(args.config)
    except ConfigError as e:
        log.error("%s", e)
        return 2

    # Imported here so --check-config needs only the standard library.
    from .caldav import Client, fetch_all
    from .server import ViewServer
    from .service import AgendaService

    client = Client(cfg.base_url, cfg.user, cfg.password)
    service = AgendaService(cfg, fetch=lambda start, end: fetch_all(client, cfg.calendars, start, end))
    try:
        server = ViewServer((cfg.listen_host, cfg.listen_port), service)
    except OSError as e:
        log.error("can't listen on %s:%d: %s", cfg.listen_host, cfg.listen_port, e.strerror)
        return 1
    stop = threading.Event()
    threading.Thread(target=service.run, args=(stop,), name="refresh", daemon=True).start()
    log.info("calendar-view %s on %s:%d: %d calendars from %s as %s, %d days in %s", __version__,
             cfg.listen_host, server.server_port, len(cfg.calendars), cfg.base_url, cfg.user, cfg.days, cfg.timezone)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
