"""How the broker is served. Production (`serve`) and the tests (`start`) share one runner factory.

`handler_cancellation=True` is load-bearing: when a client disconnects, aiohttp cancels the handler, the
handler closes its backend connection, and Ollama stops generating. `web.run_app` defaults to False
(and aiohttp's TestServer forces True), so the broker never uses either: everything goes through
`make_runner`, and the tests run on exactly these settings.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import urllib.request
from typing import Any

from aiohttp import web
from aiohttp.http_exceptions import BadHttpMethod, HttpProcessingError

from .app import make_app
from .config import Config

log = logging.getLogger("gpu_broker")

RUNNER_SETTINGS: dict[str, Any] = {
    "handler_cancellation": True,  # a client leaving cancels its handler (and so the upstream request)
    "access_log": None,  # the broker writes its own one line per request
}


class _BrokerRequestHandler(web.RequestHandler):
    """aiohttp's connection handler, with broker-style answers to requests it cannot even parse.

    Requests the HTTP parser rejects (`GET *`, `GET host:80`, bad headers) never reach the app, so the
    app's middleware and response hooks can't touch them. aiohttp logs each one as ERROR with a
    traceback and answers text/plain. Here a client-caused parse error (4xx HttpProcessingError) is one
    WARNING line and a JSON `{"error"}` 400 with `X-Backend: broker`. Everything else (5xx) keeps
    aiohttp's handling and only gains the header.
    """

    def handle_error(
        self,
        request: web.BaseRequest,
        status: int = 500,
        exc: BaseException | None = None,
        message: str | None = None,
    ) -> web.StreamResponse:
        if status < 500 and isinstance(exc, HttpProcessingError) and request.writer.output_size == 0:
            detail = " ".join(str(exc.message).split()) or type(exc).__name__
            # A non-HTTP first line (TLS, scanners) is noise: debug, as aiohttp itself does.
            level = logging.DEBUG if isinstance(exc, BadHttpMethod) else logging.WARNING
            log.log(level, "unparseable request from %s: %s: %s", request.remote, type(exc).__name__, detail)
            resp: web.StreamResponse = web.json_response({"error": f"gpu-broker: bad request: {detail}"}, status=status)
            resp.force_close()
        else:
            resp = super().handle_error(request, status, exc, message)
        resp.headers["X-Backend"] = "broker"
        return resp


class _BrokerServer(web.Server):
    def __call__(self) -> web.RequestHandler:
        return _BrokerRequestHandler(self, loop=self._loop, **self._kwargs)


class BrokerRunner(web.AppRunner):
    """AppRunner whose connections use _BrokerRequestHandler (relies on aiohttp 3.14 internals:
    AppRunner._make_server returning a plain web.Server; the tests cover it)."""

    async def _make_server(self) -> web.Server:
        server = await super()._make_server()
        server.__class__ = _BrokerServer
        return server


def make_runner(config: Config) -> web.AppRunner:
    return BrokerRunner(make_app(config), shutdown_timeout=config.shutdown_timeout, **RUNNER_SETTINGS)


async def start(config: Config, *, host: str | None = None, port: int | None = None) -> web.AppRunner:
    """Set up the production runner and listen. host/port override the config (tests bind port 0)."""
    runner = make_runner(config)
    await runner.setup()
    site = web.TCPSite(
        runner,
        config.listen_host if host is None else host,
        config.listen_port if port is None else port,
    )
    await site.start()
    return runner


def bound_port(runner: web.AppRunner) -> int:
    return int(runner.addresses[0][1])


async def serve(config: Config, stop: asyncio.Event | None = None) -> None:
    """Run until SIGTERM/SIGINT (or `stop` is set), then shut down gracefully.

    In-flight requests get `shutdown_timeout` seconds; then they are cancelled, which closes their
    backend connections. The HTTP client sessions close only after every handler has finished.
    """
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
        log.info("shutting down (in-flight requests get %ss)", config.shutdown_timeout)
        await runner.cleanup()
        log.info("stopped")


def healthcheck(config: Config, timeout: float = 2.0) -> int:
    """Exit status for the container HEALTHCHECK: 0 if /broker/healthz answers 200 on the configured port."""
    host = config.listen_host
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = f"[{host}]"
    url = f"http://{host}:{config.listen_port}/broker/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # fixed http:// URL built from the config
            return 0 if resp.status == 200 else 1
    except OSError:
        return 1
