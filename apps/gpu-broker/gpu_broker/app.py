"""The broker: an Ollama-compatible reverse proxy that picks the GPU or CPU Ollama per request."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
import string
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.web_protocol import RequestPayloadError
from yarl import URL

from .config import Config
from .gate import GateClient

log = logging.getLogger("gpu_broker")

# Proxied with gate routing (and alias rewrite of the JSON body's model).
ROUTED_PATHS = frozenset(
    {
        "/",  # "Ollama is running": the CLI and some clients probe it
        "/api/chat",
        "/api/generate",
        "/api/embed",
        "/api/embeddings",
        "/api/show",
        "/api/ps",
        "/api/version",
    }
)
ROUTED_PREFIXES = ("/v1/",)
# Model management: CPU backend only, body passed through untouched (no alias rewrite).
MANAGEMENT_PATHS = frozenset({"/api/pull", "/api/delete", "/api/create", "/api/copy", "/api/push"})
# `ollama create` uploads model files here first; they can be gigabytes, so the body is streamed.
MANAGEMENT_PREFIXES = ("/api/blobs/",)
# Answered by the broker: aliases + the CPU backend's model list.
MERGED_PATHS = frozenset({"/api/tags", "/v1/models"})
OPENAI_MODEL_PREFIX = "/v1/models/"

# Never forwarded in either direction (RFC 9110 7.6.1), plus the headers named in Connection.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "expect",  # the broker reads the whole body itself; never make a backend send 100-continue
    }
)
# aiohttp's server decodes request bodies with exactly these Content-Encodings (http_parser.py); any
# other value reaches the handler still encoded and is forwarded with its header.
SERVER_DECODED_ENCODINGS = frozenset({"gzip", "deflate", "br", "zstd"})
# Ollama still accepts the deprecated "name" on show/embeddings.
MODEL_FIELDS = ("model", "name")
MIB = 1024 * 1024
LARGE_BODY = MIB  # aiohttp's threshold for "large raw bytes body"
SMALL_BODY = MIB  # bodies up to this size never wait for (or use) the buffer budget (CONTRACTS 12.9)
UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")
ENCODED_SEPARATOR = re.compile(r"%(2[Ff]|5[Cc])")

# Per-request bookkeeping for the log line: alias, backend, failover, note, ttfb.
REQUEST_INFO = web.RequestKey("gpu_broker_info", dict)


class UpstreamFailure(Exception):
    """The backend failed before the broker sent any response byte to the client."""


class BrokerReply(Exception):
    """Abort request handling with a broker-generated response (caught in Broker.handle).

    Raised only while the request body has not been fully read (408/413/503/400 from buffered_body), so
    the connection is not kept alive afterwards. aiohttp then "lingers" for at most 10 s, draining what
    the client still sends so it can read the error before the close. The budget was already released
    when the handler returned, so lingering never holds budget.
    """

    def __init__(self, response: web.Response, *, drop_connection: bool = False) -> None:
        super().__init__(response.status)
        response.force_close()
        self.response = response
        # True when the rest of the body can never be read (a corrupt encoding): aiohttp's
        # post-response drain would hit the same decode error and log it as an ERROR traceback,
        # so the broker sends the response itself and closes the connection without draining.
        self.drop_connection = drop_connection


class ByteBudget:
    """A counting budget of bytes. acquire() waits (with a timeout) until n bytes fit; release() never blocks."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.used = 0
        self._waiters: list[asyncio.Future[None]] = []

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    async def acquire(self, n: int, timeout: float) -> None:
        if n > self.capacity:
            raise ValueError(f"{n} bytes can never fit a {self.capacity}-byte budget")
        loop = asyncio.get_running_loop()
        async with asyncio.timeout(timeout):
            while self.used + n > self.capacity:
                waiter: asyncio.Future[None] = loop.create_future()
                self._waiters.append(waiter)
                try:
                    await waiter
                finally:
                    self._waiters.remove(waiter)
        self.used += n  # no await between the check and this line

    def release(self, n: int) -> None:
        self.used -= n
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)


