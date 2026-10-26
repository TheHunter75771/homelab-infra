"""Entry point: python -m gpu_broker [--config PATH] [--check | --healthcheck]  (or env GPU_BROKER_CONFIG)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from . import __version__
from .config import ConfigError, load
from .server import healthcheck, serve

DEFAULT_CONFIG = "/etc/gpu-broker/config.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gpu-broker", description="Ollama-compatible GPU/CPU routing proxy.")
    parser.add_argument(
        "--config",
        default=os.environ.get("GPU_BROKER_CONFIG", DEFAULT_CONFIG),
        help=f"JSON config file (env GPU_BROKER_CONFIG, default {DEFAULT_CONFIG})",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="validate the config and exit")
    mode.add_argument(
        "--healthcheck",
        action="store_true",
        help="exit 0 if the broker answers /broker/healthz on the config's listen_host/listen_port",
    )
    args = parser.parse_args(argv)

    try:
        config = load(args.config)
    except ConfigError as exc:
        print(f"gpu-broker: {exc}", file=sys.stderr)
        return 2
    if args.check:
        print(f"gpu-broker: {args.config} OK ({len(config.aliases)} aliases)")
        return 0
    if args.healthcheck:
        return healthcheck(config)

    logging.basicConfig(
        level=config.log_level,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("gpu_broker").info(
        "gpu-broker %s starting (%d aliases, status_ttl=%ss)", __version__, len(config.aliases), config.status_ttl
    )
    asyncio.run(serve(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
