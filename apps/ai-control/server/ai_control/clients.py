"""Clients for the data sources: plain HTTP JSON (gate, broker, Open WebUI), helper X over its Unix socket,
and host agent Y with a bearer token.

Error messages built here go to the browser (as a section's `error`) and to the log, so they never carry
URLs, addresses, headers or the agent token: only a short, fixed description of what failed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any

import aiohttp

log = logging.getLogger("ai_control.clients")

HELPER_MAX_REQUEST = 4096
HELPER_MAX_REPLY = 8 * 1024 * 1024  # logs: 1000 lines x 2000 chars, JSON-escaped, fits
MAX_HTTP_JSON = 8 * 1024 * 1024


class SourceError(Exception):
    """A source failed. `str(exc)` is safe to show (no URLs, no secrets). `status` is the upstream HTTP
    status when there was one; `code` is the helper's error code when it refused. `kind` says what went
    wrong: "http" (a non-OK status), "shape" (an unusable body) or "network" (no usable answer at all)."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None, kind: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.kind = kind or ("http" if status is not None else "network")


def describe(exc: BaseException, timeout: float | None = None) -> str:
    """A short, URL-free description of a client-side failure."""
    if isinstance(exc, SourceError):
        return str(exc)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return f"timed out after {timeout:g}s" if timeout else "timed out"
    if isinstance(exc, aiohttp.ClientConnectorError | ConnectionRefusedError):
        return "connection refused or unreachable"
    if isinstance(exc, FileNotFoundError):
        return "socket not found"
    if isinstance(exc, PermissionError):
        return "permission denied"
    if isinstance(exc, aiohttp.ClientPayloadError | aiohttp.ServerDisconnectedError | ConnectionError):
        return "connection closed early"
    if isinstance(exc, json.JSONDecodeError | aiohttp.ContentTypeError | UnicodeDecodeError):
        return "invalid JSON reply"
    if isinstance(exc, aiohttp.ClientError):
        return f"request failed ({type(exc).__name__})"
    return f"failed ({type(exc).__name__})"


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):  # 1e400 parses to inf without ever reaching parse_constant
        raise ValueError("non-finite number")
    return value


def strict_json(data: bytes | str) -> Any:
    """json.loads without NaN/Infinity (as constants or as overflowing literals). Raises ValueError (incl.
    JSONDecodeError/UnicodeDecodeError) or RecursionError on bad input."""
    return json.loads(data, parse_constant=_reject_constant, parse_float=_finite_float)


def parse_json(data: bytes | str, what: str) -> Any:
    try:
        return strict_json(data)
    except (ValueError, RecursionError):
        raise SourceError(f"{what}invalid JSON reply", kind="shape") from None


async def _read_json(resp: aiohttp.ClientResponse) -> Any:
    """The whole body (read until EOF, capped), parsed strictly. `content.read(n)` alone returns only what
    is already buffered, so a body over ~64 KiB would arrive cut short."""
    if resp.content_length is not None and resp.content_length > MAX_HTTP_JSON:
        raise SourceError("reply too large", kind="shape")
    body = bytearray()
    async for chunk in resp.content.iter_chunked(65536):
        body += chunk
        if len(body) > MAX_HTTP_JSON:
            raise SourceError("reply too large", kind="shape")
    return parse_json(bytes(body), "")


async def get_json(
    session: aiohttp.ClientSession,
    url: str,
    timeout: float,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    want_status: tuple[int, ...] = (200,),
) -> tuple[int, Any, dict[str, str]]:
    """GET url → (status, parsed JSON, selected response headers). Any failure raises SourceError."""
    try:
        async with session.get(
            url,
            headers=headers,
            params=params,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=False,
        ) as resp:
            if resp.status not in want_status:
                raise SourceError(f"HTTP {resp.status}", status=resp.status)
            try:
                data = await _read_json(resp)
            except SourceError as exc:
                raise SourceError(str(exc), status=resp.status, kind=exc.kind) from None
            picked = {k: resp.headers[k] for k in ("X-Backend", "X-Broker-Warning") if k in resp.headers}
            return resp.status, data, picked
    except asyncio.CancelledError:
        raise
    except SourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - every client failure becomes a safe SourceError
        raise SourceError(describe(exc, timeout)) from None


