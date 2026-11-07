"""gpu-gate client: GET {gate_url}/status, cached for status_ttl; any failure means unavailable."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

log = logging.getLogger("gpu_broker.gate")


@dataclass(frozen=True)
class GateAnswer:
    available: bool
    answer: dict[str, Any] | None  # the gate's JSON, when it gave a usable one
    error: str | None  # why the answer counts as unavailable, when it failed
    fetched_at: float  # time.monotonic()


class GateClient:
    def __init__(self, session: aiohttp.ClientSession, gate_url: str, ttl: float, timeout: float) -> None:
        self._session = session
        self._url = f"{gate_url}/status"
        self._ttl = ttl
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._last: GateAnswer | None = None
        self._lock = asyncio.Lock()
        self.fetches = 0  # number of gate requests made (for /broker/status and tests)

    @property
    def last(self) -> GateAnswer | None:
        return self._last

    def _fresh(self) -> GateAnswer | None:
        last = self._last
        if last is not None and time.monotonic() - last.fetched_at < self._ttl:
            return last
        return None

    async def get(self) -> GateAnswer:
        """The cached answer if younger than ttl, else one fetch shared by all concurrent callers.

        Failures are cached for the ttl too, so a dead gate costs at most one timeout per ttl.
        """
        cached = self._fresh()
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._fresh()
            if cached is not None:
                return cached
            self._last = await self._fetch()
            return self._last

    async def _fetch(self) -> GateAnswer:
        self.fetches += 1
        try:
            async with self._session.get(self._url, timeout=self._timeout) as resp:
                if resp.status != 200:
                    raise ValueError(f"HTTP {resp.status}")
                body = await resp.json(content_type=None)
            if not isinstance(body, dict) or not isinstance(body.get("available"), bool):
                raise TypeError("answer has no boolean 'available'")
            answer = GateAnswer(body["available"], body, None, time.monotonic())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - timeout, refused, bad JSON, wrong shape: all mean unavailable
            error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            answer = GateAnswer(False, None, error, time.monotonic())
        prev = self._last
        if prev is None or prev.available != answer.available or (prev.error is None) != (answer.error is None):
            if answer.error:
                log.warning("gate unreachable or invalid (%s): routing to cpu", answer.error)
            else:
                log.info("gate says available=%s (reason=%s)", answer.available, (answer.answer or {}).get("reason"))
        return answer

    def status(self) -> dict[str, Any]:
        last = self._last
        return {
            "url": self._url,
            "ttl_s": self._ttl,
            "fetches": self.fetches,
            "available": None if last is None else last.available,
            "answer": None if last is None else last.answer,
            "error": None if last is None else last.error,
            "age_s": None if last is None else round(time.monotonic() - last.fetched_at, 3),
        }
