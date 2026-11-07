"""What each endpoint does, without HTTP (CONTRACTS §12.25.2, row F).

Every call opens the library afresh (an unmounted share is noticed at once: 503), goes through
the forge's own API and checks, and raises ``ApiError`` with a status and a short, clean message
for anything refused. Bodies are already strict-parsed JSON objects here; this module checks
their exact shape and caps.

Writes (a new request + its proposals, ticks) are serialised inside the process by
one lock; across processes (the research gate, Noah's CLI) the forge's own lock file decides,
and a busy forge is retried briefly, then answered with 503.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import stat
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

from forge import Forge, ForgeError, checklist, is_request_id
from forge.core import FORGE, SEARCHES, SHELF, SOURCES_FILE, STATE
from library import Library, LibraryError, PathRefused
from library import frontmatter as fm
from library.core import SOURCES

from . import text as T
from .auth import same_user
from .ticks import (
    ALREADY,
    NOT_FOUND,
    RESULTS,
    TICKED,
    ForgeTicks,
    Ticker,
    TickProtocolError,
    TickUnavailable,
)

log = logging.getLogger("forge_api")

# Input caps (CONTRACTS §12.25.2: 10 queries x 200 chars, 30 URLs; the rest are this module's).
MAX_QUERIES = 10
MAX_QUERY_CHARS = 200
MAX_URLS = 30
MAX_URL_CHARS = checklist.MAX_URL
MAX_TITLE_CHARS = 120
MAX_WHY_CHARS = 2000            # the request's why (becomes request.md)
MAX_QUERY_WHY_CHARS = 300       # each proposed query's why
MAX_USER_CHARS = 64            # the forge's `by` rule: 1-64 of A-Z a-z 0-9 . _ @ -
MAX_LISTED = 200                # GET /requests returns at most this many (newest first)
ITEM_READ_BYTES = 256 * 1024    # read at most this much of an item before cleaning and clipping

LOCK_BUSY = "another forge command is running"
LOCK_TRIES = 20
LOCK_SLEEP = 0.25
WRITE_WAIT = 5.0     # longest wait for this process's own write lock (Reviewer Q, Q-F2), then 503

PROVENANCE = ("\n\n(Proposed from an Open WebUI chat through forge-api: these are the assistant's "
              "words, not Noah's verbatim request.)\n")
_ITEM_PATH_RE = re.compile(r"10-sources/[a-z0-9]+(?:-[a-z0-9]+)*/[A-Za-z0-9][A-Za-z0-9._-]*")
_TITLE_RE = re.compile(r" \u2014 title: (?P<title>.*?) \u2014 why: ")

R = TypeVar("R")


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


# ---------------------------------------------------------------------- body shape

def _fields(body: Any, required: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError(400, "the body must be a JSON object")
    unknown = sorted(set(body) - set(required))
    if unknown:
        raise ApiError(400, f"unknown field(s): {T.short(', '.join(unknown), 100)} "
                            f"(allowed: {', '.join(required)})")
    missing = [k for k in required if k not in body]
    if missing:
        raise ApiError(400, f"missing field(s): {', '.join(missing)}")
    return body


def _string(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ApiError(400, f"{what} must be a string")
    if len(value) > limit:
        raise ApiError(400, f"{what} is longer than {limit} characters")
    return value


def _string_list(value: Any, what: str, max_items: int, max_chars: int) -> list[str]:
    if not isinstance(value, list):
        raise ApiError(400, f"{what} must be a list")
    if not 1 <= len(value) <= max_items:
        raise ApiError(400, f"{what} must have 1 to {max_items} entries")
    return [_string(v, f"{what}[{i}]", max_chars) for i, v in enumerate(value, start=1)]


def _checked(fn: Callable[[str], str], value: str, where: str) -> str:
    """Run one of the forge's own text checks; its refusal becomes a 400."""
    try:
        return fn(value)
    except ValueError as exc:
        raise ApiError(400, f"{where}: {T.message(exc)}") from None


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    return [i for i in items if not (i in seen or seen.add(i))]


# ---------------------------------------------------------------------- the service

