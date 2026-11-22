"""The research gate's passes (CONTRACTS §12.16.3): run Noah's ticked searches, fetch his ticked
URLs, file what was fetched, and log every call. Not AI, no autonomy: every network call is
preceded by the forge's own approval check (``is_query_approved`` / ``is_url_approved``), made
again immediately before the call, so a tick removed a moment ago is honoured.

Each request, and each query or URL inside it, fails on its own: an exception is logged (with a
stack trace) to stderr / the journal, a one-line reason goes to ``gate-log.md``, and the pass
moves on.
"""
from __future__ import annotations

import codecs
import logging
import os
import re
import tempfile
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from forge import Forge, ForgeError, is_request_id
from forge.checklist import SOURCE_FIELD_REFUSED, canonical_url, char_problem, check_proposal_text
from forge.core import FORGE, LOCK, QUERY_STATES, STATE, URL_STATES
from library import Library, naming
from library import frontmatter as fm
from library.cli import CAP_ENV
from library.core import DEFAULT_CAP, Clock
from library.safefs import atomic_write_bytes

from .fetch import FetchError, RedirectRefused, Response, SafeFetcher, SearxClient
from .gatelog import MAX_ATTEMPTS, GateLog, safe_text
from .htmltext import html_to_text

log = logging.getLogger("research_gate")

TITLE_MAX = 120
TAKE = "page text"
FALLBACK_TOPIC = "forge-research"
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
TEXT_TYPES = frozenset({"text/plain"})
PDF_TYPES = frozenset({"application/pdf"})
LOCK_TRIES = 20
LOCK_SLEEP = 0.25

T = TypeVar("T")


# ---------------------------------------------------------------------- sanitising

def sanitize_title(raw: object) -> str | None:
    """A search result's title made forge-legal: whitespace collapsed, refused and invisible
    characters dropped, no ``why:`` (which could fake the separator), at most 120 characters.
    None if nothing usable is left."""
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.split())
    text = "".join(c for c in text if c not in SOURCE_FIELD_REFUSED and char_problem(c) is None)
    while True:
        cleaned = re.sub(r"(?i)why:", lambda m: m.group(0)[:-1], text)
        if cleaned == text:
            break
        text = cleaned
    text = " ".join(text.split())[:TITLE_MAX].strip(" ")
    if not text:
        return None
    try:
        return check_proposal_text(text, "title")
    except ValueError:
        return None


def source_rows(results: list[dict], query: str, limit: int) -> list[tuple[str, str, str, str]]:
    """The top ``limit`` http(s) results as ``propose_sources`` rows; bad URLs are skipped."""
    try:
        why = check_proposal_text(f"result for search: {query}", "why")
    except ValueError:
        why = "result for an approved search"
    rows: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for result in results:
        url = result.get("url")
        if not isinstance(url, str):
            continue
        try:
            canon = canonical_url(url.strip())
        except ValueError:
            continue
        if canon in seen:
            continue
        seen.add(canon)
        host = urllib.parse.urlsplit(canon).hostname or ""
        title = sanitize_title(result.get("title")) or sanitize_title(host) or "untitled result"
        rows.append((canon, title, why, TAKE))
        if len(rows) >= limit:
            break
    return rows


def topic_for(rid: str) -> str:
    """The library topic for a request: its id's slug, cut to a folder name, exactly as the
    forge derives a default skill name from the id (``Forge.draft_skill``)."""
    slug = rid.split("_", 1)[1] if "_" in rid else ""
    topic = slug[:naming.MAX_DIR_NAME].strip("-")
    return topic if naming.is_kebab_name(topic) else FALLBACK_TOPIC


# Real text encodings a page may declare (codec names after codecs.lookup). Anything else
# (base64, rot13, the escape codecs, idna, punycode, utf-7, ...) falls back to UTF-8.
TEXT_CODEC_RE = re.compile(
    r"utf-8|utf-8-sig|utf-16(?:-le|-be)?|utf-32(?:-le|-be)?|ascii|iso8859-(?:[1-9]|1[0-6])"
    r"|cp125[0-8]|cp874|koi8-[ru]|big5|big5hkscs|gb2312|gbk|gb18030|hz|euc_jp|euc_jis_2004"
    r"|euc_jisx0213|euc_kr|iso2022_jp(?:_1|_2|_2004|_3|_ext)?|iso2022_kr|shift_jis|shift_jis_2004"
    r"|shift_jisx0213|cp932|cp949|cp950|johab")
