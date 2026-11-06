"""Entrypoint: python3 -m ai_host_agent --config /etc/ai-host-agent/config.json [--check]"""

import argparse
import logging
import signal
import sys
import threading

from . import __version__
from .auth import BearerAuth, TokenError, load_token
from .config import ConfigError, load
from .server import Agent, AgentHTTPServer

log = logging.getLogger("ai_host_agent")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ai-host-agent", description=__doc__)
    ap.add_argument("--config", help="JSON config file (default: built-in defaults)")
    ap.add_argument("--check", action="store_true",
                    help="validate the config and the token file, then exit")
    args = ap.parse_args(argv)

    try:
        cfg = load(args.config)
    except (OSError, ConfigError) as e:
        print("ai-host-agent: config: %s" % e, file=sys.stderr)
        return 2
    try:
        token = load_token(cfg["token_file"])  # refuse to start without a safe token
    except TokenError as e:
        print("ai-host-agent: token: %s" % e, file=sys.stderr)
        return 2
    if args.check:
        print("ai-host-agent: config and token OK", file=sys.stderr)
        return 0

    logging.basicConfig(stream=sys.stderr, level=cfg["log_level"],
                        format="%(levelname)s %(name)s: %(message)s")
    agent = Agent(cfg, token, BearerAuth(token))
    del token
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    servers = []
    try:
        for host in cfg["binds"]:
            srv = AgentHTTPServer(host, cfg["port"], agent)
            threading.Thread(target=srv.serve_forever, name="http-" + host, daemon=True).start()
            servers.append(srv)
            log.info("ai-host-agent %s listening on %s:%d", __version__, host, cfg["port"])
    except OSError as e:
        log.error("bind failed: %s", e)
        for srv in servers:
            srv.shutdown()
            srv.server_close()
        return 1

    stop.wait()
    log.info("stopping")
    for srv in servers:
        srv.shutdown()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