class Service:
    def __init__(self, cfg: dict[str, Any], *,
                 ticker_factory: Callable[[Forge], Ticker] = ForgeTicks,
                 library_kwargs: dict[str, Any] | None = None,
                 lock_tries: int = LOCK_TRIES, lock_sleep: float = LOCK_SLEEP,
                 write_wait: float = WRITE_WAIT) -> None:
        self.cfg = cfg
        self.ticker_factory = ticker_factory
        self.library_kwargs = dict(library_kwargs or {})
        self.lock_tries = lock_tries
        self.lock_sleep = lock_sleep
        self._write_lock = threading.Lock()
        self.write_wait = write_wait

    # ------------------------------------------------------------------ plumbing

    def _open(self) -> tuple[Library, Forge]:
        try:
            lib = Library.open(self.cfg["library_root"], **self.library_kwargs)
        except (LibraryError, OSError) as exc:
            log.error("library unavailable: %s", exc)
            raise ApiError(503, "the AI library is not available right now") from None
        return lib, Forge(lib)

    @contextlib.contextmanager
    def _writing(self) -> Iterator[None]:
        """This process's write lock, waited for at most ``write_wait`` seconds (then 503 and the
        forge is never called), so a write can't start long after its client gave up. Once a
        write has started it runs to the end: a client cut off by the connection deadline may
        still find its tick applied (check GET /requests/{rid})."""
        if not self._write_lock.acquire(timeout=self.write_wait):
            log.warning("write lock still held after %.1f s; refused", self.write_wait)
            raise ApiError(503, "the forge is busy; try again in a moment")
        try:
            yield
        finally:
            self._write_lock.release()

    def _forge_call(self, lib: Library, fn: Callable[[], R]) -> R:
        """A mutating forge call; waits briefly while another process holds the forge lock."""
        lock = os.path.join(lib.root, FORGE, ".forge-lock")
        for attempt in range(self.lock_tries):
            try:
                return fn()
            except ForgeError as exc:
                if str(exc).startswith(LOCK_BUSY):
                    if attempt + 1 < self.lock_tries:
                        time.sleep(self.lock_sleep)
                        continue
                    log.warning("forge lock still held after %d tries (%s)", self.lock_tries, lock)
                    raise ApiError(503, "the forge is busy; try again in a moment") from None
                raise
        raise AssertionError("unreachable")

    @staticmethod
    def _rid(rid: str) -> str:
        if not is_request_id(rid):
            raise ApiError(400, "not a request id (YYYY-MM-DD_kebab-title)")
        return rid

    @staticmethod
    def _read_text(lib: Library, rel: str) -> str:
        """A forge/library file as text: confined, no symlinks, regular files only."""
        path = lib.abs_path(rel)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise PathRefused(f"{rel} is not a regular file")
            data = fh.read()
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise LibraryError(f"{rel} is not UTF-8 text") from None

    def _folder(self, lib: Library, forge: Forge, rid: str) -> str:
        """The request's folder (35-forge/<rid> or its 95-shelf/<skill> folder); 404 if none."""
        try:
            return forge.request_info(rid).folder
        except ForgeError:
            raise ApiError(404, "no such request") from None

    def _active(self, lib: Library, forge: Forge, rid: str) -> str:
        folder = self._folder(lib, forge, rid)
        if folder != f"{FORGE}/{rid}":
            raise ApiError(409, "the request is shelved; nothing in it can be proposed or approved")
        return folder

    # ------------------------------------------------------------------ POST /requests

    def create_request(self, body: Any) -> dict[str, Any]:
        b = _fields(body, ("title", "why", "queries"))
        title = _checked(lambda v: checklist.check_ascii(v, "title"),
                         _string(b["title"], "title", MAX_TITLE_CHARS), "title")
        why = _checked(checklist.check_why, _string(b["why"], "why", MAX_WHY_CHARS), "why")
        raw_queries = b["queries"]
        if not isinstance(raw_queries, list) or not 1 <= len(raw_queries) <= MAX_QUERIES:
            raise ApiError(400, f"queries must be a list of 1 to {MAX_QUERIES} objects")
        rows: list[tuple[str, str]] = []
        for i, entry in enumerate(raw_queries, start=1):
            if not isinstance(entry, dict):
                raise ApiError(400, f"queries[{i}] must be an object with query and why")
            e = _fields(entry, ("query", "why"))
            q = _checked(checklist.check_query, _string(e["query"], f"queries[{i}].query", MAX_QUERY_CHARS),
                         f"queries[{i}].query")
            w = _checked(checklist.check_why, _string(e["why"], f"queries[{i}].why", MAX_QUERY_WHY_CHARS),
                         f"queries[{i}].why")
            rows.append((q, w))
        lib, forge = self._open()
        with self._writing():
            try:
                rid = self._forge_call(lib, lambda: forge.new(title, why + PROVENANCE))
            except ForgeError as exc:
                raise ApiError(409, f"refused by the forge: {T.message(exc)}") from None
            try:
                added = self._forge_call(lib, lambda: forge.propose_searches(rid, rows))
            except ForgeError as exc:
                log.error("request %s created but its searches were refused: %s", rid, exc)
                raise ApiError(409, f"request created, but the forge refused its searches: {T.message(exc)}",
                               rid=rid) from None
        log.info("created request %s with %d proposed search(es)", rid, added)
        return {"rid": rid, "proposed": added, "status": forge.request_info(rid).status}

    # ------------------------------------------------------------------ GET /requests

    def list_requests(self) -> dict[str, Any]:
        _, forge = self._open()
        infos, problems = forge.all_requests()
        if problems:
            log.warning("%d request folder(s) unreadable: %s", len(problems), "; ".join(problems)[:500])
        infos.sort(key=lambda i: i.id, reverse=True)
        out = []
        for info in infos[:MAX_LISTED]:
            out.append({
                "rid": info.id if is_request_id(info.id) else None,
                "where": SHELF if info.folder.startswith(f"{SHELF}/") else FORGE,
                "status": info.status,
                "title": T.short(info.title, MAX_TITLE_CHARS),
                "searches": {"approved": info.queries[0], "listed": info.queries[1]},
                "sources": {"approved": info.sources[0], "listed": info.sources[1]},
                "ingested": info.ingested,
            })
        return {"requests": out, "total": len(infos), "unreadable": len(problems)}

    # ------------------------------------------------------------------ GET /requests/{rid}

    def _ingested(self, lib: Library, folder: str) -> list[tuple[str, str]]:
        meta, _ = fm.parse(self._read_text(lib, f"{folder}/{STATE}"))
        entries = meta.get("ingested")
        out = []
        for entry in entries if isinstance(entries, list) else []:
            url, sep, path = str(entry).partition(" -> ")
            out.append((url, path) if sep else (url, ""))
        return out

    def get_request(self, rid: str) -> dict[str, Any]:
        self._rid(rid)
        lib, forge = self._open()
        folder = self._folder(lib, forge, rid)
        info = forge.info(folder)
        searches, bad_s = checklist.parse_searches(self._read_text(lib, f"{folder}/{SEARCHES}"))
        try:
            sources, bad_u = checklist.parse_sources(self._read_text(lib, f"{folder}/{SOURCES_FILE}"))
        except FileNotFoundError:
            sources, bad_u = [], []
        out_s = [{"n": n, "query": q.query, "why": T.short(q.why), "approved": q.checked}
                 for n, q in enumerate(searches, start=1)]
        out_u = []
        for n, s in enumerate(sources, start=1):
            m = _TITLE_RE.search(s.text)
            out_u.append({"n": n, "url": s.url, "domain": urllib.parse.urlsplit(s.url).hostname or "",
                          "title": T.short(m.group("title")) if m else None, "approved": s.checked})
        items = [{"n": n, "url": url, "path": path}
                 for n, (url, path) in enumerate(self._ingested(lib, folder), start=1)]
        return {
            "rid": rid,
            "where": SHELF if folder.startswith(f"{SHELF}/") else FORGE,
            "status": info.status,
            "title": T.short(info.title, MAX_TITLE_CHARS),
            "searches": out_s,
            "sources": out_u,
            "ingested": items,
            "pending_searches": sum(1 for q in searches if not q.checked),
            "pending_sources": sum(1 for s in sources if not s.checked),
            "malformed_lines": len(bad_s) + len(bad_u),
        }

    # ------------------------------------------------------------------ ticks

    def tick(self, kind: str, rid: str, body: Any) -> dict[str, Any]:
        """kind: "searches" or "sources"."""
        self._rid(rid)
        field = "queries" if kind == "searches" else "urls"
        b = _fields(body, (field, "user"))
        user = b["user"]
        if not isinstance(user, str) or len(user) > MAX_USER_CHARS or \
                not same_user(user, self.cfg["approver_user_id"]):
            log.warning("tick-%s on %s refused: user is not the approver", kind, rid)
            raise ApiError(403, "only the approver may approve")
        if kind == "searches":
            raw = _string_list(b[field], field, MAX_QUERIES, MAX_QUERY_CHARS)
            items = [_checked(checklist.check_query, v, f"queries[{i}]") for i, v in enumerate(raw, start=1)]
        else:
            raw = _string_list(b[field], field, MAX_URLS, MAX_URL_CHARS)
            items = [_checked(checklist.canonical_url, v.strip(), f"urls[{i}]") for i, v in enumerate(raw, start=1)]
        items = _dedupe(items)
        lib, forge = self._open()
        with self._writing():
            self._active(lib, forge, rid)
            ticker = self.ticker_factory(forge)
            call = ticker.tick_searches if kind == "searches" else ticker.tick_sources
            try:
                # The forge ticks under its own lock and writes the approval audit line
                # (approve-chat by=<id>) into state.md itself; this service writes none.
                results = self._forge_call(lib, lambda: call(rid, list(items), by=user))
            except TickUnavailable as exc:
                log.error("%s", exc)
                raise ApiError(501, "approving from chat is not available (the installed forge is too old)") from None
            except TickProtocolError as exc:
                log.error("tick-%s on %s: unexpected forge answer: %s", kind, rid, exc)
                raise ApiError(500, "internal error") from None
            except ForgeError as exc:
                raise ApiError(409, f"refused by the forge: {T.message(exc)}") from None
        counts = {r: sum(1 for _, r2, _ in results if r2 == r) for r in RESULTS}
        log.info("tick-%s on %s by the approver: %d ticked, %d already, %d not found", kind, rid,
                 counts[TICKED], counts[ALREADY], counts[NOT_FOUND])
        key = "query" if kind == "searches" else "url"
        out = []
        for item, outcome, reason in results:
            entry = {key: item, "result": outcome}
            if reason:
                entry["reason"] = T.short(reason)
            out.append(entry)
        return {"rid": rid, "results": out, "counts": counts}

    # ------------------------------------------------------------------ GET /items/{rid}/{n}

    def get_item(self, rid: str, n: int) -> dict[str, Any]:
        """The n-th ingested item of a request, found only through the forge's own record
        (``record_ingest`` -> state.md ``ingested:``) and cross-checked against the item's
        header (``source:`` = the URL, ``forge:`` = the request). Nothing from the request
        path is ever used as a file path."""
        self._rid(rid)
        lib, forge = self._open()
        folder = self._folder(lib, forge, rid)
        entries = self._ingested(lib, folder)
        if not 1 <= n <= len(entries):
            raise ApiError(404, f"no item {n} (this request has {len(entries)} ingested item(s))")
        url, rel = entries[n - 1]
        try:
            canon = checklist.canonical_url(url)
        except ValueError:
            canon = None
        parts = rel.split("/")
        if canon != url or not _ITEM_PATH_RE.fullmatch(rel) or len(parts) != 3 or parts[0] != SOURCES \
                or any(p in ("", ".", "..") for p in parts):
            log.error("request %s item %d: refused record %r", rid, n, f"{url} -> {rel}")
            raise ApiError(409, "the record for this item is not a 10-sources item; not read")
        header_rel = rel if rel.endswith(".md") else f"{rel}.md"
        try:
            meta, _ = fm.parse(self._read_text(lib, header_rel))
            filed = checklist.canonical_url(str(meta.get("source", "")))
        except (LibraryError, OSError, ValueError, fm.FrontMatterError) as exc:
            log.error("request %s item %d (%s): header unreadable: %s", rid, n, rel, exc)
            raise ApiError(409, "the item's header is missing or unreadable; not read") from None
        if filed != canon or meta.get("forge") != rid:
            log.error("request %s item %d (%s): header does not match the record", rid, n, rel)
            raise ApiError(409, "the item's header does not match the forge's record; not read")
        out = {"rid": rid, "n": n, "url": canon, "path": rel}
        if rel.lower().endswith(".pdf"):
            return {**out, "kind": "pdf", "text": "", "truncated": False,
                    "note": "stored as the original PDF; no text extraction yet"}
        try:
            data, size = self._read_item(lib, rel)
        except (LibraryError, OSError) as exc:
            log.error("request %s item %d (%s): unreadable: %s", rid, n, rel, exc)
            raise ApiError(409, "the item is missing or not a regular file") from None
        cleaned = T.strip_controls(data.decode("utf-8", "replace"))
        text, cut = T.clip_utf8(cleaned, T.ITEM_MAX_BYTES)
        return {**out, "kind": "text", "text": text, "truncated": cut or size > len(data),
                "bytes": size}

    @staticmethod
    def _read_item(lib: Library, rel: str) -> tuple[bytes, int]:
        path = lib.abs_path(rel)  # confined to the root, every component checked for symlinks
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise PathRefused(f"{rel} is not a regular file")
            return fh.read(ITEM_READ_BYTES), st.st_size
