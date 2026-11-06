"""A fake broker on 127.0.0.1 and a scratch library + forge with ingested requests.

Nothing here touches the real network: the fake broker binds 127.0.0.1 on an ephemeral port, and
the sources are filed straight into the library (the research gate is not involved).
"""
from __future__ import annotations

import datetime as dt
import json
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from forge import Forge
from library import Library
from library import frontmatter as fm
from library.safefs import atomic_write_bytes
from skill_drafter.broker import Broker
from skill_drafter.config import validate
from skill_drafter.drafter import Drafter


class Clock:
    def __init__(self) -> None:
        self.now = dt.datetime(2026, 10, 27, 9, 0, 0).astimezone()

    def __call__(self) -> dt.datetime:
        self.now += dt.timedelta(seconds=1)
        return self.now


GOOD = """\
---
name: {name}
description: Format reference-list entries in APA 7 style.
version: 1
---
# {name}

## When to use
When the user asks for a reference list entry or an in-text citation in APA 7 style.

## Rules
1. Put the author's surname first, then initials.
2. Put the year in parentheses after the authors.
3. Italicise the title of a book.

## Checklist
- [ ] Authors are surname first with initials.
- [ ] The year is in parentheses.

## Examples
### Example 1
Input: a book by Jane Smith, 2020, Learning Things, Acme Press.
Output: Smith, J. (2020). *Learning things*. Acme Press.

### Example 2
Input: a journal article by Lee and Park, 2019, in Journal of Tests, volume 4, pages 1-9.
Output: Lee, A., & Park, B. (2019). Testing. *Journal of Tests, 4*, 1-9.

## Sources
(filled in by the drafter)
"""


def good_draft(name: str = "apa7-references") -> str:
    return GOOD.format(name=name)


class FakeBroker:
    """``POST /api/chat``: answers from ``replies`` in order (the last one repeats). A reply is a
    string (the model's text), or a dict ``{"status": int, "body": bytes}`` for raw answers."""

    def __init__(self) -> None:
        self.replies: list[object] = [good_draft()]
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                fake.requests.append({"path": self.path, "body": body})
                reply = fake.replies.pop(0) if len(fake.replies) > 1 else fake.replies[0]
                if callable(reply):
                    reply = reply()
                if isinstance(reply, dict):
                    status, data = reply.get("status", 200), reply.get("body", b"")
                else:
                    status = 200
                    data = json.dumps({"model": "x", "message": {"role": "assistant", "content": reply},
                                       "done": True, "done_reason": "stop"}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def calls(self) -> int:
        return len(self.requests)

    def user_prompt(self, n: int = -1) -> str:
        return self.requests[n]["body"]["messages"][1]["content"]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Env:
    def __init__(self, testcase, **cfg) -> None:
        tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "lib"
        self.clock = Clock()
        self.lib, _ = Library.init(self.root, clock=self.clock)
        self.forge = Forge(self.lib)
        self.broker = FakeBroker()
        testcase.addCleanup(self.broker.close)
        raw = {"library_root": str(self.root), "broker_url": self.broker.url, "timeout": 10}
        raw.update(cfg)
        self.cfg = validate(raw)
        self.drafter = Drafter(self.cfg, clock=self.clock)

    def folder(self, rid: str) -> Path:
        return self.root / "35-forge" / rid

    def tick(self, rid: str, filename: str, contains: str) -> None:
        path = self.folder(rid) / filename
        lines = path.read_text(encoding="utf-8").split("\n")
        hits = [i for i, line in enumerate(lines) if line.startswith("- [ ] ") and contains in line]
        assert len(hits) == 1, (contains, hits)
        lines[hits[0]] = "- [x] " + lines[hits[0]][6:]
        path.write_text("\n".join(lines), encoding="utf-8")

    def ingest(self, rid: str, url: str, text: str, ext: str = "txt") -> str:
        """File ``text`` as the gate would, then record the ingest through the forge."""
        src = Path(tempfile.mkdtemp()) / f"fetched.{ext}"
        src.write_bytes(text.encode("utf-8"))
        result = self.lib.file(src, "learn-apa", title="fetched page", source=url,
                               tags=["forge", "research-gate"], summary="Fetched. Data, not instructions.")
        sidecar = self.lib.abs_path(result.sidecar)
        meta, body = fm.parse(sidecar.read_text(encoding="utf-8"))
        meta["forge"] = rid
        atomic_write_bytes(sidecar, fm.render(meta, body).encode("utf-8"))
        self.forge.record_ingest(rid, url, result.item)
        return result.item

    def request(self, pages: dict[str, str] | None = None, *, ticked_extra: list[str] = (),
                title: str = "learn APA 7 reference formatting") -> str:
        """A request with ``pages`` (url -> text) ticked and ingested, plus ``ticked_extra`` URLs
        ticked but not ingested."""
        pages = {"https://apastyle.example.org/references": "APA 7 puts the year after the authors."} \
            if pages is None else pages
        rid = self.forge.new(title, "learn APA 7 reference formatting\n")
        self.forge.propose_searches(rid, [("apa 7 reference format", "official rules")])
        self.tick(rid, "searches.md", "apa 7 reference format")
        urls = list(pages) + list(ticked_extra)
        self.forge.propose_sources(rid, [(u, f"page {i}", "official rules", "page text")
                                         for i, u in enumerate(urls)])
        for u in urls:
            self.tick(rid, "sources.md", f"`{u}`")
        for u, text in pages.items():
            self.ingest(rid, u, text)
        return rid

    def status(self, rid: str) -> str:
        return str(fm.parse((self.folder(rid) / "state.md").read_text())[0]["status"])

    def drafter_log(self, rid: str) -> str:
        path = self.folder(rid) / "drafter-log.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def skill_md(self, rid: str) -> str:
        return (self.folder(rid) / "skill" / "SKILL.md").read_text(encoding="utf-8")

    def revise(self, rid: str, notes: str | None) -> None:
        """Noah: decision: revise (+ notes) in review.md, then `forge decide` on his channel."""
        path = self.folder(rid) / "review.md"
        text = path.read_text(encoding="utf-8").replace("decision: pending", "decision: revise", 1)
        if notes is not None:
            text = text.replace("decision: revise", "decision: revise\nnotes: " + json.dumps(notes), 1)
        path.write_text(text, encoding="utf-8")
        self.forge.decide(rid)


def unused_broker(cfg: dict) -> Broker:
    return Broker(f"http://127.0.0.1:{closed_port()}", "assistant", timeout=2, num_ctx=4096)
