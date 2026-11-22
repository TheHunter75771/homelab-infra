"""Fake sources for the tests: Money Hub, AI Control, the ERAU dashboard, Open WebUI, the mediaserver dashboard,
Home Assistant and a push service, each a real local HTTP server; systemctl as a script. All data is synthetic."""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import inspect
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

LOGIN = "noah@example.com"
HA_TOKEN = "synthetic-ha-token-4b1d9e"  # a fixture, not a real secret
# 2026-10-28 09:30 UTC = 05:30 America/New_York (EDT): the briefing timer's moment.
NOW = dt.datetime(2026, 10, 28, 9, 30, tzinfo=dt.UTC).timestamp()


def iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat().replace("+00:00", "Z")


def b64(n: int, fill: int = 7) -> str:
    return base64.urlsafe_b64encode(bytes([fill]) * n).rstrip(b"=").decode()


def fake_subscription(i: int = 1, host: str = "fcm.googleapis.com") -> dict[str, Any]:
    p256 = base64.urlsafe_b64encode(b"\x04" + bytes([i]) * 64).rstrip(b"=").decode()
    return {"endpoint": f"https://{host}/fcm/send/device-{i}-secretcapability",
            "keys": {"p256dh": p256, "auth": b64(16, i)}, "expirationTime": None}


