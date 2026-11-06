"""Running the app: production (`serve`) and the tests (`start`) share one runner factory."""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Callable

from aiohttp import web

from .app import make_app
from .config import Config
from .push import Sender

log = logging.getLogger("mission_control")
SHUTDOWN_TIMEOUT = 3.0


async def start(cfg: Config, *, port: int | None = None, background: bool = True, sender: Sender | None = None,
                clock: Callable[[], float] = time.time) -> web.AppRunner:
    runner = web.AppRunner(make_app(cfg, background=background, sender=sender, clock=clock),
                           shutdown_timeout=SHUTDOWN_TIMEOUT, access_log=None, handler_cancellation=True)
    await runner.setup()
    site = web.TCPSite(runner, cfg.listen_host, cfg.listen_port if port is None else port)
    await site.start()
    return runner


def bound_port(runner: web.AppRunner) -> int:
    return int(runner.addresses[0][1])


async def serve(cfg: Config, stop: asyncio.Event | None = None) -> None:
    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    runner = await start(cfg)
    log.info("listening on %s:%d", cfg.listen_host, bound_port(runner))
    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        await runner.cleanup()
