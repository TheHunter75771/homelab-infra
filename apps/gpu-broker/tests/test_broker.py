"""gpu-broker tests: a stub gpu-gate and stub GPU/CPU Ollamas as real local HTTP servers.

Run: python -m unittest discover -s tests -v   (from apps/gpu-broker, with aiohttp installed)
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import io
import json
import logging
import os
import signal
import socket
import sys
import tempfile
import time
import types
import unittest
import warnings
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer  # stubs only; the broker runs on the production runner
from yarl import URL

from gpu_broker import app as broker_app
from gpu_broker.app import BROKER, FINAL_CHUNK_BYTES, RECENT_SIZE, FinalChunk, _forward_headers, _has_body
from gpu_broker.config import Config, ConfigError, from_dict
from gpu_broker.server import RUNNER_SETTINGS, bound_port, healthcheck, start

APP_DIR = Path(__file__).resolve().parent.parent

# Keep expected warnings (gate down, failover) out of the test output; assertLogs still sees them.
logging.getLogger("gpu_broker").addHandler(logging.NullHandler())

ALIASES = {
    "chat": {"gpu": "qwen3:8b", "cpu": "qwen3:1.7b"},
    "fresh": {"gpu": "big:70b", "cpu": "not-on-cpu:1b"},
}
CPU_TAGS = [
    {"name": "qwen3:1.7b", "model": "qwen3:1.7b", "size": 1400, "digest": "d1", "details": {"family": "qwen3"}},
    {"name": "llama3.2:3b", "model": "llama3.2:3b", "size": 2000, "digest": "d2", "details": {"family": "llama"}},
    {"name": "chat", "model": "chat", "size": 1, "digest": "shadowed", "details": {}},
]
CPU_V1_MODELS = [
    {"id": "qwen3:1.7b", "object": "model", "created": 111, "owned_by": "library"},
    {"id": "llama3.2:3b", "object": "model", "created": 222, "owned_by": "library"},
]


HELD_PATHS = frozenset({"/api/chat", "/api/generate", "/v1/chat/completions"})
MIB = 1024 * 1024
# management_clients for tests whose (127.0.0.1) client is allowed model management (CONTRACTS 12.23).
LOCAL = ["127.0.0.1"]


def free_port() -> int:
    """A port nothing listens on (bound, then released)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StubGate:
    def __init__(self) -> None:
        self.mode = "available"  # available | unavailable | error | slow | badjson
        self.hits = 0
        app = web.Application()
        app.router.add_get("/status", self.status)
        self.server = TestServer(app, host="127.0.0.1")

    async def status(self, _request: web.Request) -> web.StreamResponse:
        self.hits += 1
        if self.mode == "error":
            return web.json_response({"error": "boom"}, status=500)
        if self.mode == "slow":
            await asyncio.sleep(3)
        if self.mode == "badjson":
            return web.Response(text="not json", content_type="application/json")
        available = self.mode in ("available", "slow")
        return web.json_response({"available": available, "reason": "test", "avg30": 1.0, "game": None})


