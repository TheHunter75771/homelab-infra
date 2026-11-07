"""The aiohttp application: identity, guards, the API, the static PWA (module MC2's files) and the alert loop.

Identity: every request except /healthz must come from 127.0.0.1 (tailscale serve) and carry a
`Tailscale-User-Login` equal to `allowed_login` (case-insensitive). Anything else is a 403. Any local process
can set that header, which is why the server binds 127.0.0.1 only and the unit's IP filter is tight.

The one exception is POST /api/notify (B2, notify.py): senders are tagged servers with no login, so that route
and method alone authenticate by bearer token in the handler instead. It still needs the 127.0.0.1 peer.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import mimetypes
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from . import alerts as alerts_mod
from . import briefing as briefing_mod
from . import cards as cards_mod
from . import notify as notify_mod
from .config import Config
from .net import make_session, strict_json
from .push import (
    PushError,
    Sender,
    SubscriptionStore,
    fits,
    load_vapid,
    make_pywebpush_sender,
    send_all,
    subscriptions_path,
    validate_subscription,
)
from .sources import Collector

log = logging.getLogger("mission_control.app")

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
       "worker-src 'self'; manifest-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})
NOTIFY_PATH = "/api/notify"
POST_PATHS = frozenset({"/api/push/subscribe", "/api/push/unsubscribe", "/api/push/test", NOTIFY_PATH})
MAX_BODY = 8192
NOTIFY_TICK_S = 60.0  # how often the held queue is checked for the end of quiet hours
FLUSH_RETRY_FIRST_S, FLUSH_RETRY_MAX_S = 60.0, 3600.0  # an undelivered summary: 1, 2, 4 ... min, then hourly
TEST_MESSAGE = {"title": "Mission Control", "body": "Notifications work on this device.", "tag": "mc-test", "url": "/"}

mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("image/svg+xml", ".svg")


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def json_out(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> web.Response:
    return web.json_response(data, status=status, headers=headers, dumps=lambda o: json.dumps(o, allow_nan=False))


def error_response(status: int, message: str, headers: dict[str, str] | None = None) -> web.Response:
    return json_out({"error": message}, status, headers)


def _local_peer(request: web.Request) -> bool:
    transport = request.transport
    peer = transport.get_extra_info("peername") if transport is not None else None
    return bool(peer) and peer[0] == "127.0.0.1"


def is_allowed(request: web.Request, login: str) -> bool:
    """Peer 127.0.0.1, exactly one Tailscale-User-Login header, byte-for-byte equal to `login`: no trimming, no
    case folding, no Unicode normalisation (Reviewer Z-10)."""
    if not _local_peer(request):
        return False
    values = request.headers.getall("Tailscale-User-Login", [])
    if len(values) != 1:
        return False
    try:
        presented = values[0].encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(presented, login.encode("utf-8"))


# ---- state ---------------------------------------------------------------------------------------------------
class State:
    def __init__(self, cfg: Config, *, sender: Sender | None = None, clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.clock = clock
        self.session: aiohttp.ClientSession | None = None
        self.collector: Collector | None = None
        self.subs = SubscriptionStore(subscriptions_path(cfg), cfg.max_subscriptions, clock)
        self.alert_store = alerts_mod.AlertStore(os.path.join(cfg.state_dir, "alerts.json"))
        self.restart_store = alerts_mod.RestartStore(os.path.join(cfg.state_dir, "restarts.json"))
        self.notify_store = notify_mod.NotifyStore(os.path.join(cfg.state_dir, "notify.json"))
        self.notify_lock = asyncio.Lock()  # one read-modify-write of notify.json at a time
        self.flush_failures = 0  # summaries in a row that weren't delivered (in memory: a restart tries at once)
        self.flush_retry_at = 0.0
        self.vapid_public_key: str | None = None
        self.sender = sender
        self.tasks: list[asyncio.Task[Any]] = []

    def load_push(self) -> None:
        try:
            vapid = load_vapid(self.cfg.vapid_file)
        except PushError as exc:
            log.error("push disabled: %s", exc)
            return
        self.vapid_public_key = vapid["publicKey"]
        if self.sender is None:
            try:
                self.sender = make_pywebpush_sender(vapid, self.cfg)
            except ImportError:
                log.error("push disabled: pywebpush is not installed")

    async def alert_round(self) -> list[alerts_mod.Alert]:
        """One alert evaluation: collect (cached), decide what to push, push it, save the state."""
        assert self.collector is not None
        now = self.clock()
        snap = await self.collector.snapshot(max_age=min(self.cfg.cache_ttl_s, self.cfg.alert_interval_s / 2))
        old_hist = self.restart_store.load(now)
        hist = alerts_mod.update_restarts(old_hist, snap, self.cfg, now)
        if hist != old_hist:
            self.restart_store.save(hist)
        active, held = alerts_mod.evaluate(snap, self.cfg, now, hist)
        state = self.alert_store.load(now)
        to_send, nxt = alerts_mod.plan(state, active, held, now, self.cfg.alert_repeat_hours * 3600)
        for aid, e in nxt.items():
            before = state.get(aid, {}).get("phase")
            if e["phase"] == "active" and before != "active":
                log.info("alert raised: %s (%s)", aid, e["level"])
            elif e["phase"] == "clearing" and before == "active":
                log.info("alert clearing: %s", aid)
        for aid in set(state) - set(nxt):
            log.info("alert forgotten: %s", aid)
        if to_send:
            if self.sender is None:
                log.warning("%d alert(s) to push, but push is disabled", len(to_send))
            else:
                result = await send_all(self.subs, self.sender, briefing_mod.alert_push(to_send, self.cfg.lockscreen_detail))
                if result["delivered"] or not result["devices"]:
                    alerts_mod.mark_sent(nxt, to_send, now)
                log.info("alert push: %d alert(s), delivered to %d/%d device(s), %d pruned", len(to_send),
                         result["delivered"], result["devices"], result["pruned"])
        if nxt != state:
            self.alert_store.save(nxt)
        return to_send

    async def alert_loop(self) -> None:
        while True:
            try:
                await self.alert_round()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("alert round failed")
            await asyncio.sleep(self.cfg.alert_interval_s)

    async def notify_accept(self, name: str, msg: notify_mod.Message) -> tuple[str, int | None]:
        """A checked message from sender `name`: dedupe, rate, quiet hours, else send (notify.py). Returns the
        outcome (deduped | rate-limited | held | sent) and, when rate-limited, the Retry-After seconds.

        The rate and dedupe entries are written before the push goes out, so a second copy arriving meanwhile is
        deduped; if the push then reached no device (while devices exist), the dedupe entry is taken back so the
        sender's retry isn't swallowed."""
        sender = self.sender
        assert sender is not None
        key = notify_mod.dedupe_key(name, msg)
        async with self.notify_lock:
            now = self.clock()
            state = notify_mod.prune(self.notify_store.load(now), now, self.cfg.notify_dedupe_seconds)
            if key in state["dedupe"]:
                log.info("notify: %s %s: deduped", name, msg.tag)
                return "deduped", None
            times = state["rate"].get(name, [])
            if len(times) >= self.cfg.notify_rate_per_hour:
                log.warning("notify: %s %s: rate limited (%d in the last hour)", name, msg.tag, len(times))
                return "rate-limited", notify_mod.retry_after(times, now)
            state["rate"][name] = [*times, now]
            if self.cfg.notify_dedupe_seconds:
                state["dedupe"][key] = now
            if not msg.urgent and notify_mod.in_quiet_hours(self.cfg.quiet_hours, now, self.cfg.timezone):
                dropped = notify_mod.hold(state, name, msg, now)
                self.notify_store.save(state)
                log.info("notify: %s %s: held for after quiet hours (%d waiting)", name, msg.tag, len(state["held"]))
                if dropped is not None:
                    log.warning("notify: held queue full (%d); dropped the oldest (%s %s)", notify_mod.HELD_CAP,
                                dropped["sender"], dropped["tag"])
                return "held", None
            self.notify_store.save(state)
        result = await send_all(self.subs, sender, notify_mod.push_message(name, msg))
        log.info("notify: %s %s: sent%s, delivered to %d/%d device(s), %d pruned", name, msg.tag,
                 " (urgent)" if msg.urgent else "", result["delivered"], result["devices"], result["pruned"])
        if self.cfg.notify_dedupe_seconds and not result["delivered"] and result["devices"]:
            async with self.notify_lock:
                state = self.notify_store.load(self.clock())
                if state["dedupe"].get(key) == now:
                    del state["dedupe"][key]
                    try:
                        self.notify_store.save(state)
                    except OSError as exc:
                        log.error("notify state not saved (%s)", type(exc).__name__)
        return "sent", None

    def _flush_failed(self, now: float) -> float:
        """Backs off after a summary that couldn't go out: 1, 2, 4 ... minutes, at most an hour."""
        self.flush_failures += 1
        delay = min(FLUSH_RETRY_MAX_S, FLUSH_RETRY_FIRST_S * 2 ** min(self.flush_failures - 1, 16))
        self.flush_retry_at = now + delay
        return delay

    async def notify_flush(self) -> bool:
        """Outside quiet hours, everything held goes out as one summary push (notify.summary_push). It stays held
        while push is disabled, or when the summary reached no device while devices exist, and is tried again with
        a back-off (_flush_failed). Returns True when the queue was emptied. The lock is held across this send
        (about once a night), so a message held meanwhile can't be cleared with the queue unsent."""
        async with self.notify_lock:
            now = self.clock()
            state = self.notify_store.load(now)
            if not state["held"] or notify_mod.in_quiet_hours(self.cfg.quiet_hours, now, self.cfg.timezone):
                return False
            if now < self.flush_retry_at:
                return False
            n = len(state["held"]) + state["dropped"]
            if self.sender is None:
                delay = self._flush_failed(now)
                log.warning("notify: %d held message(s) to send, but push is disabled; next try in %d s", n, delay)
                return False
            result = await send_all(self.subs, self.sender, notify_mod.summary_push(state["held"], state["dropped"]))
            log.info("notify: held summary of %d message(s), delivered to %d/%d device(s), %d pruned", n,
                     result["delivered"], result["devices"], result["pruned"])
            if not result["delivered"] and result["devices"]:
                log.warning("notify: held summary not delivered; next try in %d s", self._flush_failed(now))
                return False
            self.flush_failures, self.flush_retry_at = 0, 0.0
            state = notify_mod.prune(state, now, self.cfg.notify_dedupe_seconds)
            state["held"], state["dropped"] = [], 0
            self.notify_store.save(state)
            return True

    async def notify_loop(self) -> None:
        while True:
            try:
                await self.notify_flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("held-message flush failed")
            await asyncio.sleep(NOTIFY_TICK_S)


