"""Gate sampler: GET {gate_url}/status every `sample_interval`, a 24 h ring buffer in memory, persisted
atomically to a jsonl file every `persist_interval` (and on shutdown), reloaded at start."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import aiohttp

from .clients import SourceError, get_json, strict_json

log = logging.getLogger("ai_control.sampler")

FIELDS = ("gpu_busy", "ollama_share", "avg30")
MAX_POINTS = 300


@dataclass
class GateObservation:
    """The latest gate fetch: either `status` (the gate's JSON) or `error`; `http_status` when it answered."""

    at: float
    status: dict[str, Any] | None
    error: str | None
    http_status: int | None = None
    kind: str | None = None  # http | network | shape (None when ok)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def sample_from_status(status: dict[str, Any], ts: float) -> dict[str, Any]:
    available = status.get("available")
    return {
        "ts": int(ts),
        "gpu_busy": _num(status.get("gpu_busy")),
        "ollama_share": _num(status.get("ollama_share")),
        "avg30": _num(status.get("avg30")),
        "available": available if isinstance(available, bool) else None,
    }


def _valid_sample(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    ts = _num(obj.get("ts"))
    if ts is None or ts < 0:
        return None
    out: dict[str, Any] = {"ts": int(ts)}
    for key in FIELDS:
        out[key] = _num(obj.get(key))
    avail = obj.get("available")
    out["available"] = avail if isinstance(avail, bool) else None
    return out


class GateSampler:
    def __init__(
        self,
        gate_url: str,
        path: str,
        *,
        interval: float = 10.0,
        persist_interval: float = 300.0,
        history_s: float = 86400.0,
        timeout: float = 4.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.url = f"{gate_url}/status"
        self.path = path
        self.interval = interval
        self.persist_interval = persist_interval
        self.history_s = history_s
        self.timeout = timeout
        self.clock = clock
        maxlen = int(history_s / interval) + 16 if interval > 0 else None
        self.samples: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self.last: GateObservation | None = None
        self._dirty = False

    # ---- persistence -----------------------------------------------------------------------------
    def load(self) -> int:
        """Read the jsonl file (bad lines skipped), keep the last `history_s`. Returns the count kept."""
        try:
            with open(self.path, "rb") as fh:
                lines = fh.read(64 * 1024 * 1024).splitlines()
        except FileNotFoundError:
            return 0
        except OSError as exc:
            log.warning("cannot read gate samples (%s)", exc.strerror or type(exc).__name__)
            self._set_aside()
            return 0
        cutoff = self.clock() - self.history_s
        kept = []
        bad = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                # Any failure on one line (bad UTF-8, bad JSON, NaN, absurd nesting) skips just that line.
                sample = _valid_sample(strict_json(line.decode("utf-8")))
            except (ValueError, RecursionError, MemoryError):
                sample = None
            if sample is None:
                bad += 1
            elif sample["ts"] >= cutoff:
                kept.append(sample)
        if bad:
            log.warning("skipped %d bad line(s) in the gate samples file", bad)
        kept.sort(key=lambda s: s["ts"])
        self.samples.clear()
        self.samples.extend(kept)
        return len(self.samples)

    def _set_aside(self) -> None:
        """Move an unreadable samples file out of the way (kept for inspection) so we start empty and the next
        persist can write a fresh one."""
        aside = f"{self.path}.corrupt-{int(self.clock())}"
        try:
            os.replace(self.path, aside)
            log.warning("moved the unreadable gate samples file aside to %s; starting empty", aside)
        except OSError as exc:
            log.warning("cannot move the gate samples file aside (%s); starting empty", exc.strerror or type(exc).__name__)

    def trim(self) -> None:
        cutoff = self.clock() - self.history_s
        while self.samples and self.samples[0]["ts"] < cutoff:
            self.samples.popleft()

    def snapshot(self) -> list[dict[str, Any]]:
        """Trim and copy the ring. Call on the event loop: the loop is the only thread touching the deque."""
        self.trim()
        return list(self.samples)

    async def persist_async(self) -> None:
        """Snapshot on the loop, write in a worker thread (the thread never sees the live deque)."""
        await asyncio.to_thread(self.write, self.snapshot())

    def persist(self) -> None:
        """Synchronous persist (tests, single-threaded callers)."""
        self.write(self.snapshot())

    def write(self, samples: list[dict[str, Any]]) -> None:
        """Write `samples` atomically (temp file in the same dir, fsync, rename). Never raises."""
        directory = os.path.dirname(self.path) or "."
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=".gate-samples.", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for sample in samples:
                    fh.write(json.dumps(sample, separators=(",", ":"), allow_nan=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o640)
            os.replace(tmp, self.path)
            tmp = None
            self._dirty = False
        except (OSError, ValueError) as exc:
            log.warning("cannot persist gate samples: %s", getattr(exc, "strerror", None) or type(exc).__name__)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # ---- sampling --------------------------------------------------------------------------------
    async def sample_once(self, session: aiohttp.ClientSession) -> GateObservation:
        now = self.clock()
        try:
            status, data, _ = await get_json(session, self.url, self.timeout)
            if not isinstance(data, dict) or not isinstance(data.get("available"), bool):
                obs = GateObservation(now, None, "gate answer has no boolean 'available'", status, "shape")
            else:
                obs = GateObservation(now, data, None, status)
                self.samples.append(sample_from_status(data, now))
                self._dirty = True
        except SourceError as exc:
            message = "gate has no fresh sample (HTTP 503)" if exc.status == 503 else str(exc)
            obs = GateObservation(now, None, message, exc.status, exc.kind)
        prev = self.last
        if prev is None or (prev.error is None) != (obs.error is None):
            if obs.error:
                log.warning("gate: %s", obs.error)
            else:
                log.info("gate reachable")
        self.last = obs
        self.trim()
        return obs

    async def run(self, session: aiohttp.ClientSession) -> None:
        """Sample forever; persist every persist_interval. Cancel to stop (the caller persists on shutdown)."""
        loop = asyncio.get_running_loop()
        next_persist = loop.time() + self.persist_interval
        while True:
            started = loop.time()
            try:
                await self.sample_once(session)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("gate sampler iteration failed")
            if loop.time() >= next_persist:
                if self._dirty:
                    await self.persist_async()
                next_persist = loop.time() + self.persist_interval
            await asyncio.sleep(max(0.0, self.interval - (loop.time() - started)))

    # ---- history ---------------------------------------------------------------------------------
    def history(self, minutes: int, max_points: int = MAX_POINTS) -> list[dict[str, Any]]:
        """Samples from the last `minutes`, oldest first, downsampled into at most `max_points` time buckets:
        numbers averaged, `available` false if any sample in the bucket was false, ts = the bucket's last."""
        now = self.clock()
        start = now - minutes * 60
        window = [s for s in self.samples if s["ts"] >= start]
        if len(window) <= max_points:
            return [dict(s) for s in window]
        width = (now - start) / max_points
        buckets: dict[int, list[dict[str, Any]]] = {}
        for s in window:
            idx = min(max_points - 1, int((s["ts"] - start) / width))
            buckets.setdefault(idx, []).append(s)
        out = []
        for idx in sorted(buckets):
            group = buckets[idx]
            point: dict[str, Any] = {"ts": int(group[-1]["ts"])}
            for key in FIELDS:
                vals = [g[key] for g in group if g[key] is not None]
                point[key] = round(sum(vals) / len(vals), 1) if vals else None
            avail = [g["available"] for g in group if g["available"] is not None]
            point["available"] = None if not avail else all(avail)
            out.append(point)
        return out
