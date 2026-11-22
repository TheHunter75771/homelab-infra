"""Fake sources for the server tests: gate, broker, Open WebUI and host agent as real local HTTP servers,
helper X as a real Unix-socket server, chronyc as a script, /proc as a temp tree. All data is synthetic."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

AGENT_TOKEN = "synthetic-agent-token-7f3a9c"  # a fixture, not a real secret

GATE_STATUS = {
    "available": True,
    "reason": "no_game",
    "avg30": 12.5,
    "samples": 360,
    "game": None,
    "gpu_busy": 20,
    "ollama_share": 15.0,
    "ts": 0.0,
    "uid_match": True,
    "uid_match_reason": None,
}
ALIASES = {"assistant": {"gpu": "qwen3.5:9b", "cpu": "qwen3.5:4b"}}
CPU_TAGS = [
    {"name": "assistant", "model": "assistant", "size": 1, "details": {}},
    {"name": "qwen3.5:4b", "model": "qwen3.5:4b", "size": 3_000_000_000, "modified_at": "2026-10-01T00:00:00Z",
     "details": {"quantization_level": "Q4_K_M"}},
    {"name": "nomic-embed-text:latest", "size": 270_000_000, "modified_at": "2026-10-01T00:00:00Z",
     "details": {"quantization_level": "F16"}},
]
# Host agent Y's pinned shapes (apps/ai-control/host-agent/README.md). Timestamps are epoch ints.
AGENT_OLLAMA = {
    "models": [{"name": "qwen3.5:9b", "size": 6_000_000_000, "quant": "Q4_K_M", "modified": 1792900000}],
    "loaded": [{"name": "qwen3.5:9b", "size_vram": 7_000_000_000, "expires_at": 1793000300}],
}
AGENT_SERVICES = {
    "services": [
        {"id": "ollama", "unit": "ollama.service", "load": "loaded", "active": "active", "sub": "running",
         "since": 1793000000, "restarts": 0, "main_pid": 1234, "memory_bytes": 7_500_000_000},
        {"id": "gpu-gate", "unit": "gpu-gate.service", "load": "loaded", "active": "active", "sub": "running",
         "since": 1793000000, "restarts": 0, "main_pid": 1235, "memory_bytes": 20_000_000},
    ]
}
AGENT_SUDO_ENTRIES = [{"ts": 1793000000, "user": None, "runas": "root", "cwd": "/home/x", "command": "/usr/bin/true",
                       "event": "auth_fail", "ambiguous": True, "extra": "dropped"}]
AGENT_DISK = {"disk": [{"mount": "/", "total": 1_000_000_000_000, "avail": 400_000_000_000}]}
# Shapes the old tolerant parser accepted; the pinned parser must refuse every one.
AGENT_UNPINNED = {
    "/v1/services": [{"name": "ollama", "ActiveState": "active", "SubState": "running"}],
    "/v1/ollama": {"models": [{"name": "qwen3.5:9b", "modified": "2026-10-02T00:00:00Z"}], "loaded": []},
    "/v1/disk": [{"mount": "/", "total": 1000, "avail": 500}],
    "/v1/logs": ["a bare line"],
    "/v1/sudo": [{"ts": 1793000000, "user": "claude", "runas": "root", "cwd": "/", "command": "/usr/bin/id",
                  "event": "command", "ambiguous": False}],
}
RECENT = [
    {"ts": 1000.0 + i, "path": "/api/chat", "alias": "assistant", "model": "qwen3.5:9b", "backend": "gpu",
     "status": 200, "duration_ms": 900, "eval_count": 50, "eval_tok_s": 48.9, "error": None,
     "prompt": "SHOULD NEVER PASS THROUGH", "response": "NOR THIS"}
    for i in range(249)
]
# Newest first: an embedding call for a plain model name (not an alias), so `alias` is null.
RECENT.insert(0, {"ts": 2000.0, "path": "/api/embed", "alias": None, "model": "nomic-embed-text", "backend": "cpu",
                  "status": 200, "duration_ms": 40, "eval_count": None, "eval_tok_s": None, "error": None,
                  "prompt": "SHOULD NEVER PASS THROUGH", "response": "NOR THIS"})
CONTAINERS = [
    {"name": "ollama-cpu", "state": "running", "health": "healthy", "started_at": 1793000000,
     "restart_count": 0, "image": "ollama/ollama:0.0.0"},
    {"name": "gpu-broker", "state": "running", "health": "healthy", "started_at": 1793000000,
     "restart_count": 0, "image": "homelab/gpu-broker:0.1.0"},
    {"name": "open-webui", "state": "running", "health": "healthy", "started_at": 1793000000,
     "restart_count": 0, "image": "open-webui:0.0.0"},
]
TIMERS = [{"unit": "homelab-ai-digest.timer", "next": 1793080000, "last": None, "last_result": None}]
CHRONY_OK = """Reference ID    : C0000201 (192.0.2.1)
Stratum         : 3
Ref time (UTC)  : Mon Oct 26 12:00:00 2026
System time     : 0.000250000 seconds slow of NTP time
Last offset     : +0.000012000 seconds
Leap status     : Normal
"""
CHRONY_UNSYNCED = """Reference ID    : 00000000 ()
Stratum         : 0
System time     : 0.000000000 seconds fast of NTP time
Leap status     : Not synchronised
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Http:
    """A TestServer whose routes reply per `modes[path]`: ok | slow | error | 401 | ..."""

    def __init__(self) -> None:
        self.modes: dict[str, str] = {}
        self.slow_s = 2.0
        self.hits: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self.app = web.Application()
        self.server: TestServer | None = None

    async def start(self) -> None:
        self.server = TestServer(self.app, host="127.0.0.1")
        await self.server.start_server()

    async def close(self) -> None:
        if self.server is not None:
            await self.server.close()

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.port}"

    async def _mode(self, request: web.Request) -> web.Response | None:
        self.hits.append((request.path, dict(request.query), dict(request.headers)))
        mode = self.modes.get(request.path, self.modes.get("*", "ok"))
        if mode == "slow":
            await asyncio.sleep(self.slow_s)
        elif mode == "error":
            return web.json_response({"error": "synthetic failure"}, status=500)
        elif mode == "garbage":
            return web.Response(text="<html>not json</html>")
        return None