STATE = web.AppKey("state", State)
BACKGROUND = web.AppKey("background", bool)


def _st(request: web.Request) -> State:
    return request.app[STATE]


# ---- middleware ------------------------------------------------------------------------------------------------
def _is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def _token_route(request: web.Request) -> bool:
    """POST /api/notify from the local proxy, routed to `notify`: the one place the login guard gives way to the
    handler's token check (B2). Any other method, path spelling or route keeps the guard."""
    return (request.method == "POST" and request.path == NOTIFY_PATH and request.match_info.handler is notify
            and _local_peer(request))


@web.middleware
async def guard(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
    st = _st(request)
    path = request.path
    token_route = _token_route(request)
    if path != "/healthz" and not token_route and not is_allowed(request, st.cfg.allowed_login):
        log.warning("refused %s %s: not the allowed Tailscale identity", request.method, path[:60] if path.isprintable() else "?")
        return error_response(403, "forbidden")
    if request.method == "POST" and path in POST_PATHS:
        pass
    elif request.method != "GET" or path in POST_PATHS:
        allow = "POST" if path in POST_PATHS else "GET"
        return error_response(405, "method not allowed", {"Allow": allow})
    if _is_api(path):
        site = request.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ALLOWED_FETCH_SITES:
            return error_response(403, "cross-site request refused")
    if request.method == "POST" and not token_route:  # the notify handler checks its own content type
        if request.headers.get("X-Mission-Control") != "1":
            return error_response(403, "missing X-Mission-Control header")
        if request.content_type != "application/json":
            return error_response(415, "JSON only")
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
    except Exception:
        log.exception("unhandled error")
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


# ---- handlers --------------------------------------------------------------------------------------------------
async def _body(request: web.Request, limit: int = MAX_BODY) -> dict[str, Any]:
    if request.content_length is not None and request.content_length > limit:
        raise ApiError(413, "body too large")
    raw = b""
    try:
        while len(raw) <= limit:  # read() returns what has arrived so far: keep reading to the end or past the limit
            chunk = await request.content.read(limit + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
    except Exception:  # noqa: BLE001
        raise ApiError(400, "unreadable body") from None
    if len(raw) > limit:
        raise ApiError(413, "body too large")
    try:
        data = strict_json(raw)
    except (ValueError, RecursionError):
        raise ApiError(400, "invalid JSON") from None
    if not isinstance(data, dict):
        raise ApiError(400, "body must be a JSON object")
    return data


def _no_query(request: web.Request) -> None:
    if request.query_string:
        raise ApiError(400, "no parameters accepted")


async def healthz(_request: web.Request) -> web.Response:
    return json_out({"ok": True})


async def cards(request: web.Request) -> web.Response:
    _no_query(request)
    st = _st(request)
    assert st.collector is not None
    snap = await st.collector.snapshot()
    now = st.clock()
    restarts = alerts_mod.update_restarts(st.restart_store.load(now), snap, st.cfg, now)  # in memory only
    active, _held = alerts_mod.evaluate(snap, st.cfg, now, restarts)
    shown = alerts_mod.shown(st.alert_store.load(now))
    return json_out(cards_mod.build(snap, active, shown, st.cfg, now, st.vapid_public_key, restarts))


async def briefing(request: web.Request) -> web.Response:
    _no_query(request)
    st = _st(request)
    assert st.collector is not None
    snap = await st.collector.snapshot()
    now = st.clock()
    return json_out(briefing_mod.compose(snap, alerts_mod.briefing_alerts(snap, st.cfg, now), st.cfg, now))


def _endpoint(body: dict[str, Any]) -> str:
    ep = body.get("endpoint")
    if not isinstance(ep, str) or not ep or len(ep) > 1024:
        raise ApiError(400, "endpoint required")
    return ep


async def push_subscribe(request: web.Request) -> web.Response:
    st = _st(request)
    body = await _body(request)
    try:
        sub = validate_subscription(body.get("subscription"), body.get("label"), st.cfg)
        await asyncio.to_thread(st.subs.add, sub)
    except PushError as exc:
        raise ApiError(400, str(exc)) from None
    log.info("push: device %r subscribed", sub["label"])
    return json_out({"ok": True})


async def push_unsubscribe(request: web.Request) -> web.Response:
    st = _st(request)
    endpoint = _endpoint(await _body(request))
    if not await asyncio.to_thread(st.subs.remove, endpoint):
        raise ApiError(404, "this device isn't subscribed")
    log.info("push: a device unsubscribed")
    return json_out({"ok": True})


async def push_test(request: web.Request) -> web.Response:
    st = _st(request)
    endpoint = _endpoint(await _body(request))
    if not any(s["endpoint"] == endpoint for s in st.subs.all()):
        raise ApiError(404, "this device isn't subscribed")
    if st.sender is None:
        raise ApiError(503, "push is not set up on the server")
    result = await send_all(st.subs, st.sender, TEST_MESSAGE, only=endpoint)
    if result["delivered"]:
        return json_out({"ok": True})
    if result["pruned"]:
        raise ApiError(410, "this device's subscription has expired; turn notifications on again")
    raise ApiError(502, "the push service didn't accept the test")


async def notify(request: web.Request) -> web.Response:
    """POST /api/notify (B2). The guard let it through without a login; the token is the auth. Logs name the
    sender, the tag (once it is valid) and the outcome only."""
    st = _st(request)
    sender = notify_mod.match_sender(st.cfg.notify_senders, request.headers.getall("Authorization", []))
    if sender is None:
        log.warning("notify: refused: unauthorized")
        return error_response(401, "unauthorized", {"WWW-Authenticate": "Bearer"})
    name = sender.name
    if not notify_mod.from_allowed(sender, request.headers.getall("X-Forwarded-For", [])):
        log.warning("notify: %s refused: not from an allowed address", name)
        return error_response(403, "forbidden")
    if request.content_type != "application/json":
        log.info("notify: %s refused: not JSON (415)", name)
        return error_response(415, "JSON only")
    try:
        _no_query(request)
        msg = notify_mod.parse_message(await _body(request, notify_mod.MAX_REQUEST))
    except notify_mod.BadField as exc:
        log.info("notify: %s refused: bad %s (400)", name, exc.field)
        return error_response(400, exc.field)
    except ApiError as exc:
        log.info("notify: %s refused: %s (%d)", name, exc.message, exc.status)
        raise
    if not fits(notify_mod.push_message(name, msg)):  # valid, but past what a push carries once encoded
        log.info("notify: %s %s refused: too large to push (413)", name, msg.tag)
        return error_response(413, "too large to push")
    if st.sender is None:
        log.warning("notify: %s %s: push is not set up (503)", name, msg.tag)
        return error_response(503, "push is not set up on the server")
    outcome, retry = await st.notify_accept(name, msg)
    if outcome == "rate-limited":
        return error_response(429, "rate limited", {"Retry-After": str(retry)})
    return json_out({"status": outcome}, 202)


async def api_not_found(_request: web.Request) -> web.Response:
    return error_response(404, "no such endpoint")


async def static(request: web.Request) -> web.StreamResponse:
    root = Path(_st(request).cfg.static_dir).resolve()
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


# ---- lifecycle -----------------------------------------------------------------------------------------------
async def _startup(app: web.Application) -> None:
    st = app[STATE]
    st.session = make_session(dict(st.cfg.host_map))
    st.collector = Collector(st.cfg, st.session, st.clock)
    st.load_push()
    if app.get(BACKGROUND, True):
        st.tasks.append(asyncio.create_task(st.alert_loop(), name="alerts"))
        st.tasks.append(asyncio.create_task(st.notify_loop(), name="notify"))


async def _cleanup(app: web.Application) -> None:
    st = app[STATE]
    for task in st.tasks:
        task.cancel()
    for task in st.tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    st.tasks.clear()
    if st.collector is not None:
        await st.collector.close()  # an in-flight ERAU fetch
    if st.session is not None:
        await st.session.close()


def make_app(cfg: Config, *, background: bool = True, sender: Sender | None = None,
             clock: Callable[[], float] = time.time) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=MAX_BODY)
    app[STATE] = State(cfg, sender=sender, clock=clock)
    app[BACKGROUND] = background
    app.on_response_prepare.append(_on_prepare)
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    r = app.router
    r.add_route("GET", "/healthz", healthz)
    r.add_route("GET", "/api/cards", cards)
    r.add_route("GET", "/api/briefing", briefing)
    r.add_route("POST", "/api/push/subscribe", push_subscribe)
    r.add_route("POST", "/api/push/unsubscribe", push_unsubscribe)
    r.add_route("POST", "/api/push/test", push_test)
    r.add_route("POST", NOTIFY_PATH, notify)
    r.add_route("*", "/api", api_not_found)
    r.add_route("*", "/api/{tail:.*}", api_not_found)
    r.add_route("GET", "/{tail:.*}", static)
    return app