class StubOllama:
    """A fake Ollama. Records every request; streaming endpoints hold their last chunk until released."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.requests: list[dict[str, Any]] = []
        self.mode = "ok"  # ok | status503 | hang | break_midstream | endless
        # When set, generation endpoints (echo, chat) answer with this instead (after mode's 503/hang).
        self.reply: Callable[[web.Request], Awaitable[web.StreamResponse]] | None = None
        self.release = asyncio.Event()
        self.sent_last = False
        self.write_failed = asyncio.Event()
        app = web.Application(client_max_size=1 << 30)
        app.router.add_get("/api/tags", self.tags)
        app.router.add_get("/v1/models", self.v1_models)
        app.router.add_post("/api/chat", self.chat)
        app.router.add_post("/v1/chat/completions", self.sse)
        app.router.add_route("*", "/{tail:.*}", self.echo)
        self.server = TestServer(app, host="127.0.0.1")

    async def _record(self, request: web.Request) -> Any:
        raw = await request.read()
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:  # JSONDecodeError and UnicodeDecodeError
            parsed = None
        self.requests.append(
            {"method": request.method, "path": request.raw_path, "raw": raw, "json": parsed, "headers": request.headers}
        )
        return parsed

    async def _pre(self, request: web.Request) -> web.StreamResponse | None:
        if self.mode == "status503":
            return web.json_response({"error": "gpu busy"}, status=503)
        # "hold" models a slow generation: only generation endpoints wait, /api/show etc. answer at once.
        if self.mode == "hang" or (self.mode == "hold" and request.path in HELD_PATHS):
            try:
                if self.mode == "hang":
                    await asyncio.sleep(5)
                else:
                    await asyncio.wait_for(self.release.wait(), 10)
            except asyncio.CancelledError:  # the broker closed the upstream connection
                self.write_failed.set()
                raise
        if self.reply is not None:
            return await self.reply(request)
        return None

    async def tags(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.json_response({"models": CPU_TAGS})

    async def v1_models(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.json_response({"object": "list", "data": CPU_V1_MODELS})

    async def echo(self, request: web.Request) -> web.StreamResponse:
        body = await self._record(request)
        early = await self._pre(request)
        if early is not None:
            return early
        return web.json_response({"backend": self.name, "path": request.path, "body": body}, headers={"X-Stub": "1"})

    async def chat(self, request: web.Request) -> web.StreamResponse:
        body = await self._record(request) or {}
        early = await self._pre(request)
        if early is not None:
            return early
        model = body.get("model")
        if body.get("stream") is False:
            return web.json_response({"model": model, "backend": self.name, "done": True})
        out = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await out.prepare(request)
        await out.write(json.dumps({"model": model, "backend": self.name, "done": False}).encode() + b"\n")
        if self.mode == "break_midstream":
            await asyncio.sleep(0.05)
            assert request.transport is not None
            request.transport.close()
            return out
        if self.mode == "endless":
            try:
                for i in range(500):
                    await asyncio.sleep(0.02)
                    await out.write(json.dumps({"i": i}).encode() + b"\n")
            except (ConnectionResetError, aiohttp.ClientConnectionResetError):
                self.write_failed.set()
            except asyncio.CancelledError:  # aiohttp cancels the handler when its client goes away
                self.write_failed.set()
                raise
            return out
        await asyncio.wait_for(self.release.wait(), 5)
        await out.write(json.dumps({"model": model, "done": True}).encode() + b"\n")
        self.sent_last = True
        await out.write_eof()
        return out

    async def sse(self, request: web.Request) -> web.StreamResponse:
        body = await self._record(request) or {}
        out = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await out.prepare(request)
        chunk = {"model": body.get("model"), "choices": [{"delta": {"content": "hi"}}]}
        await out.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        await asyncio.wait_for(self.release.wait(), 5)
        await out.write(b"data: [DONE]\n\n")
        self.sent_last = True
        await out.write_eof()
        return out


async def dropping_server() -> tuple[asyncio.Server, int]:
    """Accepts, reads the request head, closes without answering: a failure before the first byte."""

    async def on_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.close()

    server = await asyncio.start_server(on_conn, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


class BrokerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.gate = StubGate()
        self.gpu = StubOllama("gpu")
        self.cpu = StubOllama("cpu")
        for stub in (self.gate, self.gpu, self.cpu):
            await stub.server.start_server()
        self.client = aiohttp.ClientSession()
        self.runner: web.AppRunner | None = None

    def broker_config(self, **overrides: Any) -> Config:
        raw = {
            "gate_url": str(self.gate.server.make_url("")).rstrip("/"),
            "gpu_url": str(self.gpu.server.make_url("")).rstrip("/"),
            "cpu_url": str(self.cpu.server.make_url("")).rstrip("/"),
            "status_ttl": 5,
            "gate_timeout": 1,
            "aliases": ALIASES,
            **overrides,
        }
        return from_dict(raw)

    async def start_broker(self, **overrides: Any) -> None:
        # The production runner (server.start -> make_runner -> RUNNER_SETTINGS), never TestServer,
        # which would force handler_cancellation=True and hide a production misconfiguration.
        self.runner = await start(self.broker_config(**overrides), host="127.0.0.1", port=0)

    async def stop_broker(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    @property
    def port(self) -> int:
        assert self.runner is not None
        return bound_port(self.runner)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    async def asyncTearDown(self) -> None:
        self.gpu.release.set()
        self.cpu.release.set()
        await self.client.close()
        await self.stop_broker()
        for stub in (self.gate, self.gpu, self.cpu):
            await stub.server.close()

    async def post(self, path: str, body: Any) -> tuple[aiohttp.ClientResponse, Any]:
        # BytesIO, not json=: the client streams big test bodies instead of warning about them.
        data = io.BytesIO(json.dumps(body).encode())
        async with self.client.post(self.url(path), data=data, headers={"Content-Type": "application/json"}) as resp:
            return resp, await resp.json(content_type=None)


class RoutingTests(BrokerCase):
    async def test_available_routes_to_gpu(self) -> None:
        await self.start_broker()
        resp, body = await self.post("/api/generate", {"model": "llama3.2:3b", "prompt": "x"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["X-Backend"], "gpu")
        self.assertEqual(body["backend"], "gpu")
        self.assertEqual(len(self.gpu.requests), 1)
        self.assertEqual(self.cpu.requests, [])

    async def test_unavailable_routes_to_cpu(self) -> None:
        self.gate.mode = "unavailable"
        await self.start_broker()
        resp, body = await self.post("/api/generate", {"model": "llama3.2:3b", "prompt": "x"})
        self.assertEqual(resp.headers["X-Backend"], "cpu")
        self.assertEqual(body["backend"], "cpu")
        self.assertEqual(self.gpu.requests, [])

    async def test_gate_error_routes_to_cpu(self) -> None:
        for mode in ("error", "badjson"):
            with self.subTest(mode=mode):
                self.gate.mode = mode
                await self.start_broker()
                resp, _ = await self.post("/api/embed", {"model": "m", "input": "x"})
                self.assertEqual(resp.headers["X-Backend"], "cpu")
                await self.stop_broker()
        self.assertEqual(self.gpu.requests, [])

    async def test_gate_down_routes_to_cpu(self) -> None:
        await self.start_broker(gate_url=f"http://127.0.0.1:{free_port()}")
        resp, _ = await self.post("/api/embed", {"model": "m", "input": "x"})
        self.assertEqual(resp.headers["X-Backend"], "cpu")

    async def test_gate_timeout_routes_to_cpu_quickly(self) -> None:
        self.gate.mode = "slow"  # would say available, but only after 3 s
        await self.start_broker(gate_timeout=0.2)
        start = time.monotonic()
        resp, _ = await self.post("/api/generate", {"model": "m", "prompt": "x"})
        self.assertEqual(resp.headers["X-Backend"], "cpu")
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(self.gpu.requests, [])

    async def test_status_cached_for_ttl(self) -> None:
        await self.start_broker(status_ttl=0.4)
        for _ in range(3):
            await self.post("/api/generate", {"model": "m", "prompt": "x"})
        self.assertEqual(self.gate.hits, 1)
        # The cached answer is used even after the gate changes its mind...
        self.gate.mode = "unavailable"
        resp, _ = await self.post("/api/generate", {"model": "m", "prompt": "x"})
        self.assertEqual(resp.headers["X-Backend"], "gpu")
        # ...until the ttl expires.
        await asyncio.sleep(0.45)
        resp, _ = await self.post("/api/generate", {"model": "m", "prompt": "x"})
        self.assertEqual(resp.headers["X-Backend"], "cpu")
        self.assertEqual(self.gate.hits, 2)

    async def test_concurrent_requests_share_one_gate_fetch(self) -> None:
        await self.start_broker()
        await asyncio.gather(*(self.post("/api/generate", {"model": "m", "prompt": "x"}) for _ in range(10)))
        self.assertEqual(self.gate.hits, 1)

    async def test_gate_failure_is_cached_too(self) -> None:
        self.gate.mode = "error"
        await self.start_broker(status_ttl=5)
        for _ in range(3):
            await self.post("/api/generate", {"model": "m", "prompt": "x"})
        self.assertEqual(self.gate.hits, 1)

    async def test_read_only_endpoints_are_routed(self) -> None:
        await self.start_broker()
        for method, path in (("GET", "/api/version"), ("GET", "/api/ps"), ("GET", "/"), ("HEAD", "/")):
            async with self.client.request(method, self.url(path)) as resp:
                self.assertEqual(resp.status, 200, path)
                self.assertEqual(resp.headers["X-Backend"], "gpu", path)
        self.assertEqual([r["path"] for r in self.gpu.requests], ["/api/version", "/api/ps", "/", "/"])

    async def test_query_string_and_end_to_end_headers_pass_through(self) -> None:
        await self.start_broker()
        url = URL(self.url("/v1/models/x") + "?a=1&b=%2F", encoded=True)
        async with self.client.get(url, headers={"Authorization": "Bearer t"}) as resp:
            self.assertEqual(resp.headers["X-Backend"], "gpu")
            self.assertEqual(resp.headers["X-Stub"], "1")
        sent = self.gpu.requests[0]
        self.assertEqual(sent["path"], "/v1/models/x?a=1&b=%2F")
        self.assertEqual(sent["headers"]["Authorization"], "Bearer t")

    async def test_unknown_path_is_404_and_not_proxied(self) -> None:
        await self.start_broker()
        async with self.client.get(self.url("/api/nope")) as resp:
            self.assertEqual(resp.status, 404)
        self.assertEqual(self.gpu.requests + self.cpu.requests, [])

    async def test_backend_error_status_passes_through_from_cpu(self) -> None:
        self.gate.mode = "unavailable"
        self.cpu.mode = "status503"
        await self.start_broker()
        resp, body = await self.post("/api/generate", {"model": "m", "prompt": "x"})
        self.assertEqual(resp.status, 503)
        self.assertEqual(resp.headers["X-Backend"], "cpu")
        self.assertEqual(body, {"error": "gpu busy"})


class AliasTests(BrokerCase):
    async def test_alias_rewritten_for_gpu(self) -> None:
        await self.start_broker()
        payload = {"model": "chat", "messages": [{"role": "user", "content": "héllo"}], "stream": False}
        resp, body = await self.post("/api/chat", payload)
        self.assertEqual(resp.headers["X-Backend"], "gpu")
        sent = self.gpu.requests[0]["json"]
        self.assertEqual(sent, {**payload, "model": "qwen3:8b"})
        self.assertEqual(body["model"], "qwen3:8b")  # documented: responses echo the backend's name

    async def test_alias_rewritten_for_cpu(self) -> None:
        self.gate.mode = "unavailable"
        await self.start_broker()
        await self.post("/api/chat", {"model": "chat", "messages": [], "stream": False})
        self.assertEqual(self.cpu.requests[0]["json"]["model"], "qwen3:1.7b")

    async def test_alias_rewritten_in_openai_body(self) -> None:
        await self.start_broker()
        await self.post("/v1/embeddings", {"model": "chat", "input": "x"})
        self.assertEqual(self.gpu.requests[0]["json"]["model"], "qwen3:8b")

    async def test_deprecated_name_field_rewritten(self) -> None:
        await self.start_broker()
        await self.post("/api/show", {"name": "chat"})
        self.assertEqual(self.gpu.requests[0]["json"], {"name": "qwen3:8b"})

    async def test_non_alias_body_passes_byte_for_byte(self) -> None:
        await self.start_broker()
        raw = b'{ "model" : "llama3.2:3b",  "prompt": "x", "options": {"seed": 1} }'
        async with self.client.post(self.url("/api/generate"), data=raw, headers={"Content-Type": "application/json"}):
            pass
        self.assertEqual(self.gpu.requests[0]["raw"], raw)

    async def test_non_json_body_passes_through(self) -> None:
        await self.start_broker()
        async with self.client.post(self.url("/api/generate"), data=b"\xff\x00garbage"):
            pass
        self.assertEqual(self.gpu.requests[0]["raw"], b"\xff\x00garbage")

    async def test_alias_logged(self) -> None:
        await self.start_broker()
        with self.assertLogs("gpu_broker", "INFO") as logs:
            await self.post("/api/chat", {"model": "chat", "messages": [], "stream": False})
        line = [m for m in logs.output if "path=/api/chat" in m]
        self.assertEqual(len(line), 1, logs.output)
        for part in ("alias=chat", "backend=gpu", "status=200", "latency_ms=", "failover=no"):
            self.assertIn(part, line[0])


class StreamingTests(BrokerCase):
    async def test_ndjson_first_chunk_arrives_before_backend_sends_last(self) -> None:
        await self.start_broker()
        async with self.client.post(self.url("/api/chat"), json={"model": "chat", "messages": []}) as resp:
            self.assertEqual(resp.headers["X-Backend"], "gpu")
            self.assertEqual(resp.headers["Content-Type"], "application/x-ndjson")
            first = await asyncio.wait_for(resp.content.readline(), 2)
            self.assertFalse(self.gpu.sent_last, "the broker must not wait for the whole body")
            self.assertEqual(json.loads(first), {"model": "qwen3:8b", "backend": "gpu", "done": False})
            self.gpu.release.set()
            rest = await asyncio.wait_for(resp.read(), 2)
        self.assertTrue(self.gpu.sent_last)
        self.assertEqual([json.loads(x) for x in rest.splitlines()], [{"model": "qwen3:8b", "done": True}])

    async def test_sse_first_event_arrives_before_backend_sends_last(self) -> None:
        self.gate.mode = "unavailable"
        await self.start_broker()
        body = {"model": "chat", "messages": [], "stream": True}
        async with self.client.post(self.url("/v1/chat/completions"), json=body) as resp:
            self.assertEqual(resp.headers["X-Backend"], "cpu")
            self.assertTrue(resp.headers["Content-Type"].startswith("text/event-stream"))
            first = await asyncio.wait_for(resp.content.readuntil(b"\n\n"), 2)
            self.assertFalse(self.cpu.sent_last)
            self.assertEqual(json.loads(first[len(b"data: ") :])["model"], "qwen3:1.7b")
            self.cpu.release.set()
            rest = await asyncio.wait_for(resp.read(), 2)
        self.assertEqual(rest, b"data: [DONE]\n\n")

    async def test_client_disconnect_closes_upstream(self) -> None:
        self.gpu.mode = "endless"
        await self.start_broker()
        resp = await self.client.post(self.url("/api/chat"), json={"model": "chat", "messages": []})
        await asyncio.wait_for(resp.content.readline(), 2)
        resp.close()  # the client walks away mid-generation
        await asyncio.wait_for(self.gpu.write_failed.wait(), 3)

    async def test_client_leaving_before_first_byte_closes_upstream(self) -> None:
        self.gpu.mode = "hang"  # a slow non-streaming generation
        await self.start_broker()
        task = asyncio.create_task(self.post("/api/chat", {"model": "chat", "messages": [], "stream": False}))
        await asyncio.sleep(0.3)
        task.cancel()  # the client gives up and drops the connection
        await asyncio.wait_for(self.gpu.write_failed.wait(), 3)
        self.assertEqual(self.cpu.requests, [], "a client leaving is not a gpu failure")

    async def test_midstream_gpu_break_does_not_fail_over(self) -> None:
        self.gpu.mode = "break_midstream"
        await self.start_broker()
        with self.assertRaises(aiohttp.ClientPayloadError):
            async with self.client.post(self.url("/api/chat"), json={"model": "chat", "messages": []}) as resp:
                self.assertEqual(resp.headers["X-Backend"], "gpu")
                await asyncio.wait_for(resp.read(), 3)
        self.assertEqual(self.cpu.requests, [])


class FailoverTests(BrokerCase):
    async def assert_failed_over(self, resp: aiohttp.ClientResponse, body: Any) -> None:
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["X-Backend"], "cpu")
        self.assertEqual(resp.headers["X-Broker-Failover"], "gpu")
        self.assertEqual(body["backend"], "cpu")
        self.assertEqual(len(self.cpu.requests), 1, "retry exactly once on cpu")
        self.assertEqual(self.cpu.requests[0]["json"]["model"], "qwen3:1.7b", "alias re-resolved for cpu")

    async def test_gpu_refused_fails_over(self) -> None:
        await self.start_broker(gpu_url=f"http://127.0.0.1:{free_port()}")
        resp, body = await self.post("/api/chat", {"model": "chat", "messages": [], "stream": False})
        await self.assert_failed_over(resp, body)

    async def test_gpu_drops_connection_fails_over(self) -> None:
        server, port = await dropping_server()
        try:
            await self.start_broker(gpu_url=f"http://127.0.0.1:{port}")
            resp, body = await self.post("/api/chat", {"model": "chat", "messages": [], "stream": False})
            await self.assert_failed_over(resp, body)
        finally:
            server.close()
            await server.wait_closed()

    async def test_gpu_5xx_fails_over(self) -> None:
        self.gpu.mode = "status503"
        await self.start_broker()
        with self.assertLogs("gpu_broker", "INFO") as logs:
            resp, body = await self.post("/api/chat", {"model": "chat", "messages": [], "stream": False})
        await self.assert_failed_over(resp, body)
        self.assertEqual(len(self.gpu.requests), 1)
        self.assertTrue(any("failover=yes" in m and "backend=cpu" in m for m in logs.output), logs.output)

    async def test_gpu_first_byte_timeout_fails_over(self) -> None:
        self.gpu.mode = "hang"
        await self.start_broker(gpu_first_byte_timeout=0.3)
        start = time.monotonic()
        resp, body = await self.post("/api/chat", {"model": "chat", "messages": [], "stream": False})
        await self.assert_failed_over(resp, body)
        self.assertLess(time.monotonic() - start, 2)

    async def test_both_down_is_502_from_cpu(self) -> None:
        await self.start_broker(gpu_url=f"http://127.0.0.1:{free_port()}", cpu_url=f"http://127.0.0.1:{free_port()}")
        resp, body = await self.post("/api/chat", {"model": "chat", "messages": [], "stream": False})
        self.assertEqual(resp.status, 502)
        self.assertEqual(resp.headers["X-Backend"], "cpu")
        self.assertEqual(resp.headers["X-Broker-Failover"], "gpu")
        self.assertIn("error", body)

    async def test_cpu_failure_is_not_retried(self) -> None:
        self.gate.mode = "unavailable"
        await self.start_broker(cpu_url=f"http://127.0.0.1:{free_port()}")
        resp, _ = await self.post("/api/generate", {"model": "m", "prompt": "x"})
        self.assertEqual(resp.status, 502)
        self.assertEqual(resp.headers["X-Backend"], "cpu")
        self.assertNotIn("X-Broker-Failover", resp.headers)
        self.assertEqual(self.gpu.requests, [])


class ManagementTests(BrokerCase):
    async def test_management_goes_to_cpu_even_when_gpu_available(self) -> None:
        await self.start_broker(management_clients=LOCAL)
        calls = [  # /api/push is refused for everyone (ManagementGuardTests)
            ("POST", "/api/pull", {"model": "chat"}),
            ("POST", "/api/create", {"model": "x", "from": "y"}),
            ("POST", "/api/copy", {"source": "chat", "destination": "z"}),
            ("DELETE", "/api/delete", {"model": "chat"}),
        ]
        for method, path, body in calls:
            async with self.client.request(method, self.url(path), json=body) as resp:
                self.assertEqual(resp.status, 200, path)
                self.assertEqual(resp.headers["X-Backend"], "cpu", path)
        self.assertEqual(self.gpu.requests, [])
        self.assertEqual(self.gate.hits, 0, "management never consults the gate")
        self.assertEqual([(r["method"], r["path"]) for r in self.cpu.requests], [(m, p) for m, p, _ in calls])
        # Aliases are not rewritten for management: "chat" is not a real model to pull or delete.
        self.assertEqual(self.cpu.requests[0]["json"], {"model": "chat"})
        self.assertEqual(self.cpu.requests[3]["json"], {"model": "chat"})

    async def test_blob_upload_streams_to_cpu(self) -> None:
        await self.start_broker(max_body_mib=1, management_clients=LOCAL)
        blob = bytes(range(256)) * 8192  # 2 MiB: over max_body_mib, so it must be streamed, not buffered

        async def gen() -> Any:
            for i in range(0, len(blob), 65536):
                yield blob[i : i + 65536]

        async with self.client.post(
            self.url("/api/blobs/sha256:abc"), data=gen(), headers={"Content-Length": str(len(blob))}
        ) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["X-Backend"], "cpu")
        self.assertEqual(self.cpu.requests[0]["raw"], blob)
        self.assertEqual(self.cpu.requests[0]["method"], "POST")

    async def test_oversized_routed_body_is_413(self) -> None:
        await self.start_broker(max_body_mib=1)
        resp, body = await self.post("/api/chat", {"model": "chat", "images": ["A" * (2 << 20)]})
        self.assertEqual(resp.status, 413)
        self.assertIn("max_body_mib", body["error"])
        self.assertEqual(self.gpu.requests + self.cpu.requests, [])


# Model management lock-down (CONTRACTS 12.23). The broker listens on 127.0.0.1 with
# management_clients ["127.0.0.1"]; the refused client is the same machine connecting from 127.0.0.2.
REFUSED_SOURCE = "127.0.0.2"
# (method, path, body): every management endpoint the broker passes through, plus push.
MANAGEMENT_CALLS = [
    ("POST", "/api/pull", {"model": "m"}),
    ("POST", "/api/create", {"model": "x", "from": "m"}),
    ("POST", "/api/copy", {"source": "m", "destination": "z"}),
    ("DELETE", "/api/delete", {"model": "m"}),
    ("POST", "/api/blobs/sha256:abc", None),
]
# (method, path, body, expected status, expected X-Backend): every other endpoint, the same for any client.
OTHER_CALLS = [
    ("POST", "/api/chat", {"model": "chat", "messages": [], "stream": False}, 200, "gpu"),
    ("POST", "/api/generate", {"model": "chat", "prompt": "x"}, 200, "gpu"),
    ("POST", "/api/embed", {"model": "chat", "input": "x"}, 200, "gpu"),
    ("POST", "/api/embeddings", {"model": "chat", "prompt": "x"}, 200, "gpu"),
    ("POST", "/api/show", {"model": "chat"}, 200, "gpu"),
    ("GET", "/api/ps", None, 200, "gpu"),
    ("GET", "/api/version", None, 200, "gpu"),
    ("GET", "/", None, 200, "gpu"),
    ("GET", "/api/tags", None, 200, "cpu"),
    ("GET", "/v1/models", None, 200, "cpu"),
    ("GET", "/v1/models/chat", None, 200, "cpu"),
    ("GET", "/v1/models/llama3.2:3b", None, 200, "gpu"),
    ("POST", "/v1/chat/completions", {"model": "chat", "messages": []}, 200, "gpu"),
    ("POST", "/v1/completions", {"model": "chat", "prompt": "x"}, 200, "gpu"),
    ("POST", "/v1/embeddings", {"model": "chat", "input": "x"}, 200, "gpu"),
    ("GET", "/broker/healthz", None, 200, "broker"),
    ("GET", "/broker/status", None, 200, "broker"),
    ("GET", "/broker/recent", None, 200, "broker"),
    ("GET", "/api/nope", None, 404, "broker"),
]
# Spellings of a management path that are not the exact route. From a refused client every one is a 403.
PULL_TRICKS = [
    "//api/pull",
    "/api//pull",
    "/api/pull/",
    "/api/pull//",
    "/API/PULL",
    "/Api/Pull",
    "/api/pu%6Cl",
    "/api/pu%6cl",
    "/%2561pi/pull",  # double-encoded
    "/api%2Fpull",
    "/api%2fpull",
    "/api%5Cpull",
    "/api\\pull",
    "/api/pull%2F",
    "/api/pull%00",
    "/api/pull%20",
    "/api/pull%3Fx",
    "/api/pull;v=1",
    "/api/./pull",
    "/api/x/../pull",
    "/v1/../api/pull",
    "/v1%2F..%2Fapi%2Fpull",
    "/v1/%2e%2e/api/pull",
    "/broker/../api/pull",
    "/api/pull?x=1",
    "/api/pull#frag",
    "http://evil.example/api/pull",
    "/API/BLOBS/sha256:abc",
    "/api/blobs",
    "/api/blobs/",
    "/api//blobs/sha256:abc",
]
PUSH_TRICKS = [
    "//api/push",
    "/api//push",
    "/api/push/",
    "/API/PUSH",
    "/Api/Push",
    "/api/pu%73h",
    "/api/pu%73%68",
    "/%2561pi/push",
    "/api%2Fpush",
    "/api%5Cpush",
    "/api\\push",
    "/api/push%2F",
    "/api/push%00",
    "/api/push%20",
    "/api/push%3Fx",
    "/api/push;v=1",
    "/api/./push",
    "/api/x/../push",
    "/v1/../api/push",
    "/v1%2F..%2Fapi%2Fpush",
    "/v1/%2e%2e/api/push",
    "/broker/../api/push",
    "/api/push?x=1",
    "/api/push#frag",
    "http://evil.example/api/push",
]
ALL_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


def raw_request(method: str, target: str, body: bytes = b"", extra: str = "") -> bytes:
    head = f"{method} {target} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n{extra}Connection: close\r\n\r\n"
    return head.encode("latin-1") + body


class ManagementGuardTests(BrokerCase):
    """CONTRACTS 12.23: push always refused, the rest of management only for management_clients."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.gpu.release.set()  # the SSE stub answers at once
        self.refused = aiohttp.ClientSession(connector=aiohttp.TCPConnector(local_addr=(REFUSED_SOURCE, 0)))

    async def asyncTearDown(self) -> None:
        await self.refused.close()
        await super().asyncTearDown()

    def forwarded(self) -> list[tuple[str, str]]:
        return [(r["method"], r["path"]) for r in self.gpu.requests + self.cpu.requests]

    async def call(
        self, client: aiohttp.ClientSession, method: str, path: str, body: Any = None, headers: Any = None
    ) -> tuple[aiohttp.ClientResponse, bytes]:
        async with client.request(method, self.url(path), json=body, headers=headers) as resp:
            return resp, await resp.read()

    def assert_refused(self, resp: aiohttp.ClientResponse, data: bytes, method: str, reason: str) -> None:
        self.assertEqual(resp.status, 403)
        self.assertEqual(resp.headers["X-Backend"], "broker")
        if method != "HEAD":
            self.assertEqual(json.loads(data), {"error": f"gpu-broker: {reason}"})

    async def test_every_endpoint_from_an_allowed_and_a_refused_client(self) -> None:
        await self.start_broker(management_clients=LOCAL)
        for who, client in (("allowed", self.client), ("refused", self.refused)):
            self.gpu.requests.clear()
            self.cpu.requests.clear()
            for method, path, body, status, backend in OTHER_CALLS:
                with self.subTest(client=who, path=path):
                    resp, _ = await self.call(client, method, path, body)
                    self.assertEqual((resp.status, resp.headers["X-Backend"]), (status, backend))
            for method, path, body in MANAGEMENT_CALLS:
                with self.subTest(client=who, path=path):
                    resp, data = await self.call(client, method, path, body)
                    if who == "allowed":
                        self.assertEqual((resp.status, resp.headers["X-Backend"]), (200, "cpu"))
                    else:
                        endpoint = "/api/blobs" if path.startswith("/api/blobs/") else path
                        self.assert_refused(
                            resp, data, method, f"model management ({endpoint}) is not allowed from this client"
                        )
            with self.subTest(client=who, path="/api/push"):
                resp, data = await self.call(client, "POST", "/api/push", {"model": "m"})
                self.assert_refused(resp, data, "POST", "/api/push is disabled")
            managed = [p for _, p, _ in MANAGEMENT_CALLS]
            gpu = [r["path"] for r in self.gpu.requests]
            cpu = [r["path"] for r in self.cpu.requests]
            self.assertEqual(cpu, ["/api/tags", "/v1/models", "/v1/models"] + (managed if who == "allowed" else []))
            # Inference, embeddings, show, ps and version reached the same backend for both clients.
            self.assertEqual(
                gpu,
                ["/api/chat", "/api/generate", "/api/embed", "/api/embeddings", "/api/show", "/api/ps",
                 "/api/version", "/", "/v1/models/llama3.2:3b", "/v1/chat/completions", "/v1/completions",
                 "/v1/embeddings"],
            )  # fmt: skip
        self.assertEqual(self.gpu.requests[0]["json"]["model"], "qwen3:8b", "alias rewrite unchanged")

    async def test_push_is_refused_for_everyone_on_every_method(self) -> None:
        await self.start_broker(management_clients=LOCAL)
        for client in (self.client, self.refused):
            for method in ALL_METHODS:
                with self.subTest(method=method):
                    resp, data = await self.call(
                        client, method, "/api/push", None if method == "HEAD" else {"model": "m"}
                    )
                    self.assert_refused(resp, data, method, "/api/push is disabled")
        self.assertEqual(self.forwarded(), [])
        self.assertEqual(self.gate.hits, 0)

    async def test_management_is_refused_on_every_method_from_a_refused_client(self) -> None:
        await self.start_broker(management_clients=LOCAL)
        for _, path, _ in MANAGEMENT_CALLS:
            for method in ALL_METHODS:
                with self.subTest(path=path, method=method):
                    resp, _ = await self.call(self.refused, method, path)
                    self.assertEqual((resp.status, resp.headers["X-Backend"]), (403, "broker"))
        self.assertEqual(self.forwarded(), [])

    async def test_default_config_refuses_every_client(self) -> None:
        await self.start_broker()  # management_clients defaults to []
        self.assertEqual(self.runner.app[BROKER].config.management_clients, frozenset())  # type: ignore[union-attr]
        for method, path, body in MANAGEMENT_CALLS:
            with self.subTest(path=path):
                resp, _ = await self.call(self.client, method, path, body)
                self.assertEqual((resp.status, resp.headers["X-Backend"]), (403, "broker"))
        resp, _ = await self.call(self.client, "POST", "/api/chat", {"model": "chat", "messages": [], "stream": False})
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.forwarded(), [("POST", "/api/chat")])

    async def test_spoofed_client_headers_are_ignored(self) -> None:
        await self.start_broker(management_clients=LOCAL)
        spoofs = [
            {"X-Forwarded-For": "127.0.0.1"},
            {"X-Forwarded-For": "127.0.0.1, 127.0.0.1"},
            {"X-Forwarded-For": f"{REFUSED_SOURCE}, 127.0.0.1"},
            {"X-Real-IP": "127.0.0.1"},
            {"Forwarded": "for=127.0.0.1"},
            {"Forwarded": 'for="127.0.0.1:1234";proto=http'},
            {"True-Client-IP": "127.0.0.1"},
            {"X-Client-IP": "127.0.0.1"},
            {"CF-Connecting-IP": "127.0.0.1"},
            {"X-Originating-IP": "127.0.0.1"},
            {"X-Remote-Addr": "127.0.0.1"},
            {"Client-IP": "127.0.0.1"},
            {
                "X-Forwarded-For": "127.0.0.1",
                "X-Real-IP": "127.0.0.1",
                "Forwarded": "for=127.0.0.1",
                "X-Forwarded-Host": "127.0.0.1",
            },
        ]
        for headers in spoofs:
            with self.subTest(headers=headers):
                resp, _ = await self.call(self.refused, "POST", "/api/pull", {"model": "m"}, headers)
                self.assertEqual(resp.status, 403)
        self.assertEqual(self.forwarded(), [])
        # Nor does a header demote an allowed peer.
        demote = {"X-Forwarded-For": "10.9.9.9", "X-Real-IP": "10.9.9.9", "Forwarded": "for=10.9.9.9"}
        resp, _ = await self.call(self.client, "POST", "/api/pull", {"model": "m"}, demote)
        self.assertEqual((resp.status, resp.headers["X-Backend"]), (200, "cpu"))

    async def test_path_tricks_from_a_refused_client_are_403(self) -> None:
        await self.start_broker(management_clients=LOCAL)
        for target in PULL_TRICKS + PUSH_TRICKS:
            with self.subTest(target=target):
                status, headers, _ = await raw_http(
                    self.port, raw_request("POST", target, b'{"model":"m"}'), source=REFUSED_SOURCE
                )
                self.assertEqual((status, headers["x-backend"]), (403, "broker"))
        self.assertEqual(self.forwarded(), [])
        self.assertEqual(self.gate.hits, 0)

    async def test_push_tricks_from_an_allowed_client_are_403(self) -> None:
        await self.start_broker(management_clients=LOCAL)
        for target in ["/api/push", *PUSH_TRICKS]:
            with self.subTest(target=target):
                status, headers, body = await raw_http(self.port, raw_request("POST", target, b'{"model":"m"}'))
                self.assertEqual((status, headers["x-backend"]), (403, "broker"))
                self.assertIn(b"/api/push is disabled", body)
        self.assertEqual(self.forwarded(), [])

    async def test_pull_tricks_from_an_allowed_client_never_reach_another_route(self) -> None:
        # Only the exact route is management; every other spelling is a 400 or 404 and is not forwarded.
        # The query string and fragment are not part of the path, and /api/blobs/ is the blob prefix.
        await self.start_broker(management_clients=LOCAL)
        exact = {"/api/pull?x=1": "/api/pull?x=1", "/api/pull#frag": "/api/pull", "/api/blobs/": "/api/blobs/"}
        for target in PULL_TRICKS:
            with self.subTest(target=target):
                self.cpu.requests.clear()
                status, headers, _ = await raw_http(self.port, raw_request("POST", target, b'{"model":"m"}'))
                if target in exact:
                    self.assertEqual((status, headers["x-backend"]), (200, "cpu"))
                    self.assertEqual([r["path"] for r in self.cpu.requests], [exact[target]])
                else:
                    self.assertIn(status, (400, 404))
                    self.assertEqual(headers["x-backend"], "broker")
                    self.assertEqual(self.cpu.requests, [])
        self.assertEqual(self.gpu.requests, [])

    async def test_v1_routes_are_unchanged_and_carry_no_management(self) -> None:
        await self.start_broker(management_clients=LOCAL)
        for path in ("/v1/api/pull", "/v1/pull", "/v1/models/api/pull"):  # routed /v1/* passthrough, not management
            with self.subTest(path=path):
                resp, _ = await self.call(self.refused, "POST", path, {"model": "m"})
                self.assertEqual((resp.status, resp.headers["X-Backend"]), (200, "gpu"))
        self.assertEqual([p for _, p in self.forwarded()], ["/v1/api/pull", "/v1/pull", "/v1/models/api/pull"])

    async def test_refusals_are_recorded_without_bodies_or_addresses(self) -> None:
        await self.start_broker(management_clients=LOCAL)
        secret = {"model": "CANARY-model", "files": {"x": "sha256:CANARY"}, "system": "CANARY library text"}
        with self.assertLogs("gpu_broker", "INFO") as logs:
            resp, data = await self.call(self.refused, "POST", "/api/create", secret)
            self.assertEqual(resp.status, 403)
            resp, _ = await self.call(self.client, "POST", "/api/push", {"model": "CANARY-model"})
            self.assertEqual(resp.status, 403)
            resp, _ = await self.call(self.client, "POST", "/api/pull", {"model": "m"})  # allowed: not recorded
            self.assertEqual(resp.status, 200)
        self.assertNotIn(REFUSED_SOURCE.encode(), data)
        async with self.client.get(self.url("/broker/recent")) as r:
            text = await r.text()
        push, create = json.loads(text)
        self.assertEqual(
            {k: push[k] for k in ("path", "alias", "model", "backend", "status", "eval_count", "error")},
            {"path": "/api/push", "alias": None, "model": None, "backend": "broker", "status": 403,
             "eval_count": None, "error": "/api/push is disabled"},
        )  # fmt: skip
        self.assertEqual(
            {k: create[k] for k in ("path", "backend", "status", "error")},
            {"path": "/api/create", "backend": "broker", "status": 403,
             "error": "model management (/api/create) is not allowed from this client"},
        )  # fmt: skip
        for leak in ("CANARY", REFUSED_SOURCE, "127.0.0.1"):
            self.assertNotIn(leak, text)
        refused_lines = [r for r in logs.records if "refused:" in r.getMessage()]
        self.assertEqual([r.levelno for r in refused_lines], [logging.WARNING] * 2)
        self.assertIn(f"peer {REFUSED_SOURCE}", refused_lines[0].getMessage())
        self.assertIn("status=403", refused_lines[0].getMessage())
        self.assertFalse(any("CANARY" in r.getMessage() for r in logs.records))

    async def test_tricks_are_recorded_too(self) -> None:
        await self.start_broker(management_clients=LOCAL)
        await raw_http(self.port, raw_request("POST", "//api/push"), source=REFUSED_SOURCE)
        await raw_http(self.port, raw_request("POST", "/API/PULL"), source=REFUSED_SOURCE)
        async with self.client.get(self.url("/broker/recent")) as r:
            entries = await r.json()
        self.assertEqual([(e["path"], e["status"]) for e in entries], [("/API/PULL", 403), ("//api/push", 403)])

    async def test_refusal_never_reads_the_body(self) -> None:
        # A 100 MiB blob upload that sends only its head: answered at once, nothing reserved or forwarded.
        await self.start_broker(management_clients=LOCAL, max_body_mib=1, max_buffered_bytes=MIB)
        for source, target in ((REFUSED_SOURCE, "/api/blobs/sha256:abc"), ("127.0.0.1", "/api/push")):
            with self.subTest(target=target):
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port, local_addr=(source, 0))
                writer.write(f"POST {target} HTTP/1.1\r\nHost: x\r\nContent-Length: {100 * MIB}\r\n\r\n".encode())
                writer.write(b"x" * 1000)
                await writer.drain()
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
                writer.close()
                self.assertTrue(head.startswith(b"HTTP/1.1 403"), head[:40])
                self.assertIn(b"Connection: close", head)
        self.assertEqual(self.runner.app[BROKER].budget.used, 0)  # type: ignore[union-attr]
        self.assertEqual(self.forwarded(), [])

    async def test_expect_100_continue_is_not_sent_to_a_refused_request(self) -> None:
        await self.start_broker(management_clients=LOCAL)
        head = "Host: x\r\nContent-Length: 13\r\nExpect: 100-continue\r\n\r\n"
        for source, target in ((REFUSED_SOURCE, "/api/pull"), ("127.0.0.1", "/api/push")):
            with self.subTest(target=target):
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port, local_addr=(source, 0))
                writer.write(f"POST {target} HTTP/1.1\r\n{head}".encode())
                await writer.drain()
                first = await asyncio.wait_for(reader.readuntil(b"\r\n"), 2)
                writer.close()
                self.assertTrue(first.startswith(b"HTTP/1.1 403"), first)
        # An allowed management request still gets its 100 Continue, then the backend's answer.
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(f"POST /api/pull HTTP/1.1\r\n{head}".encode())
        await writer.drain()
        self.assertTrue((await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)).startswith(b"HTTP/1.1 100"))
        writer.write(b'{"model":"m"}')
        await writer.drain()
        self.assertTrue((await asyncio.wait_for(reader.readuntil(b"\r\n"), 2)).startswith(b"HTTP/1.1 200"))
        writer.close()
        self.assertEqual(self.forwarded(), [("POST", "/api/pull")])

    async def test_forwarding_chokepoint_refuses_even_if_the_middleware_did_not(self) -> None:
        # Defence in depth: with the middleware's check disabled, open_upstream still refuses.
        real = broker_app.management_refusal

        def middleware_blind(request: Any, config: Config, extra_targets: Any = ()) -> str | None:
            return real(request, config, extra_targets) if extra_targets else None

        await self.start_broker(management_clients=LOCAL)
        with mock.patch.object(broker_app, "management_refusal", middleware_blind):
            for client, method, path, body in (
                (self.refused, "POST", "/api/pull", {"model": "m"}),
                (self.refused, "POST", "/api/blobs/sha256:abc", None),
                (self.client, "POST", "/api/push", {"model": "m"}),
            ):
                with self.subTest(path=path):
                    resp, _ = await self.call(client, method, path, body)
                    self.assertEqual((resp.status, resp.headers["X-Backend"]), (403, "broker"))
            resp, _ = await self.call(self.client, "POST", "/api/pull", {"model": "m"})
            self.assertEqual(resp.status, 200)
        self.assertEqual(self.forwarded(), [("POST", "/api/pull")])

    async def test_ipv6_peer(self) -> None:
        if not socket.has_ipv6:
            self.skipTest("no IPv6")
        try:
            with socket.socket(socket.AF_INET6) as s:
                s.bind(("::1", 0))
        except OSError:
            self.skipTest("no IPv6 loopback")
        for clients, status in ((["::1"], 200), (["::1/128"], 200), (LOCAL, 403)):
            with self.subTest(clients=clients):
                runner = await start(self.broker_config(management_clients=clients), host="::1", port=0)
                try:
                    url = f"http://[::1]:{bound_port(runner)}/api/pull"
                    async with self.client.post(url, json={"model": "m"}) as resp:
                        self.assertEqual(resp.status, status)
                finally:
                    await runner.cleanup()