class _UpstreamBroke(Exception):
    """The backend failed after the response started (no failover possible)."""


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def broker_error(status: int, message: str) -> web.Response:
    """A response the broker generates itself (X-Backend: broker, Ollama-style error body)."""
    resp = web.json_response({"error": f"gpu-broker: {message}"}, status=status)
    resp.headers["X-Backend"] = "broker"
    return resp


def path_problem(raw_path: str) -> str | None:
    """Why the request target would change under normalization, or None if it is already normal.

    Rejected: anything not in origin-form (absolute-form `http://...`, asterisk `*`), empty segments
    (`//`), dot segments (`.`/`..`), percent-encoded unreserved characters (`%2e`, `%41`), which
    RFC 3986 normalization would decode, and encoded separators (`%2F`, `%5C`), which a backend may
    decode into a path separator (`/v1%2F..%2Fapi%2Fpull`). The broker never forwards a path a backend could read differently.
    """
    if not raw_path.startswith("/"):
        return "request target is not origin-form"
    path = raw_path.split("?", 1)[0]
    if "//" in path:
        return "empty path segment (//)"
    separator = ENCODED_SEPARATOR.search(path)
    if separator:
        return f"encoded path separator ({separator.group(0)})"
    for match in PERCENT.finditer(path):
        if chr(int(match.group(1), 16)) in UNRESERVED:
            return f"percent-encoded unreserved character ({match.group(0)})"
    if any(segment in (".", "..") for segment in path.split("/")):
        return "dot segment"
    return None


def _connection_tokens(headers: Any) -> set[str]:
    tokens: set[str] = set()
    for value in headers.getall("Connection", ()):
        tokens.update(t.strip().lower() for t in value.split(",") if t.strip())
    return tokens


def _forward_headers(headers: Any, *, keep_length: bool, upstream: bool, body_decoded: bool = False) -> dict[str, str]:
    """Copy end-to-end headers.

    upstream=True for the request the broker sends to a backend. body_decoded=True when aiohttp has
    already decoded the request body's Content-Encoding, so the header (and its length) no longer apply.
    """
    drop = HOP_BY_HOP | _connection_tokens(headers)
    if body_decoded:
        drop = drop | {"content-encoding", "content-length"}
    out: dict[str, str] = {}
    for key, value in headers.items():
        low = key.lower()
        if low in drop or (low == "content-length" and not keep_length):
            continue
        out[key] = value
    if upstream and "accept-encoding" not in {k.lower() for k in out}:
        # aiohttp would otherwise advertise gzip; never hand a client an encoding it did not ask for.
        out["Accept-Encoding"] = "identity"
    return out


def _body_decoded(request: web.Request) -> bool:
    encoding = request.headers.get("Content-Encoding", "")
    return encoding.isascii() and encoding.lower() in SERVER_DECODED_ENCODINGS


async def _next_chunk(chunks: Any) -> bytes | None:
    """The next body chunk from the backend, None at the end; backend errors become _UpstreamBroke."""
    try:
        data, _ = await chunks.__anext__()
    except StopAsyncIteration:
        return None
    except (aiohttp.ClientError, OSError, TimeoutError) as exc:
        raise _UpstreamBroke(_describe(exc)) from exc
    return bytes(data)


class JsonBody:
    """The request body, with the model rewritten per backend when it names an alias."""

    def __init__(self, raw: bytes, config: Config) -> None:
        self.raw = raw
        self.alias: str | None = None
        self._doc: dict[str, Any] | None = None
        self._config = config
        if not raw:
            return
        try:
            doc = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return  # not JSON: pass through unchanged
        if not isinstance(doc, dict):
            return
        for key in MODEL_FIELDS:
            value = doc.get(key)
            if isinstance(value, str) and value in config.aliases:
                self.alias = value
                self._doc = doc
                break

    def for_backend(self, backend: str) -> bytes:
        if self._doc is None or self.alias is None:
            return self.raw
        doc = dict(self._doc)
        model = self._config.aliases[self.alias].for_backend(backend)
        for key in MODEL_FIELDS:
            if doc.get(key) == self.alias:
                doc[key] = model
        # ASCII output: any string json.loads accepted (even a lone surrogate from "\ud800") re-encodes.
        return json.dumps(doc, ensure_ascii=True, separators=(",", ":")).encode("ascii")