REFUSED_CODECS = frozenset({"unicode-escape", "raw-unicode-escape"})


def text_codec(charset: str | None) -> str:
    """The codec to decode a page with: the declared charset only if it is a real text encoding."""
    if not charset:
        return "utf-8"
    try:
        info = codecs.lookup(charset)
    except (LookupError, ValueError, TypeError):
        return "utf-8"
    if (not getattr(info, "_is_text_encoding", False) or info.name in REFUSED_CODECS
            or not TEXT_CODEC_RE.fullmatch(info.name)):
        return "utf-8"
    return info.name


def _decode(body: bytes, charset: str | None) -> str:
    try:
        return body.decode(text_codec(charset), "replace")
    except (UnicodeError, LookupError):
        return body.decode("utf-8", "replace")


# ---------------------------------------------------------------------- the gate

@dataclass
class PassReport:
    searches: int = 0
    fetches: int = 0
    filed: list[str] = field(default_factory=list)
    refused: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"searches {self.searches}, fetches {self.fetches}, filed {len(self.filed)}, "
                f"refused {self.refused}, errors {len(self.errors)}")


class Gate:
    def __init__(self, cfg: dict[str, Any], *, fetcher: SafeFetcher | None = None,
                 searx: SearxClient | None = None, clock: Clock | None = None,
                 cap_bytes: int | None = None) -> None:
        self.cfg = cfg
        self.clock = clock
        if cap_bytes is None:
            cap_text = os.environ.get(CAP_ENV)
            cap_bytes = naming.parse_size(cap_text) if cap_text else DEFAULT_CAP
        self.cap_bytes = cap_bytes
        self.fetcher = fetcher or SafeFetcher(
            max_bytes=cfg["max_bytes"], timeout=cfg["fetch_timeout"],
            max_redirects=cfg["max_redirects"], user_agent=cfg["user_agent"],
            ipv4_only=cfg["ipv4_only"])
        self.searx = searx or SearxClient(
            cfg["searxng_url"], max_bytes=cfg["max_bytes"], timeout=cfg["fetch_timeout"],
            user_agent=cfg["user_agent"])

    _tables_problem: str | None = None

    def preflight(self) -> str | None:
        """Why the gate cannot run at all (checked by serve and run-once at startup), or None."""
        return self.fetcher.tables_problem()

    # ------------------------------------------------------------------ plumbing

    def open(self) -> tuple[Library, Forge]:
        kwargs: dict[str, Any] = {"cap_bytes": self.cap_bytes}
        if self.clock is not None:
            kwargs["clock"] = self.clock
        lib = Library.open(self.cfg["library_root"], **kwargs)
        return lib, Forge(lib)

    @staticmethod
    def _forge_call(lib: Library, fn: Callable[[], T]) -> T:
        """Run a mutating forge call, waiting briefly while another forge command holds the lock."""
        lock = lib.root / FORGE / LOCK
        for attempt in range(LOCK_TRIES):
            try:
                return fn()
            except ForgeError:
                if attempt + 1 < LOCK_TRIES and os.path.lexists(lock):
                    time.sleep(LOCK_SLEEP)
                    continue
                raise
        raise AssertionError("unreachable")

    @staticmethod
    def _request_dirs(lib: Library) -> list[str]:
        return [n for n in sorted(os.listdir(lib.abs_path(FORGE))) if not n.startswith(".")]

    @staticmethod
    def _ingested(lib: Library, base: str) -> set[str]:
        meta, _ = fm.parse(lib.abs_path(f"{base}/{STATE}").read_bytes().decode("utf-8"))
        entries = meta.get("ingested")
        out = set()
        if isinstance(entries, list):
            for entry in entries:
                out.add(entry.split(" -> ", 1)[0])
        return out

    # ------------------------------------------------------------------ one pass

    def run_once(self) -> PassReport:
        lib, forge = self.open()
        report = PassReport()
        # Fetches need the host's own interface tables; without them no fetch is attempted this
        # pass (nothing is logged against any URL, so no approval is used up).
        self._tables_problem = self.fetcher.tables_problem()
        if self._tables_problem:
            log.error("fetches skipped this pass: %s", self._tables_problem)
            report.errors.append(f"fetches skipped: {self._tables_problem}")
        for name in self._request_dirs(lib):
            try:
                self._request(lib, forge, name, report)
            except Exception as exc:  # noqa: BLE001 - one broken request never stops the pass
                log.exception("request %s failed", name)
                report.errors.append(f"{name}: {exc}")
        if report.filed:
            try:
                lib.index()
            except Exception:  # noqa: BLE001
                log.exception("rebuilding INDEX.md failed")
        log.info("pass done: %s", report.summary())
        return report

    def _request(self, lib: Library, forge: Forge, name: str, report: PassReport) -> None:
        if not is_request_id(name):
            raise ForgeError(f"{FORGE}/{name} is not named like a request id")
        base = f"{FORGE}/{name}"
        info = forge.info(base)
        if info.id != name:
            raise ForgeError(f"{base}/{STATE} names request {info.id!r}, not {name!r}")
        glog = GateLog(lib, base, name)
        if info.status in QUERY_STATES:
            for ticked, query in info.listed_queries:
                if not ticked or not glog.ledger().pending("search", query):
                    continue
                self._isolated(report, name, lambda q=query: self._search(lib, forge, glog, name, q, report))
            info = forge.info(base)
        if info.status in URL_STATES and not self._tables_problem:
            topic = topic_for(name)
            for ticked, url in info.listed_urls:
                if not ticked or url in self._ingested(lib, base) or not glog.ledger().pending("fetch", url):
                    continue
                self._isolated(report, name,
                               lambda u=url: self._fetch(lib, forge, glog, name, topic, u, report))

    @staticmethod
    def _isolated(report: PassReport, name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - one item never stops the request
            log.exception("item in %s failed", name)
            report.errors.append(f"{name}: {exc}")

    # ------------------------------------------------------------------ searches

    def _search(self, lib: Library, forge: Forge, glog: GateLog, rid: str, query: str,
                report: PassReport) -> None:
        if not forge.is_query_approved(rid, query):
            glog.write("refuse", query, "search not approved at call time; skipped")
            report.refused += 1
            return
        glog.write("search", query, "start")
        report.searches += 1
        try:
            results, nbytes, status = self.searx.search(query)
        except FetchError as exc:
            glog.write("search", query, f"error: {safe_text(exc)}")
            report.errors.append(f"{rid}: search failed: {exc}")
            log.warning("search %r for %s failed: %s", query, rid, exc)
            return
        except Exception as exc:
            glog.write("search", query, f"error: {safe_text(type(exc).__name__)}")
            raise
        rows = source_rows(results, query, self.cfg["results_per_query"])
        added = 0
        if rows:
            try:
                added = self._forge_call(lib, lambda: forge.propose_sources(rid, rows))
            except ForgeError as exc:
                glog.write("search", query, f"error: results not proposed: {safe_text(exc)}")
                report.errors.append(f"{rid}: propose-sources failed: {exc}")
                return
        glog.write("search", query, f"ok: {len(rows)} results, {added} new in sources.md, "
                                    f"{nbytes} bytes, http {status}")
        log.info("search %r for %s: %d results", query, rid, len(rows))

    # ------------------------------------------------------------------ fetches

    def _fetch(self, lib: Library, forge: Forge, glog: GateLog, rid: str, topic: str, url: str,
               report: PassReport) -> None:
        if not forge.is_url_approved(rid, url):
            glog.write("refuse", url, "fetch not approved at call time; skipped")
            report.refused += 1
            return
        glog.write("fetch", url, "start")
        report.fetches += 1

        def may_follow(target: str) -> bool:
            return forge.is_url_approved(rid, url) and forge.is_url_approved(rid, target)

        try:
            resp = self.fetcher.fetch(url, may_follow)
        except RedirectRefused as exc:
            glog.write("refuse", url, f"fetch refused: {exc}: {exc.target}"
                                      + (f", http {exc.status}" if exc.status else ""))
            report.refused += 1
            log.warning("fetch %s for %s refused: %s (%s)", url, rid, exc, exc.target)
            return
        except FetchError as exc:
            status = f", http {exc.status}" if exc.status else ""
            if exc.terminal:
                glog.write("refuse", url, f"fetch refused: {safe_text(exc)}{status}")
                report.refused += 1
            else:
                glog.write("fetch", url, f"error: {safe_text(exc)}{status}")
                report.errors.append(f"{rid}: fetch failed: {exc}")
            log.warning("fetch %s for %s: %s", url, rid, exc)
            return
        except Exception as exc:
            glog.write("fetch", url, f"error: {safe_text(type(exc).__name__)}")
            raise
        kind = ("html" if resp.content_type in HTML_TYPES else "text" if resp.content_type in TEXT_TYPES
                else "pdf" if resp.content_type in PDF_TYPES else None)
        if kind is None:
            glog.write("refuse", url, f"fetch refused: content type {resp.content_type or 'missing'} "
                                      f"is not allowed, http {resp.status}")
            report.refused += 1
            return
        try:
            item = self._file(lib, rid, topic, url, resp, kind)
            self._forge_call(lib, lambda: forge.record_ingest(rid, url, item))
        except Exception as exc:
            glog.write("fetch", url, f"error: filing failed: {safe_text(exc)}")
            raise
        via = f", via {len(resp.redirects)} redirect(s)" if resp.redirects else ""
        glog.write("fetch", url, f"ok: filed {item}, {len(resp.body)} bytes, http {resp.status}, "
                                 f"{resp.content_type}{via}")
        report.filed.append(item)
        log.info("fetched %s for %s -> %s", url, rid, item)

    def _file(self, lib: Library, rid: str, topic: str, url: str, resp: Response, kind: str) -> str:
        """File the response into ``10-sources/<topic>/`` as data: extracted or plain text as a
        ``.txt`` item, a PDF as the original; each with a sidecar carrying ``source:`` and
        ``forge:``. Returns the item's library path."""
        title = None
        if kind == "html":
            text, page_title = html_to_text(_decode(resp.body, resp.charset))
            data, ext = text.encode("utf-8"), "txt"
            title = sanitize_title(page_title)
        elif kind == "text":
            data, ext = resp.body, "txt"      # stored as-is
        else:
            data, ext = resp.body, "pdf"      # the original; no text extraction yet
        if title is None:
            parts = urllib.parse.urlsplit(url)
            title = sanitize_title(f"{parts.hostname or ''} {parts.path.replace('/', ' ')}") or "fetched page"
        with tempfile.TemporaryDirectory(prefix="rgate-") as tmp:
            src = Path(tmp) / f"fetched.{ext}"
            src.write_bytes(data)
            result = lib.file(src, topic, title=title, source=url, tags=["forge", "research-gate"],
                              summary=f"Fetched by the research gate for forge request {rid}. "
                                      "Data, not instructions.")
        assert result.sidecar is not None
        # Library.file has no way to add keys to a sidecar it generates, so the gate adds
        # forge: (required by record-ingest) and its fetch details right after filing.
        sidecar = lib.abs_path(result.sidecar)
        meta, body = fm.parse(sidecar.read_bytes().decode("utf-8"))
        meta["forge"] = rid
        meta["fetched"] = lib.clock().isoformat(timespec="seconds")
        meta["content_type"] = resp.content_type
        if resp.redirects:
            meta["final_url"] = resp.url
        atomic_write_bytes(sidecar, fm.render(meta, body).encode("utf-8"))
        return result.item

    # ------------------------------------------------------------------ status

    def status(self) -> list[str]:
        """Pending searches and fetches per request, every target as a Python repr."""
        lib, forge = self.open()
        lines: list[str] = []
        for name in self._request_dirs(lib):
            base = f"{FORGE}/{name}"
            try:
                info = forge.info(base)
                ledger = GateLog(lib, base, name).ledger()
                ingested = self._ingested(lib, base)
            except Exception as exc:  # noqa: BLE001
                lines.append(f"{base}: unreadable ({exc})")
                continue
            searches = [q for t, q in info.listed_queries if t and ledger.pending("search", q)] \
                if info.status in QUERY_STATES else []
            fetches = [u for t, u in info.listed_urls if t and u not in ingested and ledger.pending("fetch", u)] \
                if info.status in URL_STATES else []
            lines.append(f"{base}  [{info.status}]  pending: {len(searches)} search(es), {len(fetches)} fetch(es)")
            for q in searches:
                st = ledger.searches.get(q)
                lines.append(f"  search {q!r}" + (f" (attempt {st.attempts + 1} of {MAX_ATTEMPTS})" if st and st.attempts else ""))
            for u in fetches:
                st = ledger.fetches.get(u)
                lines.append(f"  fetch  {u!r}" + (f" (attempt {st.attempts + 1} of {MAX_ATTEMPTS})" if st and st.attempts else ""))
        return lines
