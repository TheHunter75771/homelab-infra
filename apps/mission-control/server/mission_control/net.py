"""HTTP client side: one aiohttp session (no proxies, a static host map instead of DNS for mapped names),
bounded JSON reads, and failures turned into SourceError with a short, URL-free message.

Error text built here reaches the cards, the briefing and the journal, so it never carries a URL, a header,
a token or a response body: only a fixed description of what failed.
"""

from __future__ import annotations

import asyncio
import json
import math
import socket
from typing import Any

import aiohttp
from aiohttp.abc import AbstractResolver

MAX_JSON = 2 * 1024 * 1024  # the ERAU snapshot is the biggest reply (~0.5 MB on 2026-10-28); capped here


class SourceError(Exception):
    """A source failed. `str(exc)` is safe to show and log. `status` is the upstream HTTP status, if any."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def describe(exc: BaseException, timeout: float | None = None) -> str:
    if isinstance(exc, SourceError):
        return str(exc)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return f"timed out after {timeout:g}s" if timeout else "timed out"
    if isinstance(exc, aiohttp.ClientConnectorCertificateError):
        return "TLS certificate not accepted"
    if isinstance(exc, (aiohttp.ClientConnectorError, ConnectionRefusedError)):
        return "connection refused or unreachable"
    if isinstance(exc, (aiohttp.ClientPayloadError, aiohttp.ServerDisconnectedError, ConnectionError)):
        return "connection closed early"
    if isinstance(exc, aiohttp.ClientError):
        return f"request failed ({type(exc).__name__})"
    return f"failed ({type(exc).__name__})"


def _no_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


def _finite_float(text: str) -> float:
    v = float(text)
    if not math.isfinite(v):
        raise ValueError("non-finite number")
    return v


def strict_json(data: bytes | str) -> Any:
    return json.loads(data, parse_constant=_no_constant, parse_float=_finite_float)


class StaticResolver(AbstractResolver):
    """Mapped host names resolve to their configured IP (ai-box has no MagicDNS, and the unit's IP filter allows
    exactly those addresses). TLS still checks the certificate against the name in the URL. Other names go to
    the system resolver."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = {k.lower().rstrip("."): v for k, v in mapping.items()}
        self.fallback = aiohttp.ThreadedResolver()

    async def resolve(self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET) -> list[Any]:
        ip = self.mapping.get(host.lower().rstrip("."))
        if ip is None:
            return await self.fallback.resolve(host, port, family)
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [{"hostname": host, "host": ip, "port": port, "family": fam, "proto": 0, "flags": socket.AI_NUMERICHOST}]

    async def close(self) -> None:
        await self.fallback.close()


def make_session(host_map: dict[str, str]) -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(limit=16, limit_per_host=4, resolver=StaticResolver(host_map), ttl_dns_cache=60)
    return aiohttp.ClientSession(connector=connector, trust_env=False, auto_decompress=True)


async def _read_capped(resp: aiohttp.ClientResponse, limit: int) -> bytes:
    if resp.content_length is not None and resp.content_length > limit:
        raise SourceError("reply too large")
    body = bytearray()
    async for chunk in resp.content.iter_chunked(65536):
        body += chunk
        if len(body) > limit:
            raise SourceError("reply too large")
    return bytes(body)


async def request_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    timeout: float,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    limit: int = MAX_JSON,
) -> Any:
    """One request with no body and no redirects -> parsed JSON of a 200 reply. Any failure is a SourceError."""
    try:
        async with session.request(
            method,
            url,
            headers=headers,
            params=params,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=False,
        ) as resp:
            if resp.status != 200:
                raise SourceError(f"HTTP {resp.status}", status=resp.status)
            body = await _read_capped(resp, limit)
            try:
                return strict_json(body)
            except (ValueError, RecursionError):
                raise SourceError("invalid JSON reply", status=resp.status) from None
    except asyncio.CancelledError:
        raise
    except SourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - every client failure becomes a safe SourceError
        raise SourceError(describe(exc, timeout)) from None


async def get_json(session: aiohttp.ClientSession, url: str, timeout: float, **kw: Any) -> Any:
    return await request_json(session, "GET", url, timeout, **kw)


async def get_status(session: aiohttp.ClientSession, url: str, timeout: float) -> int:
    """GET url -> its HTTP status (body discarded, at most 64 KiB read)."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=False) as resp:
            await resp.content.read(65536)
            return resp.status
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SourceError(describe(exc, timeout)) from None
