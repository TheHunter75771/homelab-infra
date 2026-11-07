"""Fake SearXNG and web servers on 127.0.0.1, a test resolver, and a scratch library + forge.

Nothing here touches the real network: the resolver maps fake public names to 127.0.0.1 and
raises for anything else, and every server binds 127.0.0.1 on an ephemeral port.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import tempfile
import threading
import time
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from forge import Forge
from library import Library
from research_gate.config import validate
from research_gate.fetch import SafeFetcher, SearxClient
from research_gate.gate import Gate


HOST_V6 = "2600:1700:aaaa:bbbb:1234:5678:9abc:def0"   # the host's own global IPv6 (a /64, noprefixroute)
HOST_V4 = "44.1.2.3"                                  # a public IPv4 on a local interface


def _hex6(addr: str) -> str:
    import ipaddress
    return f"{int(ipaddress.IPv6Address(addr)):032x}"


def make_proc(directory: Path, *, if_inet6: str | None = None, ipv6_route: str | None = None,
              fib_trie: str | None = None, route: str | None = None) -> dict[str, str]:
    """Fake /proc/net tables (the real formats, taken from a Linux host). The default host has
    loopback, a ULA /128 (tailnet-like), a global /64 ``mngtmpaddr noprefixroute`` address (so no
    route exists for that /64), an on-link /48 route, a gateway route, and a public IPv4 /24."""
    directory.mkdir(parents=True, exist_ok=True)
    if if_inet6 is None:
        if_inet6 = (f"{_hex6('::1')} 01 80 10 80       lo\n"
                    f"{_hex6('fd00:db8::7')} 03 80 00 80 tailscale0\n"
                    f"{_hex6(HOST_V6)} 02 40 00 00  enp42s0\n"
                    f"{_hex6('fe80::db8:5')} 02 40 20 80  enp42s0\n")
    if ipv6_route is None:
        z = "0" * 32
        ipv6_route = (f"{_hex6('2600:1700:dddd::')} 30 {z} 00 {z} 00000100 00000001 00000000 00000001  enp42s0\n"
                      f"{_hex6('2a00::')} 10 {z} 00 {_hex6('fe80::1')} 00000400 00000001 00000000 00000003  enp42s0\n"
                      f"{z} 00 {z} 00 {_hex6('fe80::1')} 00000064 0000000d 00000000 00000003  enp42s0\n"
                      f"{z} 00 {z} 00 {z} ffffffff 00000001 00000000 00200200       lo\n")
    if fib_trie is None:
        fib_trie = ("Main:\n  +-- 0.0.0.0/0 3 0 5\n     |-- 0.0.0.0\n        /0 universe UNICAST\n"
                    "     +-- 44.1.2.0/24 2 0 2\n        |-- 44.1.2.3\n           /32 host LOCAL\n"
                    "     |-- 127.0.0.1\n        /32 host LOCAL\n")
    if route is None:
        route = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
                 "eth0\t00000000\t0102012C\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
                 "eth0\t0002012C\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n")
    paths = {}
    for key, text in (("if_inet6", if_inet6), ("ipv6_route", ipv6_route), ("fib_trie", fib_trie), ("route", route)):
        path = directory / key
        if text is not False:
            path.write_text(text)
        paths[key] = str(path)
    return paths


class Clock:
    def __init__(self) -> None:
        self.now = dt.datetime.now().astimezone().replace(microsecond=0)

    def __call__(self) -> dt.datetime:
        self.now += dt.timedelta(seconds=1)
        return self.now


class Resolver:
    """name -> list of answers; each call pops the next answer if a list of lists is given."""

    def __init__(self, table: dict[str, object] | None = None) -> None:
        self.table: dict[str, object] = dict(table or {})
        self.calls: list[str] = []
        self.families: list[int] = []

    def __call__(self, host: str, port: int, timeout: float, family: int = 0) -> list[str]:
        """Answers are returned unfiltered (even for AF_INET), so the fetcher's own filter is tested."""
        self.calls.append(host)
        self.families.append(family)
        if host not in self.table:
            raise OSError(f"test resolver: unknown name {host}")
        ans = self.table[host]
        if callable(ans):
            return ans()
        return list(ans)  # type: ignore[arg-type]


class Rebinding:
    """Answers ``first`` on the first lookup and ``later`` afterwards."""

    def __init__(self, first: list[str], later: list[str]) -> None:
        self.first, self.later, self.n = first, later, 0

    def __call__(self) -> list[str]:
        self.n += 1
        return list(self.first if self.n == 1 else self.later)


class Server:
    """A ThreadingHTTPServer on 127.0.0.1 whose routes are ``path -> handler(req)`` functions.

    ``req`` is the BaseHTTPRequestHandler; handlers write the whole response themselves.
    Every request is recorded as (path, headers dict).
    """

    def __init__(self) -> None:
        self.routes: dict[str, object] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.stop = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802
                outer.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                route = outer.routes.get(urllib.parse.urlsplit(self.path).path)
                if route is None:
                    send(self, 404, b"not found", "text/plain")
                    return
                try:
                    route(self)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def paths(self) -> list[str]:
        return [p for p, _ in self.requests]

    def close(self) -> None:
        self.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()


def send(req: BaseHTTPRequestHandler, status: int, body: bytes, ctype: str | None,
         headers: dict[str, str] | None = None) -> None:
    req.send_response(status)
    if ctype is not None:
        req.send_header("Content-Type", ctype)
    for k, v in (headers or {}).items():
        req.send_header(k, v)
    req.send_header("Content-Length", str(len(body)))
    req.send_header("Connection", "close")
    req.end_headers()
    req.wfile.write(body)
    req.wfile.flush()


