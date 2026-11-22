"""Running the app: production (`serve`) and the tests (`start`) share one runner factory.

aiohttp answers a request it can't parse (a header over 8190 bytes, a control character in a header, a bad chunk
size, ...) itself, before the app and its middlewares see it, and both its 400 body and its log line quote the
parser's message, which holds raw request bytes: a bearer token included. `start` keeps them out of both. The
connection handler answers those with a fixed "400 Bad Request" and logs the error type only, and every aiohttp
logger drops exception text and tracebacks (RequestBytesFilter). aiohttp 3.14 has no option for the first part:
its runner always builds a plain RequestHandler, so `start` gives the runner's server a subclass that hands out
_QuietRequestHandler instead (tests/test_review_b2.py pins the result).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

from aiohttp import web
from aiohttp.http_exceptions import HttpProcessingError

from .app import make_app
from .config import Config
from .push import Sender

log = logging.getLogger("mission_control")
SHUTDOWN_TIMEOUT = 3.0
REDACTED_LOGGERS = ("aiohttp.server", "aiohttp.web", "aiohttp.internal", "aiohttp.access")


class RequestBytesFilter(logging.Filter):
    """An aiohttp record keeps its line and its exception's type. The exception's text and traceback, which can
    quote raw request bytes, are dropped."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        if exc is not None or record.exc_text:
            try:
                line = record.getMessage()
            except Exception:  # noqa: BLE001 - a malformed record still gets redacted
                line = str(record.msg)
            record.msg = "%s (%s; details withheld)"
            record.args = (line, type(exc).__name__ if exc is not None else "exception")
            record.exc_info = None
            record.exc_text = None
        return True


def redact_aiohttp_logs() -> None:
    for name in REDACTED_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RequestBytesFilter) for f in logger.filters):
            logger.addFilter(RequestBytesFilter())


class _QuietRequestHandler(web.RequestHandler):
    """aiohttp's connection handler, except for a request it couldn't parse: a fixed body, the error type logged."""

    __slots__ = ()  # the same layout as RequestHandler, so a handler's class can be swapped (_QuietServer)

    def handle_error(self, request: Any, status: int = 500, exc: BaseException | None = None,
                     message: str | None = None) -> web.StreamResponse:
        if isinstance(exc, HttpProcessingError):
            log.warning("refused a request that couldn't be parsed: HTTP %d (%s)", status, type(exc).__name__)
            try:
                phrase = HTTPStatus(status).phrase
            except ValueError:
                phrase = "Error"
            return super().handle_error(request, status, None, f"{status} {phrase}")
        return super().handle_error(request, status, exc, message)


class _QuietServer(web.Server):
    def __call__(self) -> web.RequestHandler:
        handler = super().__call__()
        handler.__class__ = _QuietRequestHandler
        return handler


async def start(cfg: Config, *, port: int | None = None, background: bool = True, sender: Sender | None = None,
                clock: Callable[[], float] = time.time) -> web.AppRunner:
    redact_aiohttp_logs()
    runner = web.AppRunner(make_app(cfg, background=background, sender=sender, clock=clock),
                           shutdown_timeout=SHUTDOWN_TIMEOUT, access_log=None, handler_cancellation=True)
    await runner.setup()
    assert runner.server is not None
    runner.server.__class__ = _QuietServer  # its connections get _QuietRequestHandler (see the module docstring)
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