async def get_status(session: aiohttp.ClientSession, url: str, timeout: float) -> int:
    """GET url → its HTTP status (body discarded). Failure to connect raises SourceError."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=False) as resp:
            await resp.content.read(65536)
            return resp.status
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SourceError(describe(exc, timeout)) from None


class HelperClient:
    """Helper X: one JSON line in, one JSON line out, one request per connection (§12.15.2 / §11.2)."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path

    async def call(self, op: str, timeout: float, **args: Any) -> Any:
        request = json.dumps({"op": op, **args}, separators=(",", ":")).encode() + b"\n"
        if len(request) > HELPER_MAX_REQUEST:
            raise SourceError("helper request too large")
        try:
            return await asyncio.wait_for(self._roundtrip(request), timeout)
        except asyncio.CancelledError:
            raise
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SourceError(describe(exc, timeout)) from None

    async def _roundtrip(self, request: bytes) -> Any:
        reader, writer = await asyncio.open_unix_connection(self.socket_path, limit=HELPER_MAX_REPLY + 1)
        try:
            writer.write(request)
            await writer.drain()
            # No half-close: the helper reads one line, answers one line, and closes.
            try:
                line = await reader.readuntil(b"\n")
            except asyncio.IncompleteReadError as exc:
                line = exc.partial
                if not line:
                    raise SourceError("helper closed the connection without a reply") from None
            except asyncio.LimitOverrunError:
                raise SourceError("helper reply too large") from None
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        try:
            reply = strict_json(line)
        except (ValueError, RecursionError):
            raise SourceError("helper sent invalid JSON") from None
        if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
            raise SourceError("helper reply has no boolean 'ok'")
        if not reply["ok"]:
            err = reply.get("error") if isinstance(reply.get("error"), dict) else {}
            code = str(err.get("code") or "error")[:64]
            message = str(err.get("message") or "refused")[:300]
            raise SourceError(f"helper: {code}: {message}", code=code)
        return reply.get("data")


def pinned_list(data: Any, key: str, op: str) -> list[dict[str, Any]]:
    """Helper X's pinned shape (apps/ai-control/helper/README.md): an object whose `key` is a list of
    objects. Anything else is a SourceError, so the caller's section (or endpoint) reports it."""
    if not isinstance(data, dict) or not isinstance(data.get(key), list) or not all(isinstance(x, dict) for x in data[key]):
        raise SourceError(f"helper {op} reply doesn't match its pinned shape")
    return data[key]


class AgentClient:
    """Host agent Y on the gaming PC. The bearer token is read from a file on every call (so a rotated
    token is picked up) and never logged or returned."""

    def __init__(self, base_url: str, token_file: str) -> None:
        self.base_url = base_url
        self.token_file = token_file

    def _token(self) -> str:
        try:
            with open(self.token_file, encoding="utf-8") as fh:
                token = fh.read(4096).strip()
        except OSError:
            raise SourceError("agent token file unreadable") from None
        if not token or any(c.isspace() for c in token):
            raise SourceError("agent token file is empty or malformed")
        return token

    async def get(self, session: aiohttp.ClientSession, path: str, timeout: float, params: dict[str, str] | None = None) -> Any:
        token = self._token()
        try:
            _status, data, _ = await get_json(
                session, f"{self.base_url}{path}", timeout, headers={"Authorization": f"Bearer {token}"}, params=params
            )
        except SourceError as exc:
            if exc.status == 401:
                raise SourceError("agent rejected the token (401)", status=401) from None
            # Belt and braces: never let the token ride out in a message.
            raise SourceError(str(exc).replace(token, "[redacted]"), status=exc.status) from None
        return data