def page(body: bytes | str, ctype: str = "text/html; charset=utf-8", status: int = 200,
         headers: dict[str, str] | None = None):
    data = body.encode("utf-8") if isinstance(body, str) else body
    return lambda req: send(req, status, data, ctype, headers)


def redirect(location: str, status: int = 302, headers: dict[str, str] | None = None):
    return lambda req: send(req, status, b"", "text/plain", {"Location": location, **(headers or {})})


def gzip_bomb(decoded_size: int) -> bytes:
    z = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    chunk = b"\0" * (1024 * 1024)
    out = bytearray()
    left = decoded_size
    while left > 0:
        n = min(left, len(chunk))
        out += z.compress(chunk[:n])
        left -= n
    out += z.flush()
    return bytes(out)


def gz(data: bytes) -> bytes:
    return gzip.compress(data)


def slow_body(server: Server, ctype: str = "text/plain", every: float = 0.1, total: int = 10_000):
    """Sends headers at once, then one byte every ``every`` seconds (a slowloris body)."""
    def handler(req: BaseHTTPRequestHandler) -> None:
        req.send_response(200)
        req.send_header("Content-Type", ctype)
        req.send_header("Content-Length", str(total))
        req.end_headers()
        req.wfile.flush()
        for _ in range(total):
            if server.stop.is_set():
                return
            req.wfile.write(b"a")
            req.wfile.flush()
            time.sleep(every)
    return handler


def slow_headers(server: Server, every: float = 0.1):
    """Trickles the status line and headers one byte at a time (a slowloris head)."""
    def handler(req: BaseHTTPRequestHandler) -> None:
        for b in b"HTTP/1.1 200 OK\r\nX-Filler: " + b"a" * 10_000:
            if server.stop.is_set():
                return
            req.wfile.write(bytes([b]))
            req.wfile.flush()
            time.sleep(every)
    return handler


class FakeSearx:
    """``/search?q=..&format=json`` answering from ``results[q]``; counts calls per query."""

    def __init__(self) -> None:
        self.server = Server()
        self.results: dict[str, list[dict]] = {}
        self.calls: list[str] = []
        self.fail_next = 0
        self.server.routes["/search"] = self._handle

    def _handle(self, req: BaseHTTPRequestHandler) -> None:
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(req.path).query)
        q = qs.get("q", [""])[0]
        self.calls.append(q)
        if self.fail_next:
            self.fail_next -= 1
            send(req, 502, b"bad gateway", "text/plain")
            return
        assert qs.get("format") == ["json"]
        body = json.dumps({"query": q, "results": self.results.get(q, [])}).encode()
        send(req, 200, body, "application/json")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.port}"

    def close(self) -> None:
        self.server.close()


class Env:
    """A scratch library with the forge, fake SearXNG + web server, resolver and a Gate."""

    def __init__(self, testcase, *, max_bytes: int = 64 * 1024, timeout: float = 2.0,
                 max_redirects: int = 3, allow_loopback: bool = True, resolver: Resolver | None = None) -> None:
        tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "lib"
        self.clock = Clock()
        self.lib, _ = Library.init(self.root, clock=self.clock)
        self.forge = Forge(self.lib)
        self.searx = FakeSearx()
        testcase.addCleanup(self.searx.close)
        self.web = Server()
        testcase.addCleanup(self.web.close)
        self.resolver = resolver or Resolver()
        for name in ("news.example.test", "docs.example.test", "other.example.test", "evil.example.test"):
            self.resolver.table.setdefault(name, ["127.0.0.1"])
        self.cfg = validate({"library_root": str(self.root), "searxng_url": self.searx.url,
                             "max_bytes": max_bytes, "fetch_timeout": timeout,
                             "max_redirects": max_redirects, "results_per_query": 8})
        self.proc = make_proc(self.base / "proc")
        self.fetcher = SafeFetcher(max_bytes=max_bytes, timeout=timeout, max_redirects=max_redirects,
                                   user_agent=self.cfg["user_agent"], resolver=self.resolver,
                                   proc_paths=self.proc, _test_allow_loopback=allow_loopback)
        self.gate = Gate(self.cfg, fetcher=self.fetcher,
                         searx=SearxClient(self.searx.url, max_bytes=max_bytes, timeout=timeout,
                                           user_agent=self.cfg["user_agent"]),
                         clock=self.clock)

    def url(self, path: str, host: str = "news.example.test") -> str:
        return f"http://{host}:{self.web.port}{path}"

    # ---------------------------------------------------------------- forge helpers

    def new_request(self, title: str = "ROCm setup") -> str:
        return self.forge.new(title, "Please learn how to set up ROCm.\n")

    def folder(self, rid: str) -> Path:
        return self.root / "35-forge" / rid

    def tick(self, rid: str, filename: str, contains: str) -> None:
        path = self.folder(rid) / filename
        lines = path.read_text(encoding="utf-8").split("\n")
        hits = [i for i, line in enumerate(lines) if line.startswith("- [ ] ") and contains in line]
        assert len(hits) == 1, f"expected one unticked line containing {contains!r} in {filename}: {hits}"
        lines[hits[0]] = "- [x] " + lines[hits[0]][6:]
        path.write_text("\n".join(lines), encoding="utf-8")

    def gate_log(self, rid: str) -> str:
        path = self.folder(rid) / "gate-log.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def with_sources(self, urls: list[str], rid: str | None = None, query: str = "rocm install guide") -> str:
        """A request whose ticked search has run and proposed ``urls`` (all unticked)."""
        rid = rid or self.new_request()
        self.forge.propose_searches(rid, [(query, "learn the install steps")])
        self.tick(rid, "searches.md", query)
        self.searx.results[query] = [{"url": u, "title": f"Result {i}"} for i, u in enumerate(urls)]
        self.gate.run_once()
        return rid