class LogInjectionTests(BrokerCase):
    """CONTRACTS 12.23 Lead decision 6: client text in a log line is escaped, so one request is one line."""

    # Encoded CR/LF (a forged second log line), NEL, U+2028/2029, ESC and DEL. (No `/` or `%5C`, which
    # would change which route answers; backslashes are covered by the unit test.)
    EVIL = "%0Apath=x%20status=200%0D%0AWARNING%20forged%C2%85nel%E2%80%A8ls%E2%80%A9ps%1Besc%7Fdel"
    SHOWN = "\\x0apath=x status=200\\x0d\\x0aWARNING forged\\x85nel\\u2028ls\\u2029ps\\x1besc\\x7fdel"

    async def test_control_characters_in_a_path_never_split_a_log_line(self) -> None:
        await self.start_broker(management_clients=LOCAL, gpu_url=f"http://127.0.0.1:{free_port()}")
        # A decoded LF never matches the catch-all route (aiohttp's own 404), so the routed case, which
        # reaches the failover WARNING, uses VT instead of LF.
        routed_evil, routed_shown = self.EVIL.replace("%0A", "%0B"), self.SHOWN.replace("\\x0a", "\\x0b")
        cases = [
            ("GET", f"/api/x{self.EVIL}", "127.0.0.1", 404, self.SHOWN),  # unknown path: the request line
            ("POST", f"/api/pull;{self.EVIL}", REFUSED_SOURCE, 403, self.SHOWN),  # refused: a WARNING line
            ("POST", f"/v1/x{routed_evil}", "127.0.0.1", 200, routed_shown),  # gpu down: failover WARNING too
        ]
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        for method, target, source, status, shown in cases:
            with self.subTest(target=target), self.assertLogs("gpu_broker", "DEBUG") as logs:
                got, _, _ = await raw_http(self.port, raw_request(method, target, b"{}"), source=source)
                self.assertEqual(got, status)
            lines = [fmt.format(r) for r in logs.records]
            for line in lines:
                self.assertEqual(len(line.splitlines()), 1, line)  # CR, LF, VT, FF, NEL, U+2028/2029, ...
                self.assertTrue(all(c.isprintable() for c in line), repr(line))
            per_request = [line for line in lines if " method=" in line]
            self.assertEqual(len(per_request), 1, lines)
            self.assertIn(shown, per_request[0])
            if target.startswith("/v1/"):
                [failover] = [line for line in lines if "gpu failed before first byte" in line]
                self.assertIn(shown, failover)

    def test_log_safe_unit(self) -> None:
        from gpu_broker.app import log_safe

        self.assertEqual(log_safe("/api/chat"), "/api/chat")
        self.assertEqual(log_safe("/ü/✓/😀 x"), "/ü/✓/😀 x")
        self.assertEqual(log_safe("a\nb\rc\td\x00e"), "a\\x0ab\\x0dc\\x09d\\x00e")
        self.assertEqual(log_safe("\x1b\x7f\x85\x9f"), "\\x1b\\x7f\\x85\\x9f")
        self.assertEqual(log_safe("\u2028\u2029\u200b\ufeff"), "\\u2028\\u2029\\u200b\\ufeff")
        self.assertEqual(log_safe("\U000e0001"), "\\U000e0001")
        self.assertEqual(log_safe("a\\x0ab"), "a\\\\x0ab", "a sent escape can't pass for a real one")
        self.assertEqual(log_safe(None), "None")


class ModelListTests(BrokerCase):
    async def test_api_tags_merges_aliases_and_cpu_models(self) -> None:
        await self.start_broker()
        async with self.client.get(self.url("/api/tags")) as resp:
            self.assertEqual(resp.headers["X-Backend"], "cpu")
            body = await resp.json()
        names = [m["name"] for m in body["models"]]
        self.assertEqual(names, ["chat", "fresh", "qwen3:1.7b", "llama3.2:3b"])
        chat = body["models"][0]
        self.assertEqual(chat["model"], "chat")
        self.assertEqual(chat["details"], {"family": "qwen3"}, "alias borrows the cpu model's metadata")
        self.assertEqual(body["models"][1]["size"], 0, "cpu model missing: a minimal entry")
        self.assertEqual(self.gpu.requests, [])
        self.assertEqual(self.gate.hits, 0)

    async def test_v1_models_merges_aliases_and_cpu_models(self) -> None:
        await self.start_broker()
        async with self.client.get(self.url("/v1/models")) as resp:
            self.assertEqual(resp.headers["X-Backend"], "cpu")
            body = await resp.json()
        self.assertEqual(body["object"], "list")
        self.assertEqual([m["id"] for m in body["data"]], ["chat", "fresh", "qwen3:1.7b", "llama3.2:3b"])
        self.assertEqual(body["data"][0]["created"], 111)

    async def test_tags_with_cpu_down_lists_aliases_only(self) -> None:
        await self.start_broker(cpu_url=f"http://127.0.0.1:{free_port()}")
        async with self.client.get(self.url("/api/tags")) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["X-Broker-Warning"], "cpu-model-list-unavailable")
            body = await resp.json()
        self.assertEqual([m["name"] for m in body["models"]], ["chat", "fresh"])


