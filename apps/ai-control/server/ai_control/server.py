"""How the server is run. Production (`serve`) and the tests (`start`) share one runner factory.

Requests the HTTP parser rejects (bad request line, malformed URL, bad headers) never reach the app, so
aiohttp would answer them as text/plain without our headers and log a traceback. `_Handler` answers a
client-caused parse error with one WARNING line and a JSON `{"error"}` 400 carrying the CSP/no-store
headers. (It relies on aiohttp 3.13 internals: AppRunner._make_server returning a plain web.Server; the
tests cover it.)
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from aiohttp import web
from aiohttp.http_exceptions import BadHttpMethod, HttpProcessingError

from .app import make_app, security_headers
from .config import Config

log = logging.getLogger("ai_control")

RUNNER_SETTINGS: dict[str, Any] = {
    "handler_cancellation": True,  # a client leaving cancels its handler: frees SSE slots at once
    "access_log": None,
}
SHUTDOWN_TIMEOUT = 3.0


def _raw_400() -> bytes:
    body = b'{"error": "bad request"}'
    headers = {"Content-Type": "application/json; charset=utf-8", "Content-Length": str(len(body)), "Connection": "close"}
    security_headers("/api/", headers)
    head = "HTTP/1.1 400 Bad Request\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
    return head.encode("ascii") + body


RAW_400 = _raw_400()


class _Handler(web.RequestHandler):
    def data_received(self, data: bytes) -> None:
        try:
            super().data_received(data)
        except ValueError:
            # aiohttp 3.13's C parser lets yarl's ValueError escape for some request targets
            # (e.g. "GET http://[bad/"); asyncio would log a fatal-error traceback and drop the connection.
            log.warning("unparseable request: invalid request target")
            transport = self.transport
            if transport is not None and not transport.is_closing():
                transport.write(RAW_400)
                transport.close()

    def handle_error(
        self,
        request: web.BaseRequest,
        status: int = 500,
        exc: BaseException | None = None,
        message: str | None = None,
    ) -> web.StreamResponse:
        if status < 500 and isinstance(exc, HttpProcessingError) and request.writer.output_size == 0:
            detail = " ".join(str(exc.message).split())[:200] or type(exc).__name__
            level = logging.DEBUG if isinstance(exc, BadHttpMethod) else logging.WARNING
            log.log(level, "unparseable request: %s: %s", type(exc).__name__, detail)
            resp: web.StreamResponse = web.json_response({"error": "bad request"}, status=status)
            resp.force_close()
            security_headers("/api/", resp.headers)
            return resp
        resp = super().handle_error(request, status, exc, message)
        security_headers("/api/", resp.headers)
        return resp


class _Server(web.Server):
    def __call__(self) -> web.RequestHandler:
        return _Handler(self, loop=self._loop, **self._kwargs)


class Runner(web.AppRunner):
    async def _make_server(self) -> web.Server:
        server = await super()._make_server()
        server.__class__ = _Server
        return server


def make_runner(config: Config, *, background: bool = True) -> web.AppRunner:
    return Runner(make_app(config, background=background), shutdown_timeout=SHUTDOWN_TIMEOUT, **RUNNER_SETTINGS)


async def start(
    config: Config, *, host: str | None = None, port: int | None = None, background: bool = True
) -> web.AppRunner:
    runner = make_runner(config, background=background)
    await runner.setup()
    site = web.TCPSite(runner, config.listen_host if host is None else host, config.listen_port if port is None else port)
    await site.start()
    return runner


def bound_port(runner: web.AppRunner) -> int:
    return int(runner.addresses[0][1])


async def serve(config: Config, stop: asyncio.Event | None = None) -> None:
    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    runner = await start(config)
    log.info("listening on %s:%d", config.listen_host, bound_port(runner))
    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        await runner.cleanup()
        log.info("stopped")
