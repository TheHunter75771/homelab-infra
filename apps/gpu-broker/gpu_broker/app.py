"""The broker: an Ollama-compatible reverse proxy that picks the GPU or CPU Ollama per request."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import io
import ipaddress
import json
import logging
import re
import string
import time
import urllib.parse
from collections.abc import AsyncIterator, Callable, Iterable
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.web_protocol import RequestPayloadError
from aiohttp.web_urldispatcher import _default_expect_handler
from yarl import URL

from .config import Config, IPAddress
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
# Model management: CPU backend only, body passed through untouched (no alias rewrite). Guarded
# (CONTRACTS 12.23): /api/push is refused for everyone; the rest only for the config's management_clients.
PUSH_PATH = "/api/push"
MANAGEMENT_PATHS = frozenset({"/api/pull", "/api/delete", "/api/create", "/api/copy", PUSH_PATH})
# `ollama create` uploads model files here first; they can be gigabytes, so the body is streamed.
BLOBS_PATH = "/api/blobs"
MANAGEMENT_PREFIXES = (BLOBS_PATH + "/",)
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
# Where gpu_num_ctx / cpu_num_ctx apply: Ollama's native generation endpoints, whose body carries
# `options.num_ctx`. Never the embedding endpoints (the embedding model has its own context), and never
# /v1/*: Ollama's OpenAI-compatible layer has no num_ctx field, so a /v1 request always loads the model at
# its default context (the Modelfile's num_ctx, else the server's OLLAMA_CONTEXT_LENGTH or built-in default).
NUM_CTX_PATHS = frozenset({"/api/chat", "/api/generate"})
MIB = 1024 * 1024
LARGE_BODY = MIB  # aiohttp's threshold for "large raw bytes body"
SMALL_BODY = MIB  # bodies up to this size never wait for (or use) the buffer budget (CONTRACTS 12.9)
# Responses that never have a body (RFC 9110 6.4.1): to HEAD, and 1xx/204/304.
NO_BODY_STATUSES = frozenset({204, 304})
UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")
ENCODED_SEPARATOR = re.compile(r"%(2[Ff]|5[Cc])")
# For management_endpoint(): a scheme + authority (absolute-form target), and what ends a path.
_SCHEME_AUTHORITY = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/?#]*")
_PATH_END = re.compile(r"[?#]")
_UNQUOTE_ROUNDS = 4

# Per-request bookkeeping for the log line and /broker/recent (see _log_middleware for the keys).
REQUEST_INFO = web.RequestKey("gpu_broker_info", dict)
# The message of a response the broker generated itself (broker_error), for /broker/recent's `error`.
BROKER_MESSAGE = web.ResponseKey("gpu_broker_message", str)

# /broker/recent (CONTRACTS 12.15.4): the last RECENT_SIZE completed requests, newest first, memory only.
RECENT_SIZE = 200
# Only generation and embedding requests are recorded: the clients' polling (/api/tags, /api/version,
# HEAD /, ...) would otherwise push real work out of the ring. Rejected targets are not recorded either.
RECORDED_PATHS = frozenset(
    {
        "/api/chat",
        "/api/generate",
        "/api/embed",
        "/api/embeddings",
        "/v1/chat/completions",
        "/v1/completions",
        "/v1/embeddings",
    }
)
# Bytes of a response body kept to read the final JSON chunk's token counts. The counts are the last
# fields of Ollama's final chunk (after the `context` array on /api/generate) and `usage` is the last
# field of an OpenAI object, so the tail of the body is enough however long the response is.
FINAL_CHUNK_BYTES = 8 * 1024
# Length caps for the strings a /broker/recent entry holds.
RECENT_PATH_CHARS = 256
RECENT_NAME_CHARS = 128
RECENT_ERROR_CHARS = 200
# Token counts in the final chunk: only a JSON key position matches (`{` or `,` before the key), so the
# same words inside a JSON string (where the quotes are escaped as \") never do.
_EVAL_COUNT = re.compile(rb'[{,]\s*"eval_count"\s*:\s*(\d{1,19})')
_EVAL_DURATION = re.compile(rb'[{,]\s*"eval_duration"\s*:\s*(\d{1,19})')
_COMPLETION_TOKENS = re.compile(rb'[{,]\s*"completion_tokens"\s*:\s*(\d{1,19})')


def _canonical_path(target: str) -> str:
    """The loosest reading of a request target's path that any client, proxy or backend might apply.

    Used only to decide whether a target is model management, never to route: the scheme and authority
    of an absolute-form target, the query and fragment, `;params` and stray whitespace/control
    characters are dropped; percent-encoding is decoded repeatedly (double-encoding too); `\\` counts
    as `/`; case is folded; empty and dot segments are resolved; a trailing slash is ignored.
    """
    path = _SCHEME_AUTHORITY.sub("", target)
    path = _PATH_END.split(path, 1)[0]
    for _ in range(_UNQUOTE_ROUNDS):
        decoded = _PATH_END.split(urllib.parse.unquote(path, errors="replace"), 1)[0]
        if decoded == path:
            break
        path = decoded
    segments: list[str] = []
    for segment in path.replace("\\", "/").lower().split("/"):
        segment = "".join(c for c in segment.split(";", 1)[0] if c.isprintable() and not c.isspace())
        if segment in ("", "."):
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return "/" + "/".join(segments)


def management_endpoint(*targets: str) -> str | None:
    """The model-management endpoint any of these targets could reach (`/api/pull`, ..., `/api/blobs`), or None.

    Deliberately broader than the routing in Broker._dispatch: `/API/Push/`, `//api/push`, `/api/pus%68`,
    `/api%2Fpush`, `/v1/../api/pull` and `/api/blobs` all count, so a refused client is refused (403)
    whatever spelling it tries, before any other check can answer differently.
    """
    for target in targets:
        path = _canonical_path(target)
        if path in MANAGEMENT_PATHS:
            return path
        if path == BLOBS_PATH or path.startswith(MANAGEMENT_PREFIXES):
            return BLOBS_PATH
    return None


def peer_ip(request: web.BaseRequest) -> IPAddress | None:
    """The TCP peer's address: the connection's own socket peername, never a header (X-Forwarded-For,
    X-Real-IP, Forwarded) and never request.remote, which `clone(remote=...)` can override.

    An IPv4-mapped IPv6 peer (a dual-stack listener) is reported as IPv4. A scoped IPv6 peer
    (`fe80::1%eth0`) keeps its scope, so it never equals a configured (unscoped) address.
    """
    peername = request.protocol.peername
    if not isinstance(peername, (tuple, list)) or not peername or not isinstance(peername[0], str):
        return None  # closed transport, or a Unix socket
    try:
        addr = ipaddress.ip_address(peername[0])
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def management_refusal(request: web.BaseRequest, config: Config, extra_targets: Iterable[str] = ()) -> str | None:
    """Why this request must be refused as model management (CONTRACTS 12.23), or None if it may go on.

    Not management: None. `/api/push` (any spelling, any method, any client): refused. Any other
    management endpoint: allowed only when the TCP peer is in `management_clients` (default: nobody).
    """
    endpoint = management_endpoint(request.path, request.raw_path, *extra_targets)
    if endpoint is None:
        return None
    if endpoint == PUSH_PATH:
        return "/api/push is disabled"
    peer = peer_ip(request)
    if peer is None or peer not in config.management_clients:
        return f"model management ({endpoint}) is not allowed from this client"
    return None


def log_safe(value: object) -> str:
    """Client-supplied text (path, method, ...) made safe for one log line.

    Every character str.isprintable() rejects becomes an escape: the C0 controls (CR, LF, TAB, ...), DEL,
    the C1 controls (NEL U+0085 included), U+2028/U+2029 and the other separator and format characters. A
    backslash is doubled, so an escape in the log can't be faked by sending its text. One request is
    therefore always one log line, whatever bytes it carried (`%0A`, `%0D`, `%C2%85`, `%E2%80%A8`, ...).
    """
    text = str(value)
    if text.isprintable() and "\\" not in text:
        return text
    out = []
    for c in text:
        code = ord(c)
        if c == "\\":
            out.append("\\\\")
        elif c.isprintable():
            out.append(c)
        elif code < 0x100:
            out.append(f"\\x{code:02x}")
        elif code < 0x10000:
            out.append(f"\\u{code:04x}")
        else:
            out.append(f"\\U{code:08x}")
    return "".join(out)


class UpstreamFailure(Exception):
    """The backend failed before the broker sent any response byte to the client.

    `reason` is the short, URL-free form for /broker/recent: "HTTP 503" or the exception's type name.
    """

    def __init__(self, message: str, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason or message


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

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def refuse(request: web.BaseRequest, reason: str) -> web.Response:
    """403 for a refused management request. The body is never read; the connection is not reused."""
    info = request.get(REQUEST_INFO)
    if info is not None:
        info["refused"] = reason
    resp = broker_error(403, reason)
    if request.can_read_body:
        resp.force_close()
    return resp


def broker_error(status: int, message: str) -> web.Response:
    """A response the broker generates itself (X-Backend: broker, Ollama-style error body)."""
    resp = web.json_response({"error": f"gpu-broker: {message}"}, status=status)
    resp.headers["X-Backend"] = "broker"
    resp[BROKER_MESSAGE] = message
    return resp


class FinalChunk:
    """The tail of a response body, kept to read the token counts of its final JSON chunk.

    Never holds more than `limit` bytes, however large the body or any one line of it. Only the counts
    are read out; the bytes themselves (response text) go nowhere and are dropped with the object.
    """

    def __init__(self, limit: int = FINAL_CHUNK_BYTES) -> None:
        self.limit = limit
        self.tail = bytearray()
        self.peak = 0  # the most bytes ever held (the tests check it against limit)

    def feed(self, data: bytes) -> None:
        if len(data) >= self.limit:
            self.tail[:] = data[-self.limit :]
        else:
            excess = len(self.tail) + len(data) - self.limit
            if excess > 0:
                del self.tail[:excess]  # trim first, so the buffer never grows past limit
            self.tail += data
        self.peak = max(self.peak, len(self.tail))

    def final_line(self) -> bytes:
        """The last line that is not blank or an SSE `data: [DONE]` (possibly cut at its start)."""
        for line in reversed(bytes(self.tail).split(b"\n")):
            line = line.strip()
            if line and line != b"data: [DONE]":
                return line
        return b""

    def counts(self) -> tuple[int | None, int | None]:
        """(eval_count, eval_duration in ns) from the final chunk; None for whatever it lacks.

        Ollama: `eval_count` + `eval_duration`. OpenAI-compatible: `usage.completion_tokens`, no duration.
        """
        line = self.final_line()
        count = _last_int(_EVAL_COUNT, line)
        if count is None:
            return _last_int(_COMPLETION_TOKENS, line), None
        return count, _last_int(_EVAL_DURATION, line)


def _last_int(pattern: re.Pattern[bytes], line: bytes) -> int | None:
    found = pattern.findall(line)
    return int(found[-1]) if found else None


class RecentLog:
    """The newest-first ring of the last `size` completed requests (metadata only, memory only)."""

    def __init__(self, size: int = RECENT_SIZE) -> None:
        self._ring: collections.deque[dict[str, Any]] = collections.deque(maxlen=size)

    def __len__(self) -> int:
        return len(self._ring)

    def add(self, entry: dict[str, Any]) -> None:
        self._ring.appendleft(entry)

    def newest_first(self) -> list[dict[str, Any]]:
        return list(self._ring)


def _clip(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value if len(value) <= limit else value[: limit - 1] + "…"


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


def _has_body(method: str, resp: aiohttp.ClientResponse) -> bool:
    """Whether the backend's response can carry a body the relay should read.

    Load-bearing: aiohttp gives every bodiless response one process-wide reader, `aiohttp.EMPTY_PAYLOAD`
    (an EmptyStreamReader). Its readchunk() reports the end of the stream only to the first reader in
    the process, then returns (b"", True) forever without awaiting anything, so iter_chunks() never
    ends. Iterating it spun the event loop at 100% CPU: the second HEAD (or 204/304) relayed since start
    hung the whole broker. Such a response is never read, only released. The EMPTY_PAYLOAD check is the
    backstop for any other case aiohttp hands it out (the pure-Python parser does for 200 +
    Content-Length: 0); relay() also stops on an empty chunk at EOF, whatever iter_chunks() says.
    """
    if method == "HEAD" or resp.status in NO_BODY_STATUSES or 100 <= resp.status < 200:
        return False
    return resp.content is not aiohttp.EMPTY_PAYLOAD


async def _next_chunk(chunks: Any) -> bytes | None:
    """The next body chunk from the backend, None at the end; backend errors become _UpstreamBroke."""
    try:
        data, _ = await chunks.__anext__()
    except StopAsyncIteration:
        return None
    except (aiohttp.ClientError, OSError, TimeoutError) as exc:
        raise _UpstreamBroke(_describe(exc), type(exc).__name__) from exc
    return bytes(data)


class JsonBody:
    """The request body, with the model rewritten per backend when it names an alias.

    On /api/chat and /api/generate an alias request also gets the backend's fixed context, when the config
    sets one (gpu_num_ctx / cpu_num_ctx): `options.num_ctx` is overwritten, and `options` created if missing.
    """

    def __init__(self, raw: bytes, config: Config, path: str) -> None:
        self.raw = raw
        self.alias: str | None = None
        self.requested: str | None = None  # the model name the client asked for (alias or not)
        self._doc: dict[str, Any] | None = None
        self._config = config
        self._path = path
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
        if self.alias is not None:
            self.requested = self.alias
        else:
            self.requested = next((v for k in MODEL_FIELDS if isinstance(v := doc.get(k), str)), None)

    def model_for(self, backend: str) -> str | None:
        """The model name the given backend is asked for."""
        if self.alias is not None:
            return self._config.aliases[self.alias].for_backend(backend)
        return self.requested

    def num_ctx_for(self, backend: str) -> int | None:
        """The num_ctx forced on this request for `backend`, or None when the body's own value is sent.

        Forced only for an alias in `model` (the field chat and generate read) on NUM_CTX_PATHS, when the
        backend has a fixed context. A non-object `options` is left alone: Ollama refuses that body (400)
        before it loads anything, and the broker never turns a broken request into a working one.
        """
        forced = self._config.num_ctx_for(backend)
        if forced is None or self._doc is None or self._path not in NUM_CTX_PATHS:
            return None
        if self._doc.get("model") != self.alias:
            return None
        options = self._doc.get("options")
        if options is not None and not isinstance(options, dict):
            return None
        return forced

    def num_ctx_change(self, backend: str) -> dict[str, int | None] | None:
        """For /broker/recent: {"requested": the client's integer num_ctx or None, "sent": forced}, or None."""
        sent = self.num_ctx_for(backend)
        if sent is None or self._doc is None:
            return None
        options = self._doc.get("options")
        value = options.get("num_ctx") if isinstance(options, dict) else None
        requested = value if isinstance(value, int) and not isinstance(value, bool) else None
        return {"requested": requested, "sent": sent}

    def for_backend(self, backend: str) -> bytes:
        if self._doc is None or self.alias is None:
            return self.raw
        doc = dict(self._doc)
        model = self._config.aliases[self.alias].for_backend(backend)
        for key in MODEL_FIELDS:
            if doc.get(key) == self.alias:
                doc[key] = model
        forced = self.num_ctx_for(backend)
        if forced is not None:
            # A new dict: the parsed body stays as the client sent it for the other backend's attempt.
            doc["options"] = {**(doc.get("options") or {}), "num_ctx": forced}
        # ASCII output: any string json.loads accepted (even a lone surrogate from "\ud800") re-encodes.
        return json.dumps(doc, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _num_ctx_change(request: web.Request, backend: str, body: JsonBody) -> dict[str, int | None] | None:
    """The body's num_ctx change for `backend` (see JsonBody.num_ctx_change), logged at DEBUG. Numbers only."""
    change = body.num_ctx_change(backend)
    if change is not None:
        requested, sent = change["requested"], change["sent"]
        assert sent is not None
        log.debug(
            "num_ctx for %s on %s: %s -> %d%s",
            backend,
            log_safe(request.path),
            "-" if requested is None else requested,
            sent,
            # Same as any prompt over the context today: Ollama truncates it. Not worth more than DEBUG.
            f" (smaller than asked: a prompt over {sent} tokens is truncated)" if (requested or 0) > sent else "",
        )
    return change


class Broker:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.started = time.monotonic()
        self.proxy_session: aiohttp.ClientSession | None = None
        self.api_session: aiohttp.ClientSession | None = None
        self.gate: GateClient | None = None
        self.budget = ByteBudget(config.max_buffered_bytes)
        self.recent = RecentLog()
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
            # HEAD is answered like GET; aiohttp sends the headers (with Content-Length) and no body.
            if request.method not in ("GET", "HEAD"):
                return broker_error(405, f"{request.method} not allowed on {path}")
            return await self.merged_models(request)
        if (
            path.startswith(OPENAI_MODEL_PREFIX)
            and request.method in ("GET", "HEAD")
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
        log.warning(
            "unreadable request body on %s from %s: %s", log_safe(request.path), request.remote, log_safe(detail)
        )
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
            body = JsonBody(raw, self.config, request.path)
            info["alias"] = body.alias
            info["requested"] = body.requested
            backend = await self.choose_backend()
            if backend == "gpu":
                info["attempt"], info["model"] = "gpu", body.model_for("gpu")
                info["num_ctx"] = _num_ctx_change(request, "gpu", body)
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
                    log.warning(
                        "gpu failed before first byte on %s (%s): retrying once on cpu", log_safe(request.path), exc
                    )
                    info["failover"] = True
                    info["failover_reason"] = exc.reason
                    backend = "cpu"
            if backend == "cpu":
                info["attempt"], info["model"] = "cpu", body.model_for("cpu")
                info["num_ctx"] = _num_ctx_change(request, "cpu", body)
                try:
                    opened = await self.open_upstream(
                        request, "cpu", headers, body.for_backend("cpu"), first_byte_timeout=None, fail_on_status=False
                    )
                except UpstreamFailure as exc:
                    if self.closing:
                        return self.shutting_down("cpu", exc)
                    info["backend"] = "cpu"
                    info["error"] = f"cpu unavailable: {exc.reason}"
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
        request[REQUEST_INFO]["attempt"] = "cpu"
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
            info = request[REQUEST_INFO]
            info["backend"] = "cpu"
            info["error"] = f"cpu unavailable: {exc.reason}"
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

        Returns (response, chunk iterator, first chunk or None for an empty body). A response that
        cannot have a body (see _has_body) is not read at all: (response, None, None). Anything that
        goes wrong before then (connect error, reset, timeout, a failover status when
        fail_on_status) raises UpstreamFailure; nothing has been sent to the client yet.
        """
        session = self.proxy_session
        if session is None or session.closed:
            raise UpstreamFailure("broker is shutting down", "broker shutting down")
        base = URL(self.base_url(backend), encoded=True)
        url = URL(self.base_url(backend) + request.raw_path, encoded=True)
        # The last line of defence (the middleware refuses first): whatever routing decided, a request
        # whose forwarded path (below the backend's base path) could reach model management goes out
        # only if the guard allows it.
        sent = (url.raw_path[len(base.raw_path.rstrip("/")) :], url.path[len(base.path.rstrip("/")) :])
        refusal = management_refusal(request, self.config, sent)
        if refusal is not None:
            raise BrokerReply(refuse(request, refusal))
        if isinstance(data, bytes) and len(data) > LARGE_BODY:
            data = io.BytesIO(data)  # sent in chunks instead of one write that blocks the loop
        resp: aiohttp.ClientResponse | None = None
        ok = False
        try:
            async with asyncio.timeout(first_byte_timeout):
                resp = await session.request(request.method, url, headers=headers, data=data, allow_redirects=False)
                if fail_on_status and resp.status in self.config.failover_statuses:
                    raise UpstreamFailure(f"HTTP {resp.status}")
                chunks: Any = None
                first: bytes | None = None
                if _has_body(request.method, resp):
                    chunks = resp.content.iter_chunks().__aiter__()
                    with contextlib.suppress(StopAsyncIteration):
                        first = (await chunks.__anext__())[0]
            ok = True
            return resp, chunks, first
        except (aiohttp.ClientError, OSError, TimeoutError, RuntimeError) as exc:
            # RuntimeError: the session was closed under us (shutdown) - a failure, not a crash.
            raise UpstreamFailure(_describe(exc), type(exc).__name__) from exc
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
        """Stream the backend response to the client chunk by chunk (never buffered whole).

        Each chunk is also fed, after it is written, to a bounded FinalChunk, which reads the final
        chunk's token counts for /broker/recent.
        """
        info = request[REQUEST_INFO]
        final = FinalChunk()
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
                    final.feed(data)  # after the write: the tracker never delays a chunk
                elif resp.content.at_eof():
                    break  # an empty chunk at EOF ends the body: never spin on a reader that won't block
                data = await _next_chunk(chunks)
            await out.write_eof()
            clean = True
        except _UpstreamBroke as exc:
            # The backend broke mid-stream. Bytes are already out, so no failover: cut the client
            # connection so it sees a truncated response, not a clean end.
            info["note"] = f"upstream-broke: {exc}"
            info["error"] = f"{backend} broke mid-stream: {exc.reason}"
            log.warning("%s backend broke mid-stream on %s: %s", backend, log_safe(request.path), exc)
            if request.transport is not None:
                request.transport.close()
        except (ConnectionResetError, aiohttp.ClientConnectionResetError):
            info["note"] = "client-gone"  # closing the upstream below makes Ollama stop generating
        finally:
            if clean:
                resp.release()
            else:
                resp.close()
            info["eval_count"], info["eval_duration"] = final.counts()
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
            log.warning("%s: %s", log_safe(request.path), warning)
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
                "num_ctx": {"gpu": self.config.gpu_num_ctx, "cpu": self.config.cpu_num_ctx},
                "buffer": {
                    "max_bytes": self.budget.capacity,
                    "used_bytes": self.budget.used,
                    "waiting": self.budget.waiting,
                },
                "uptime_s": round(time.monotonic() - self.started, 1),
            }
        )

    async def recent_requests(self, _request: web.Request) -> web.Response:
        """GET /broker/recent: the last RECENT_SIZE completed requests, newest first (CONTRACTS 12.15.4)."""
        return web.json_response(self.recent.newest_first())


def _recent_entry(
    request: web.Request,
    info: dict[str, Any],
    status: int | None,
    resp: web.StreamResponse | None,
    duration_s: float,
) -> dict[str, Any]:
    """One /broker/recent entry. Metadata only: never a body, a query string or a backend URL."""
    note = info["note"] or ""
    if note == "client-gone":
        status = 499
    elif note == "shutdown-cancel":
        status = None
    backend = (resp.headers.get("X-Backend") if resp is not None else None) or info["backend"] or info["attempt"]
    errors = []
    if info["failover"]:
        errors.append(f"gpu failed before first byte: {info['failover_reason']}")
    if info["error"]:
        errors.append(info["error"])
    elif note == "client-gone":
        errors.append("client disconnected")
    elif note == "shutdown-cancel":
        errors.append("shutting down")
    elif resp is not None and BROKER_MESSAGE in resp:
        errors.append(resp[BROKER_MESSAGE])
    elif status is not None and status >= 400:
        errors.append(f"HTTP {status}" + (f" from {backend}" if backend in ("gpu", "cpu") else ""))
    count, duration_ns = info["eval_count"], info["eval_duration"]
    return {
        "ts": round(time.time(), 3),
        "path": _clip(request.path, RECENT_PATH_CHARS),
        "alias": _clip(info["alias"], RECENT_NAME_CHARS),
        "model": _clip(info["model"], RECENT_NAME_CHARS),
        "backend": backend,
        "status": status,
        "duration_ms": int(duration_s * 1000),
        "eval_count": count,
        "eval_tok_s": round(count * 1e9 / duration_ns, 2) if count is not None and duration_ns else None,
        "error": _clip("; ".join(errors), RECENT_ERROR_CHARS) if errors else None,
        "num_ctx": info["num_ctx"],
    }


def _log_middleware() -> Callable[..., Any]:
    @web.middleware
    async def middleware(request: web.Request, handler: Callable[..., Any]) -> web.StreamResponse:
        start = time.monotonic()
        info: dict[str, Any] = {
            "alias": None,  # the alias the body named, None for a non-alias model
            "requested": None,  # the model the body named, alias or not
            "model": None,  # what `requested` was mapped to for the backend tried last
            "attempt": None,  # the backend tried last, even if it never answered
            "backend": None,  # the backend that answered
            "failover": False,
            "failover_reason": None,  # short, URL-free: "HTTP 503", "ClientConnectorError"
            "error": None,  # short, URL-free reason for /broker/recent
            "eval_count": None,
            "eval_duration": None,
            "note": None,
            "ttfb": None,
            "refused": None,  # why a management request was refused (CONTRACTS 12.23)
            "num_ctx": None,  # {"requested", "sent"} when the backend tried last got a forced num_ctx
        }
        request[REQUEST_INFO] = info
        status = 500
        resp: web.StreamResponse | None = None
        problem: str | None = None
        try:
            # Management is decided first, on the TCP peer and every reading of the target, so no
            # spelling of a management path from a refused client gets any other answer than 403.
            refusal = management_refusal(request, request.app[BROKER].config)
            if refusal is not None:
                resp = refuse(request, refusal)
            elif (problem := path_problem(request.raw_path)) is not None:
                info["note"] = f"rejected {request.raw_path!r}: {problem}"
                resp = broker_error(400, f"request path is not normalized: {problem}")
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
            if info["refused"]:
                # The client's address goes to the log only, never to the response or /broker/recent.
                info["note"] = f"refused: {info['refused']} (peer {peer_ip(request)})"
                level = logging.WARNING
            log.log(
                level,
                "path=%s method=%s alias=%s backend=%s status=%s latency_ms=%d ttfb_ms=%s failover=%s%s",
                log_safe(request.path),
                log_safe(request.method),
                log_safe(info["alias"] or "-"),
                info["backend"] or "-",
                {"client-gone": "499", "shutdown-cancel": "-"}.get(info["note"] or "", status),
                (end - start) * 1000,
                "-" if info["ttfb"] is None else int((info["ttfb"] - start) * 1000),
                "yes" if info["failover"] else "no",
                f" note={info['note']!r}" if info["note"] else "",
            )
            if info["refused"] or (problem is None and request.path in RECORDED_PATHS):
                try:
                    entry = _recent_entry(request, info, status, resp, end - start)
                except Exception:  # bookkeeping must never fail a request
                    log.exception("could not record %s in /broker/recent", log_safe(request.path))
                else:
                    request.app[BROKER].recent.add(entry)

    return middleware


async def _mark_broker_responses(_request: web.Request, response: web.StreamResponse) -> None:
    """Anything the broker answers itself (404/405/413/400, /broker/*, aiohttp's own errors)."""
    if "X-Backend" not in response.headers:
        response.headers["X-Backend"] = "broker"


BROKER = web.AppKey("gpu_broker", Broker)


async def _expect_handler(request: web.Request) -> None:
    """`Expect: 100-continue`: never invite the body of a request the guard will refuse (a blob upload
    can be gigabytes). The middleware then answers it with the 403; everything else is aiohttp's default.
    """
    if management_refusal(request, request.app[BROKER].config) is None:
        await _default_expect_handler(request)


def make_app(config: Config) -> web.Application:
    broker = Broker(config)
    app = web.Application(middlewares=[_log_middleware()], client_max_size=config.max_body_mib * 1024 * 1024)
    app[BROKER] = broker
    app.cleanup_ctx.append(broker.startup)
    app.on_shutdown.append(broker.on_shutdown)
    app.on_response_prepare.append(_mark_broker_responses)
    app.router.add_get("/broker/healthz", broker.healthz, expect_handler=_expect_handler)
    app.router.add_get("/broker/status", broker.status, expect_handler=_expect_handler)
    app.router.add_get("/broker/recent", broker.recent_requests, expect_handler=_expect_handler)
    app.router.add_route("*", "/{tail:.*}", broker.handle, expect_handler=_expect_handler)
    return app