class Broker:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.started = time.monotonic()
        self.proxy_session: aiohttp.ClientSession | None = None
        self.api_session: aiohttp.ClientSession | None = None
        self.gate: GateClient | None = None
        self.budget = ByteBudget(config.max_buffered_bytes)
        self.closing = False

    # ---- lifecycle -------------------------------------------------------------------------------
    async def startup(self, _app: web.Application) -> AsyncIterator[None]:
        cfg = self.config
        # Proxy session: raw bytes (no decompression) and no total/read timeout, because a CPU
        # generation can legitimately take minutes. Only the TCP connect is bounded.
        self.proxy_session = aiohttp.ClientSession(
            auto_decompress=False,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=cfg.connect_timeout),
        )
        # API session: the broker's own small JSON calls (gate status, CPU model list).
        self.api_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, sock_connect=cfg.connect_timeout),
        )
        self.gate = GateClient(self.api_session, cfg.gate_url, cfg.status_ttl, cfg.gate_timeout)
        yield
        # Runs after the server has finished (or cancelled) every handler.
        await self.proxy_session.close()
        await self.api_session.close()

    async def on_shutdown(self, _app: web.Application) -> None:
        # From here on a failed backend request is reported, never retried on the other backend.
        self.closing = True

    # ---- routing ---------------------------------------------------------------------------------
    def base_url(self, backend: str) -> str:
        return self.config.gpu_url if backend == "gpu" else self.config.cpu_url

    async def choose_backend(self) -> str:
        assert self.gate is not None
        answer = await self.gate.get()
        return "gpu" if answer.available else "cpu"

    async def handle(self, request: web.Request) -> web.StreamResponse:
        try:
            return await self._dispatch(request)
        except BrokerReply as reply:
            if reply.drop_connection:
                await reply.response.prepare(request)
                await reply.response.write_eof()  # queued in the transport, which flushes on close
                request.protocol.force_close()  # no lingering drain of the undecodable body
            return reply.response

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        path = request.path
        if path in MERGED_PATHS:
            if request.method != "GET":
                return broker_error(405, f"{request.method} not allowed on {path}")
            return await self.merged_models(request)
        if (
            path.startswith(OPENAI_MODEL_PREFIX)
            and request.method == "GET"
            and path[len(OPENAI_MODEL_PREFIX) :] in self.config.aliases
        ):
            return await self.openai_alias_model(request, path[len(OPENAI_MODEL_PREFIX) :])
        if path in MANAGEMENT_PATHS or path.startswith(MANAGEMENT_PREFIXES):
            return await self.proxy_management(request)
        if path in ROUTED_PATHS or path.startswith(ROUTED_PREFIXES):
            return await self.proxy_routed(request)
        return broker_error(404, f"does not proxy {path}")

    @contextlib.asynccontextmanager
    async def buffered_body(self, request: web.Request) -> AsyncIterator[bytes]:
        """Read the whole request body into memory, under the byte budget, for as long as the block runs.

        - A body of SMALL_BODY (1 MiB) or less never waits for budget and does not use any.
        - A larger body reserves budget before it is held: its Content-Length up front when that is the
          real size, or max_body_mib as soon as an undeclared (chunked, or Content-Encoding-decoded)
          body passes 1 MiB, shrunk to the actual size once read. Waiting longer than
          buffer_wait_timeout -> 503.
        - The read itself must finish within body_read_timeout (time spent waiting for budget does not
          count) -> 408. Over max_body_mib -> 413. A corrupt encoded body -> 400.
        - The reservation is released when the block exits, whatever the reason (response, error,
          failover, cancellation).
        """
        if not request.can_read_body:
            yield b""
            return
        cfg = self.config
        limit = cfg.max_body_mib * MIB
        loop = asyncio.get_running_loop()
        deadline = loop.time() + cfg.body_read_timeout
        reserved = 0

        async def reserve(target: int) -> None:
            """Grow the reservation to `target` bytes in total."""
            nonlocal reserved, deadline
            n = target - reserved
            started = loop.time()
            try:
                await self.budget.acquire(n, cfg.buffer_wait_timeout)
            except TimeoutError:
                raise BrokerReply(
                    broker_error(
                        503,
                        f"buffer budget busy: no room for {n} bytes within buffer_wait_timeout "
                        f"({cfg.buffer_wait_timeout:g}s)",
                    )
                ) from None
            reserved = target
            deadline += loop.time() - started

        try:
            declared = None if _body_decoded(request) else request.content_length
            if declared is not None and declared > limit:
                raise BrokerReply(self.too_large())
            if declared is not None and declared > SMALL_BODY:
                await reserve(declared)
            parts: list[bytes] = []
            total = 0
            while True:
                try:
                    async with asyncio.timeout_at(deadline):
                        chunk = await request.content.readany()
                except TimeoutError:
                    raise BrokerReply(
                        broker_error(
                            408, f"request body not received within body_read_timeout ({cfg.body_read_timeout:g}s)"
                        )
                    ) from None
                except RequestPayloadError as exc:
                    raise self.unreadable_body(request, exc) from None
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise BrokerReply(self.too_large())
                parts.append(chunk)
                if total > SMALL_BODY and total > reserved:
                    # Size undeclared (or the declaration was wrong): reserve the worst case, shrink below.
                    await reserve(limit)
            body = b"".join(parts)
            parts.clear()
            if reserved > total:
                self.budget.release(reserved - total)
                reserved = total
            yield body
        finally:
            if reserved:
                self.budget.release(reserved)

    def unreadable_body(self, request: web.Request, exc: BaseException) -> BrokerReply:
        """400 for a body aiohttp can't decode: one WARNING line, then the connection is dropped."""
        detail = " ".join(str(exc).split())
        log.warning("unreadable request body on %s from %s: %s", request.path, request.remote, detail)
        return BrokerReply(broker_error(400, f"unreadable request body: {detail}"), drop_connection=True)

    def too_large(self) -> web.Response:
        return broker_error(413, f"request body over max_body_mib ({self.config.max_body_mib} MiB)")

    def shutting_down(self, backend: str, exc: Exception) -> web.Response:
        log.info("%s failed during shutdown (%s): not retrying", backend, exc)
        return broker_error(503, "shutting down")

    async def proxy_routed(self, request: web.Request) -> web.StreamResponse:
        info = request[REQUEST_INFO]
        headers = _forward_headers(
            request.headers, keep_length=False, upstream=True, body_decoded=_body_decoded(request)
        )
        # The body stays in memory (and in the budget) from the read until the first response byte:
        # it is needed for the GPU->CPU retry.
        async with self.buffered_body(request) as raw:
            body = JsonBody(raw, self.config)
            info["alias"] = body.alias
            backend = await self.choose_backend()
            if backend == "gpu":
                try:
                    opened = await self.open_upstream(
                        request,
                        "gpu",
                        headers,
                        body.for_backend("gpu"),
                        first_byte_timeout=self.config.gpu_first_byte_timeout,
                        fail_on_status=True,
                    )
                except UpstreamFailure as exc:
                    if self.closing:
                        return self.shutting_down("gpu", exc)
                    log.warning("gpu failed before first byte on %s (%s): retrying once on cpu", request.path, exc)
                    info["failover"] = True
                    backend = "cpu"
            if backend == "cpu":
                try:
                    opened = await self.open_upstream(
                        request, "cpu", headers, body.for_backend("cpu"), first_byte_timeout=None, fail_on_status=False
                    )
                except UpstreamFailure as exc:
                    if self.closing:
                        return self.shutting_down("cpu", exc)
                    info["backend"] = "cpu"
                    return self.bad_gateway("cpu", exc, failover=info["failover"])
            del raw, body
        return await self.relay(request, backend, *opened)

    async def proxy_management(self, request: web.Request) -> web.StreamResponse:
        streamed = request.path.startswith(MANAGEMENT_PREFIXES)
        decoded = _body_decoded(request)
        headers = _forward_headers(request.headers, keep_length=streamed, upstream=True, body_decoded=decoded)
        data: Any
        if streamed or not request.can_read_body:
            data = request.content.iter_chunked(1 << 16) if request.can_read_body else None
            opened = await self._open_management(request, headers, data)
        else:
            async with self.buffered_body(request) as data:
                opened = await self._open_management(request, headers, data)
                del data
        if isinstance(opened, web.Response):
            return opened
        return await self.relay(request, "cpu", *opened)

    async def _open_management(
        self, request: web.Request, headers: dict[str, str], data: Any
    ) -> tuple[aiohttp.ClientResponse, Any, bytes | None] | web.Response:
        try:
            return await self.open_upstream(
                request, "cpu", headers, data, first_byte_timeout=None, fail_on_status=False
            )
        except UpstreamFailure as exc:
            unreadable = request.content.exception()
            if isinstance(unreadable, RequestPayloadError):
                # A streamed body (blob upload) that can't be decoded: the client's fault, not the CPU's.
                raise self.unreadable_body(request, unreadable) from None
            if self.closing:
                return self.shutting_down("cpu", exc)
            request[REQUEST_INFO]["backend"] = "cpu"
            return self.bad_gateway("cpu", exc)

    # ---- upstream --------------------------------------------------------------------------------
    async def open_upstream(
        self,
        request: web.Request,
        backend: str,
        headers: dict[str, str],
        data: Any,
        *,
        first_byte_timeout: float | None,
        fail_on_status: bool,
    ) -> tuple[aiohttp.ClientResponse, Any, bytes | None]:
        """Send the request and wait for the first body chunk.

        Returns (response, chunk iterator, first chunk or None for an empty body). Anything that
        goes wrong before then (connect error, reset, timeout, a failover status when
        fail_on_status) raises UpstreamFailure; nothing has been sent to the client yet.
        """
        session = self.proxy_session
        if session is None or session.closed:
            raise UpstreamFailure("broker is shutting down")
        url = URL(self.base_url(backend) + request.raw_path, encoded=True)
        if isinstance(data, bytes) and len(data) > LARGE_BODY:
            data = io.BytesIO(data)  # sent in chunks instead of one write that blocks the loop
        resp: aiohttp.ClientResponse | None = None
        ok = False
        try:
            async with asyncio.timeout(first_byte_timeout):
                resp = await session.request(request.method, url, headers=headers, data=data, allow_redirects=False)
                if fail_on_status and resp.status in self.config.failover_statuses:
                    raise UpstreamFailure(f"HTTP {resp.status}")
                chunks = resp.content.iter_chunks().__aiter__()
                try:
                    first: bytes | None = (await chunks.__anext__())[0]
                except StopAsyncIteration:
                    first = None
            ok = True
            return resp, chunks, first
        except (aiohttp.ClientError, OSError, TimeoutError, RuntimeError) as exc:
            # RuntimeError: the session was closed under us (shutdown) - a failure, not a crash.
            raise UpstreamFailure(_describe(exc)) from exc
        finally:
            # Covers failures and cancellation (the client left while we waited): closing the
            # connection makes Ollama abandon the request instead of generating for nobody.
            if not ok and resp is not None:
                resp.close()

    async def relay(
        self,
        request: web.Request,
        backend: str,
        resp: aiohttp.ClientResponse,
        chunks: Any,
        first: bytes | None,
    ) -> web.StreamResponse:
        """Stream the backend response to the client chunk by chunk (never buffered whole)."""
        info = request[REQUEST_INFO]
        info["backend"] = backend
        info["ttfb"] = time.monotonic()
        keep_length = "transfer-encoding" not in resp.headers
        headers = _forward_headers(resp.headers, keep_length=keep_length, upstream=False)
        out = web.StreamResponse(status=resp.status, reason=resp.reason, headers=headers)
        out.headers["X-Backend"] = backend
        if info.get("failover"):
            out.headers["X-Broker-Failover"] = "gpu"
        clean = False
        try:
            await out.prepare(request)
            data = first
            while data is not None:
                if data:
                    await out.write(data)
                data = await _next_chunk(chunks)
            await out.write_eof()
            clean = True
        except _UpstreamBroke as exc:
            # The backend broke mid-stream. Bytes are already out, so no failover: cut the client
            # connection so it sees a truncated response, not a clean end.
            info["note"] = f"upstream-broke: {exc}"
            log.warning("%s backend broke mid-stream on %s: %s", backend, request.path, exc)
            if request.transport is not None:
                request.transport.close()
        except (ConnectionResetError, aiohttp.ClientConnectionResetError):
            info["note"] = "client-gone"  # closing the upstream below makes Ollama stop generating
        finally:
            if clean:
                resp.release()
            else:
                resp.close()
        return out

    def bad_gateway(self, backend: str, exc: Exception, *, failover: bool = False) -> web.Response:
        resp = web.json_response({"error": f"gpu-broker: {backend} backend unavailable: {exc}"}, status=502)
        resp.headers["X-Backend"] = backend
        if failover:
            resp.headers["X-Broker-Failover"] = "gpu"
        return resp

    # ---- model lists -----------------------------------------------------------------------------
    async def cpu_model_list(
        self, request: web.Request, path: str, key: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """The CPU backend's model list at `path` (entries under `key`), or ([], why it failed)."""
        assert self.api_session is not None
        try:
            async with self.api_session.get(self.config.cpu_url + path) as resp:
                if resp.status != 200:
                    raise ValueError(f"HTTP {resp.status}")
                doc = await resp.json(content_type=None)
            listed = doc.get(key) if isinstance(doc, dict) else None
            if not isinstance(listed, list):
                raise TypeError(f"no '{key}' list in answer")
            return [m for m in listed if isinstance(m, dict)], None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure: list the aliases alone, say why
            warning = f"cpu model list unavailable: {_describe(exc)}"
            request[REQUEST_INFO]["note"] = warning
            log.warning("%s: %s", request.path, warning)
            return [], warning

    async def merged_models(self, request: web.Request) -> web.Response:
        """/api/tags and /v1/models: the aliases presented as models, then the CPU backend's models."""
        request[REQUEST_INFO]["backend"] = "cpu"
        openai = request.path == "/v1/models"
        key, name_key = ("data", "id") if openai else ("models", "name")
        cpu_models, warning = await self.cpu_model_list(request, request.path, key)
        by_name = {m.get(name_key): m for m in cpu_models}
        aliases = [self._alias_entry(alias, by_name, openai) for alias in self.config.aliases]
        rest = [m for m in cpu_models if m.get(name_key) not in self.config.aliases]
        body: dict[str, Any] = {"object": "list", "data": aliases + rest} if openai else {"models": aliases + rest}
        return self._cpu_json(body, warning)

    async def openai_alias_model(self, request: web.Request, alias: str) -> web.Response:
        """GET /v1/models/<alias>: the alias's entry, as it appears in /v1/models."""
        info = request[REQUEST_INFO]
        info["backend"] = "cpu"
        info["alias"] = alias
        cpu_models, warning = await self.cpu_model_list(request, "/v1/models", "data")
        entry = self._alias_entry(alias, {m.get("id"): m for m in cpu_models}, openai=True)
        return self._cpu_json(entry, warning)

    def _cpu_json(self, body: dict[str, Any], warning: str | None) -> web.Response:
        out = web.json_response(body)
        out.headers["X-Backend"] = "cpu"
        if warning:
            out.headers["X-Broker-Warning"] = "cpu-model-list-unavailable"
        return out

    def _alias_entry(self, alias: str, by_name: dict[Any, dict[str, Any]], openai: bool) -> dict[str, Any]:
        """An alias as a model entry, borrowing the CPU model's metadata when the CPU backend has it."""
        cpu_model = self.config.aliases[alias].cpu
        if openai:
            base = by_name.get(cpu_model) or {"object": "model", "created": 0, "owned_by": "gpu-broker"}
            return {**base, "id": alias}
        base = by_name.get(cpu_model) or {
            "modified_at": "1970-01-01T00:00:00Z",
            "size": 0,
            "digest": "",
            "details": {},
        }
        return {**base, "name": alias, "model": alias}

    # ---- broker endpoints ------------------------------------------------------------------------
    async def healthz(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def status(self, _request: web.Request) -> web.Response:
        assert self.gate is not None
        return web.json_response(
            {
                "gate": self.gate.status(),
                "routing_to": None if self.gate.last is None else ("gpu" if self.gate.last.available else "cpu"),
                "aliases": {name: {"gpu": a.gpu, "cpu": a.cpu} for name, a in self.config.aliases.items()},
                "buffer": {
                    "max_bytes": self.budget.capacity,
                    "used_bytes": self.budget.used,
                    "waiting": self.budget.waiting,
                },
                "uptime_s": round(time.monotonic() - self.started, 1),
            }
        )


def _log_middleware() -> Callable[..., Any]:
    @web.middleware
    async def middleware(request: web.Request, handler: Callable[..., Any]) -> web.StreamResponse:
        start = time.monotonic()
        info: dict[str, Any] = {"alias": None, "backend": None, "failover": False, "note": None, "ttfb": None}
        request[REQUEST_INFO] = info
        status = 500
        try:
            problem = path_problem(request.raw_path)
            if problem is not None:
                info["note"] = f"rejected {request.raw_path!r}: {problem}"
                resp: web.StreamResponse = broker_error(400, f"request path is not normalized: {problem}")
            else:
                resp = await handler(request)
            status = resp.status
            return resp
        except web.HTTPException as exc:
            status = exc.status
            raise
        except asyncio.CancelledError:
            # aiohttp cancels the handler when the client disconnects, or when shutdown_timeout runs
            # out on SIGTERM; either way the upstream connection is closed.
            info["note"] = "shutdown-cancel" if request.app[BROKER].closing else "client-gone"
            raise
        finally:
            end = time.monotonic()
            level = logging.DEBUG if request.path.startswith("/broker/") else logging.INFO
            log.log(
                level,
                "path=%s method=%s alias=%s backend=%s status=%s latency_ms=%d ttfb_ms=%s failover=%s%s",
                request.path,
                request.method,
                info["alias"] or "-",
                info["backend"] or "-",
                {"client-gone": "499", "shutdown-cancel": "-"}.get(info["note"] or "", status),
                (end - start) * 1000,
                "-" if info["ttfb"] is None else int((info["ttfb"] - start) * 1000),
                "yes" if info["failover"] else "no",
                f" note={info['note']!r}" if info["note"] else "",
            )

    return middleware


async def _mark_broker_responses(_request: web.Request, response: web.StreamResponse) -> None:
    """Anything the broker answers itself (404/405/413/400, /broker/*, aiohttp's own errors)."""
    if "X-Backend" not in response.headers:
        response.headers["X-Backend"] = "broker"


BROKER = web.AppKey("gpu_broker", Broker)


def make_app(config: Config) -> web.Application:
    broker = Broker(config)
    app = web.Application(middlewares=[_log_middleware()], client_max_size=config.max_body_mib * 1024 * 1024)
    app[BROKER] = broker
    app.cleanup_ctx.append(broker.startup)
    app.on_shutdown.append(broker.on_shutdown)
    app.on_response_prepare.append(_mark_broker_responses)
    app.router.add_get("/broker/healthz", broker.healthz)
    app.router.add_get("/broker/status", broker.status)
    app.router.add_route("*", "/{tail:.*}", broker.handle)
    return app
