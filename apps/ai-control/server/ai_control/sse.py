"""SSE /api/events: a `summary` ping whenever the summary hash changes, plus keepalive comments.

- At most `max_clients` streams. A slot is reserved synchronously (before any await) and released in a
  `finally`, so a client that disconnects (its handler is cancelled) always frees it.
- Backpressure: pings are coalesced (one pending flag per client, never a queue), and a client is dropped
  when its transport has more than `buffer_limit` bytes buffered or a write stalls for `write_timeout`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

log = logging.getLogger("ai_control.sse")


class Dropped(Exception):
    pass


class Client:
    __slots__ = ("pending",)

    def __init__(self) -> None:
        self.pending = asyncio.Event()


class EventHub:
    def __init__(self, max_clients: int, buffer_limit: int, write_timeout: float, keepalive: float) -> None:
        self.max_clients = max_clients
        self.buffer_limit = buffer_limit
        self.write_timeout = write_timeout
        self.keepalive = keepalive
        self.clients: set[Client] = set()
        self.latest: str | None = None
        self.dropped = 0

    def reserve(self) -> Client | None:
        """Synchronous: no await between the check and the add."""
        if len(self.clients) >= self.max_clients:
            return None
        client = Client()
        self.clients.add(client)
        return client

    def release(self, client: Client) -> None:
        self.clients.discard(client)

    def notify(self, core_hash: str) -> None:
        self.latest = core_hash
        for client in self.clients:
            client.pending.set()

    def too_backed_up(self, request: web.Request) -> bool:
        transport = request.transport
        if transport is None or transport.is_closing():
            return True
        return transport.get_write_buffer_size() > self.buffer_limit

    async def _send(self, request: web.Request, resp: web.StreamResponse, text: str) -> None:
        if self.too_backed_up(request):
            raise Dropped("client not reading (buffer over limit)")
        try:
            await asyncio.wait_for(resp.write(text.encode()), self.write_timeout)
        except asyncio.TimeoutError:
            raise Dropped("write stalled") from None
        except (ConnectionError, RuntimeError):
            raise Dropped("connection closed") from None

    def _event(self) -> str:
        return f"event: summary\ndata: {json.dumps({'hash': self.latest}, separators=(',', ':'))}\n\n"

    async def handle(self, request: web.Request) -> web.StreamResponse:
        client = self.reserve()  # before any await
        if client is None:
            return web.json_response({"error": "too many event streams"}, status=503, headers={"Retry-After": "10"})
        try:
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream", "X-Accel-Buffering": "no"},
            )
            await resp.prepare(request)
            await self._send(request, resp, "retry: 10000\n\n" + self._event())
            while True:
                try:
                    await asyncio.wait_for(client.pending.wait(), self.keepalive)
                except asyncio.TimeoutError:
                    await self._send(request, resp, ": keepalive\n\n")
                    continue
                client.pending.clear()
                await self._send(request, resp, self._event())
        except Dropped as exc:
            self.dropped += 1
            log.info("event stream dropped: %s", exc)
            return resp
        finally:
            self.release(client)

    def stats(self) -> dict[str, Any]:
        return {"clients": len(self.clients), "max": self.max_clients, "dropped": self.dropped}