class FakeGate(_Http):
    def __init__(self) -> None:
        super().__init__()
        self.status = dict(GATE_STATUS)
        self.app.router.add_get("/status", self.handle)

    async def handle(self, request: web.Request) -> web.Response:
        if (r := await self._mode(request)) is not None:
            return r
        if self.modes.get("/status") == "503":
            return web.json_response({"error": "no fresh sample"}, status=503)
        return web.json_response(self.status)


class FakeBroker(_Http):
    def __init__(self) -> None:
        super().__init__()
        self.routing_to = "gpu"
        self.ps_backend = "gpu"
        self.tags_warning = False
        r = self.app.router
        r.add_get("/broker/status", self.status)
        r.add_get("/broker/recent", self.recent)
        r.add_get("/api/tags", self.tags)
        r.add_get("/api/ps", self.ps)

    async def status(self, request: web.Request) -> web.Response:
        if (r := await self._mode(request)) is not None:
            return r
        return web.json_response({"gate": {}, "routing_to": self.routing_to, "aliases": ALIASES})

    async def recent(self, request: web.Request) -> web.Response:
        if (r := await self._mode(request)) is not None:
            return r
        if self.modes.get("/broker/recent") == "wrapped":
            return web.json_response({"recent": RECENT})  # the tolerated-but-unpinned shape
        if self.modes.get("/broker/recent") == "nondict":
            return web.json_response([RECENT[0], "not an object"])
        return web.json_response(RECENT)

    async def tags(self, request: web.Request) -> web.Response:
        if (r := await self._mode(request)) is not None:
            return r
        headers = {"X-Backend": "cpu"}
        if self.tags_warning:
            headers["X-Broker-Warning"] = "cpu-model-list-unavailable"
            return web.json_response({"models": CPU_TAGS[:1]}, headers=headers)
        return web.json_response({"models": CPU_TAGS}, headers=headers)

    async def ps(self, request: web.Request) -> web.Response:
        if (r := await self._mode(request)) is not None:
            return r
        models = [{"name": "qwen3.5:4b", "size_vram": 0, "expires_at": "2026-10-26T12:05:00Z"}]
        return web.json_response({"models": models}, headers={"X-Backend": self.ps_backend})


