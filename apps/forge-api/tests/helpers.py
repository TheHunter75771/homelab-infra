"""A scratch library + the real forge (K3's tick methods included), and the API served on 127.0.0.1.

Nothing here touches a real host: every server binds 127.0.0.1 on an ephemeral port, and the
"other peer" in the auth tests is 127.0.0.2 (the same loopback interface, a different source
address), so the peer check sees a real TCP peer address, never a header.
"""
from __future__ import annotations

import datetime as dt
import http.client
import io
import json
import logging
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any

from forge import Forge
from forge.core import SEARCHES
from forge_api.server import App, ForgeApiServer
from forge_api.service import Service
from forge_api.ticks import ForgeTicks
from library import Library
from library import frontmatter as fm
from library.safefs import atomic_write_bytes

TOKEN = "t0k3n-" + "Q" * 20 + "-forge-api-test-secret"   # 48 chars, b64token alphabet
APPROVER = "8a1f6c2e-0d5b-4c11-9e7a-3b2f1d0c9e88"
ALLOWED = "127.0.0.1"
OTHER = "127.0.0.2"


class Clock:
    def __init__(self) -> None:
        self.now = dt.datetime(2026, 10, 27, 10, 0, 0, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        self.now += dt.timedelta(seconds=1)
        return self.now


class Env:
    """A scratch library, a Service over it, and helpers to build forge state."""

    def __init__(self, testcase, *, ticker_factory=ForgeTicks, lock_tries: int = 20, lock_sleep: float = 0.05) -> None:
        tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "lib"
        self.clock = Clock()
        self.lib, _ = Library.init(self.root, clock=self.clock)
        self.forge = Forge(self.lib)
        self.cfg = {
            "library_root": str(self.root), "bind": "127.0.0.1", "port": 0,
            "allowed_peers": [ALLOWED], "token_file": "/nonexistent", "approver_user_id": APPROVER,
        }
        self.service = Service(self.cfg, ticker_factory=ticker_factory, library_kwargs={"clock": self.clock},
                               lock_tries=lock_tries, lock_sleep=lock_sleep)

    def folder(self, rid: str) -> Path:
        return self.root / "35-forge" / rid

    def read(self, rid: str, name: str) -> str:
        p = self.folder(rid) / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def new_request(self, queries=(("apa 7 citation rules", "learn the rules"),
                                   ("apa 7 reference list format", "reference list"),
                                   ("apa 7 in-text citations", "in-text form"))) -> str:
        rid = self.forge.new("APA 7 citations", "Learn APA 7.\n")
        self.forge.propose_searches(rid, list(queries))
        return rid

    def hand_tick(self, rid: str, filename: str, contains: str) -> None:
        path = self.folder(rid) / filename
        lines = path.read_text(encoding="utf-8").split("\n")
        hits = [i for i, line in enumerate(lines) if line.startswith("- [ ] ") and contains in line]
        assert len(hits) == 1, hits
        lines[hits[0]] = "- [x] " + lines[hits[0]][6:]
        path.write_text("\n".join(lines), encoding="utf-8")

    def with_sources(self, urls: list[str], rid: str | None = None) -> str:
        rid = rid or self.new_request()
        self.hand_tick(rid, SEARCHES, "apa 7 citation rules")
        rows = [(u, f"Result {i} title", "result for search: apa 7 citation rules", "page text")
                for i, u in enumerate(urls)]
        self.forge.propose_sources(rid, rows)
        return rid

    def ingest(self, rid: str, url: str, content: bytes, ext: str = "txt", *, topic: str = "apa-7-citations") -> str:
        """What the research gate does after a fetch: file, add forge:, record_ingest."""
        src = self.base / f"fetched.{ext}"
        src.write_bytes(content)
        result = self.lib.file(src, topic, title="Fetched page", source=url, tags=["forge"],
                               summary="test item")
        sidecar = self.lib.abs_path(result.sidecar)
        meta, body = fm.parse(sidecar.read_bytes().decode("utf-8"))
        meta["forge"] = rid
        atomic_write_bytes(sidecar, fm.render(meta, body).encode("utf-8"))
        return self.forge.record_ingest(rid, url, result.item)


class Served:
    """The API on 127.0.0.1:<ephemeral> in a thread, plus a client."""

    def __init__(self, testcase, env: Env, **app_kwargs: Any) -> None:
        self.env = env
        self.app = App(env.cfg, TOKEN, env.service, **app_kwargs)
        self.server = ForgeApiServer(self.app, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        testcase.addCleanup(self.close)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def call(self, method: str, path: str, body: Any = None, *, token: str | None = TOKEN,
             source: str = ALLOWED, headers: dict[str, str] | None = None, raw_body: bytes | None = None,
             content_type: str | None = "application/json") -> tuple[int, Any, http.client.HTTPResponse]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10, source_address=(source, 0))
        try:
            hdrs: dict[str, str] = {}
            if token is not None:
                hdrs["Authorization"] = f"Bearer {token}"
            data = None
            if raw_body is not None:
                data = raw_body
            elif body is not None:
                data = json.dumps(body).encode("utf-8")
            if data is not None and content_type:
                hdrs["Content-Type"] = content_type
            hdrs.update(headers or {})
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            payload = resp.read()
            try:
                parsed = json.loads(payload) if payload else None
            except ValueError:
                parsed = payload
            return resp.status, parsed, resp
        finally:
            conn.close()

    def raw(self, data: bytes, *, source: str = ALLOWED) -> bytes:
        """Send raw bytes; return everything the server answers."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=10, source_address=(source, 0)) as s:
            s.sendall(data)
            s.shutdown(socket.SHUT_WR)
            out = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    return out
                out += chunk


class LogCapture:
    """Everything the service logs (DEBUG and up, tracebacks included), unredacted."""

    def __init__(self, testcase) -> None:
        self.buf = io.StringIO()
        self.handler = logging.StreamHandler(self.buf)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        self.logger = logging.getLogger("forge_api")
        self.old_level = self.logger.level
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)
        self.old_propagate = self.logger.propagate
        self.logger.propagate = False
        testcase.addCleanup(self.close)

    def close(self) -> None:
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.old_level)
        self.logger.propagate = self.old_propagate

    @property
    def text(self) -> str:
        self.handler.flush()
        return self.buf.getvalue()
