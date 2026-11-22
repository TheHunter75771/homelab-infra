"""The aiohttp application: guards, identity, the read-only API (§12.15.5) and the static web app."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
from aiohttp import web

from .clients import AgentClient, HelperClient, SourceError, get_json, pinned_list
from .config import PC_LOG_UNITS, Config
from .sampler import GateSampler
from .sse import EventHub
from .summary import SummaryBuilder, agent_logs_view, agent_sudo_view, etag_for

log = logging.getLogger("ai_control.app")

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'"
)
REFUSED_FETCH_SITES = frozenset({"same-site", "cross-site"})
ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})
USERS = ("claude", "localai")
# Always used with fullmatch: `$` would also accept a trailing newline ("30%0A").
SESSION_ID_RE = re.compile(r"[0-9A-Z]{6}|[0-9A-Za-z/._-]{1,64}")
INT_RE = re.compile(r"[0-9]{1,10}")
REPLAY_MAX_BYTES = 262144
MAX_LINE = 2000

# Fields passed through from upstream lists: anything else (e.g. prompt text, should a source ever send it)
# never reaches the browser.
REQUEST_FIELDS = ("ts", "path", "alias", "model", "backend", "status", "duration_ms", "eval_count", "eval_tok_s", "error")
SUDO_FIELDS = ("ts", "user", "runas", "cwd", "command", "event", "ambiguous")
SESSION_FIELDS = ("id", "ts", "runas", "tty", "cwd", "command")
LOCALAI_FIELDS = ("ts", "unit", "comm", "message")

mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("font/woff2", ".woff2")



class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def json_out(data: Any, *, status: int = 200, headers: dict[str, str] | None = None) -> web.Response:
    """JSON that is always valid: NaN/Infinity raise instead of being emitted as bare tokens."""
    return web.json_response(data, status=status, headers=headers, dumps=_strict_dumps)


def _strict_dumps(obj: Any) -> str:
    return json.dumps(obj, allow_nan=False)


def error_response(status: int, message: str, headers: dict[str, str] | None = None) -> web.Response:
    return json_out({"error": message}, status=status, headers=headers)


# ---- query validation ----------------------------------------------------------------------------
Validator = Callable[[str], Any]


def int_param(lo: int, hi: int) -> Validator:
    def check(raw: str) -> int:
        if not INT_RE.fullmatch(raw) or not lo <= int(raw) <= hi:
            raise ValueError(f"must be an integer {lo}-{hi}")
        return int(raw)

    return check


def enum_param(*values: str) -> Validator:
    def check(raw: str) -> str:
        if raw not in values:
            raise ValueError(f"must be one of {', '.join(values)}")
        return raw

    return check


def since_param(raw: str) -> int:
    """Epoch seconds, not in the future (one day of slack)."""
    if not INT_RE.fullmatch(raw) or int(raw) > time.time() + 86400:
        raise ValueError("must be epoch seconds (an integer, not in the future)")
    return int(raw)


def parse_query(request: web.Request, spec: dict[str, tuple[Validator, Any]]) -> dict[str, Any]:
    """Every param must be known and appear once; each is validated. spec: name → (validator, default);
    a default of `REQUIRED` makes the param mandatory."""
    try:
        query = request.query
    except Exception:  # noqa: BLE001 - a query string yarl can't parse
        raise ApiError(400, "malformed query string") from None
    out: dict[str, Any] = {}
    for key in query:
        if key not in spec:
            raise ApiError(400, f"unknown parameter {_safe_name(key)!r}")
        if len(query.getall(key)) > 1:
            raise ApiError(400, f"parameter {key!r} given more than once")
    for name, (validator, default) in spec.items():
        raw = query.get(name)
        if raw is None:
            if default is REQUIRED:
                raise ApiError(400, f"parameter {name!r} is required")
            out[name] = default
            continue
        try:
            out[name] = validator(raw)
        except ValueError as exc:
            raise ApiError(400, f"parameter {name!r} {exc}") from None
    return out


class _Required:
    def __repr__(self) -> str:
        return "REQUIRED"


REQUIRED: Any = _Required()


def _safe_name(value: str) -> str:
    return "".join(c for c in value[:40] if c.isprintable())


def valid_session_id(sid: str) -> bool:
    if not SESSION_ID_RE.fullmatch(sid) or ".." in sid:
        return False
    # Stricter than the helper's regex: no absolute or empty path segments.
    return not sid.startswith("/") and not sid.endswith("/") and "//" not in sid


# ---- identity ------------------------------------------------------------------------------------
def _clean_header(value: str | None) -> str | None:
    if not value:
        return None
    out = "".join(
        c for c in value if unicodedata.category(c)[0] != "C" and not 0xD800 <= ord(c) <= 0xDFFF
    ).strip()[:256]
    return out or None


def identity(request: web.Request) -> dict[str, str | None] | None:
    """Tailscale identity headers, trusted only when the TCP peer is 127.0.0.1 (tailscale serve)."""
    transport = request.transport
    peer = transport.get_extra_info("peername") if transport is not None else None
    if not peer or peer[0] != "127.0.0.1":
        return None
    login = _clean_header(request.headers.get("Tailscale-User-Login"))
    name = _clean_header(request.headers.get("Tailscale-User-Name"))
    if login is None and name is None:
        return None
    return {"login": login, "name": name}


# ---- state ---------------------------------------------------------------------------------------
class State:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.session: aiohttp.ClientSession | None = None
        self.helper = HelperClient(cfg.helper_socket)
        self.agent = AgentClient(cfg.agent_url, cfg.agent_token_file)
        self.sampler = GateSampler(
            cfg.gate_url,
            cfg.gate_samples_path,
            interval=cfg.sample_interval,
            persist_interval=cfg.persist_interval,
            history_s=cfg.history_hours * 3600,
            timeout=cfg.source_timeout,
        )
        self.hub = EventHub(cfg.sse_max_clients, cfg.sse_buffer_limit, cfg.sse_write_timeout, cfg.sse_keepalive)
        self.builder: SummaryBuilder | None = None
        self.tasks: list[asyncio.Task[Any]] = []


CONFIG = web.AppKey("config", Config)
STATE = web.AppKey("state", State)
BACKGROUND = web.AppKey("background", bool)


def _state(request: web.Request) -> State:
    return request.app[STATE]


# ---- middleware + headers --------------------------------------------------------------------------
def _is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


@web.middleware
async def guard_middleware(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
    if request.method != "GET":
        return error_response(405, "method not allowed (read-only)", {"Allow": "GET"})
    if _is_api(request.path):
        site = request.headers.get("Sec-Fetch-Site")
        if site is not None and (site in REFUSED_FETCH_SITES or site not in ALLOWED_FETCH_SITES):
            return error_response(403, "cross-site request refused")
    try:
        return await handler(request)
    except ApiError as exc:
        return error_response(exc.status, exc.message)
    except web.HTTPException as exc:
        if exc.status < 400:
            raise
        return error_response(exc.status, exc.reason or "error")
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - never a traceback to the client
        log.exception("unhandled error on %s", _safe_name(request.path))
        return error_response(500, "internal error")


def security_headers(path: str, headers: Any) -> None:
    headers["Content-Security-Policy"] = CSP
    headers["X-Content-Type-Options"] = "nosniff"
    headers["Referrer-Policy"] = "no-referrer"
    headers["X-Frame-Options"] = "DENY"
    if _is_api(path) or path == "/healthz":
        headers["Cache-Control"] = "no-store"
    elif "Cache-Control" not in headers:
        headers["Cache-Control"] = "no-cache"


async def _on_prepare(request: web.Request, response: web.StreamResponse) -> None:
    security_headers(request.path, response.headers)


# ---- upstream helpers ----------------------------------------------------------------------------
def _helper_status(exc: SourceError) -> int:
    # Helper X's error codes (apps/ai-control/helper/README.md).
    code = exc.code or ""
    if code == "not-found":
        return 404
    if code in ("bad-request", "not-allowed"):
        return 400
    return 502


async def _helper(request: web.Request, op: str, **args: Any) -> Any:
    st = _state(request)
    try:
        return await st.helper.call(op, st.cfg.request_timeout, **args)
    except SourceError as exc:
        if exc.code == "damaged":
            raise ApiError(422, "this session log is damaged") from None
        raise ApiError(_helper_status(exc) if exc.code else 502, str(exc)) from None


async def _helper_list(request: web.Request, op: str, key: str, **args: Any) -> list[dict[str, Any]]:
    """A helper op whose pinned reply is {key: [objects]}; any other shape is a 502."""
    data = await _helper(request, op, **args)
    try:
        return pinned_list(data, key, op)
    except SourceError as exc:
        raise ApiError(502, str(exc)) from None


async def _agent(request: web.Request, path: str, params: dict[str, str], view: Callable[[Any], Any]) -> Any:
    st = _state(request)
    assert st.session is not None
    try:
        data = await st.agent.get(st.session, path, st.cfg.request_timeout, params=params)
    except SourceError as exc:
        status = 400 if exc.status == 400 else 404 if exc.status == 404 else 502
        raise ApiError(status, f"host agent: {exc}") from None
    try:
        return view(data)
    except SourceError as exc:
        raise ApiError(502, str(exc)) from None


def _fields(items: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    out = []
    for item in items:
        row = {}
        for f in fields:
            v = item.get(f)
            row[f] = v[:MAX_LINE] if isinstance(v, str) else v if isinstance(v, (int, float, bool)) or v is None else str(v)[:MAX_LINE]
        out.append(row)
    return out


# ---- handlers ------------------------------------------------------------------------------------
async def healthz(_request: web.Request) -> web.Response:
    return json_out({"ok": True})


async def summary(request: web.Request) -> web.StreamResponse:
    parse_query(request, {})
    st = _state(request)
    assert st.builder is not None
    built = await st.builder.get()
    ident = identity(request)
    etag = etag_for(built, ident)
    inm = request.headers.get("If-None-Match")
    if inm and (inm.strip() == "*" or etag in [t.strip().removeprefix("W/") for t in inm.split(",")]):
        return web.Response(status=304, headers={"ETag": etag})
    data = dict(built.data)
    data["server"] = {**built.data["server"], "identity": ident}
    return json_out(data, headers={"ETag": etag})


async def gate_history(request: web.Request) -> web.Response:
    q = parse_query(request, {"minutes": (int_param(30, 1440), 30)})
    return json_out(_state(request).sampler.history(q["minutes"]))


async def requests_(request: web.Request) -> web.Response:
    q = parse_query(request, {"limit": (int_param(1, 200), 50)})
    st = _state(request)
    assert st.session is not None
    try:
        _s, data, _h = await get_json(st.session, f"{st.cfg.broker_url}/broker/recent", st.cfg.request_timeout)
    except SourceError as exc:
        raise ApiError(502, f"broker: {exc}") from None
    # Pinned (Lead, 2026-10-26): a bare array of objects, newest first. `alias` may be null.
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        raise ApiError(502, "broker recent reply doesn't match its pinned shape")
    return json_out(_fields(data[: q["limit"]], REQUEST_FIELDS))


async def logs(request: web.Request) -> web.Response:
    st = _state(request)

    def source(raw: str) -> str:
        if raw in {sid for sid, _label in st.cfg.log_sources}:
            return raw
        raise ValueError("is not an allowed log source")

    q = parse_query(request, {"source": (source, REQUIRED), "lines": (int_param(1, 1000), 200)})
    if q["source"] in PC_LOG_UNITS:
        lines = await _agent(request, "/v1/logs", {"unit": PC_LOG_UNITS[q["source"]], "lines": str(q["lines"])},
                             agent_logs_view)
        return json_out([line[:MAX_LINE] for line in lines])
    entries = await _helper_list(request, "logs", "lines", source=q["source"], lines=q["lines"])
    if not all(isinstance(e.get("text"), str) for e in entries):
        raise ApiError(502, "helper logs reply doesn't match its pinned shape")
    return json_out([e["text"][:MAX_LINE] for e in entries])


async def log_sources(request: web.Request) -> web.Response:
    parse_query(request, {})
    return json_out([{"id": sid, "label": label} for sid, label in _state(request).cfg.log_sources])


async def audit_sudo(request: web.Request) -> web.Response:
    q = parse_query(
        request,
        {
            "host": (enum_param("aibox", "pc"), "aibox"),
            "user": (enum_param(*USERS), REQUIRED),
            "since": (since_param, None),
            "limit": (int_param(1, 500), 100),
        },
    )
    if q["host"] == "pc":
        params = {"user": q["user"], "limit": str(q["limit"])}
        if q["since"] is not None:
            params["since"] = str(q["since"])
        items = await _agent(request, "/v1/sudo", params, agent_sudo_view)
    else:
        args: dict[str, Any] = {"user": q["user"], "limit": q["limit"]}
        if q["since"] is not None:
            args["since"] = q["since"]
        items = await _helper_list(request, "sudo-commands", "commands", **args)
        if not all(type(e.get("ambiguous")) is bool for e in items):
            raise ApiError(502, "helper sudo-commands reply doesn't match its pinned shape")
    return json_out(_fields(items[: q["limit"]], SUDO_FIELDS))


async def audit_sessions(request: web.Request) -> web.Response:
    q = parse_query(request, {"user": (enum_param(*USERS), REQUIRED)})
    data = await _helper(request, "sudo-sessions", user=q["user"], limit=200)
    try:
        items = pinned_list(data, "sessions", "sudo-sessions")
    except SourceError as exc:
        raise ApiError(502, str(exc)) from None
    skipped = data.get("skipped")
    if not isinstance(skipped, int) or isinstance(skipped, bool) or skipped < 0:
        raise ApiError(502, "helper sudo-sessions reply doesn't match its pinned shape")
    # `skipped`: sessions among the newest whose metadata couldn't be parsed (left out by the helper).
    return json_out({"sessions": _fields(items, SESSION_FIELDS), "skipped": skipped})


def session_id_param(raw: str) -> str:
    if not valid_session_id(raw):
        raise ValueError("is not a valid session id")
    return raw


async def audit_replay(request: web.Request) -> web.Response:
    q = parse_query(
        request,
        {
            "user": (enum_param(*USERS), REQUIRED),
            "id": (session_id_param, REQUIRED),
            "output": (enum_param("0", "1"), "0"),
        },
    )
    sid = q["id"]
    want_output = q["output"] == "1"
    data = await _helper(request, "sudo-replay", user=q["user"], id=sid, max_bytes=REPLAY_MAX_BYTES, output=want_output)
    if not isinstance(data, dict) or not isinstance(data.get("id"), str):
        raise ApiError(502, "helper sudo-replay reply doesn't match its pinned shape")
    # X's object as reported, incl. its `id` (the path form), never the id the client sent.
    reply = {k: (v[:REPLAY_MAX_BYTES] if isinstance(v, str) else v) for k, v in data.items()}
    if not want_output:
        reply["output"] = None  # belt and braces: the helper already sends null
    reply.setdefault("user", q["user"])
    return json_out(reply)


async def audit_localai(request: web.Request) -> web.Response:
    q = parse_query(request, {"since": (since_param, None), "limit": (int_param(1, 500), 100)})
    args: dict[str, Any] = {"limit": q["limit"]}
    if q["since"] is not None:
        args["since"] = q["since"]
    items = await _helper_list(request, "localai-activity", "entries", **args)
    return json_out(_fields(items[: q["limit"]], LOCALAI_FIELDS))


async def events(request: web.Request) -> web.StreamResponse:
    # No await before the hub reserves the slot (inside handle()).
    if request.query_string:
        return error_response(400, "no parameters accepted")
    return await _state(request).hub.handle(request)


async def api_not_found(_request: web.Request) -> web.Response:
    return error_response(404, "no such endpoint")


async def static(request: web.Request) -> web.StreamResponse:
    root = Path(_state(request).cfg.static_dir).resolve()
    rel = request.match_info.get("tail", "")
    parts = [p for p in rel.split("/") if p]
    if any(p == ".." for p in parts) or "\\" in rel or "\0" in rel:
        raise ApiError(400, "bad path")
    if parts and not any(p.startswith(".") for p in parts):
        target = root.joinpath(*parts).resolve()
        if target.is_relative_to(root) and target.is_file():
            return web.FileResponse(target)
    index = root / "index.html"
    if index.is_file():
        return web.FileResponse(index, headers={"Cache-Control": "no-cache"})
    raise ApiError(404, "web app not installed")


# ---- lifecycle -----------------------------------------------------------------------------------
async def _startup(app: web.Application) -> None:
    st = app[STATE]
    st.session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=32, limit_per_host=8),
        trust_env=False,
        auto_decompress=True,
    )
    loaded = await asyncio.to_thread(st.sampler.load)
    log.info("loaded %d gate samples", loaded)
    st.builder = SummaryBuilder(st.cfg, st.session, st.helper, st.agent, st.sampler)
    st.builder.on_change.append(lambda built: st.hub.notify(built.core_hash))
    if app.get(BACKGROUND, True):
        st.tasks.append(asyncio.create_task(st.sampler.run(st.session), name="gate-sampler"))
        st.tasks.append(asyncio.create_task(st.builder.run(), name="summary"))


async def _cleanup(app: web.Application) -> None:
    st = app[STATE]
    for task in st.tasks:
        task.cancel()
    for task in st.tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    st.tasks.clear()
    await st.sampler.persist_async()
    if st.session is not None:
        await st.session.close()


def make_app(cfg: Config, *, background: bool = True) -> web.Application:
    app = web.Application(middlewares=[guard_middleware], client_max_size=8192)
    app[CONFIG] = cfg
    app[STATE] = State(cfg)
    app[BACKGROUND] = background
    app.on_response_prepare.append(_on_prepare)
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    r = app.router
    for path, handler in (
        ("/healthz", healthz),
        ("/api/summary", summary),
        ("/api/gate/history", gate_history),
        ("/api/requests", requests_),
        ("/api/logs", logs),
        ("/api/audit/sudo", audit_sudo),
        ("/api/audit/sessions", audit_sessions),
        ("/api/audit/replay", audit_replay),
        ("/api/logs/sources", log_sources),
        ("/api/audit/localai", audit_localai),
        ("/api/events", events),
        ("/api", api_not_found),
        ("/api/{tail:.*}", api_not_found),
        ("/{tail:.*}", static),
    ):
        r.add_route("GET", path, handler)
    return app