class BrokerEndpointTests(BrokerCase):
    async def test_healthz(self) -> None:
        await self.start_broker()
        async with self.client.get(self.url("/broker/healthz")) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(await resp.json(), {"ok": True})
        self.assertEqual(self.gate.hits, 0)

    async def test_status_reports_last_gate_answer_and_age(self) -> None:
        await self.start_broker()
        async with self.client.get(self.url("/broker/status")) as resp:
            before = await resp.json()
        self.assertIsNone(before["gate"]["answer"])
        self.assertIsNone(before["routing_to"])
        await self.post("/api/generate", {"model": "m", "prompt": "x"})
        await asyncio.sleep(0.1)
        async with self.client.get(self.url("/broker/status")) as resp:
            after = await resp.json()
        self.assertTrue(after["gate"]["available"])
        self.assertEqual(after["gate"]["answer"]["reason"], "test")
        self.assertGreaterEqual(after["gate"]["age_s"], 0.1)
        self.assertEqual(after["routing_to"], "gpu")
        self.assertEqual(after["aliases"]["chat"], ALIASES["chat"])
        self.assertEqual(self.gate.hits, 1, "/broker/status does not itself ask the gate")

    async def test_status_reports_gate_error(self) -> None:
        self.gate.mode = "error"
        await self.start_broker()
        await self.post("/api/generate", {"model": "m", "prompt": "x"})
        async with self.client.get(self.url("/broker/status")) as resp:
            body = await resp.json()
        self.assertFalse(body["gate"]["available"])
        self.assertIn("HTTP 500", body["gate"]["error"])
        self.assertEqual(body["routing_to"], "cpu")


async def raw_http(port: int, request: bytes, source: str | None = None) -> tuple[int, dict[str, str], bytes]:
    """Send bytes as-is (aiohttp's client would normalize the path) and parse the response.

    `source`: the local address to connect from (a 127.0.0.0/8 address other than the broker's).
    """
    local_addr = None if source is None else (source, 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", port, local_addr=local_addr)
    writer.write(request)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 3)
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return int(lines[0].split()[1]), headers, body


class ReviewRound1Tests(BrokerCase):
    """Fixes from review round 1 (CONTRACTS.md 12.9)."""

    # -- 1. handler cancellation in production ------------------------------------------------------
    def test_production_runner_cancels_handlers(self) -> None:
        # The behaviour is covered by test_client_leaving_before_first_byte_closes_upstream (which runs on
        # this runner and fails with False) and by the entry-point test below; this pins the setting.
        self.assertIs(RUNNER_SETTINGS["handler_cancellation"], True)

    # -- 2. lone surrogate --------------------------------------------------------------------------
    async def test_alias_body_with_lone_surrogate(self) -> None:
        await self.start_broker()
        raw = b'{"model": "chat", "prompt": "a\\ud800b", "stream": false}'
        async with self.client.post(self.url("/api/generate"), data=raw) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["X-Backend"], "gpu")
        sent = self.gpu.requests[0]
        self.assertIn(b"\\ud800", sent["raw"])
        self.assertEqual(json.loads(sent["raw"])["model"], "qwen3:8b")

    async def test_large_alias_body_forwarded_intact_on_both_attempts(self) -> None:
        self.gpu.mode = "status503"  # forces the CPU retry, which must resend the whole body
        await self.start_broker(max_body_mib=8)
        payload = {"model": "chat", "images": ["B" * (3 << 20)], "stream": False}
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            app_body = json.dumps(payload).encode()
            async with self.client.post(self.url("/api/chat"), data=io.BytesIO(app_body)) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers["X-Backend"], "cpu")
        self.assertEqual(self.gpu.requests[0]["json"], {**payload, "model": "qwen3:8b"})
        self.assertEqual(self.cpu.requests[0]["json"], {**payload, "model": "qwen3:1.7b"})

    # -- 3. compressed request bodies ---------------------------------------------------------------
    async def test_gzip_alias_body_is_forwarded_decoded(self) -> None:
        await self.start_broker()
        payload = {"model": "chat", "prompt": "x" * 2000, "stream": False}
        headers = {"Content-Encoding": "gzip", "Content-Type": "application/json"}
        async with self.client.post(
            self.url("/api/generate"), data=gzip.compress(json.dumps(payload).encode()), headers=headers
        ) as resp:
            self.assertEqual(resp.status, 200)
        sent = self.gpu.requests[0]
        self.assertNotIn("Content-Encoding", sent["headers"])
        self.assertEqual(sent["json"], {**payload, "model": "qwen3:8b"})
        self.assertEqual(int(sent["headers"]["Content-Length"]), len(sent["raw"]))

    async def test_gzip_non_alias_and_blob_bodies_are_forwarded_decoded(self) -> None:
        await self.start_broker(management_clients=LOCAL)
        plain = json.dumps({"model": "llama3.2:3b", "prompt": "y" * 1000}).encode()
        blob = bytes(range(256)) * 64
        for path, body in (("/api/generate", plain), ("/api/blobs/sha256:abc", blob), ("/api/create", plain)):
            packed = gzip.compress(body)
            hdrs = {"Content-Encoding": "gzip", "Content-Length": str(len(packed))}
            async with self.client.post(self.url(path), data=packed, headers=hdrs) as resp:
                self.assertEqual(resp.status, 200, path)
        received = self.gpu.requests + self.cpu.requests
        self.assertEqual([r["raw"] for r in received], [plain, blob, plain])
        for r in received:
            self.assertNotIn("Content-Encoding", r["headers"])

    async def test_unknown_content_encoding_passes_through_with_its_header(self) -> None:
        await self.start_broker()
        hdrs = {"Content-Encoding": "x-custom"}
        async with self.client.post(self.url("/api/generate"), data=b"opaque", headers=hdrs) as resp:
            self.assertEqual(resp.status, 200)
        self.assertEqual(self.gpu.requests[0]["raw"], b"opaque")
        self.assertEqual(self.gpu.requests[0]["headers"]["Content-Encoding"], "x-custom")

    # -- 4. Connection-named headers ----------------------------------------------------------------
    async def test_headers_named_in_connection_are_not_forwarded(self) -> None:
        await self.start_broker()
        status, _, _ = await raw_http(
            self.port,
            b"GET /api/version HTTP/1.1\r\nHost: x\r\nConnection: close, X-Hop-Secret\r\n"
            b"X-Hop-Secret: 1\r\nX-End-To-End: 2\r\n\r\n",
        )
        self.assertEqual(status, 200)
        sent = self.gpu.requests[0]["headers"]
        self.assertNotIn("X-Hop-Secret", sent)
        self.assertEqual(sent["X-End-To-End"], "2")

    def test_response_headers_named_in_connection_are_dropped(self) -> None:
        from multidict import CIMultiDict

        upstream = CIMultiDict(
            [("Connection", "x-a"), ("Connection", "X-B, keep-alive"), ("X-A", "1"), ("X-B", "2"), ("X-C", "3")]
        )
        self.assertEqual(_forward_headers(upstream, keep_length=True, upstream=False), {"X-C": "3"})

    # -- 5/6. request targets that are not already normal --------------------------------------------
    async def test_unnormalized_or_non_origin_targets_are_400(self) -> None:
        # An allowed management client: from any other client the /api/pull spellings below are a 403
        # (ManagementGuardTests.test_path_tricks_from_a_refused_client_are_403).
        await self.start_broker(management_clients=LOCAL)
        targets = [
            b"GET /api/../broker/status",
            b"GET /api/%2e%2e/broker/status",
            b"GET /api/%2E%2E/broker/status",
            b"POST /api/ch%61t",
            b"GET /./api/tags",
            b"GET //api/tags",
            b"GET /api//tags",
            b"GET /v1/models/%2e",
            b"GET /v1%2F..%2Fapi%2Fpull",
            b"POST /v1%2f..%2fapi%2fpull",
            b"GET /v1/models/hf.co%2Fowner%2Fm",
            b"GET /api%5Ctags",
            b"GET /api%5ctags",
            b"GET http://evil.example/api/tags",
            b"OPTIONS *",
        ]
        for target in targets:
            with self.subTest(target=target):
                status, headers, body = await raw_http(
                    self.port, target + b" HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                self.assertEqual(status, 400)
                self.assertEqual(headers["x-backend"], "broker")
                self.assertIn(b"not normalized", body)
        self.assertEqual(self.gpu.requests + self.cpu.requests, [])
        self.assertEqual(self.gate.hits, 0)

    async def test_normal_targets_with_other_encoded_characters_still_proxy(self) -> None:
        await self.start_broker()
        status, headers, _ = await raw_http(
            self.port, b"GET /v1/models/qwen3%3A8b?x=%2e&y=%2F HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-backend"], "gpu")
        self.assertEqual(self.gpu.requests[0]["path"], "/v1/models/qwen3%3A8b?x=%2e&y=%2F")

    async def test_unparseable_targets_are_a_quiet_json_400(self) -> None:
        await self.start_broker()
        for target in (b"GET *", b"GET *x", b"GET host:80"):
            with self.subTest(target=target):
                with (
                    self.assertLogs("gpu_broker", "WARNING") as broker_logs,
                    self.assertNoLogs("aiohttp.server", "ERROR"),
                ):
                    status, headers, body = await raw_http(
                        self.port, target + b" HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
                    )
                self.assertEqual(status, 400)
                self.assertEqual(headers["x-backend"], "broker")
                self.assertIn("bad request", json.loads(body)["error"])
                self.assertTrue(any("unparseable request" in m for m in broker_logs.output), broker_logs.output)
        self.assertEqual(self.gpu.requests + self.cpu.requests, [])

    # -- 7. buffer budget, X-Backend: broker, shutdown, /v1/models/<alias> -----------------------------
    # Budget tests use max_body_mib=2 and a 4 MiB budget, so two 1.5 MiB bodies fill it.
    BUDGET: ClassVar[dict[str, Any]] = {
        "max_body_mib": 2,
        "max_buffered_bytes": 4 * MIB,
        "buffer_wait_timeout": 0.3,
        "body_read_timeout": 0.6,
    }

    @property
    def budget(self) -> Any:
        assert self.runner is not None
        return self.runner.app[BROKER].budget

    async def wait_for_budget(self, used: int, timeout: float = 3) -> None:
        deadline = time.monotonic() + timeout
        while self.budget.used != used:
            if time.monotonic() > deadline:
                self.fail(f"budget used={self.budget.used}, expected {used}")
            await asyncio.sleep(0.02)

    async def trickle(self, path: str, declared: int, sent: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """A client that declares a body, sends part of it, then stalls."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(
            f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            f"Content-Length: {declared}\r\nConnection: close\r\n\r\n".encode()
            + b"{" * sent
        )
        await writer.drain()
        return reader, writer

    def big_chat(self, size: int = 3 * MIB // 2) -> dict[str, Any]:
        return {"model": "chat", "messages": [], "stream": False, "pad": "x" * size}

    async def timed_post(self, path: str, body: Any) -> tuple[int, str, float]:
        start = time.monotonic()
        resp, _ = await self.post(path, body)
        return resp.status, resp.headers["X-Backend"], time.monotonic() - start

    async def test_trickling_clients_cannot_starve_anyone(self) -> None:
        # The Reviewer's scenario: clients that declare big bodies and then trickle (or stall).
        await self.start_broker(**self.BUDGET)
        stalled = [await self.trickle("/api/chat", 2 * MIB, 10) for _ in range(2)]  # reserve the whole budget
        small_stalled = [await self.trickle("/api/show", 100, 10) for _ in range(4)]  # small: no budget at all
        await self.wait_for_budget(4 * MIB)
        # A small request is never queued behind them...
        status, backend, took = await self.timed_post("/api/show", {"model": "chat"})
        self.assertEqual((status, backend), (200, "gpu"))
        self.assertLess(took, 0.25)
        # ...a large one waits buffer_wait_timeout, then gets 503 from the broker...
        status, backend, took = await self.timed_post("/api/chat", self.big_chat())
        self.assertEqual((status, backend), (503, "broker"))
        self.assertGreaterEqual(took, 0.3)
        # ...and every stalled client gets 408 after body_read_timeout, which frees the budget.
        for reader, writer in stalled + small_stalled:
            answer = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            writer.close()
            self.assertTrue(answer.startswith(b"HTTP/1.1 408"), answer[:40])
            self.assertIn(b"X-Backend: broker", answer)
        await self.wait_for_budget(0)
        status, backend, _ = await self.timed_post("/api/chat", self.big_chat())
        self.assertEqual((status, backend), (200, "gpu"))
        self.assertEqual(self.budget.used, 0)

    async def test_slow_generations_do_not_block_small_requests(self) -> None:
        # The Reviewer's scenario: four slow CPU generations in flight, then a tiny /api/show.
        self.gate.mode = "unavailable"
        self.cpu.mode = "hold"
        await self.start_broker(**self.BUDGET)
        small = [
            asyncio.create_task(self.post("/api/chat", {"model": "chat", "messages": [], "stream": False}))
            for _ in range(4)
        ]
        large = [asyncio.create_task(self.post("/api/chat", self.big_chat())) for _ in range(2)]
        await self.wait_for_budget(2 * len(json.dumps(self.big_chat())))
        while len(self.cpu.requests) < 6:
            await asyncio.sleep(0.02)
        status, backend, took = await self.timed_post("/api/show", {"model": "chat"})
        self.assertEqual((status, backend), (200, "cpu"))
        self.assertLess(took, 0.25, "a small request must not wait behind slow generations")
        status, backend, _ = await self.timed_post("/api/chat", self.big_chat())
        self.assertEqual((status, backend), (503, "broker"), "no budget left for a third large body")
        self.cpu.release.set()
        results = await asyncio.wait_for(asyncio.gather(*small, *large), 3)
        self.assertEqual([r.status for r, _ in results], [200] * 6)
        self.assertEqual(self.budget.used, 0)

    async def test_large_body_waits_for_budget_then_proceeds(self) -> None:
        self.gpu.mode = "hold"
        await self.start_broker(**{**self.BUDGET, "buffer_wait_timeout": 3})
        held = [asyncio.create_task(self.post("/api/chat", self.big_chat())) for _ in range(2)]
        while len(self.gpu.requests) < 2:
            await asyncio.sleep(0.02)
        waiting = asyncio.create_task(self.post("/api/chat", self.big_chat()))
        while self.budget.waiting == 0:
            await asyncio.sleep(0.02)
        async with self.client.get(self.url("/broker/status")) as resp:
            buffer = (await resp.json())["buffer"]
        self.assertEqual(buffer["max_bytes"], 4 * MIB)
        self.assertEqual(buffer["waiting"], 1)
        self.gpu.release.set()
        results = await asyncio.wait_for(asyncio.gather(*held, waiting), 3)
        self.assertEqual([r.status for r, _ in results], [200, 200, 200])
        self.assertEqual(self.budget.used, 0)

    async def test_undeclared_length_reserves_then_shrinks(self) -> None:
        self.gpu.mode = "hold"
        await self.start_broker(**self.BUDGET)
        body = json.dumps(self.big_chat()).encode()

        async def chunked() -> Any:  # no Content-Length: transfer-encoding chunked
            for i in range(0, len(body), 65536):
                yield body[i : i + 65536]

        task = asyncio.create_task(self.client.post(self.url("/api/chat"), data=chunked()))
        while not self.gpu.requests:
            await asyncio.sleep(0.02)
        self.assertEqual(self.budget.used, len(body), "the worst-case reservation shrinks to the real size")
        self.gpu.release.set()
        resp = await asyncio.wait_for(task, 3)
        self.assertEqual(resp.status, 200)
        resp.release()
        self.assertEqual(json.loads(self.gpu.requests[0]["raw"])["pad"], self.big_chat()["pad"])
        self.assertEqual(self.budget.used, 0)

    async def test_decoded_body_is_budgeted_by_its_decoded_size(self) -> None:
        self.gpu.mode = "hold"
        await self.start_broker(**self.BUDGET)
        body = json.dumps(self.big_chat()).encode()
        packed = gzip.compress(body)  # ~1.5 KiB on the wire, 1.5 MiB decoded
        hdrs = {"Content-Encoding": "gzip", "Content-Type": "application/json"}
        task = asyncio.create_task(self.client.post(self.url("/api/chat"), data=packed, headers=hdrs))
        while not self.gpu.requests:
            await asyncio.sleep(0.02)
        self.assertEqual(self.budget.used, len(body))
        self.gpu.release.set()
        (await asyncio.wait_for(task, 3)).release()
        self.assertEqual(self.budget.used, 0)
        # A body that decodes past max_body_mib is refused, however small it was on the wire.
        bomb = gzip.compress(b"{" + b" " * (3 * MIB) + b"}")
        async with self.client.post(self.url("/api/chat"), data=bomb, headers=hdrs) as resp:
            self.assertEqual(resp.status, 413)
            self.assertEqual(resp.headers["X-Backend"], "broker")
        self.assertEqual(self.budget.used, 0)

    async def test_compressed_length_over_1mib_is_not_trusted(self) -> None:
        # Content-Length of an encoded body is its compressed size; the budget must track decoded bytes.
        self.gpu.mode = "hold"
        await self.start_broker(**self.BUDGET)
        body = json.dumps({"model": "llama3.2:3b", "pad": base64.b64encode(os.urandom(1400 * 1024)).decode()}).encode()
        packed = gzip.compress(body)
        self.assertGreater(len(packed), MIB)
        self.assertLess(len(packed), len(body))
        hdrs = {"Content-Encoding": "gzip", "Content-Type": "application/json"}
        task = asyncio.create_task(self.client.post(self.url("/api/chat"), data=io.BytesIO(packed), headers=hdrs))
        while not self.gpu.requests:
            await asyncio.sleep(0.02)
        self.assertEqual(self.budget.used, len(body))
        self.gpu.release.set()
        (await asyncio.wait_for(task, 3)).release()
        self.assertEqual(self.gpu.requests[0]["raw"], body)
        self.assertEqual(self.budget.used, 0)

    async def test_budget_released_on_every_path(self) -> None:
        await self.start_broker(**self.BUDGET)
        big = self.big_chat()
        # failover: GPU 503 -> CPU 200
        self.gpu.mode = "status503"
        status, backend, _ = await self.timed_post("/api/chat", big)
        self.assertEqual((status, backend), (200, "cpu"))
        self.assertEqual(self.budget.used, 0, "failover")
        # error: both backends answer 503/unreachable -> cpu answer passes through
        self.cpu.mode = "status503"
        status, _, _ = await self.timed_post("/api/chat", big)
        self.assertEqual(status, 503)
        self.assertEqual(self.budget.used, 0, "error")
        self.gpu.mode = self.cpu.mode = "ok"

        # 413 on an undeclared body that grows past the limit after reserving
        async def endless() -> Any:
            for _ in range(64):
                yield b"x" * 65536

        async with self.client.post(self.url("/api/chat"), data=endless()) as resp:
            self.assertEqual(resp.status, 413)
        self.assertEqual(self.budget.used, 0, "413")
        # cancel: the client leaves while the GPU holds the request
        self.gpu.mode = "hold"
        task = asyncio.create_task(self.post("/api/chat", big))
        while not self.gpu.requests or self.budget.used == 0:
            await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.wait_for(self.gpu.write_failed.wait(), 3)
        await self.wait_for_budget(0)
        # 408: a large declared body that stalls
        reader, writer = await self.trickle("/api/chat", 2 * MIB, 10)
        await self.wait_for_budget(2 * MIB)
        self.assertTrue((await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)).startswith(b"HTTP/1.1 408"))
        writer.close()
        await self.wait_for_budget(0)

    async def test_corrupt_gzip_body_is_one_warning_and_no_traceback(self) -> None:
        await self.start_broker(management_clients=LOCAL)
        body = b"\x1f\x8b\x08\x00not-really-gzip" * 200
        paths = ["/api/chat", "/api/chat", "/api/create", "/api/blobs/sha256:abc"]  # buffered x3, streamed
        with self.assertLogs(level="DEBUG") as logs:  # root: every logger, aiohttp's included
            for path in paths:  # a client repeating it must not flood the log either
                request = (
                    f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Encoding: gzip\r\n".encode()
                    + f"Content-Length: {len(body)}\r\n\r\n".encode()
                    + body
                )
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
                writer.write(request)
                await writer.drain()
                answer = await asyncio.wait_for(reader.read(), 3)  # the broker closes: EOF, no drain
                writer.close()
                self.assertTrue(answer.startswith(b"HTTP/1.1 400"), answer[:60])
                self.assertIn(b"X-Backend: broker", answer)
                self.assertIn(b"unreadable request body", answer)
            await asyncio.sleep(0.2)  # give a post-response drain time to log, if one ran
        bad = [r for r in logs.records if r.levelno >= logging.ERROR or r.exc_info]
        self.assertEqual(bad, [], [r.getMessage() for r in bad])
        warnings_ = [r for r in logs.records if r.levelno == logging.WARNING]
        self.assertEqual(len(warnings_), len(paths), [r.getMessage() for r in warnings_])
        self.assertTrue(all("unreadable request body" in r.getMessage() for r in warnings_))
        self.assertEqual(self.gpu.requests, [])
        self.assertEqual(self.budget.used, 0)

    async def test_declared_body_over_limit_is_refused_before_reading(self) -> None:
        await self.start_broker(**self.BUDGET)
        reader, writer = await self.trickle("/api/chat", 3 * MIB, 0)
        answer = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
        writer.close()
        self.assertTrue(answer.startswith(b"HTTP/1.1 413"), answer[:40])
        self.assertEqual(self.budget.used, 0)

    def test_byte_budget_unit(self) -> None:
        from gpu_broker.app import ByteBudget

        async def scenario() -> None:
            budget = ByteBudget(10)
            await budget.acquire(6, 1)
            with self.assertRaises(TimeoutError):
                await budget.acquire(5, 0.05)
            self.assertEqual((budget.used, budget.waiting), (6, 0))
            waiter = asyncio.create_task(budget.acquire(5, 1))
            await asyncio.sleep(0.01)
            budget.release(6)
            await waiter
            self.assertEqual(budget.used, 5)
            with self.assertRaises(ValueError):
                await budget.acquire(11, 1)

        asyncio.run(scenario())

    async def test_broker_generated_responses_say_broker(self) -> None:
        await self.start_broker(max_body_mib=1, management_clients=LOCAL)
        cases = [
            ("GET", "/api/nope", None, 404),
            ("POST", "/api/push", {"model": "x"}, 403),
            ("POST", "/api/tags", None, 405),
            ("POST", "/api/chat", {"model": "chat", "images": ["A" * (2 << 20)]}, 413),
            ("POST", "/api/create", {"model": "x", "files": "A" * (2 << 20)}, 413),
            ("GET", "/broker/healthz", None, 200),
            ("GET", "/broker/status", None, 200),
        ]
        for method, path, body, status in cases:
            with self.subTest(path=path, status=status):
                data = None if body is None else io.BytesIO(json.dumps(body).encode())
                async with self.client.request(method, self.url(path), data=data) as resp:
                    self.assertEqual(resp.status, status)
                    self.assertEqual(resp.headers["X-Backend"], "broker")
                    if status >= 400:
                        self.assertIn("error", await resp.json())

    async def test_no_failover_during_shutdown(self) -> None:
        await self.start_broker(gpu_url=f"http://127.0.0.1:{free_port()}")
        assert self.runner is not None
        self.runner.app[BROKER].closing = True  # what on_shutdown sets on SIGTERM
        resp, body = await self.post("/api/chat", {"model": "chat", "messages": [], "stream": False})
        self.assertEqual(resp.status, 503)
        self.assertEqual(resp.headers["X-Backend"], "broker")
        self.assertIn("shutting down", body["error"])
        self.assertEqual(self.cpu.requests, [])

    async def test_closed_session_is_a_clean_failure(self) -> None:
        await self.start_broker()
        assert self.runner is not None
        broker = self.runner.app[BROKER]
        assert broker.proxy_session is not None
        await broker.proxy_session.close()  # what cleanup does once handlers are done
        broker.closing = True
        resp, body = await self.post("/api/chat", {"model": "chat", "messages": [], "stream": False})
        self.assertEqual(resp.status, 503)
        self.assertIn("shutting down", body["error"])
        self.assertEqual(self.gpu.requests + self.cpu.requests, [])

    async def test_v1_model_alias_lookup(self) -> None:
        await self.start_broker()
        async with self.client.get(self.url("/v1/models/chat")) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["X-Backend"], "cpu")
            body = await resp.json()
        self.assertEqual(body, {"id": "chat", "object": "model", "created": 111, "owned_by": "library"})
        async with self.client.get(self.url("/v1/models/fresh")) as resp:
            self.assertEqual((await resp.json())["id"], "fresh")
        self.assertEqual([r["path"] for r in self.cpu.requests], ["/v1/models", "/v1/models"])
        self.assertEqual(self.gpu.requests, [])
        # A real model name is still proxied to the routed backend.
        async with self.client.get(self.url("/v1/models/llama3.2:3b")) as resp:
            self.assertEqual(resp.headers["X-Backend"], "gpu")

    # -- 8. healthcheck follows the config -----------------------------------------------------------
    async def test_healthcheck_uses_configured_port(self) -> None:
        await self.start_broker()
        for host in ("127.0.0.1", "0.0.0.0"):
            cfg = self.broker_config(listen_host=host, listen_port=self.port)
            self.assertEqual(await asyncio.to_thread(healthcheck, cfg), 0, host)
        cfg = self.broker_config(listen_port=free_port())
        self.assertEqual(await asyncio.to_thread(healthcheck, cfg, 0.5), 1)


class SpawnedBrokerCase(BrokerCase):
    """The broker as its own process (`python -m gpu_broker`), so a broker that blocks its event loop
    can't block the test's: the test's timeouts still fire and the test fails instead of hanging."""

    async def spawn(
        self, *, env: dict[str, str] | None = None, prelude: str | None = None, **overrides: Any
    ) -> tuple[asyncio.subprocess.Process, str, int]:
        """Start `python -m gpu_broker`. `env` adds environment variables; `prelude` is Python run in the
        child before the broker's main() (to disable one defence and test another)."""
        port = free_port()
        cfg = {
            "gate_url": str(self.gate.server.make_url("")).rstrip("/"),
            "gpu_url": str(self.gpu.server.make_url("")).rstrip("/"),
            "cpu_url": str(self.cpu.server.make_url("")).rstrip("/"),
            "listen_host": "127.0.0.1",
            "listen_port": port,
            "aliases": ALIASES,
            **overrides,
        }
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(cfg, fh)
        self.addCleanup(os.unlink, path)
        if prelude is None:
            argv = ["-m", "gpu_broker", "--config", path]
        else:
            main = f"from gpu_broker.__main__ import main; raise SystemExit(main(['--config', {path!r}]))"
            argv = ["-c", f"{prelude}\n{main}"]
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            *argv,
            cwd=APP_DIR,
            env=None if env is None else {**os.environ, **env},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async def kill() -> None:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

        self.addAsyncCleanup(kill)
        for _ in range(100):
            with contextlib.suppress(aiohttp.ClientError):
                async with self.client.get(f"http://127.0.0.1:{port}/broker/healthz") as resp:
                    if resp.status == 200:
                        return proc, path, port
            await asyncio.sleep(0.05)
        self.fail("broker did not start")

    async def healthcheck_rc(self, path: str) -> int:
        check = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "gpu_broker", "--config", path, "--healthcheck", cwd=APP_DIR
        )
        return await check.wait()


