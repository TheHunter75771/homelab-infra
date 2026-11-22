"""HTTP for the gate: the SSRF-safe page fetcher and the SearXNG client (CONTRACTS §12.16.3).

Both use ``http.client`` directly, so nothing reads proxy settings from the environment, keeps
cookies or sends credentials. Every exchange has one total deadline (a watchdog shuts the socket
down when it passes, so a server that trickles bytes cannot hold the gate), a byte cap enforced
while reading (on the wire and after gzip/deflate decoding, so a compression bomb stops at the
cap), and a fixed User-Agent.

The page fetcher:

* takes only URLs that ``forge.checklist.canonical_url`` accepts (http/https, no user info);
* resolves the host **once** per hop through an injectable resolver, refuses the hop if any
  returned address is not public (``netguard``), and connects to that address itself, sending
  ``Host`` (and, for https, SNI plus certificate checks) for the *name*;
* follows a redirect only if ``may_follow(target)`` says the target is itself ticked, re-resolving
  it, at most ``max_redirects`` times.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
import urllib.parse
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field

from forge.checklist import canonical_url

from .netguard import AddressPolicy, AddressRefused, Resolver, TablesUnreadable, system_resolver

NOT_COUNTED = "not counted: "  # an error prefix the ledger does not count as an attempt

REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
CHUNK = 64 * 1024


class FetchError(Exception):
    """A fetch failed. ``terminal`` means retrying cannot help (a refusal, not a hiccup)."""

    def __init__(self, message: str, *, terminal: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.terminal = terminal
        self.status = status


def refused(message: str, status: int | None = None) -> FetchError:
    return FetchError(message, terminal=True, status=status)


def transient(message: str, status: int | None = None) -> FetchError:
    return FetchError(message, terminal=False, status=status)


@dataclass
class Response:
    url: str                      # the URL that answered (after redirects)
    status: int
    content_type: str             # lowercased media type, no parameters
    charset: str | None
    body: bytes                   # decoded (gzip/deflate removed)
    wire_bytes: int
    redirects: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------- connections

class _PinnedHTTP(http.client.HTTPConnection):
    """Connects to a fixed address; ``Host`` still names the original host."""

    def __init__(self, host: str, port: int, addr: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._addr = addr
        self.raw_sock: socket.socket | None = None

    def connect(self) -> None:
        self.sock = socket.create_connection((self._addr, self.port), self.timeout)
        self.raw_sock = self.sock
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


class _PinnedHTTPS(_PinnedHTTP):
    """TLS to a fixed address, with SNI and certificate checks for the host *name*."""

    default_port = 443

    def __init__(self, host: str, port: int, addr: str, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host, port, addr, timeout)
        self._context = context

    def connect(self) -> None:
        super().connect()
        assert self.sock is not None
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        self.raw_sock = self.sock  # wrap_socket detached the plain socket; the watchdog needs this one


def _default_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


# ---------------------------------------------------------------------- body decoding

class _Decoder:
    """gzip / deflate / identity, refusing output past ``limit`` bytes (bomb-safe)."""

    def __init__(self, encoding: str, limit: int) -> None:
        self.limit = limit
        self.total = 0
        self.encoding = encoding
        self._first = True
        if encoding in ("gzip", "x-gzip"):
            self._z = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            self._z = zlib.decompressobj(zlib.MAX_WBITS)
        elif encoding == "identity":
            self._z = None
        else:
            raise refused("the content encoding is not supported (only gzip, deflate, identity)")

    def _take(self, piece: bytes) -> bytes:
        self.total += len(piece)
        if self.total > self.limit:
            raise refused(f"larger than the {self.limit}-byte cap after decoding")
        return piece

    def feed(self, data: bytes) -> bytes:
        if self._z is None:
            return self._take(data)
        try:
            return self._inflate(data)
        except zlib.error:
            if self.encoding == "deflate" and self._first and self.total == 0:
                # Some servers send raw deflate (no zlib header): retry this first chunk raw.
                self._z = zlib.decompressobj(-zlib.MAX_WBITS)
                self._first = False
                try:
                    return self._inflate(data)
                except zlib.error as exc:
                    raise refused(f"bad deflate data ({exc})") from None
            raise refused(f"bad {self.encoding} data") from None
        finally:
            self._first = False

    def _inflate(self, data: bytes) -> bytes:
        out = bytearray()
        buf = data
        while buf:
            if self._z.eof:  # trailing bytes after the stream: ignored, never decoded
                break
            piece = self._z.decompress(buf, self.limit - self.total + 1)
            out += self._take(piece)
            buf = self._z.unconsumed_tail
        return bytes(out)

    def finish(self) -> bytes:
        if self._z is None:
            return b""
        try:
            tail = self._take(self._z.flush(self.limit - self.total + 1))
        except zlib.error as exc:
            raise refused(f"bad {self.encoding} data ({exc})") from None
        if not self._z.eof:
            raise transient(f"the {self.encoding} body was cut short")
        return tail


# ---------------------------------------------------------------------- one exchange

class _Watchdog:
    """Shuts the connection's socket down at the deadline, waking any blocked read."""

    def __init__(self, conn: _PinnedHTTP, deadline: float) -> None:
        self.conn = conn
        self.fired = False
        self._timer = threading.Timer(max(0.0, deadline - time.monotonic()), self._fire)
        self._timer.daemon = True

    def _fire(self) -> None:
        self.fired = True
        sock = self.conn.raw_sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def __enter__(self) -> _Watchdog:
        self._timer.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._timer.cancel()