class FakeWebui(_Http):
    def __init__(self) -> None:
        super().__init__()
        self.app.router.add_get("/health", self.health)

    async def health(self, request: web.Request) -> web.Response:
        if (r := await self._mode(request)) is not None:
            return r
        return web.json_response({"status": True})


class FakeAgent(_Http):
    def __init__(self) -> None:
        super().__init__()
        self.token = AGENT_TOKEN
        r = self.app.router
        for path in ("/v1/services", "/v1/ollama", "/v1/disk", "/v1/logs", "/v1/sudo", "/healthz"):
            r.add_get(path, self.handle)

    async def handle(self, request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            self.hits.append((request.path, dict(request.query), dict(request.headers)))
            return web.json_response({"error": "unauthorized"}, status=401)
        if (r := await self._mode(request)) is not None:
            return r
        if self.modes.get(request.path, self.modes.get("*")) == "unpinned":
            return web.json_response(AGENT_UNPINNED[request.path])
        body: Any = {
            "/v1/services": AGENT_SERVICES,
            "/v1/ollama": AGENT_OLLAMA,
            "/v1/disk": AGENT_DISK,
            "/v1/logs": {"unit": f"{request.query.get('unit')}.service", "truncated": False,
                         "lines": ["2026-10-26T12:00:00+0000 pc ollama[1]: synthetic line"]},
            "/v1/sudo": {"user": request.query.get("user"), "truncated": False,
                         "entries": [{**e, "user": request.query.get("user")} for e in AGENT_SUDO_ENTRIES]},
            "/healthz": {"ok": True},
        }[request.path]
        return web.json_response(body)


class FakeHelper:
    """Helper X: one JSON line in, one JSON line out, per connection."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.requests: list[dict[str, Any]] = []
        self.mode = "ok"  # ok | slow | garbage | close | refuse | unpinned (tolerated-but-wrong shapes)
        self.slow_s = 2.0
        self.docker = "active"
        self.big_lines = 0  # > 0: `logs` answers this many long lines (a reply far over 64 KiB)
        self.chunk = 0  # > 0: write the reply in pieces of this size, with a pause between them
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._conn, path=self.path)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    def reply(self, req: dict[str, Any]) -> dict[str, Any]:
        op = req.get("op")
        if self.mode == "refuse":
            return {"ok": False, "error": {"code": "bad-request", "message": "synthetic refusal"}}
        if self.mode == "unpinned":
            # Shapes the old tolerant parser accepted: bare lists instead of the pinned wrappers.
            bare = {"containers": CONTAINERS, "timers": TIMERS, "logs": ["a line"], "sudo-sessions": [],
                    "sudo-commands": [], "localai-activity": [], "sudo-replay": "text"}
            return {"ok": True, "op": op, "data": bare.get(op)}
        if op == "containers":
            if self.docker != "active":
                return {"ok": True, "op": op, "data": {"docker": self.docker, "containers": []}}
            return {"ok": True, "op": op, "data": {"docker": "active", "containers": CONTAINERS}}
        if op == "timers":
            return {"ok": True, "op": op, "data": {"timers": TIMERS}}
        if op == "logs" and self.big_lines:
            lines = [{"ts": 1793000000, "text": f"big line {i} " + "x" * 300} for i in range(self.big_lines)]
            return {"ok": True, "op": op, "data": {"source": req.get("source"), "kind": "unit", "lines": lines}}
        if op == "logs":
            lines = [{"ts": 1793000000 + i, "text": f"synthetic {req.get('source')} line {i}"} for i in range(3)]
            return {"ok": True, "op": op, "data": {"source": req.get("source"), "kind": "unit", "lines": lines}}
        if op == "sudo-commands":
            rows = [{"ts": 1793000000, "user": req.get("user"), "runas": "root", "cwd": "/", "command": "/usr/bin/id",
                     "event": "command", "ambiguous": False},
                    {"ts": 1793000001, "user": req.get("user"), "runas": "root", "cwd": "/", "command": "/usr/bin/su",
                     "event": "refused", "ambiguous": True}]
            return {"ok": True, "op": op, "data": {"user": req.get("user"), "commands": rows}}
        if op == "sudo-sessions":
            rows = [{"id": "00/00/01", "ts": 1793000000, "runas": "root", "tty": "pts/0", "cwd": "/", "command": "/usr/bin/id"}]
            return {"ok": True, "op": op, "data": {"user": req.get("user"), "sessions": rows, "total": 2, "skipped": 1,
                                                  "complete": True}}
        if op == "sudo-replay":
            if req.get("id") == "00/00/99":
                return {"ok": False, "error": {"code": "not-found", "message": "no such session"}}
            if req.get("id") == "00/00/98":
                return {"ok": False, "error": {"code": "damaged", "message": "metadata unparseable at /var/log/x"}}
            # A TSID is reported in its path form, as X does. Output is sent regardless of the flag, to prove
            # S nulls it.
            sid = req.get("id")
            if len(sid) == 6 and "/" not in sid:
                sid = f"{sid[0:2]}/{sid[2:4]}/{sid[4:6]}"
            data = {"id": sid, "ts": 1793000000, "runas": "root", "tty": "pts/0", "cwd": "/", "command": "/usr/bin/id",
                    "output": "uid=0(root)", "output_truncated": False}
            return {"ok": True, "op": op, "data": data}
        if op == "localai-activity":
            rows = [{"ts": 1793000000, "unit": "x.service", "comm": "python3", "message": "synthetic"}]
            return {"ok": True, "op": op, "data": {"user": "localai", "entries": rows}}
        return {"ok": False, "error": {"code": "unknown-op", "message": "unknown op"}}

    async def _conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readuntil(b"\n")
            req = json.loads(line)
            self.requests.append(req)
            if self.mode == "slow":
                await asyncio.sleep(self.slow_s)
            if self.mode == "garbage":
                writer.write(b"not json\n")
            elif self.mode != "close":
                out = json.dumps(self.reply(req)).encode() + b"\n"
                step = self.chunk or len(out)
                for i in range(0, len(out), step):
                    writer.write(out[i:i + step])
                    await writer.drain()
                    if self.chunk:
                        await asyncio.sleep(0.002)
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            writer.close()


def make_proc(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "loadavg").write_text("0.52 0.40 0.30 1/200 12345\n")
    (root / "meminfo").write_text(
        "MemTotal:       16000000 kB\nMemFree:         1000000 kB\nMemAvailable:    8000000 kB\n"
        "SwapTotal:       2000000 kB\nSwapFree:        1500000 kB\n"
    )


def make_chronyc(root: Path) -> Path:
    """A fake chronyc: prints chrony.out, exits with chrony.rc, sleeps chrony.sleep seconds first."""
    root.mkdir(parents=True, exist_ok=True)
    script = root / "chronyc"
    script.write_text(
        '#!/bin/sh\nd=$(dirname "$0")\n[ -f "$d/chrony.sleep" ] && sleep "$(cat "$d/chrony.sleep")"\n'
        'cat "$d/chrony.out"\nexit "$(cat "$d/chrony.rc")"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    (root / "chrony.out").write_text(CHRONY_OK)
    (root / "chrony.rc").write_text("0")
    return script


def write_token(path: Path, token: str = AGENT_TOKEN) -> None:
    path.write_text(token + "\n")
    os.chmod(path, 0o600)