class HeadAndNoBodyTests(SpawnedBrokerCase):
    """Bodiless backend responses (HEAD, 204). aiohttp shares one EmptyStreamReader for all of them, and
    reading it a second time in a process spun the broker's event loop forever (see app._has_body)."""

    async def call(
        self, port: int, method: str, path: str, body: Any = None
    ) -> tuple[aiohttp.ClientResponse, bytes]:
        async with asyncio.timeout(3):  # a regression fails here, it doesn't hang the suite
            async with self.client.request(method, f"http://127.0.0.1:{port}{path}", json=body) as resp:
                return resp, await resp.read()

    async def test_head_then_gets_and_a_chat_all_complete(self) -> None:
        _proc, _, port = await self.spawn()
        for _ in range(3):
            resp, body = await self.call(port, "HEAD", "/")
            self.assertEqual((resp.status, resp.headers["X-Backend"], body), (200, "gpu", b""))
            self.assertIn("Content-Length", resp.headers)  # the GET size, as HEAD should say
        resp, body = await self.call(port, "GET", "/")
        self.assertEqual((resp.status, json.loads(body)["path"]), (200, "/"))
        resp, body = await self.call(port, "GET", "/api/version")
        self.assertEqual(resp.status, 200)
        resp, body = await self.call(port, "POST", "/api/chat", {"model": "chat", "messages": [], "stream": False})
        self.assertEqual((resp.status, json.loads(body)["model"]), (200, "qwen3:8b"))
        resp, body = await self.call(port, "HEAD", "/api/version")
        self.assertEqual((resp.status, body), (200, b""))
        resp, _ = await self.call(port, "GET", "/broker/healthz")
        self.assertEqual(resp.status, 200)
        self.assertEqual([r["method"] for r in self.gpu.requests], ["HEAD"] * 3 + ["GET", "GET", "POST", "HEAD"])

    async def test_head_api_tags_answers_like_get_without_a_body(self) -> None:
        _proc, _, port = await self.spawn()
        for path in ("/api/tags", "/v1/models", "/v1/models/chat"):
            with self.subTest(path=path):
                head, head_body = await self.call(port, "HEAD", path)
                get, get_body = await self.call(port, "GET", path)
                self.assertEqual((head.status, head.headers["X-Backend"], head_body), (200, "cpu", b""))
                self.assertEqual(int(head.headers["Content-Length"]), len(get_body))
                self.assertEqual(get.status, 200)
        resp, _ = await self.call(port, "HEAD", "/")  # and a proxied HEAD after them
        self.assertEqual(resp.status, 200)

    async def test_204_from_the_backend_is_relayed_without_a_body(self) -> None:
        async def no_content(_request: web.Request) -> web.StreamResponse:
            return web.Response(status=204)

        self.gpu.reply = no_content
        _proc, _, port = await self.spawn()
        for _ in range(3):
            resp, body = await self.call(port, "POST", "/api/embed", {"model": "m", "input": "x"})
            self.assertEqual((resp.status, resp.headers["X-Backend"], body), (204, "gpu", b""))
            self.assertNotIn("Transfer-Encoding", resp.headers)
        # On the wire: nothing after the head (no chunked terminator a 204 must not have).
        status, headers, raw = await raw_http(
            port,
            b"POST /api/embed HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            b'Content-Length: 13\r\nConnection: close\r\n\r\n{"model":"m"}',
        )
        self.assertEqual((status, raw), (204, b""))
        self.assertNotIn("content-length", headers)
        self.gpu.reply = None
        resp, _ = await self.call(port, "GET", "/api/version")
        self.assertEqual(resp.status, 200)

    async def empty_200_three_times(self, port: int) -> None:
        for _ in range(3):
            for method, path, body in (
                ("GET", "/api/version", None),  # routed
                ("POST", "/api/generate", {"model": "m", "prompt": "p"}),  # routed, buffered body
                ("DELETE", "/api/delete", {"model": "x"}),  # management (cpu)
            ):
                resp, data = await self.call(port, method, path, body)
                self.assertEqual((resp.status, data), (200, b""), (method, path))
        resp, _ = await self.call(port, "GET", "/broker/healthz")
        self.assertEqual(resp.status, 200)

    async def test_content_length_zero_with_the_pure_python_parser(self) -> None:
        # The backstop: aiohttp's pure-Python parser hands 200 + Content-Length: 0 the shared
        # EMPTY_PAYLOAD, which no method/status rule catches.
        async def empty(_request: web.Request) -> web.StreamResponse:
            return web.Response(status=200, body=b"")

        self.gpu.reply = self.cpu.reply = empty
        _proc, _, port = await self.spawn(env={"AIOHTTP_NO_EXTENSIONS": "1"}, management_clients=LOCAL)
        await self.empty_200_three_times(port)

    async def test_relay_stops_on_an_empty_chunk_at_eof(self) -> None:
        # The relay's own guard, with _has_body disabled in the child: every bodiless response (HEAD,
        # 204, the shared EMPTY_PAYLOAD) is then iterated, and only the guard stops the loop.
        async def no_content(_request: web.Request) -> web.StreamResponse:
            return web.Response(status=204)

        async def empty(_request: web.Request) -> web.StreamResponse:
            return web.Response(status=200, body=b"")

        prelude = "import gpu_broker.app as app\napp._has_body = lambda method, resp: True"
        self.cpu.reply = empty
        _proc, _, port = await self.spawn(env={"AIOHTTP_NO_EXTENSIONS": "1"}, prelude=prelude, management_clients=LOCAL)
        for _ in range(3):
            resp, body = await self.call(port, "HEAD", "/")
            self.assertEqual((resp.status, body), (200, b""))
        self.gpu.reply = no_content
        for _ in range(3):
            resp, body = await self.call(port, "POST", "/api/embed", {"model": "m"})
            self.assertEqual((resp.status, body), (204, b""))
        self.gpu.reply = empty
        await self.empty_200_three_times(port)
        self.gpu.reply = None
        resp, body = await self.call(port, "POST", "/api/chat", {"model": "chat", "messages": [], "stream": False})
        self.assertEqual((resp.status, json.loads(body)["model"]), (200, "qwen3:8b"))


