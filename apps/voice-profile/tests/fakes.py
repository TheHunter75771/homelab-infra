"""Synthetic .docx files, synthetic prose, a fake broker on 127.0.0.1 and a scratch library.

Every sample here is generated: no real writing is read or stored (the repo is public). The fake
broker binds 127.0.0.1 on an ephemeral port; nothing touches the real network.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

from library import Library
from voice_profile.config import validate

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"

# ---------------------------------------------------------------------- prose

_VOCAB = ["river", "engine", "signal", "harbor", "lantern", "circuit", "maple", "granite", "orbit", "canvas", "pilot", "ledger", "compass", "meadow", "quartz", "tunnel", "beacon", "cobalt", "fabric", "glacier", "hammer", "island", "jacket", "kettle", "ladder", "magnet", "needle", "oyster", "pepper", "quiver", "rocket", "saddle", "timber", "umbrella", "velvet", "wagon", "yonder", "zephyr", "anchor", "bridge", "candle", "dolphin", "falcon", "garden", "helmet", "ivory", "jungle", "kayak", "lemon", "mirror", "nickel", "orchard", "parcel", "quiet", "rabbit", "silver", "tablet", "uncle", "violet", "walnut", "yellow", "zebra", "arrow", "basket", "cactus", "desert", "ember", "forest", "gravel", "harvest", "inlet", "jasper", "kernel", "lagoon", "marble", "nectar", "olive", "prairie", "quarry", "raven", "sandal", "thistle", "upland", "valley", "willow", "yarrow", "zinc", "acorn", "bamboo", "cedar", "dune", "estuary", "fern"]
_GLUE = ("the", "a", "my", "our", "this", "every", "each", "that")
_VERBS = ("moves", "holds", "shows", "builds", "carries", "turns", "keeps", "finds", "makes", "tests")


def sentence(rng: random.Random, n_words: int) -> str:
    out = [rng.choice(_GLUE)]
    while len(out) < n_words - 1:
        out.append(rng.choice(_VOCAB) if len(out) % 3 else rng.choice(_VERBS + _GLUE))
    out.append(rng.choice(_VOCAB))
    text = " ".join(out[:n_words])
    return text[0].upper() + text[1:] + "."


def prose(seed: int, paras: int = 4, sents: int = 6, words: int = 12) -> list[str]:
    """Deterministic nonsense prose: different seeds share almost no 5-word shingles."""
    rng = random.Random(seed)
    return [" ".join(sentence(rng, words + rng.randint(-3, 3)) for _ in range(sents)) for _ in range(paras)]


# ---------------------------------------------------------------------- docx XML

def run(text: str, *, hidden: bool = False) -> str:
    rpr = "<w:rPr><w:vanish/></w:rPr>" if hidden else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def para(text: str = "", *, style: str | None = None, left: int | None = None, hanging: int | None = None,
         first_line: int | None = None, numbered: bool = False, inner: str = "") -> str:
    ppr = ""
    if style:
        ppr += f'<w:pStyle w:val="{style}"/>'
    if numbered:
        ppr += '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
    if left is not None or hanging is not None or first_line is not None:
        attrs = "".join(f' w:{k}="{v}"' for k, v in (("left", left), ("hanging", hanging),
                                                       ("firstLine", first_line)) if v is not None)
        ppr += f"<w:ind{attrs}/>"
    body = "".join(run(line) + ("<w:r><w:br/></w:r>" if i < len(text.split("\n")) - 1 else "")
                   for i, line in enumerate(text.split("\n"))) if text else ""
    return f"<w:p>{f'<w:pPr>{ppr}</w:pPr>' if ppr else ''}{body}{inner}</w:p>"


def document(body: str, *, prolog: str = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>') -> str:
    return (f'{prolog}<w:document xmlns:w="{W}" xmlns:mc="{MC}" xmlns:wps="{WPS}">'
            f"<w:body>{body}<w:sectPr/></w:body></w:document>")


CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/'
                 'package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')


def docx_bytes(document_xml: str | bytes, extra: dict[str, bytes | str] | None = None,
               compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("word/document.xml", document_xml)
        zf.writestr("word/comments.xml", f'<w:comments xmlns:w="{W}"><w:comment w:id="0">'
                                         f'{para("COMMENTCANARY text from a comment")}</w:comment></w:comments>')
        zf.writestr("word/header1.xml", f'<w:hdr xmlns:w="{W}">{para("HEADERCANARY running head")}</w:hdr>')
        zf.writestr("word/footer1.xml", f'<w:ftr xmlns:w="{W}">{para("FOOTERCANARY page 1")}</w:ftr>')
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def essay(seed: int, *, title: str = "A Synthetic Essay", paras: int = 4, cover: bool = True,
          references: bool = True, extra_body: str = "") -> str:
    """A whole synthetic essay: an APA-like cover page, prose, and a reference list."""
    body = ""
    if cover:
        body += "".join(para(x) for x in (title, "Student Q. Example", "College of Testing, Example University",
                                           "ABC 101: Composition", "Professor Placeholder", "October 1, 2026"))
    body += "".join(para(p, first_line=720) for p in prose(seed, paras))
    body += extra_body
    if references:
        body += para("References", style="Heading1")
        body += para("Author, A. (2020). REFCANARY title of a work. Publisher.", left=720, hanging=720)
    return document(body)


def write_docx(folder: Path, name: str, document_xml: str | bytes, *, mtime: float | None = None, **kw) -> Path:
    path = folder / name
    path.write_bytes(docx_bytes(document_xml, **kw))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ---------------------------------------------------------------------- the model's replies

GOOD = """\
---
status: draft
---
# Voice profile

