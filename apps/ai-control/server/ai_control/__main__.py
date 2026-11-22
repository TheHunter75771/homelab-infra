"""Entry point: python3 -m ai_control [--config PATH] [--check]  (or env AI_CONTROL_CONFIG)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from . import __version__
from .config import ConfigError, load
from .server import serve

DEFAULT_CONFIG = "/etc/ai-control/config.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai-control", description="AI Control dashboard server (read-only).")
    parser.add_argument(
        "--config",
        default=os.environ.get("AI_CONTROL_CONFIG", DEFAULT_CONFIG),
        help=f"JSON config file (env AI_CONTROL_CONFIG, default {DEFAULT_CONFIG})",
    )
    parser.add_argument("--check", action="store_true", help="validate the config and exit")
    args = parser.parse_args(argv)

    try:
        config = load(args.config)
    except ConfigError as exc:
        print(f"ai-control: {exc}", file=sys.stderr)
        return 2
    if args.check:
        print(f"ai-control: {args.config} OK")
        return 0

    logging.basicConfig(
        level=config.log_level,
        stream=sys.stderr,
        format="%(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("ai_control").info("ai-control %s starting", __version__)
    asyncio.run(serve(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
