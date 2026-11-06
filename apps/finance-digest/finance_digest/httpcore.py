"""Shared HTTP plumbing for the Money Hub reader and the broker client.

* A hand-built opener: plain HTTP/HTTPS only, no ProxyHandler (finance data never goes through an
  environment proxy), no file/ftp/data handlers, and redirects refused.
* A capped, deadline-bound body read.
* ``with_deadline``: an overall limit on a whole request, which also covers a server that trickles
  its status line or headers (the socket timeout only bounds each single read).

This module never chooses a method or a URL; its callers build the request.
"""
from __future__ import annotations

import ipaddress
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CHUNK = 64 * 1024


class HttpFailure(Exception):
    """A failure whose message is safe to log or show: no URL, no body, no secret."""


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            fp.close()
        except Exception:
            pass
        raise HttpFailure(f"answered with a redirect (HTTP {code}); refused")


def build_opener() -> urllib.request.OpenerDirector:
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        _RefuseRedirects(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


def base_url(raw: object, what: str) -> str:
    """``scheme://host[:port]`` from a configured URL, or HttpFailure. Never echoes the value."""
    bad = HttpFailure(f"{what} is not a plain http(s)://host:port address")
    try:
        parts = urllib.parse.urlsplit(str(raw or "").strip())
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError:
        raise bad from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise bad
    if "@" in parts.netloc or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise bad
    return f"{parts.scheme}://{parts.netloc}"


_DEFAULT_PORTS = {"http": 80, "https": 443}


def endpoint(raw: object, what: str) -> tuple[str, int]:
    """(host key, effective port) of a configured URL, for "is this the same server?" checks.

    Literal-only (no DNS): an IP is normalised (IPv4-mapped IPv6 unwrapped, so ::ffff:127.0.0.1 is
    127.0.0.1), every loopback address and ``localhost`` share one key, a name is lowercased without
    a trailing dot. The port is the explicit one (``:08710`` is 8710) or the scheme's default.
    """
    base = base_url(raw, what)
    parts = urllib.parse.urlsplit(base)
    host = (parts.hostname or "").rstrip(".").lower()
    port = parts.port if parts.port is not None else _DEFAULT_PORTS[parts.scheme]
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        ip = None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if host == "localhost" or host.endswith(".localhost") or (ip is not None and ip.is_loopback):
        return "loopback", port
    if ip is not None and ip.is_unspecified:
        return "loopback", port  # 0.0.0.0 / :: reach this host too
    return (str(ip) if ip is not None else host), port


def same_server(a: object, b: object) -> bool:
    return endpoint(a, "a URL") == endpoint(b, "a URL")


def read_capped(response, deadline: float, max_bytes: int, what: str) -> bytes:
    read = getattr(response, "read1", None) or response.read
    chunks: list[bytes] = []
    size = 0
    while True:
        if time.monotonic() > deadline:
            raise HttpFailure(f"{what} took too long to answer; gave up")
        chunk = read(CHUNK)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > max_bytes:
            raise HttpFailure(f"{what}'s answer is larger than {max_bytes} bytes; refused")
        chunks.append(chunk)


def send(request: urllib.request.Request, *, timeout: float, total: float, max_bytes: int, what: str) -> bytes:
    """Send ``request`` with the safe opener and return the body (HTTP 2xx only).

    ``timeout`` bounds each socket operation; ``total`` bounds the whole request.
    """
    deadline = time.monotonic() + total

    def go() -> bytes:
        try:
            with build_opener().open(request, timeout=timeout) as response:
                return read_capped(response, deadline, max_bytes, what)
        except HttpFailure:
            raise
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
            raise HttpFailure(f"{what} answered HTTP {code}") from None
        except Exception:
            raise HttpFailure(f"{what} is unreachable (connection failed or timed out)") from None

    return with_deadline(go, total + 1, what)


def with_deadline(fn, seconds: float, what: str):
    """Run ``fn`` in a daemon thread; HttpFailure if it has not finished within ``seconds``."""
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - handed back to the caller below
            box["error"] = e

    worker = threading.Thread(target=target, name=f"http-{what}", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise HttpFailure(f"{what} took too long to answer; gave up")
    if "error" in box:
        err = box["error"]
        if isinstance(err, HttpFailure):
            raise err
        raise HttpFailure(f"{what} failed unexpectedly") from None
    return box["value"]