## Tone
Direct and practical, with a steady, confident attitude toward the reader.

## Sentence style
Mid-length sentences of about twelve words, mostly declarative, opening with the subject.

## Word choice
Plain concrete nouns and verbs; few contractions; first person is rare.

## Structure
Each paragraph opens with a topic sentence and closes by tying back to the main point.

## Do
- Lead with the point, then support it.
- Keep sentences short and concrete.

## Don't
- Don't stack long chains of clauses.
- Don't use slang.

## Examples
### Example 1
Before: The meeting was moved because of the weather.
After: Weather moved the meeting, so we met a day later.

### Example 2
Before: There are many reasons to save money.
After: Saving money gives you options when plans change.
"""


class FakeBroker:
    """``POST /api/chat``: answers from ``replies`` in order (the last one repeats). A reply is a
    string (the model's text), a callable returning one, a dict ``{"content": str, "prompt_eval_count": int,
    "done_reason": str}``, or ``{"status": int, "body": bytes}`` for raw answers."""

    def __init__(self) -> None:
        self.replies: list[object] = [GOOD]
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
                if isinstance(reply, dict) and "status" in reply:
                    status, data = reply["status"], reply.get("body", b"")
                else:
                    if isinstance(reply, str):
                        reply = {"content": reply}
                    status = 200
                    payload = {"model": "x", "message": {"role": "assistant", "content": reply["content"]},
                               "done": True, "done_reason": reply.get("done_reason", "stop")}
                    if "prompt_eval_count" in reply:
                        payload["prompt_eval_count"] = reply["prompt_eval_count"]
                    data = json.dumps(payload).encode()
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


class Clock:
    def __init__(self) -> None:
        self.now = dt.datetime(2026, 10, 27, 9, 0, 0).astimezone()

    def __call__(self) -> dt.datetime:
        return self.now


class Env:
    """A scratch library with the mount sentinel, a samples folder and a fake broker."""

    def __init__(self, testcase, **cfg) -> None:
        tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "lib"
        self.clock = Clock()
        self.lib, _ = Library.init(self.root, clock=self.clock)
        (self.root / ".ai-library-sentinel").write_text("ai-library\n")
        self.samples = self.root / "40-writing" / "samples"
        self.writing = self.root / "40-writing"
        self.broker = FakeBroker()
        testcase.addCleanup(self.broker.close)
        raw = {"library_root": str(self.root), "broker_url": self.broker.url, "timeout": 10, "min_words": 60}
        raw.update(cfg)
        self.cfg = validate(raw)

    def builder(self, **kw):
        from voice_profile.build import Builder
        return Builder(self.cfg, clock=self.clock, **kw)

    def add(self, name: str, document_xml: str | bytes, **kw) -> Path:
        return write_docx(self.samples, name, document_xml, **kw)

    def add_corpus(self, n: int = 5) -> None:
        for i in range(n):
            self.add(f"Essay {i + 1}.docx", essay(100 + i, title=f"Essay Number {i + 1}"), mtime=1_700_000_000 + i)

    def read(self, rel: str) -> str:
        return (self.writing / rel).read_text(encoding="utf-8")


def snapshot(folder: Path) -> dict[str, bytes]:
    out = {}
    for p in sorted(folder.rglob("*")):
        rel = p.relative_to(folder).as_posix()
        out[rel] = p.read_bytes() if p.is_file() and not p.is_symlink() else b"<dir-or-link>"
    return out