def _describe(exc: BaseException) -> str:
    """A local description of a failure that never repeats words a server sent (an
    HTTPException's text is the server's status line or headers)."""
    if isinstance(exc, ssl.SSLError):
        return f"TLS error {exc.reason or type(exc).__name__}"
    if isinstance(exc, http.client.HTTPException):
        return f"bad HTTP response ({type(exc).__name__})"
    if isinstance(exc, OSError) and exc.strerror:
        return f"{type(exc).__name__}: {exc.strerror}"
    return type(exc).__name__


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise transient("the total time limit passed")
    return left


_MEDIA_TYPE_RE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,62}/[a-z0-9][a-z0-9!#$&^_.+-]{0,62}")


def _media_type(value: str | None) -> tuple[str, str | None]:
    """(media type, charset) from a Content-Type header. The media type is a plain lowercase
    ``type/subtype`` token or ``"invalid"``, never the server's free text."""
    if not value:
        return "", None
    parts = value.split(";")
    mtype = parts[0].strip().lower()
    if not _MEDIA_TYPE_RE.fullmatch(mtype):
        mtype = "invalid"
    charset = None
    for p in parts[1:]:
        k, _, v = p.partition("=")
        if k.strip().lower() == "charset":
            charset = v.strip().strip('"').strip("'").lower() or None
    return mtype, charset


def exchange(conn: _PinnedHTTP, target: str, headers: dict[str, str], *, deadline: float,
             max_bytes: int, url: str) -> tuple[int, str | None, Response | None]:
    """GET ``target`` on ``conn``. Returns (status, Location, response-or-None).

    A redirect returns its Location without reading the body; any status other than 200 is a
    FetchError; a 200 is read in full under the byte cap and the deadline.
    """
    with _Watchdog(conn, deadline) as dog:
        try:
            conn.timeout = _remaining(deadline)
            conn.request("GET", target, headers=headers)
            if conn.sock is not None:
                conn.sock.settimeout(_remaining(deadline))
            resp = conn.getresponse()
            status = resp.status
            if status in REDIRECT_CODES:
                return status, resp.getheader("Location"), None
            if status != 200:
                retry = status == 429 or status >= 500
                raise FetchError(f"http status {status}", terminal=not retry, status=status)
            length = resp.getheader("Content-Length")
            if length is not None and length.strip().isdigit() and int(length) > max_bytes:
                raise refused(f"Content-Length {int(length)} is over the {max_bytes}-byte cap", status)
            encoding = (resp.getheader("Content-Encoding") or "identity").strip().lower()
            decoder = _Decoder(encoding, max_bytes)
            mtype, charset = _media_type(resp.getheader("Content-Type"))
            body = bytearray()
            wire = 0
            while True:
                if conn.sock is not None:
                    conn.sock.settimeout(_remaining(deadline))
                chunk = resp.read1(CHUNK)
                if not chunk:
                    break
                wire += len(chunk)
                if wire > max_bytes:
                    raise refused(f"larger than the {max_bytes}-byte cap", status)
                body += decoder.feed(chunk)
            if dog.fired:
                raise transient("the total time limit passed")
            _remaining(deadline)
            if (length is not None and length.strip().isdigit() and resp.chunked is False
                    and wire != int(length)):
                raise transient(f"the body was cut short ({wire} of {int(length)} bytes)")
            body += decoder.finish()
            return status, None, Response(url, status, mtype, charset, bytes(body), wire)
        except FetchError:
            raise
        except (OSError, http.client.HTTPException, ValueError) as exc:
            if dog.fired or time.monotonic() >= deadline:
                raise transient("the total time limit passed") from None
            raise transient(_describe(exc)) from None
        finally:
            conn.close()


def _split(url: str) -> tuple[str, str, int, str]:
    """(scheme, host without brackets, port, request target) of a canonical URL."""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme
    host = parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    elif url.endswith("?"):
        target += "?"
    return scheme, host, port, target


# ---------------------------------------------------------------------- the page fetcher

class SafeFetcher:
    """Fetches public pages only. ``resolver``, ``ssl_context`` and the loopback allowance are
    injection points for tests; production uses the defaults."""

    def __init__(self, *, max_bytes: int, timeout: float, max_redirects: int, user_agent: str,
                 resolver: Resolver | None = None, ssl_context: ssl.SSLContext | None = None,
                 proc_paths: dict[str, str] | None = None, ipv4_only: bool = False,
                 _test_allow_loopback: bool = False) -> None:
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.user_agent = user_agent
        self.ipv4_only = ipv4_only
        self.resolver: Resolver = resolver or system_resolver
        self.ssl_context = ssl_context or _default_context()
        self.policy = AddressPolicy(_test_allow_loopback=_test_allow_loopback, proc_paths=proc_paths)

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html, application/xhtml+xml, text/plain, application/pdf;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "close",
        }

    def _connect_target(self, url: str, deadline: float) -> tuple[_PinnedHTTP, str]:
        scheme, host, port, target = _split(url)
        try:
            ipaddress.ip_address(host)
            literal = True
        except ValueError:
            literal = False
        if literal:
            addrs = [host]  # an IP literal: checked below, no lookup
        else:
            try:
                family = socket.AF_INET if self.ipv4_only else socket.AF_UNSPEC
                addrs = list(self.resolver(host, port, _remaining(deadline), family=family))  # the one lookup
            except FetchError:
                raise
            except (OSError, ValueError) as exc:
                raise transient(f"DNS: {exc}") from None
        if self.ipv4_only:
            # The unit allows AF_INET only: IPv6 answers are never considered.
            addrs = [a for a in addrs if ":" not in a]
            if not addrs:
                raise refused(f"{host} has no IPv4 address (ipv4_only is set)")
        self.policy.refresh()  # the host's own addresses/prefixes, fresh for every hop
        try:
            addrs = self.policy.check_all(host, addrs)
        except AddressRefused as exc:
            raise refused(str(exc)) from None
        except TablesUnreadable as exc:
            raise transient(f"{NOT_COUNTED}{exc}") from None
        addr = addrs[0]
        if scheme == "https":
            conn: _PinnedHTTP = _PinnedHTTPS(host, port, addr, _remaining(deadline), self.ssl_context)
        else:
            conn = _PinnedHTTP(host, port, addr, _remaining(deadline))
        return conn, target

    def tables_problem(self) -> str | None:
        """Re-read the host's interface tables; why they cannot be used, or None."""
        self.policy.refresh()
        return self.policy.local.unreadable(need_v6=not self.ipv4_only)

    def fetch(self, url: str, may_follow: Callable[[str], bool]) -> Response:
        """Fetch ``url`` (canonical). ``may_follow(target)`` decides each redirect."""
        try:
            current = canonical_url(url)
        except ValueError as exc:
            raise refused(f"URL refused: {exc}") from None
        deadline = time.monotonic() + self.timeout
        redirects: list[str] = []
        while True:
            conn, target = self._connect_target(current, deadline)
            status, location, resp = exchange(conn, target, self._headers(), deadline=deadline,
                                              max_bytes=self.max_bytes, url=current)
            if resp is not None:
                resp.redirects = redirects
                return resp
            if not location:
                raise refused(f"http status {status} redirect without a Location", status)
            try:
                nxt = canonical_url(urllib.parse.urljoin(current, location.strip()))
            except ValueError as exc:
                raise refused("redirect to a URL the gate refuses (not canonical http/https)", status) from None
            if len(redirects) >= self.max_redirects:
                raise RedirectRefused(nxt, f"more than {self.max_redirects} redirects", status)
            if not may_follow(nxt):
                raise RedirectRefused(nxt, "redirect target is not ticked", status)
            redirects.append(nxt)
            current = nxt