class Fake:
    """A local HTTP server with a request log, a per-path override and an optional delay."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.delay = 0.0
        self.override: dict[str, web.StreamResponse | Any] = {}
        self.app = web.Application()
        self.app.router.add_route("*", "/{tail:.*}", self._dispatch)
        self.server = TestServer(self.app, host="127.0.0.1")

    async def start(self) -> None:
        await self.server.start_server()

    async def stop(self) -> None:
        await self.server.close()

    @property
    def url(self) -> str:
        return str(self.server.make_url("")).rstrip("/")

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        self.requests.append((request.method, request.path, dict(request.query)))
        if self.delay:
            await asyncio.sleep(self.delay)
        if request.path in self.override:
            o = self.override[request.path]
            if inspect.iscoroutinefunction(o):
                return await o(request)
            return o() if callable(o) else o
        return await self.handle(request)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        return web.json_response({"error": "not found"}, status=404)

    def hits(self, path: str) -> int:
        return sum(1 for _m, p, _q in self.requests if p == path)


class FakeMoneyHub(Fake):
    def __init__(self) -> None:
        super().__init__()
        self.state = {
            "sync_last_at": "2026-10-05T13:00:26.147836",  # naive UTC, like Money Hub
            "sync_last_attempt_at": "2026-10-27T13:00:05.981938",
            "sync_failing": True,
            "sync_error": "SimpleFinError: /accounts failed: HTTP 403",
            "stale_accounts": 8,
            "inbox_count": 0,
            "months": ["2026-10"],
        }
        self.bills = {
            "2026-10": [
                {"id": 1, "name": "Rent", "amount_cents": 120000, "due_rule": "1st", "check": "1st", "paid": False},
                {"id": 2, "name": "Car insurance", "amount_cents": 15025, "due_rule": "28th", "check": "15th", "paid": False},
                {"id": 3, "name": "Phone", "amount_cents": 4500, "due_rule": "30th", "check": "15th", "paid": False},
                {"id": 4, "name": "Streaming", "amount_cents": 1599, "due_rule": "29th", "check": "15th", "paid": True},
                {"id": 5, "name": "Gym", "amount_cents": 3000, "due_rule": "biweekly", "check": "—", "paid": False},
            ],
            "2026-11": [
                {"id": 1, "name": "Rent", "amount_cents": 120000, "due_rule": "1st", "check": "1st", "paid": False},
                {"id": 6, "name": "Water", "amount_cents": 3333, "due_rule": "15th", "check": "15th", "paid": False},
            ],
        }
        self.budget = {
            "month": "2026-10",
            "categories": [
                {"id": 1, "name": "Groceries", "spent_cents": 30000, "cap_cents": 40000},
                {"id": 2, "name": "Fun", "spent_cents": 12000, "cap_cents": 10000},
                {"id": 3, "name": "Gas", "spent_cents": 9000, "cap_cents": 10000},
                {"id": 4, "name": "Uncapped", "spent_cents": 5000, "cap_cents": 0},
            ],
        }

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.method != "GET":
            return web.json_response({"error": "fake hub: write attempted"}, status=405)
        if request.path == "/api/state":
            return web.json_response(self.state)
        if request.path == "/api/bills":
            period = request.query.get("period", "")
            return web.json_response({"period": period, "rows": self.bills.get(period, []), "paid_cents": 0,
                                      "remaining_cents": 0})
        if request.path == "/api/budget":
            return web.json_response(self.budget)
        return await super().handle(request)


def erau_payload(now: float = NOW) -> dict[str, Any]:
    h = 3600

    def task(code: str, title: str, at: float, **kw: Any) -> dict[str, Any]:
        milestones = kw.pop("milestones", None) or [{"label": "Due", "at": iso(at), "done": False}]
        return {"courseId": kw.pop("courseId", 1), "courseCode": code, "title": title, "done": False,
                "skipped": False, "lapsed": False, "optional": False, "overdue": False, "milestones": milestones, **kw}

    tasks = [
        task("ABCD 101", "Essay 2: Rhetorical analysis", now + 18.5 * h - 60),  # tonight 11:59 PM ET
        task("MATH 111", "Quiz 4", now + 40 * h),
        task("MATH 111", "Quiz 5", now + 60 * h),  # beyond 48 h
        task("HIST 101", "Hidden course work", now + 5 * h, courseId=99),
        task("ABCD 101", "Finished already", now + 6 * h, done=True),
        task("PHYS 102", "Late lab", now - 20 * h, overdue=True),
        task("PHYS 102", "Draft (soft, past)", now - 2 * h,
             milestones=[{"label": "Draft", "at": iso(now - 2 * h), "soft": True, "early": True, "done": False}]),
        task("CS 118", "Project", now + 30 * h,
             milestones=[{"label": "Start", "at": iso(now + 30 * h), "early": True, "done": False},
                         {"label": "Due", "at": iso(now + 100 * h), "done": False}]),
    ]
    return {
        "snapshot": {"fetchedAt": iso(now - 600), "tasks": tasks, "inbox": [{"excerpt": "PRIVATE INBOX TEXT"}]},
        "status": {"syncing": False, "needsSignIn": False, "stale": False, "fetchedAt": iso(now - 600),
                   "tokenSet": True, "tokenUser": "Noah"},
        "settings": {"hiddenCourses": [99]},
        "push": {"publicKey": "x", "devices": [{"name": "iPhone", "endpointHost": "web.push.apple.com"}]},
    }


class FakeErau(Fake):
    def __init__(self) -> None:
        super().__init__()
        self.payload = erau_payload()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.path == "/api/snapshot" and request.method == "GET":
            return web.json_response(self.payload)
        return await super().handle(request)


def aicontrol_summary() -> dict[str, Any]:
    T, G = 1_000_000_000_000, 10**9
    return {
        "server": {"time": int(NOW), "timeZone": "America/New_York", "version": "0.1.0", "identity": None},
        "routing": {"to": "gpu", "reason": "no_game", "avg30": 5.2},
        "services": [
            {"id": "gate", "status": "ok", "detail": "available=True, no_game", "since": 1},
            {"id": "pc-ollama", "status": "ok", "detail": "active (running)", "since": 1},
            {"id": "cpu-ollama", "status": "ok", "detail": "running (healthy)", "since": 1},
            {"id": "broker", "status": "ok", "detail": "routing to gpu", "since": 1},
            {"id": "webui", "status": "ok", "detail": "healthy", "since": 1},
        ],
        "resources": {
            "aibox": {"load1": 0.1, "disk": [{"mount": "/", "total": 500 * G, "avail": 430 * G}]},
            "pc": {"disk": [{"mount": "/", "total": T, "avail": 470 * G},
                            {"mount": "/mnt/share", "total": 4 * T, "avail": int(0.2 * T)}]},  # 95% used
        },
        "alerts": [],
    }


class FakeAIControl(Fake):
    def __init__(self) -> None:
        super().__init__()
        self.summary = aicontrol_summary()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.path == "/api/summary":
            return web.json_response(self.summary)
        return await super().handle(request)


class FakeWebui(Fake):
    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.path == "/health":
            return web.json_response({"status": True})
        return await super().handle(request)


class FakeMediaserver(Fake):
    def __init__(self) -> None:
        super().__init__()
        self.backups = {"status": "ok", "lastOk": iso(NOW - 5 * 3600), "maxAgeHours": 36}

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.path == "/api/summary":
            return web.json_response({"backups": self.backups, "apps": [{"name": "jellyfin"}]})
        return await super().handle(request)


class FakeHA(Fake):
    def __init__(self) -> None:
        super().__init__()
        self.states = {
            "sensor.living_room_temperature": {"entity_id": "sensor.living_room_temperature", "state": "71.2",
                                               "attributes": {"friendly_name": "Living room", "unit_of_measurement": "°F"}},
            "lock.front_door": {"entity_id": "lock.front_door", "state": "locked",
                                "attributes": {"friendly_name": "Front door"}},
        }

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Authorization") != f"Bearer {HA_TOKEN}":
            return web.json_response({"message": "401: Unauthorized"}, status=401)
        if request.path.startswith("/api/states/"):
            e = self.states.get(request.path.removeprefix("/api/states/"))
            if e is None:
                return web.json_response({"message": "Entity not found."}, status=404)
            return web.json_response(e)
        return await super().handle(request)


class FakePushService(Fake):
    """A push service: POST /<id> answers status[id] (default 201)."""

    def __init__(self) -> None:
        super().__init__()
        self.status: dict[str, int] = {}
        self.bodies: list[tuple[str, Any, bytes]] = []

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        key = request.path.strip("/")
        self.bodies.append((key, request.headers.copy(), body))
        return web.Response(status=self.status.get(key, 201))


SYSTEMCTL = r'''#!/usr/bin/env python3
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(here, "units.json")))
open(os.path.join(here, "pid"), "w").write(str(os.getpid()))
if cfg.get("sleep"):
    time.sleep(cfg["sleep"])
if cfg.get("exit"):
    sys.exit(cfg["exit"])
args = sys.argv[1:]
assert args[:4] == ["show", "--no-pager", "--timestamp=unix", "-p"], args
units = args[args.index("--") + 1:]
blocks = []
for u in units:
    p = cfg["units"].get(u, {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead", "Result": "success",
                             "NRestarts": "0"})
    p = {"Id": u, **p}
    blocks.append("\n".join(f"{k}={v}" for k, v in p.items()))
print("\n\n".join(blocks))
'''


def make_systemctl(directory: Path, units: dict[str, dict[str, str]], **extra: Any) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "systemctl"
    script.write_text(SYSTEMCTL.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    set_units(directory, units, **extra)
    return script


def set_units(directory: Path, units: dict[str, dict[str, str]], **extra: Any) -> None:
    (directory / "units.json").write_text(json.dumps({"units": units, **extra}))


def default_units(now: float = NOW) -> dict[str, dict[str, str]]:
    return {
        "forge-api.service": {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "Result": "success",
                              "NRestarts": "0", "ExecMainExitTimestamp": "", "StateChangeTimestamp": f"@{int(now - 3600)}"},
        "ai-control.service": {"LoadState": "loaded", "ActiveState": "active", "SubState": "running",
                               "Result": "success", "NRestarts": "0"},
        "ai-openwebui-backup.service": {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
                                        "Result": "success", "NRestarts": "0",
                                        "ExecMainExitTimestamp": f"@{int(now - 6 * 3600)}"},
    }


def write_vapid(path: Path) -> dict[str, str]:
    """A real P-256 key pair when `cryptography` is importable (needed by the real pywebpush test), else
    well-formed fixture bytes."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1())
        priv = key.private_numbers().private_value.to_bytes(32, "big")
        pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    except ImportError:
        priv, pub = bytes([9]) * 32, b"\x04" + bytes([8]) * 64

    def enc(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    vapid = {"publicKey": enc(pub), "privateKey": enc(priv), "subject": "mailto:noah@example.com"}
    path.write_text(json.dumps(vapid))
    os.chmod(path, 0o640)
    return vapid


class RecordingSender:
    """A fake Web Push sender: status per endpoint (default 201); `raise_for` endpoints raise an exception whose
    text carries the endpoint and the keys, as a careless library might."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.status: dict[str, int] = {}
        self.raise_for: set[str] = set()

    def __call__(self, sub: dict[str, Any], payload: str) -> int:
        self.calls.append((sub["endpoint"], json.loads(payload)))
        if sub["endpoint"] in self.raise_for:
            raise ConnectionError(f"POST {sub['endpoint']} failed with keys {sub['keys']}")
        return self.status.get(sub["endpoint"], 201)
