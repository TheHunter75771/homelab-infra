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
import unittest
import warnings
from pathlib import Path
from typing import Any, ClassVar

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer  # stubs only; the broker runs on the production runner
from yarl import URL

from gpu_broker.app import BROKER, _forward_headers
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

    async def _pre(self, request: web.Request) -> web.Response | None:
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
        return None

    async def tags(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.json_response({"models": CPU_TAGS})

    async def v1_models(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.json_response({"object": "list", "data": CPU_V1_MODELS})

    async def echo(self, request: web.Request) -> web.Response:
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
        await self.start_broker()
        calls = [
            ("POST", "/api/pull", {"model": "chat"}),
            ("POST", "/api/push", {"model": "x"}),
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
        self.assertEqual(self.cpu.requests[4]["json"], {"model": "chat"})

    async def test_blob_upload_streams_to_cpu(self) -> None:
        await self.start_broker(max_body_mib=1)
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


async def raw_http(port: int, request: bytes) -> tuple[int, dict[str, str], bytes]:
    """Send bytes as-is (aiohttp's client would normalize the path) and parse the response."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
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
        await self.start_broker()
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
        await self.start_broker()
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
        await self.start_broker()
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
        await self.start_broker(max_body_mib=1)
        cases = [
            ("GET", "/api/nope", None, 404),
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


class EntryPointTests(BrokerCase):
    """`python -m gpu_broker` as the container runs it: real signals, real runner, real logs."""

    async def spawn(self, **overrides: Any) -> tuple[asyncio.subprocess.Process, str, int]:
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
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "gpu_broker",
            "--config",
            path,
            cwd=APP_DIR,
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


if __name__ == "__main__":
    unittest.main()