class RedirectRefused(FetchError):
    def __init__(self, target: str, why: str, status: int | None) -> None:
        super().__init__(why, terminal=True, status=status)
        self.target = target


# ---------------------------------------------------------------------- SearXNG

class SearxClient:
    """``GET {base}/search?q=<query>&format=json`` against the configured instance only (it is
    the gate's own loopback service, so no address policy applies; no redirects are followed)."""

    def __init__(self, base_url: str, *, max_bytes: int, timeout: float, user_agent: str) -> None:
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"searxng_url {base_url!r} is not an http(s) URL")
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.prefix = parts.path.rstrip("/")
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.user_agent = user_agent

    def search(self, query: str) -> tuple[list[dict], int, int]:
        """(results, body bytes, http status). Raises FetchError."""
        deadline = time.monotonic() + self.timeout
        target = f"{self.prefix}/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
        if self.scheme == "https":
            conn: _PinnedHTTP = _PinnedHTTPS(self.host, self.port, self.host, self.timeout, _default_context())
        else:
            conn = _PinnedHTTP(self.host, self.port, self.host, self.timeout)
        headers = {"User-Agent": self.user_agent, "Accept": "application/json",
                   "Accept-Encoding": "identity", "Connection": "close"}
        status, _, resp = exchange(conn, target, headers, deadline=deadline, max_bytes=self.max_bytes,
                                   url=f"{self.scheme}://{self.host}:{self.port}{target}")
        if resp is None:
            raise transient(f"SearXNG answered with a redirect (http {status}); not followed", status)
        try:
            data = json.loads(resp.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise transient("SearXNG did not return JSON", status) from None
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise transient("SearXNG JSON has no results list", status)
        return [r for r in results if isinstance(r, dict)], len(resp.body), status