class ManagementGuardSpawnedTests(SpawnedBrokerCase):
    """HEAD and OPTIONS on management paths, allowed (proxied, so in a broker subprocess: see
    HeadAndNoBodyTests) and refused, with management_clients read from a real config file."""

    async def test_head_and_options_on_management_paths(self) -> None:
        _proc, _, port = await self.spawn(management_clients=LOCAL)
        refused = aiohttp.ClientSession(connector=aiohttp.TCPConnector(local_addr=(REFUSED_SOURCE, 0)))
        self.addAsyncCleanup(refused.close)

        async def call(client: aiohttp.ClientSession, method: str, path: str) -> tuple[int, str, bytes]:
            async with asyncio.timeout(3), client.request(method, f"http://127.0.0.1:{port}{path}") as resp:
                return resp.status, resp.headers["X-Backend"], await resp.read()

        for method in ("HEAD", "OPTIONS"):
            for path in ("/api/blobs/sha256:abc", "/api/pull", "/api/delete"):
                with self.subTest(method=method, path=path):
                    self.assertEqual((await call(refused, method, path))[:2], (403, "broker"))
                    status, backend, body = await call(self.client, method, path)
                    self.assertEqual((status, backend), (200, "cpu"))
                    if method == "HEAD":
                        self.assertEqual(body, b"")
            with self.subTest(method=method, path="/api/push"):
                self.assertEqual((await call(self.client, method, "/api/push"))[:2], (403, "broker"))
        self.assertEqual(
            [(r["method"], r["path"]) for r in self.cpu.requests],
            [(m, p) for m in ("HEAD", "OPTIONS") for p in ("/api/blobs/sha256:abc", "/api/pull", "/api/delete")],
        )
        self.assertEqual(self.gpu.requests, [])

    async def test_bad_management_clients_fail_the_config_check(self) -> None:
        base = {"gate_url": "http://g", "gpu_url": "http://g", "cpu_url": "http://c"}
        for bad in ([" 127.0.0.1"], ["10.0.0.0/8"], "127.0.0.1"):
            with self.subTest(bad=bad):
                fd, path = tempfile.mkstemp(suffix=".json")
                with os.fdopen(fd, "w") as fh:
                    json.dump({**base, "management_clients": bad}, fh)
                self.addCleanup(os.unlink, path)
                check = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "gpu_broker", "--config", path, "--check",
                    cwd=APP_DIR, stderr=asyncio.subprocess.PIPE,
                )  # fmt: skip
                _, err = await check.communicate()
                self.assertEqual(check.returncode, 2)
                self.assertIn(b"management_clients", err)


class EntryPointTests(SpawnedBrokerCase):
    """`python -m gpu_broker` as the container runs it: real signals, real runner, real logs."""

    async def test_entrypoint_client_leaving_cancels_the_backend_request(self) -> None:
        self.gpu.mode = "hold"
        _proc, _, port = await self.spawn()
        task = asyncio.create_task(
            self.client.post(
                f"http://127.0.0.1:{port}/api/chat", json={"model": "chat", "messages": [], "stream": False}
            )
        )
        for _ in range(40):
            if self.gpu.requests:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.wait_for(self.gpu.write_failed.wait(), 3)
        self.assertEqual(self.cpu.requests, [])

    async def test_sigterm_mid_request_exits_cleanly_without_failover(self) -> None:
        self.gpu.mode = "hold"
        proc, path, port = await self.spawn(shutdown_timeout=0.5)
        self.assertEqual(await self.healthcheck_rc(path), 0)
        task = asyncio.create_task(
            self.client.post(
                f"http://127.0.0.1:{port}/api/chat", json={"model": "chat", "messages": [], "stream": False}
            )
        )
        for _ in range(40):
            if self.gpu.requests:
                break
            await asyncio.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        out, _ = await asyncio.wait_for(proc.communicate(), 5)
        with contextlib.suppress(aiohttp.ClientError):
            await task
        log = out.decode()
        self.assertEqual(proc.returncode, 0, log)
        self.assertNotIn("Traceback", log)
        self.assertIn("shutting down", log)
        self.assertIn("note='shutdown-cancel'", log)
        self.assertNotIn("client-gone", log)
        self.assertEqual(self.cpu.requests, [], "no failover to cpu on the way out")
        await asyncio.wait_for(self.gpu.write_failed.wait(), 1)
        self.assertEqual(await self.healthcheck_rc(path), 1)


CANARY = "CANARY-7f3a9c-never-stored"
STATS = {"eval_count": 42, "eval_duration": 2_000_000_000}  # 21 tokens/s


def ndjson_reply(chunks: list[Any]) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    """A streamed Ollama answer: each item written as one NDJSON line (bytes are written as-is)."""

    async def reply(request: web.Request) -> web.StreamResponse:
        out = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await out.prepare(request)
        for chunk in chunks:
            await out.write(chunk if isinstance(chunk, bytes) else json.dumps(chunk).encode() + b"\n")
        await out.write_eof()
        return out

    return reply


def json_reply(doc: Any) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    async def reply(_request: web.Request) -> web.StreamResponse:
        return web.json_response(doc)

    return reply


