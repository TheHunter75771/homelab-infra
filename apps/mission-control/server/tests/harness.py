"""Shared test harness: every fake source, a temp state dir, the server on an ephemeral port with background
loops off and a fixed clock. Tests drive the alert round and the briefing directly."""

from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar

import aiohttp

from mission_control.app import STATE, State
from mission_control.config import Config, from_dict
from mission_control.server import bound_port, start

from . import fakes

LOGGER = logging.getLogger("mission_control")


class LogCapture:
    """Everything the app logs, at DEBUG, as one string (for the no-secrets tests)."""

    def __init__(self) -> None:
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))

    def __enter__(self) -> LogCapture:  # noqa: PYI034
        LOGGER.addHandler(self.handler)
        self._level = LOGGER.level
        LOGGER.setLevel(logging.DEBUG)
        LOGGER.propagate = False
        return self

    def __exit__(self, *exc: object) -> None:
        LOGGER.removeHandler(self.handler)
        LOGGER.setLevel(self._level)

    @property
    def text(self) -> str:
        return self.stream.getvalue()


class Clock:
    def __init__(self, now: float = fakes.NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Harness(unittest.IsolatedAsyncioTestCase):
    extra: ClassVar[dict[str, Any]] = {}
    start_server = True

    async def asyncSetUp(self) -> None:
        LOGGER.propagate = False
        if not any(isinstance(h, logging.NullHandler) for h in LOGGER.handlers):
            LOGGER.addHandler(logging.NullHandler())
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state_dir = self.dir / "state"
        self.state_dir.mkdir()
        web = self.dir / "web"
        web.mkdir()
        (web / "index.html").write_text("<!doctype html><title>Mission Control</title>")
        (web / "sw.js").write_text("self.addEventListener('push', () => {});\n")
        self.vapid = fakes.write_vapid(self.dir / "vapid.json")
        self.hub, self.erau, self.aic, self.webui = fakes.FakeMoneyHub(), fakes.FakeErau(), fakes.FakeAIControl(), fakes.FakeWebui()
        self.ms, self.ha = fakes.FakeMediaserver(), fakes.FakeHA()
        self.fakes = [self.hub, self.erau, self.aic, self.webui, self.ms, self.ha]
        for f in self.fakes:
            await f.start()
        self.systemctl = fakes.make_systemctl(self.dir / "sysd", fakes.default_units())
        self.last_ok = self.dir / "LAST_OK"
        self.last_ok.write_text(fakes.iso(fakes.NOW - 3 * 3600) + "\n")
        self.ha_token = self.dir / "ha-token"
        self.cfg_raw: dict[str, Any] = {
            "allowed_login": fakes.LOGIN,
            "listen_port": 8097,
            "static_dir": str(web),
            "state_dir": str(self.state_dir),
            "vapid_file": str(self.dir / "vapid.json"),
            "source_timeout_s": 2.0,
            "aicontrol_url": self.aic.url,
            "webui_url": self.webui.url,
            "moneyhub_url": self.hub.url,
            "erau_url": self.erau.url,
            "mediaserver_url": self.ms.url,
            "ha_url": self.ha.url,
            "ha_token_file": str(self.ha_token),
            "ha_entities": ["sensor.living_room_temperature", "lock.front_door"],
            "systemctl": str(self.systemctl),
            "units": ["forge-api.service", "ai-control.service"],
            "backups": [
                {"id": "owui-stage", "label": "Open WebUI (ai-box)", "kind": "unit", "unit": "ai-openwebui-backup.service",
                 "max_age_hours": 30},
                {"id": "owui-file", "label": "Open WebUI LAST_OK", "kind": "file", "path": str(self.last_ok)},
                {"id": "mediaserver", "label": "Mediaserver", "kind": "mediaserver"},
            ],
            "links": {"ai": "https://ai-box.example.ts.net/", "money": "http://100.64.0.1:8710/"},
            **self.extra,
        }
        self.clock = Clock()
        self.sender = fakes.RecordingSender()
        self.runner = None
        self.http = aiohttp.ClientSession()
        if self.start_server:
            await self.restart()

    async def restart(self, **overrides: Any) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
        self.cfg_raw.update(overrides)
        self.cfg: Config = from_dict(self.cfg_raw)
        self.runner = await start(self.cfg, port=0, background=False, sender=self.sender, clock=self.clock)
        self.base = f"http://127.0.0.1:{bound_port(self.runner)}"

    @property
    def state(self) -> State:
        assert self.runner is not None
        return self.runner.app[STATE]

    async def asyncTearDown(self) -> None:
        await self.http.close()
        if self.runner is not None:
            await self.runner.cleanup()
        for f in self.fakes:
            await f.stop()
        self.tmp.cleanup()

    def config(self, **overrides: Any) -> Config:
        return from_dict({**self.cfg_raw, **overrides})

    async def get(self, path: str, *, login: str | None = fakes.LOGIN, headers: dict[str, str] | None = None
                  ) -> tuple[int, Any]:
        h = dict(headers or {})
        if login is not None:
            h["Tailscale-User-Login"] = login
        async with self.http.get(self.base + path, headers=h, allow_redirects=False) as r:
            body = await r.read()
            try:
                return r.status, json.loads(body)
            except ValueError:
                return r.status, body.decode("utf-8", "replace")

    async def post(self, path: str, body: Any, *, login: str | None = fakes.LOGIN, headers: dict[str, str] | None = None
                   ) -> tuple[int, Any]:
        h = {"Content-Type": "application/json", "X-Mission-Control": "1", **(headers or {})}
        if login is not None:
            h["Tailscale-User-Login"] = login
        async with self.http.post(self.base + path, data=json.dumps(body), headers=h) as r:
            return r.status, await r.json(content_type=None)