class RecentTests(BrokerCase):
    """GET /broker/recent (CONTRACTS 12.15.4)."""

    async def recent(self) -> list[dict[str, Any]]:
        async with self.client.get(self.url("/broker/recent")) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["X-Backend"], "broker")
            return await resp.json()

    async def wait_recent(self, n: int, timeout: float = 3) -> list[dict[str, Any]]:
        """Entries are added when the handler finishes, which can be after the client has gone."""
        deadline = time.monotonic() + timeout
        while len(self.runner.app[BROKER].recent) < n:  # type: ignore[union-attr]
            if time.monotonic() > deadline:
                self.fail(f"/broker/recent never reached {n} entries")
            await asyncio.sleep(0.02)
        return await self.recent()

    def assert_entry(self, entry: dict[str, Any], **expected: Any) -> None:
        self.assertEqual(
            set(entry),
            {
                "ts", "path", "alias", "model", "backend", "status", "duration_ms", "eval_count", "eval_tok_s",
                "error", "num_ctx",
            },
        )  # fmt: skip
        if "num_ctx" not in expected:
            self.assertIsNone(entry["num_ctx"], "no fixed context configured: nothing is normalised")
        self.assertIsInstance(entry["ts"], float)
        self.assertLessEqual(abs(entry["ts"] - time.time()), 30)
        self.assertIsInstance(entry["duration_ms"], int)
        self.assertGreaterEqual(entry["duration_ms"], 0)
        self.assertEqual({k: entry[k] for k in expected}, expected)

    async def test_streamed_chat_records_final_chunk_counts(self) -> None:
        self.gpu.reply = ndjson_reply([{"message": {"content": "tok"}, "done": False}] * 3 + [{"done": True, **STATS}])
        await self.start_broker()
        async with self.client.post(self.url("/api/chat"), json={"model": "chat", "messages": []}) as resp:
            self.assertEqual(resp.headers["X-Backend"], "gpu")
            self.assertEqual(len((await resp.read()).splitlines()), 4)
        [entry] = await self.wait_recent(1)
        self.assert_entry(
            entry,
            path="/api/chat",
            alias="chat",
            model="qwen3:8b",
            backend="gpu",
            status=200,
            eval_count=42,
            eval_tok_s=21.0,
            error=None,
        )

    async def test_non_streamed_generate_records_counts_after_a_long_body(self) -> None:
        # One JSON object far bigger than the kept tail: a long response and /api/generate's `context`
        # come before the counts, which are the object's last fields.
        self.gate.mode = "unavailable"
        doc = {
            "model": "llama3.2:3b",
            "response": "word " * 20000,
            "done": True,
            "context": list(range(5000)),
            "prompt_eval_count": 3,
            "eval_count": 10,
            "eval_duration": 500_000_000,
        }
        self.assertGreater(len(json.dumps(doc)), 4 * FINAL_CHUNK_BYTES)
        self.cpu.reply = json_reply(doc)
        await self.start_broker()
        _, body = await self.post("/api/generate", {"model": "llama3.2:3b", "prompt": "x", "stream": False})
        self.assertEqual(body["eval_count"], 10)
        [entry] = await self.wait_recent(1)
        self.assert_entry(
            entry,
            path="/api/generate",
            alias=None,  # not an alias: null, and the model is the requested name
            model="llama3.2:3b",
            backend="cpu",
            status=200,
            eval_count=10,
            eval_tok_s=20.0,
            error=None,
        )

    async def test_openai_usage_gives_eval_count_without_rate(self) -> None:
        usage = {"prompt_tokens": 5, "completion_tokens": 17, "total_tokens": 22}
        self.gpu.reply = json_reply({"id": "c1", "object": "text_completion", "choices": [{"text": "x"}], "usage": usage})
        await self.start_broker()
        await self.post("/v1/completions", {"model": "chat", "prompt": "x"})
        sse = [
            b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":9,"total_tokens":14}}\n\n',
            b"data: [DONE]\n\n",
        ]
        self.gpu.reply = ndjson_reply(sse)
        await self.post_raw("/v1/completions", {"model": "chat", "prompt": "x", "stream": True})
        newest, oldest = await self.wait_recent(2)
        self.assert_entry(oldest, path="/v1/completions", alias="chat", model="qwen3:8b", eval_count=17, eval_tok_s=None)
        self.assert_entry(newest, path="/v1/completions", eval_count=9, eval_tok_s=None)

    async def post_raw(self, path: str, body: Any) -> bytes:
        async with self.client.post(self.url(path), json=body) as resp:
            return await resp.read()

    async def test_no_final_counts_is_null(self) -> None:
        await self.start_broker()
        await self.post("/api/embed", {"model": "m", "input": "x"})  # the echo stub: no counts
        [entry] = await self.wait_recent(1)
        self.assert_entry(entry, path="/api/embed", eval_count=None, eval_tok_s=None, status=200, error=None)

    async def test_failover_records_final_backend_and_first_error(self) -> None:
        self.gpu.mode = "status503"
        self.cpu.reply = json_reply({"done": True, **STATS})
        await self.start_broker()
        resp, _ = await self.post("/api/chat", {"model": "chat", "messages": [], "stream": False})
        self.assertEqual(resp.headers["X-Backend"], "cpu")
        [entry] = await self.wait_recent(1)
        self.assert_entry(
            entry,
            alias="chat",
            model="qwen3:1.7b",
            backend="cpu",
            status=200,
            eval_count=42,
            eval_tok_s=21.0,
            error="gpu failed before first byte: HTTP 503",
        )

    async def test_failover_on_refused_gpu_names_no_address(self) -> None:
        port = free_port()
        await self.start_broker(gpu_url=f"http://127.0.0.1:{port}")
        await self.post("/api/chat", {"model": "chat", "messages": [], "stream": False})
        [entry] = await self.wait_recent(1)
        self.assert_entry(entry, backend="cpu", status=200, error="gpu failed before first byte: ClientConnectorError")

    async def test_both_down_records_both_reasons_and_no_url(self) -> None:
        gpu_port, cpu_port = free_port(), free_port()
        await self.start_broker(
            gpu_url=f"http://user:secret@127.0.0.1:{gpu_port}", cpu_url=f"http://user:secret@127.0.0.1:{cpu_port}"
        )
        resp, _ = await self.post("/api/chat", {"model": "chat", "messages": [], "stream": False})
        self.assertEqual(resp.status, 502)
        [entry] = await self.wait_recent(1)
        self.assert_entry(
            entry,
            backend="cpu",
            model="qwen3:1.7b",
            status=502,
            eval_count=None,
            error="gpu failed before first byte: ClientConnectorError; cpu unavailable: ClientConnectorError",
        )
        text = json.dumps(entry)
        for leak in ("secret", "127.0.0.1", str(gpu_port), str(cpu_port), "http"):
            self.assertNotIn(leak, text)

    async def test_errors_record_status_and_short_reason(self) -> None:
        self.gate.mode = "unavailable"
        self.cpu.mode = "status503"
        await self.start_broker(max_body_mib=1)
        await self.post("/api/generate", {"model": "m", "prompt": "x"})  # the backend's own error status
        await self.post("/api/chat", {"model": "chat", "images": ["A" * (2 << 20)]})  # broker 413
        too_large, backend_503 = await self.wait_recent(2)
        self.assert_entry(backend_503, path="/api/generate", backend="cpu", status=503, error="HTTP 503 from cpu")
        self.assert_entry(too_large, path="/api/chat", backend="broker", status=413, alias=None, model=None)
        self.assertIn("max_body_mib", too_large["error"])

    async def test_only_generation_and_embedding_paths_are_recorded(self) -> None:
        await self.start_broker(management_clients=LOCAL)  # an allowed pull is not recorded; a refusal is
        polling = [
            ("GET", "/api/tags"),
            ("GET", "/api/version"),
            ("GET", "/api/ps"),
            # Proxied HEAD is tested in HeadAndNoBodyTests, in a broker subprocess: without the fix a second
            # proxied HEAD in this process would block this event loop, so the suite would hang, not fail.
            ("GET", "/"),
            ("HEAD", "/api/tags"),
            ("GET", "/v1/models"),
            ("GET", "/v1/models/chat"),
            ("GET", "/v1/models/llama3.2:3b"),
            ("POST", "/api/show"),
            ("POST", "/api/pull"),
            ("GET", "/api/nope"),
            ("GET", "/broker/status"),
            ("GET", "/broker/healthz"),
        ]
        for method, path in polling:
            body = {"model": "chat"} if method == "POST" else None
            async with self.client.request(method, self.url(path), json=body) as resp:
                self.assertLess(resp.status, 500, path)
        # Rejected targets are not recorded, even ones that would normalize to a recorded path.
        for target in (b"POST /api/../api/chat", b"POST /api/ch%61t", b"POST http://evil.example/api/chat"):
            status, _, _ = await raw_http(
                self.port, target + b" HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            self.assertEqual(status, 400, target)
        self.assertEqual(await self.recent(), [])
        # Every generation and embedding path is (the contract's list, spelled out), and nothing else got in.
        recorded = [
            "/api/chat",
            "/api/generate",
            "/api/embed",
            "/api/embeddings",
            "/v1/chat/completions",
            "/v1/completions",
            "/v1/embeddings",
        ]
        self.gpu.release.set()  # the SSE stub holds its last event until released
        for path in recorded:
            await self.post_raw(path, {"model": "chat", "stream": False})
        await asyncio.sleep(0.1)
        recent = await self.recent()
        self.assertEqual([e["path"] for e in recent], recorded[::-1])

    async def test_alias_is_null_unless_an_alias_was_asked_for(self) -> None:
        await self.start_broker()
        await self.post("/api/embed", {"model": "llama3.2:3b", "input": "x"})
        await self.post("/api/embeddings", {"name": "chat", "prompt": "x"})  # the deprecated field counts too
        await self.post("/api/embed", {"input": "x"})  # no model at all
        no_model, by_name, plain = await self.wait_recent(3)
        self.assert_entry(plain, alias=None, model="llama3.2:3b")
        self.assert_entry(by_name, alias="chat", model="qwen3:8b")
        self.assert_entry(no_model, alias=None, model=None)

    async def test_midstream_break_records_reason(self) -> None:
        self.gpu.mode = "break_midstream"
        await self.start_broker()
        with contextlib.suppress(aiohttp.ClientPayloadError):
            await self.post_raw("/api/chat", {"model": "chat", "messages": []})
        [entry] = await self.wait_recent(1)
        self.assert_entry(entry, backend="gpu", status=200, eval_count=None)
        self.assertTrue(entry["error"].startswith("gpu broke mid-stream: "), entry["error"])
        self.assertNotIn("127.0.0.1", entry["error"])

    async def test_client_abort_mid_stream_is_recorded(self) -> None:
        self.gpu.mode = "endless"
        await self.start_broker()
        resp = await self.client.post(self.url("/api/chat"), json={"model": "chat", "messages": []})
        await asyncio.wait_for(resp.content.readline(), 2)
        resp.close()
        await asyncio.wait_for(self.gpu.write_failed.wait(), 3)
        [entry] = await self.wait_recent(1)
        self.assert_entry(
            entry, alias="chat", model="qwen3:8b", backend="gpu", status=499, eval_count=None, error="client disconnected"
        )

    async def test_client_abort_before_first_byte_is_recorded(self) -> None:
        self.gpu.mode = "hang"
        await self.start_broker()
        task = asyncio.create_task(self.post("/api/chat", {"model": "chat", "messages": [], "stream": False}))
        while not self.gpu.requests:
            await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.wait_for(self.gpu.write_failed.wait(), 3)
        [entry] = await self.wait_recent(1)
        self.assert_entry(entry, model="qwen3:8b", backend="gpu", status=499, error="client disconnected")

    async def test_ring_keeps_the_newest_200(self) -> None:
        await self.start_broker()
        for i in range(RECENT_SIZE + 5):
            resp, _ = await self.post("/api/embed", {"model": f"m{i}", "input": "x"})
            self.assertEqual(resp.status, 200)
        recent = await self.recent()
        self.assertEqual(len(recent), RECENT_SIZE)
        self.assertEqual(recent[0]["model"], f"m{RECENT_SIZE + 4}")
        self.assertEqual(recent[-1]["model"], "m5")
        self.assertEqual([e["model"] for e in recent], [f"m{i}" for i in range(RECENT_SIZE + 4, 4, -1)])

    async def test_no_prompt_or_response_text_is_ever_stored(self) -> None:
        prompt = [{"role": "user", "content": f"secret question {CANARY}"}]
        echo = {"message": {"content": f"answer {CANARY}"}, "done": False}
        self.gpu.reply = ndjson_reply([echo, echo, {"message": {"content": CANARY}, "done": True, **STATS}])
        await self.start_broker()
        # streamed chat; a canary in the query string, too
        await self.post_raw(f"/api/chat?note={CANARY}", {"model": "chat", "messages": prompt})
        # non-streamed generate whose whole answer is the canary
        self.gpu.reply = json_reply({"response": CANARY * 50, "done": True, **STATS})
        await self.post("/api/generate", {"model": "llama3.2:3b", "prompt": CANARY, "stream": False})
        # OpenAI-compatible, plus a non-JSON body
        self.gpu.reply = json_reply({"choices": [{"text": CANARY}], "usage": {"completion_tokens": 3}})
        await self.post("/v1/completions", {"model": "chat", "prompt": CANARY})
        async with self.client.post(self.url("/api/generate"), data=f"not json {CANARY}".encode()):
            pass
        # an error answer that echoes the prompt, and a failover
        self.gpu.reply = None
        self.gpu.mode = "status503"
        self.cpu.mode = "status503"
        await self.post("/api/generate", {"model": "chat", "prompt": CANARY})
        self.cpu.mode = "ok"
        await self.post("/api/generate", {"model": "chat", "prompt": CANARY})
        recent = await self.wait_recent(6)
        self.assertEqual(recent[-1]["eval_count"], 42, "the stream was read")
        async with self.client.get(self.url("/broker/recent")) as resp:
            raw = await resp.text()
        self.assertNotIn("CANARY", raw)
        self.assertNotIn("secret question", raw)
        self.assertNotIn("answer", raw)

    async def test_huge_streamed_response_keeps_a_bounded_tail(self) -> None:
        kept: list[FinalChunk] = []

        class Spy(FinalChunk):
            def __init__(self) -> None:
                super().__init__()
                kept.append(self)

        # ~16 MiB: one 8 MiB line (no newline for 128 writes), then ~8 MiB of ordinary lines, then the counts.
        piece = b"x" * 65536
        line = b'{"message":{"content":"' + piece * 128 + b'"},"done":false}\n'
        many = (json.dumps({"message": {"content": "y" * 1000}, "done": False}).encode() + b"\n") * 8000
        chunks: list[Any] = [line[i : i + 65536] for i in range(0, len(line), 65536)]
        chunks += [many[i : i + 65536] for i in range(0, len(many), 65536)]
        chunks.append({"done": True, **STATS})
        self.gpu.reply = ndjson_reply(chunks)
        await self.start_broker()
        with mock.patch.object(broker_app, "FinalChunk", Spy):
            received = len(await self.post_raw("/api/chat", {"model": "chat", "messages": []}))
            [entry] = await self.wait_recent(1)
        self.assertGreater(received, 15 * MIB)
        self.assertEqual(len(kept), 1)
        self.assertLessEqual(kept[0].peak, FINAL_CHUNK_BYTES)
        self.assertLessEqual(len(kept[0].tail), FINAL_CHUNK_BYTES)
        self.assertEqual((entry["eval_count"], entry["eval_tok_s"]), (42, 21.0))


class FinalChunkTests(unittest.TestCase):
    FINAL = b'{"model":"m","done":true,"context":[1,2,3],"eval_count":42,"eval_duration":2000000000}\n'

    def test_counts_split_across_any_chunk_boundary(self) -> None:
        body = b'{"message":{"content":"a"},"done":false}\n' * 3 + self.FINAL
        for size in (1, 2, 7, 64):
            with self.subTest(size=size):
                final = FinalChunk(limit=128)
                for i in range(0, len(body), size):
                    final.feed(body[i : i + size])
                self.assertEqual(final.counts(), (42, 2_000_000_000))
                self.assertLessEqual(final.peak, 128)

    def test_only_the_final_chunk_counts(self) -> None:
        final = FinalChunk()
        final.feed(self.FINAL + b'{"message":{"content":"more"},"done":false}\n')
        self.assertEqual(final.counts(), (None, None))

    def test_counts_inside_text_are_not_read(self) -> None:
        final = FinalChunk()
        final.feed(b'{"message":{"content":"x,\\"eval_count\\":999,\\"eval_duration\\":1"},"done":true}\n')
        self.assertEqual(final.counts(), (None, None))

    def test_sse_done_and_crlf_are_skipped(self) -> None:
        final = FinalChunk()
        final.feed(b'data: {"choices":[],"usage":{"completion_tokens":9}}\r\n\r\ndata: [DONE]\r\n\r\n')
        self.assertEqual(final.counts(), (9, None))

    def test_one_huge_write_keeps_only_the_limit(self) -> None:
        final = FinalChunk(limit=1024)
        final.feed(b"z" * (4 * MIB) + self.FINAL)
        self.assertEqual(len(final.tail), 1024)
        self.assertEqual(final.peak, 1024)
        self.assertEqual(final.counts(), (42, 2_000_000_000))


class HasBodyTests(unittest.TestCase):
    def resp(self, status: int, content: Any) -> Any:
        return types.SimpleNamespace(status=status, content=content)

    def test_bodiless_responses_are_never_read(self) -> None:
        reader = object()  # stands for a real StreamReader
        self.assertTrue(_has_body("GET", self.resp(200, reader)))
        self.assertTrue(_has_body("POST", self.resp(500, reader)))
        for method, status in (("HEAD", 200), ("HEAD", 404), ("GET", 204), ("GET", 304), ("GET", 101)):
            with self.subTest(method=method, status=status):
                self.assertFalse(_has_body(method, self.resp(status, reader)))

    def test_the_shared_empty_payload_is_never_read(self) -> None:
        # The backstop: whatever the method and status, aiohttp's shared empty reader is not iterated.
        for method in ("GET", "POST", "DELETE"):
            with self.subTest(method=method):
                self.assertFalse(_has_body(method, self.resp(200, aiohttp.EMPTY_PAYLOAD)))


class RelayOrderTests(BrokerCase):
    async def test_each_chunk_is_written_before_the_tracker_sees_it(self) -> None:
        events: list[tuple[str, bytes]] = []
        write = web.StreamResponse.write

        async def spy_write(out: web.StreamResponse, data: bytes) -> None:
            await write(out, data)
            if "X-Backend" in out.headers:  # the broker's response, not a stub's
                events.append(("written", bytes(data)))

        class SpyFinal(FinalChunk):
            def feed(self, data: bytes) -> None:
                events.append(("fed", bytes(data)))
                super().feed(data)

        lines = [{"message": {"content": f"t{i}"}, "done": False} for i in range(5)] + [{"done": True, **STATS}]

        async def slow_stream(request: web.Request) -> web.StreamResponse:
            out = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
            await out.prepare(request)
            for line in lines:
                await out.write(json.dumps(line).encode() + b"\n")
                await asyncio.sleep(0.02)  # one line per relay chunk
            await out.write_eof()
            return out

        self.gpu.reply = slow_stream
        await self.start_broker()
        with mock.patch.object(web.StreamResponse, "write", spy_write), mock.patch.object(broker_app, "FinalChunk", SpyFinal):
            async with self.client.post(self.url("/api/chat"), json={"model": "chat", "messages": []}) as resp:
                await resp.read()
        self.assertGreaterEqual(len(events), 2 * len(lines))
        # Strictly alternating: every chunk's write has completed before the tracker is fed that chunk.
        for i in range(0, len(events), 2):
            written, fed = events[i], events[i + 1]
            self.assertEqual((written[0], fed[0]), ("written", "fed"), events[i : i + 2])
            self.assertEqual(written[1], fed[1])


class UnparseableRequestLogTests(BrokerCase):
    async def test_malformed_requests_never_log_request_bytes(self) -> None:
        canary = b"CANARYchunk"
        head = b"POST /api/generate HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n"
        requests = [
            head + b"Content-Type: application/json\r\n\r\n10\r\n{\"p\":\"" + canary[:4] + b"\r\n" + canary + b" prompt\r\n",
            head + b"Content-Type: application/json\r\n\r\nzz" + canary + b"\r\nabc\r\n0\r\n\r\n",
            head + b"Content-Encoding: deflate\r\n\r\n20\r\n" + canary + b"0123456789012345678901\r\n0\r\n\r\n",
            b"POST /api/generate HTTP/1.1\r\nHost: x\r\nContent-Length: 30\r\nContent-Encoding: gzip\r\n\r\n"
            + b"\x1f\x8b\x08\x00" + canary + b"x" * 15,
            b"GET /api/version HTTP/1.1\r\nHost: x\r\nX-Bad: " + canary + b"\x00\r\n\r\n",
        ]
        await self.start_broker()
        with self.assertLogs(level="DEBUG") as logs:  # root: every logger, aiohttp's included
            for raw in requests:
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
                writer.write(raw)
                await writer.drain()
                answer = await asyncio.wait_for(reader.read(), 3)
                writer.close()
                self.assertIn(b" 400 ", answer.split(b"\r\n", 1)[0])
            await asyncio.sleep(0.2)
        unparseable = [r for r in logs.records if "unparseable request" in r.getMessage()]
        self.assertGreaterEqual(len(unparseable), 3, logs.output)
        for record in logs.records:
            text = record.getMessage() + (logging.Formatter().formatException(record.exc_info) if record.exc_info else "")
            self.assertNotIn("CANARY", text)
        async with self.client.get(self.url("/broker/recent")) as resp:
            self.assertNotIn("CANARY", await resp.text())


FIXED = 32768  # gpu_num_ctx in the tests below (the inventory's value)
EMBED_ALIASES = {**ALIASES, "embed": {"gpu": "nomic-embed-text", "cpu": "nomic-embed-text"}}


def as_sent(body: dict[str, Any], backend: str, aliases: dict[str, dict[str, str]] = ALIASES) -> dict[str, Any]:
    """`body` with its alias model/name fields rewritten for `backend`, and nothing else changed."""
    return {k: aliases[v][backend] if k in ("model", "name") and v in aliases else v for k, v in body.items()}


class NumCtxTests(BrokerCase):
    """gpu_num_ctx / cpu_num_ctx: one fixed context for alias generation requests (README "Fixed context")."""

    async def entries(self, n: int) -> list[dict[str, Any]]:
        """The newest-first /broker/recent once it holds n entries (they are added as handlers finish)."""
        deadline = time.monotonic() + 3
        while len(self.runner.app[BROKER].recent) < n:  # type: ignore[union-attr]
            if time.monotonic() > deadline:
                self.fail(f"/broker/recent never reached {n} entries")
            await asyncio.sleep(0.02)
        async with self.client.get(self.url("/broker/recent")) as resp:
            entries = await resp.json()
        self.assertEqual(len(entries), n)
        return entries

    async def post_bytes(self, path: str, body: Any) -> aiohttp.ClientResponse:
        """POST a JSON body (bytes as-is) and read the whole answer, whatever its type (SSE included)."""
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        async with self.client.post(self.url(path), data=data, headers={"Content-Type": "application/json"}) as resp:
            await resp.read()
            return resp

    async def test_alias_to_gpu_is_overwritten(self) -> None:
        await self.start_broker(gpu_num_ctx=FIXED)
        chat = {
            "model": "chat",
            "messages": [{"role": "user", "content": "héllo"}],
            "stream": False,
            "keep_alive": "30m",
            "options": {"num_ctx": 16384, "temperature": 0},
        }
        generate = {"model": "chat", "prompt": "x", "options": {"temperature": 0, "num_ctx": 4096, "seed": 3}}
        resp, _ = await self.post("/api/chat", chat)
        self.assertEqual(resp.headers["X-Backend"], "gpu")
        resp, _ = await self.post("/api/generate", generate)
        self.assertEqual(resp.headers["X-Backend"], "gpu")
        sent_chat, sent_generate = (r["json"] for r in self.gpu.requests)
        self.assertEqual(sent_chat, {**as_sent(chat, "gpu"), "options": {"num_ctx": FIXED, "temperature": 0}})
        self.assertEqual(
            sent_generate, {**as_sent(generate, "gpu"), "options": {"temperature": 0, "num_ctx": FIXED, "seed": 3}}
        )
        self.assertEqual(self.cpu.requests, [])
        newest, oldest = await self.entries(2)
        self.assertEqual(oldest["num_ctx"], {"requested": 16384, "sent": FIXED})
        self.assertEqual(newest["num_ctx"], {"requested": 4096, "sent": FIXED})
        self.assertEqual((oldest["path"], oldest["alias"], oldest["model"]), ("/api/chat", "chat", "qwen3:8b"))

    async def test_any_client_value_is_replaced(self) -> None:
        await self.start_broker(gpu_num_ctx=FIXED)
        # Larger (shrunk), equal, smaller, and values that aren't integers (Ollama would refuse a string).
        values: list[Any] = [65536, FIXED, 2048, "16384", 16384.0, None, True]
        for value in values:
            await self.post("/api/generate", {"model": "chat", "prompt": "x", "options": {"num_ctx": value}})
        self.assertEqual([r["json"]["options"] for r in self.gpu.requests], [{"num_ctx": FIXED}] * len(values))
        recorded = [e["num_ctx"] for e in reversed(await self.entries(len(values)))]
        requested = [65536, FIXED, 2048, None, None, None, None]  # only an integer is recorded
        self.assertEqual(recorded, [{"requested": r, "sent": FIXED} for r in requested])

    async def test_options_are_created_when_missing(self) -> None:
        await self.start_broker(gpu_num_ctx=FIXED)
        bodies: list[dict[str, Any]] = [
            {"model": "chat", "prompt": "x"},  # the Auto router's classifier, finance-digest: no num_ctx at all
            {"model": "chat", "prompt": "x", "options": None},
            {"model": "chat", "prompt": "x", "options": {}},
            {"model": "chat", "messages": [], "stream": False, "options": {"temperature": 0}},
        ]
        for body in bodies:
            await self.post("/api/chat" if "messages" in body else "/api/generate", body)
        sent = [r["json"] for r in self.gpu.requests]
        expected_options = [{"num_ctx": FIXED}] * 3 + [{"temperature": 0, "num_ctx": FIXED}]
        self.assertEqual(sent, [{**as_sent(b, "gpu"), "options": o} for b, o in zip(bodies, expected_options)])
        recorded = [e["num_ctx"] for e in await self.entries(len(bodies))]
        self.assertEqual(recorded, [{"requested": None, "sent": FIXED}] * len(bodies))

    async def test_alias_to_cpu_is_untouched(self) -> None:
        self.gate.mode = "unavailable"
        await self.start_broker(gpu_num_ctx=FIXED)
        bodies: list[dict[str, Any]] = [
            {"model": "chat", "prompt": "x", "options": {"num_ctx": 16384}},
            {"model": "chat", "messages": [], "stream": False},
            {"model": "chat", "prompt": "x", "options": {"temperature": 0}},
        ]
        for body in bodies:
            resp, _ = await self.post("/api/chat" if "messages" in body else "/api/generate", body)
            self.assertEqual(resp.headers["X-Backend"], "cpu")
        self.assertEqual([r["json"] for r in self.cpu.requests], [as_sent(b, "cpu") for b in bodies])
        self.assertEqual(self.gpu.requests, [])
        self.assertEqual([e["num_ctx"] for e in await self.entries(len(bodies))], [None] * len(bodies))

    async def test_failover_sends_the_clients_own_value_to_cpu(self) -> None:
        self.gpu.mode = "status503"
        await self.start_broker(gpu_num_ctx=FIXED)
        body = {"model": "chat", "prompt": "x", "options": {"num_ctx": 16384, "temperature": 0}}
        resp, _ = await self.post("/api/generate", body)
        self.assertEqual(resp.headers["X-Backend"], "cpu")
        self.assertEqual(self.gpu.requests[0]["json"]["options"], {"num_ctx": FIXED, "temperature": 0})
        self.assertEqual(self.cpu.requests[0]["json"], as_sent(body, "cpu"))
        [entry] = await self.entries(1)
        self.assertEqual((entry["backend"], entry["model"]), ("cpu", "qwen3:1.7b"))
        self.assertIsNone(entry["num_ctx"], "the entry describes the CPU attempt, which was not touched")

    async def test_cpu_num_ctx_is_its_own_setting(self) -> None:
        body = {"model": "chat", "prompt": "x", "options": {"num_ctx": 16384}}
        # cpu_num_ctx alone: the GPU gets the client's value.
        await self.start_broker(cpu_num_ctx=8192)
        await self.post("/api/generate", body)
        self.assertEqual(self.gpu.requests[-1]["json"], as_sent(body, "gpu"))
        await self.stop_broker()
        # Both set: each backend gets its own, including the CPU retry after a GPU failure.
        self.gpu.mode = "status503"
        await self.start_broker(gpu_num_ctx=FIXED, cpu_num_ctx=8192)
        resp, _ = await self.post("/api/generate", body)
        self.assertEqual(resp.headers["X-Backend"], "cpu")
        self.assertEqual(self.gpu.requests[-1]["json"]["options"], {"num_ctx": FIXED})
        self.assertEqual(self.cpu.requests[-1]["json"], {**as_sent(body, "cpu"), "options": {"num_ctx": 8192}})
        [entry] = await self.entries(1)
        self.assertEqual(entry["num_ctx"], {"requested": 16384, "sent": 8192})

    async def test_non_alias_models_pass_through_untouched(self) -> None:
        await self.start_broker(gpu_num_ctx=FIXED, cpu_num_ctx=8192)
        raws = [
            ("/api/generate", b'{"model": "llama3.2:3b", "prompt": "x", "options": {"num_ctx": 16384}}'),
            ("/api/generate", b'{ "model":"llama3.2:3b","prompt":"x" }'),
            ("/api/chat", b'{"model":"qwen3:8b","messages":[],"stream":false}'),  # an alias's GPU model, by name
            ("/api/generate", b'{"prompt":"x","options":{"num_ctx":4096}}'),  # no model at all
            ("/api/chat", b'{"model":"chat ","messages":[],"stream":false}'),  # not the alias's exact name
        ]
        for path, raw in raws:
            await self.post_bytes(path, raw)
        self.assertEqual([r["raw"] for r in self.gpu.requests], [raw for _, raw in raws], "forwarded byte for byte")
        # The alias only in the deprecated `name` (chat and generate read `model`): `name` is rewritten as
        # before, but the request is for llama3.2:3b, so its options stay as sent.
        await self.post("/api/generate", {"model": "llama3.2:3b", "name": "chat", "prompt": "x"})
        self.assertEqual(self.gpu.requests[-1]["json"], {"model": "llama3.2:3b", "name": "qwen3:8b", "prompt": "x"})
        self.assertEqual([e["num_ctx"] for e in await self.entries(len(raws) + 1)], [None] * (len(raws) + 1))

    async def test_streaming_still_works(self) -> None:
        await self.start_broker(gpu_num_ctx=FIXED)
        body = {"model": "chat", "messages": [], "options": {"num_ctx": 16384}}
        async with self.client.post(self.url("/api/chat"), json=body) as resp:
            self.assertEqual(resp.headers["X-Backend"], "gpu")
            self.assertEqual(resp.headers["Content-Type"], "application/x-ndjson")
            first = await asyncio.wait_for(resp.content.readline(), 2)
            self.assertFalse(self.gpu.sent_last, "the broker must not wait for the whole body")
            self.assertEqual(json.loads(first), {"model": "qwen3:8b", "backend": "gpu", "done": False})
            self.gpu.release.set()
            rest = await asyncio.wait_for(resp.read(), 2)
        self.assertTrue(self.gpu.sent_last)
        self.assertEqual([json.loads(x) for x in rest.splitlines()], [{"model": "qwen3:8b", "done": True}])
        self.assertEqual(self.gpu.requests[0]["json"]["options"], {"num_ctx": FIXED})
        # A streamed answer with token counts: still recorded from the final chunk.
        self.gpu.reply = ndjson_reply([{"response": "tok", "done": False}] * 3 + [{"done": True, **STATS}])
        async with self.client.post(self.url("/api/generate"), json={"model": "chat", "prompt": "x"}) as resp:
            self.assertEqual(len((await resp.read()).splitlines()), 4)
        newest, oldest = await self.entries(2)
        self.assertEqual((oldest["status"], oldest["num_ctx"]), (200, {"requested": 16384, "sent": FIXED}))
        self.assertEqual((newest["eval_count"], newest["eval_tok_s"]), (42, 21.0))
        self.assertEqual(newest["num_ctx"], {"requested": None, "sent": FIXED})

    async def test_embeddings_are_untouched(self) -> None:
        cases: list[tuple[str, dict[str, Any]]] = [
            ("/api/embed", {"model": "embed", "input": "x", "options": {"num_ctx": 2048}}),
            ("/api/embed", {"model": "embed", "input": ["x", "y"]}),
            ("/api/embeddings", {"model": "embed", "prompt": "x", "options": {"num_ctx": 2048}}),
            ("/api/embeddings", {"name": "embed", "prompt": "x"}),
            ("/v1/embeddings", {"model": "embed", "input": "x"}),
            ("/api/embed", {"model": "chat", "input": "x", "options": {"num_ctx": 2048}}),  # any alias, embedding path
        ]
        for mode, stub, backend in (("available", self.gpu, "gpu"), ("unavailable", self.cpu, "cpu")):
            with self.subTest(backend=backend):
                self.gate.mode = mode
                await self.start_broker(gpu_num_ctx=FIXED, cpu_num_ctx=8192, aliases=EMBED_ALIASES)
                for path, body in cases:
                    resp, _ = await self.post(path, body)
                    self.assertEqual(resp.headers["X-Backend"], backend)
                sent = [r["json"] for r in stub.requests[-len(cases) :]]
                self.assertEqual(sent, [as_sent(b, backend, EMBED_ALIASES) for _, b in cases])
                self.assertEqual([e["num_ctx"] for e in await self.entries(len(cases))], [None] * len(cases))
                await self.stop_broker()

    async def test_openai_and_other_routed_paths_are_untouched(self) -> None:
        await self.start_broker(gpu_num_ctx=FIXED)
        self.gpu.release.set()  # the SSE stub holds its last event until released
        cases: list[tuple[str, dict[str, Any]]] = [
            ("/v1/chat/completions", {"model": "chat", "messages": [], "stream": True}),
            ("/v1/completions", {"model": "chat", "prompt": "x"}),
            ("/api/show", {"model": "chat"}),
        ]
        for path, body in cases:
            resp = await self.post_bytes(path, body)
            self.assertEqual((resp.status, resp.headers["X-Backend"]), (200, "gpu"), path)
        self.assertEqual([r["json"] for r in self.gpu.requests], [as_sent(b, "gpu") for _, b in cases])
        self.assertEqual([e["num_ctx"] for e in await self.entries(2)], [None, None])  # /api/show isn't recorded

    async def test_malformed_options_are_forwarded_as_sent(self) -> None:
        await self.start_broker(gpu_num_ctx=FIXED)
        malformed: list[Any] = ["num_ctx=16384", [16384], 5, True]
        for options in malformed:
            await self.post("/api/generate", {"model": "chat", "prompt": "x", "options": options})
        self.assertEqual([r["json"]["options"] for r in self.gpu.requests], malformed)
        self.assertEqual([e["num_ctx"] for e in await self.entries(len(malformed))], [None] * len(malformed))

    async def test_default_null_passes_the_clients_value_through(self) -> None:
        await self.start_broker()
        body = {"model": "chat", "prompt": "x", "options": {"num_ctx": 16384}}
        await self.post("/api/generate", body)
        await self.post("/api/generate", {"model": "chat", "prompt": "x"})
        self.assertEqual(
            [r["json"] for r in self.gpu.requests], [as_sent(body, "gpu"), {"model": "qwen3:8b", "prompt": "x"}]
        )
        self.assertEqual([e["num_ctx"] for e in await self.entries(2)], [None, None])
        async with self.client.get(self.url("/broker/status")) as resp:
            self.assertEqual((await resp.json())["num_ctx"], {"gpu": None, "cpu": None})

    async def test_status_shows_the_fixed_contexts(self) -> None:
        await self.start_broker(gpu_num_ctx=FIXED, cpu_num_ctx=8192)
        async with self.client.get(self.url("/broker/status")) as resp:
            self.assertEqual((await resp.json())["num_ctx"], {"gpu": FIXED, "cpu": 8192})

    async def test_logged_at_debug_only_and_never_the_prompt(self) -> None:
        await self.start_broker(gpu_num_ctx=FIXED)
        with self.assertLogs("gpu_broker", "DEBUG") as logs:
            await self.post("/api/generate", {"model": "chat", "prompt": CANARY, "options": {"num_ctx": 65536}})
            await self.post("/api/chat", {"model": "chat", "messages": [{"role": "user", "content": CANARY}],
                                          "stream": False, "options": {"num_ctx": 16384}})  # fmt: skip
            await self.post("/api/generate", {"model": "chat", "prompt": CANARY})
            await self.entries(3)
        ctx = [r for r in logs.records if "num_ctx" in r.getMessage()]
        shrunk = f"num_ctx for gpu on /api/generate: 65536 -> {FIXED} (smaller than asked: a prompt over {FIXED} tokens"
        self.assertEqual(
            [(r.levelno, r.getMessage()) for r in ctx],
            [
                (logging.DEBUG, shrunk + " is truncated)"),
                (logging.DEBUG, f"num_ctx for gpu on /api/chat: 16384 -> {FIXED}"),
                (logging.DEBUG, f"num_ctx for gpu on /api/generate: - -> {FIXED}"),
            ],
        )
        for record in logs.records:
            self.assertNotIn(CANARY, record.getMessage())
        async with self.client.get(self.url("/broker/recent")) as resp:
            self.assertNotIn(CANARY, await resp.text())


class NumCtxSpawnedTests(SpawnedBrokerCase):
    async def check_rc(self, **extra: Any) -> tuple[int, bytes]:
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump({"gate_url": "http://g", "gpu_url": "http://g", "cpu_url": "http://c", **extra}, fh)
        self.addCleanup(os.unlink, path)
        check = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "gpu_broker", "--config", path, "--check",
            cwd=APP_DIR, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        _, err = await check.communicate()
        assert check.returncode is not None
        return check.returncode, err

    async def test_bad_num_ctx_fails_the_config_check(self) -> None:
        for key in ("gpu_num_ctx", "cpu_num_ctx"):
            for bad in ("32768", 32768.5, 1024, 300000, True):
                with self.subTest(key=key, bad=bad):
                    rc, err = await self.check_rc(**{key: bad})
                    self.assertEqual(rc, 2)
                    self.assertIn(key.encode(), err)
        self.assertEqual(await self.check_rc(gpu_num_ctx=FIXED, cpu_num_ctx=None), (0, b""))

    async def test_startup_line_names_the_fixed_contexts(self) -> None:
        proc, _, _ = await self.spawn(gpu_num_ctx=FIXED)
        assert proc.stdout is not None
        line = await asyncio.wait_for(proc.stdout.readline(), 5)
        self.assertIn(f"gpu_num_ctx={FIXED}, cpu_num_ctx=None".encode(), line)


class ManagementUnitTests(unittest.TestCase):
    """The pieces of the 12.23 guard on their own: what counts as management, and whose address it is."""

    def test_management_endpoint_reads_every_spelling(self) -> None:
        from gpu_broker.app import management_endpoint

        for target in PULL_TRICKS:
            if "blobs" not in target.lower():
                with self.subTest(target=target):
                    self.assertEqual(management_endpoint(target), "/api/pull")
        for target in PUSH_TRICKS:
            with self.subTest(target=target):
                self.assertEqual(management_endpoint(target), "/api/push")
        for target in ("/api/blobs", "/api/blobs/", "/API/Blobs/sha256:x", "/api//blobs/x"):
            self.assertEqual(management_endpoint(target), "/api/blobs", target)
        for target in ("/api/create", "/api/copy", "/api/delete", "/api/delete/"):
            self.assertEqual(management_endpoint(target), target.rstrip("/"), target)
        self.assertEqual(management_endpoint("/api/chat", "/api/pull"), "/api/pull", "any target counts")
        for target in (
            "/", "/api/chat", "/api/generate", "/api/embed", "/api/embeddings", "/api/show", "/api/tags",
            "/api/ps", "/api/version", "/v1/chat/completions", "/v1/models/api/pull", "/v1/api/pull",
            "/api/pullx", "/api/pull-request", "/api/blobsx", "/api/chat?x=/api/pull", "/api/chat#/api/pull",
            "/api/pull/../chat", "/api", "/broker/status",
        ):  # fmt: skip
            with self.subTest(target=target):
                self.assertIsNone(management_endpoint(target))

    def test_peer_ip_uses_the_socket_only(self) -> None:
        import ipaddress

        from gpu_broker.app import peer_ip

        def req(peername: Any, **headers: str) -> Any:
            return types.SimpleNamespace(protocol=types.SimpleNamespace(peername=peername), headers=headers,
                                         remote="127.0.0.1")  # fmt: skip

        self.assertEqual(peer_ip(req(("10.1.2.3", 5))), ipaddress.IPv4Address("10.1.2.3"))
        self.assertEqual(peer_ip(req(("10.1.2.3", 5), **{"X-Forwarded-For": "127.0.0.1"})), ipaddress.IPv4Address("10.1.2.3"))
        self.assertEqual(peer_ip(req(("::ffff:10.1.2.3", 5, 0, 0))), ipaddress.IPv4Address("10.1.2.3"))
        self.assertEqual(peer_ip(req(("fd00::3", 5, 0, 0))), ipaddress.IPv6Address("fd00::3"))
        scoped = peer_ip(req(("fe80::1%eth0", 5, 0, 0)))
        self.assertNotIn(scoped, from_dict({**ConfigTests.BASE, "management_clients": ["fe80::1"]}).management_clients)
        for peername in (None, "/run/broker.sock", ("not-an-ip", 1), ()):
            self.assertIsNone(peer_ip(req(peername)), peername)


class ConfigTests(unittest.TestCase):
    BASE: ClassVar[dict[str, str]] = {
        "gate_url": "http://g:11500",
        "gpu_url": "http://gpu:11434/",
        "cpu_url": "http://cpu:11434",
    }

    def test_defaults(self) -> None:
        cfg = from_dict(self.BASE)
        self.assertEqual(cfg.listen_port, 11434)
        self.assertEqual(cfg.status_ttl, 5.0)
        self.assertEqual(cfg.gate_timeout, 1.0)
        self.assertIsNone(cfg.gpu_first_byte_timeout)
        self.assertEqual(cfg.gpu_url, "http://gpu:11434")
        self.assertEqual(cfg.aliases, {})

    def test_urls_required(self) -> None:
        for key in ("gate_url", "gpu_url", "cpu_url"):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                from_dict({k: v for k, v in self.BASE.items() if k != key})

    def test_bad_values_rejected(self) -> None:
        bad = [
            {"aliases": {"a": {"gpu": "x"}}},
            {"aliases": {"a": {"gpu": "x", "cpu": ""}}},
            {"aliases": ["a"]},
            {"status_ttl": -1},
            {"gate_timeout": "1"},
            {"listen_port": 0},
            {"failover_statuses": ["500"]},
            {"max_body_mib": 64, "max_buffered_bytes": 63 * 1024 * 1024},
            {"body_read_timeout": 0},
            {"typo_key": 1},
            {"gate_url": "ftp://x"},
        ]
        for extra in bad:
            with self.subTest(extra=extra), self.assertRaises(ConfigError):
                from_dict({**self.BASE, **extra})

    def test_num_ctx(self) -> None:
        cfg = from_dict(self.BASE)
        self.assertEqual((cfg.gpu_num_ctx, cfg.cpu_num_ctx), (None, None), "default: the client's value")
        for key in ("gpu_num_ctx", "cpu_num_ctx"):
            for good in (None, 2048, 16384, 32768, 262144):
                with self.subTest(key=key, good=good):
                    self.assertEqual(getattr(from_dict({**self.BASE, key: good}), key), good)
            for bad in (2047, 262145, 0, -1, True, False, 32768.0, "32768", "", [32768], {"gpu": 32768}):
                with self.subTest(key=key, bad=bad), self.assertRaises(ConfigError) as ctx:
                    from_dict({**self.BASE, key: bad})
                self.assertIn(key, str(ctx.exception))
        cfg = from_dict({**self.BASE, "gpu_num_ctx": 32768, "cpu_num_ctx": 8192})
        self.assertEqual((cfg.num_ctx_for("gpu"), cfg.num_ctx_for("cpu")), (32768, 8192))
        cfg = from_dict({**self.BASE, "gpu_num_ctx": 32768})
        self.assertEqual((cfg.num_ctx_for("gpu"), cfg.num_ctx_for("cpu")), (32768, None))

    def test_management_clients(self) -> None:
        import ipaddress

        self.assertEqual(from_dict(self.BASE).management_clients, frozenset(), "default: nobody")
        cfg = from_dict({**self.BASE, "management_clients": ["172.30.0.3", "172.30.0.4/32", "fd00::3", "FD00::4/128"]})
        self.assertEqual(
            cfg.management_clients,
            {ipaddress.ip_address(a) for a in ("172.30.0.3", "172.30.0.4", "fd00::3", "fd00::4")},
        )
        bad = [
            "172.30.0.3",  # a string, not a list
            {"172.30.0.3": True},
            None,
            [None],
            [1],
            [True],
            [["172.30.0.3"]],
            [""],
            [" 172.30.0.3"],
            ["172.30.0.3 "],
            ["172.30.0.3\n"],
            ["open-webui"],  # names are never resolved
            ["*"],
            ["172.30.0.03"],  # leading zero
            ["172.30.0"],
            ["172.30.0.256"],
            ["172.30.0.0/24"],
            ["172.30.0.3/31"],
            ["172.30.0.3/0"],
            ["0.0.0.0/0"],
            ["172.30.0.3/032"],
            ["172.30.0.3/"],
            ["172.30.0.3/32/32"],
            ["172.30.0.3/255.255.255.255"],
            ["fd00::/64"],
            ["fd00::3/127"],
            ["fd00::3/32"],
            ["fe80::1%eth0"],
            ["::ffff:172.30.0.3"],
            ["0.0.0.0"],
            ["::"],
            ["224.0.0.1"],
            ["ff02::1"],
            ["255.255.255.255"],
            ["172.30.0.3", "172.30.0.3/32"],  # listed twice
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ConfigError) as ctx:
                from_dict({**self.BASE, "management_clients": value})
            self.assertIn("management_clients", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
