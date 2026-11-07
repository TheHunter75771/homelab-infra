"""The /api/summary builder: every source fetched in parallel, each isolated with its own timeout (<= 4 s),
so a slow or dead source marks only its own section `{"error": ...}`. Also the alert rules and the ETag."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

import aiohttp

from . import __version__
from .clients import AgentClient, HelperClient, SourceError, describe, get_json, get_status, pinned_list
from .config import Config
from .hostinfo import chrony, detect_timezone, read_resources
from .sampler import GateSampler

log = logging.getLogger("ai_control.summary")

SERVICE_IDS = ("gate", "pc-ollama", "cpu-ollama", "broker", "webui", "helper", "agent")
CRIT_WHEN_DOWN = frozenset({"broker", "cpu-ollama"})
SERVICE_NAMES = {
    "gate": "GPU gate",
    "pc-ollama": "Gaming PC Ollama",
    "cpu-ollama": "ai-box CPU Ollama",
    "broker": "GPU broker",
    "webui": "Open WebUI",
    "helper": "ai-box helper",
    "agent": "Gaming PC host agent",
}
# Broker /api/tags and /api/ps hit the broker's per-request log; refresh them at most this often.
BROKER_MODELS_INTERVAL = 60.0


@dataclass(frozen=True)
class Built:
    data: dict[str, Any]  # identity is filled per request
    core_hash: str  # hash of everything except server.time and server.identity
    built_at: float  # loop time


class Failed:
    """A source's failure, carried in place of its value."""

    __slots__ = ("error", "status")

    def __init__(self, error: str, status: int | None = None) -> None:
        self.error = error
        self.status = status

    def section(self) -> dict[str, str]:
        return {"error": self.error}


def _pick(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _text(value: Any, limit: int = 300) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


BAD_DATA = "invalid data from a source"


SAFE_LOG_INTERVAL = 600.0  # one traceback per (section, exception type) per 10 min
_safe_logged: dict[tuple[str, str], float] = {}


def safe(what: str, fn: Callable[[], Any], fallback: Any = None) -> Any:
    """Run one section's post-processing. Any exception, or a value that isn't strict JSON (NaN/Infinity),
    turns only that section into `fallback` (default {"error": ...}); never a 500 for the whole summary.
    The failure is logged with its traceback (so a real code bug is locatable in the journal), rate-limited
    per section and exception type; the traceback holds no upstream values, only code locations and the
    exception message."""
    try:
        out = fn()
        json.dumps(out, allow_nan=False)
        return out
    except Exception as exc:  # noqa: BLE001 - upstream data is untrusted; isolate it
        key = (what, type(exc).__name__)
        now = time.monotonic()
        last = _safe_logged.get(key)
        if last is None or now - last >= SAFE_LOG_INTERVAL:
            _safe_logged[key] = now
            log.warning("summary %s: bad upstream data or a bug (%s); section replaced by an error",
                        what, type(exc).__name__, exc_info=True)
        return {"error": BAD_DATA} if fallback is None else fallback


def epoch(value: Any) -> int | None:
    """A timestamp as integer epoch seconds (the contract's form): ints pass, Ollama's RFC 3339 text is
    converted, anything else (or Ollama's year-1 "never") is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    if isinstance(value, str):
        try:
            ts = int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except (ValueError, OverflowError, OSError):
            return None
        return ts if ts > 0 else None
    return None


def _model_entry(m: dict[str, Any]) -> dict[str, Any]:
    """An Ollama /api/tags model (CPU side, via the broker) → {name, size, quant, modified: epoch}."""
    details = m.get("details") if isinstance(m.get("details"), dict) else {}
    return {
        "name": _text(_pick(m, "name", "model"), 200),
        "size": _int_or_none(m.get("size")),
        "quant": _text(details.get("quantization_level"), 64),
        "modified": epoch(m.get("modified_at")),
    }


def _loaded_entry(m: dict[str, Any]) -> dict[str, Any]:
    """An Ollama /api/ps entry (CPU side, via the broker) → {name, size_vram, expires_at: epoch}."""
    return {
        "name": _text(_pick(m, "name", "model"), 200),
        "size_vram": _int_or_none(m.get("size_vram")),
        "expires_at": epoch(m.get("expires_at")),
    }


def _list_in(data: Any, *keys: str) -> list[dict[str, Any]] | None:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return [x for x in data[k] if isinstance(x, dict)]
    return None


def norm_model(name: str) -> str:
    return name if ":" in name else f"{name}:latest"


def containers_view(data: Any) -> tuple[str, dict[str, dict[str, Any]]]:
    """Helper `containers` reply (pinned): {"docker": "active"|"inactive"|"failed", "containers": [{name, state,
    health, started_at, restart_count, image}]} → (docker state, {name: container})."""
    items = pinned_list(data, "containers", "containers")
    docker = data.get("docker")
    if not isinstance(docker, str) or not all(isinstance(c.get("name"), str) for c in items):
        raise SourceError("helper containers reply doesn't match its pinned shape")
    return docker, {c["name"]: c for c in items}


def container_status(c: dict[str, Any] | None, docker: str) -> tuple[str, str]:
    if docker != "active":
        return "down", f"docker {docker}"
    if c is None:
        return "down", "container not found"
    state = str(c.get("state") or "unknown")
    if state == "missing":
        return "down", "container missing"
    health = c.get("health")
    health = None if health in (None, "", "none") else str(health)
    detail = state if health is None else f"{state} ({health})"
    if state == "running":
        return ("ok" if health in (None, "healthy") else "warn"), detail
    if state in ("restarting", "created", "paused"):
        return "warn", detail
    return "down", detail


# ---- host agent Y: pinned shapes (apps/ai-control/host-agent/README.md), parsed strictly -------------
def _y_bad(path: str) -> SourceError:
    return SourceError(f"host agent {path} reply doesn't match its pinned shape")


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _int_or_null(v: Any) -> bool:
    return v is None or _is_int(v)


def _str_or_null(v: Any) -> bool:
    return v is None or isinstance(v, str)


def _y_list(data: Any, key: str, path: str) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get(key), list) or not all(isinstance(x, dict) for x in data[key]):
        raise _y_bad(path)
    return data[key]


def agent_services_view(data: Any) -> dict[str, dict[str, Any]]:
    """/v1/services → {id: Service | {id, unit, error}}."""
    out: dict[str, dict[str, Any]] = {}
    for e in _y_list(data, "services", "/v1/services"):
        if not isinstance(e.get("id"), str) or not isinstance(e.get("unit"), str):
            raise _y_bad("/v1/services")
        if "error" in e:
            if not isinstance(e["error"], str):
                raise _y_bad("/v1/services")
        elif not (
            all(_str_or_null(e.get(k)) for k in ("load", "active", "sub"))
            and all(k in e and _int_or_null(e[k]) for k in ("since", "restarts", "main_pid", "memory_bytes"))
        ):
            raise _y_bad("/v1/services")
        out[e["id"]] = e
    return out


def unit_status(e: dict[str, Any] | None) -> tuple[str, str]:
    if e is None:
        return "unknown", "not reported by the host agent"
    if "error" in e:
        return "unknown", f"systemctl: {_text(e['error'], 200)}"
    active = e.get("active") or "unknown"
    sub = e.get("sub")
    detail = active if sub is None else f"{active} ({sub})"
    if active == "active":
        return "ok", detail
    if active in ("activating", "reloading", "deactivating"):
        return "warn", detail
    if active == "unknown":
        return "unknown", detail
    return "down", detail


def _y_part(part: Any, check: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]] | dict[str, str]:
    """One half of /v1/ollama: a list of entries, or that half's own {"error"}."""
    if isinstance(part, dict) and set(part) == {"error"} and isinstance(part["error"], str):
        return {"error": f"gaming PC Ollama: {_text(part['error'], 200)}"}
    if not isinstance(part, list) or not all(isinstance(x, dict) and check(x) for x in part):
        raise _y_bad("/v1/ollama")
    return part


def agent_ollama_view(data: Any) -> tuple[list[dict[str, Any]] | dict[str, str], list[dict[str, Any]] | dict[str, str]]:
    """/v1/ollama → (models | {error}, loaded | {error}); timestamps are already epoch ints."""
    if not isinstance(data, dict) or "models" not in data or "loaded" not in data:
        raise _y_bad("/v1/ollama")
    models = _y_part(data["models"], lambda m: isinstance(m.get("name"), str) and _int_or_null(m.get("size"))
                     and _str_or_null(m.get("quant")) and _int_or_null(m.get("modified")))
    loaded = _y_part(data["loaded"], lambda m: isinstance(m.get("name"), str) and _int_or_null(m.get("size_vram"))
                     and _int_or_null(m.get("expires_at")))
    if isinstance(models, list):
        models = [{"name": m["name"], "size": m.get("size"), "quant": m.get("quant"), "modified": m.get("modified")}
                  for m in models]
    if isinstance(loaded, list):
        loaded = [{"name": m["name"], "size_vram": m.get("size_vram"), "expires_at": m.get("expires_at")} for m in loaded]
    return models, loaded


def agent_disk_view(data: Any) -> list[dict[str, Any]]:
    """/v1/disk → [{mount, total, avail} | {mount, error}]."""
    out = []
    for d in _y_list(data, "disk", "/v1/disk"):
        if not isinstance(d.get("mount"), str):
            raise _y_bad("/v1/disk")
        if "error" in d:
            if not isinstance(d["error"], str):
                raise _y_bad("/v1/disk")
            out.append({"mount": d["mount"], "error": _text(d["error"], 200)})
        elif _is_int(d.get("total")) and _is_int(d.get("avail")):
            out.append({"mount": d["mount"], "total": d["total"], "avail": d["avail"]})
        else:
            raise _y_bad("/v1/disk")
    return out


def agent_logs_view(data: Any) -> list[str]:
    """/v1/logs → ["line", ...]."""
    if not isinstance(data, dict) or not isinstance(data.get("lines"), list) or not all(isinstance(x, str) for x in data["lines"]):
        raise _y_bad("/v1/logs")
    return data["lines"]


def agent_sudo_view(data: Any) -> list[dict[str, Any]]:
    """/v1/sudo → its entries; each must carry a boolean `ambiguous`."""
    entries = _y_list(data, "entries", "/v1/sudo")
    if not all(type(e.get("ambiguous")) is bool for e in entries):
        raise _y_bad("/v1/sudo")
    return entries


class SummaryBuilder:
    def __init__(
        self,
        cfg: Config,
        session: aiohttp.ClientSession,
        helper: HelperClient,
        agent: AgentClient,
        sampler: GateSampler,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.session = session
        self.helper = helper
        self.agent = agent
        self.sampler = sampler
        self.clock = clock
        self.timezone = detect_timezone(cfg.timezone)
        self.current: Built | None = None
        self._lock = asyncio.Lock()
        self._since: dict[str, tuple[str, float]] = {}
        self._broker_models: tuple[float, Any, Any] | None = None  # (loop time, tags, ps)
        self.on_change: list[Callable[[Built], None]] = []

    # ---- sources ---------------------------------------------------------------------------------
    async def _guard(self, coro: Awaitable[Any]) -> Any:
        timeout = self.cfg.source_timeout
        try:
            return await asyncio.wait_for(coro, timeout)
        except asyncio.CancelledError:
            raise
        except SourceError as exc:
            return Failed(str(exc), exc.status)
        except Exception as exc:  # noqa: BLE001 - a source's bug must not break the other sections
            if not isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                log.warning("summary source failed: %s", describe(exc))
            return Failed(describe(exc, timeout))

    async def _broker_json(self, path: str) -> tuple[Any, dict[str, str]]:
        _s, data, headers = await get_json(self.session, f"{self.cfg.broker_url}{path}", self.cfg.source_timeout)
        return data, headers

    async def _broker_models_cached(self) -> tuple[Any, Any]:
        now = asyncio.get_running_loop().time()
        cached = self._broker_models
        if cached is not None and now - cached[0] < BROKER_MODELS_INTERVAL:
            return cached[1], cached[2]
        tags, ps = await asyncio.gather(
            self._guard(self._broker_json("/api/tags")), self._guard(self._broker_json("/api/ps"))
        )
        # Only cache when the broker answered; otherwise retry on the next build.
        if not isinstance(tags, Failed):
            self._broker_models = (now, tags, ps)
        return tags, ps

    async def _webui(self) -> int:
        return await get_status(self.session, f"{self.cfg.webui_url}/health", self.cfg.source_timeout)

    async def _aibox(self) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(read_resources, self.cfg.proc_root, self.cfg.disk_mounts)
        except (OSError, ValueError, IndexError):
            raise SourceError("cannot read /proc") from None

    async def _agent(self, path: str) -> Any:
        return await self.agent.get(self.session, path, self.cfg.source_timeout)

    # ---- build -----------------------------------------------------------------------------------
    async def get(self, max_age: float | None = None) -> Built:
        """The cached summary if younger than max_age (default summary_ttl), else a fresh build (one at a
        time; concurrent callers share it)."""
        max_age = self.cfg.summary_ttl if max_age is None else max_age
        loop = asyncio.get_running_loop()
        cur = self.current
        if cur is not None and loop.time() - cur.built_at < max_age:
            return cur
        async with self._lock:
            cur = self.current
            if cur is not None and loop.time() - cur.built_at < max_age:
                return cur
            return await self.build()

    async def build(self) -> Built:
        g = self._guard
        (
            broker_status,
            broker_models,
            webui,
            containers,
            timers,
            agent_services,
            agent_ollama,
            agent_disk,
            aibox,
            clock,
        ) = await asyncio.gather(
            g(self._broker_json("/broker/status")),
            self._broker_models_cached(),
            g(self._webui()),
            g(self.helper.call("containers", self.cfg.source_timeout)),
            g(self.helper.call("timers", self.cfg.source_timeout)),
            g(self._agent("/v1/services")),
            g(self._agent("/v1/ollama")),
            g(self._agent("/v1/disk")),
            g(self._aibox()),
            g(chrony(self.cfg.chronyc, self.cfg.source_timeout)),
        )
        tags, ps = broker_models
        now = self.clock()

        # broker
        if isinstance(broker_status, Failed):
            bstatus = {}
        else:
            bstatus = broker_status[0] if isinstance(broker_status[0], dict) else {}
        aliases_raw = bstatus.get("aliases") if isinstance(bstatus.get("aliases"), dict) else {}
        aliases = {
            str(k): {"gpu": _text(v.get("gpu"), 200), "cpu": _text(v.get("cpu"), 200)}
            for k, v in aliases_raw.items()
            if isinstance(v, dict)
        }

        # helper containers
        docker_state, cmap = "unknown", {}
        if not isinstance(containers, Failed):
            try:
                docker_state, cmap = containers_view(containers)
            except SourceError as exc:
                containers = Failed(str(exc))
        if isinstance(timers, Failed):
            timers_section: Any = timers.section()
        else:
            try:
                timers_section = pinned_list(timers, "timers", "timers")
            except SourceError as exc:
                timers = Failed(str(exc))
                timers_section = timers.section()

        # host agent Y: strict views; a reply in the wrong shape fails like an unreachable call
        def view(raw: Any, fn: Callable[[Any], Any]) -> Any:
            if isinstance(raw, Failed):
                return raw
            try:
                return fn(raw)
            except SourceError as exc:
                return Failed(str(exc))

        agent_services = view(agent_services, agent_services_view)
        agent_ollama = view(agent_ollama, agent_ollama_view)
        agent_disk = view(agent_disk, agent_disk_view)

        # Every section is post-processed under its own guard (see safe()).
        routing = safe("routing", lambda: self._routing(broker_status, bstatus, now))
        services = self._services(broker_status, webui, containers, docker_state, cmap, timers, agent_services,
                                  agent_ollama, agent_disk, now)
        models = self._models(tags, ps, agent_ollama, aliases, broker_status)
        resources = self._resources(aibox, agent_disk, agent_ollama)
        clock_section = clock.section() if isinstance(clock, Failed) else safe("clock", lambda: clock)
        timers_section = safe("timers", lambda: timers_section)

        data: dict[str, Any] = {
            "server": {"time": int(now), "timeZone": self.timezone, "version": __version__, "identity": None},
            "routing": routing,
            "services": services,
            "resources": resources,
            "models": models,
            "clock": clock_section,
            "timers": timers_section,
        }
        data["alerts"] = self._alerts(data)
        core = {k: v for k, v in data.items() if k != "server"}
        core["server"] = {"timeZone": self.timezone, "version": __version__}
        core_hash = hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]
        built = Built(data, core_hash, asyncio.get_running_loop().time())
        prev = self.current
        self.current = built
        if prev is None or prev.core_hash != built.core_hash:
            for cb in list(self.on_change):
                try:
                    cb(built)
                except Exception:  # noqa: BLE001
                    log.exception("summary change callback failed")
        return built

    async def run(self) -> None:
        """Rebuild every summary_interval so /api/events can ping on change and `since` stays accurate."""
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            try:
                async with self._lock:
                    await self.build()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("summary build failed")
            await asyncio.sleep(max(0.0, self.cfg.summary_interval - (loop.time() - started)))

    # ---- sections --------------------------------------------------------------------------------
    def _gate_view(self, now: float) -> tuple[dict[str, Any] | None, str | None]:
        gate, error, _kind, _http = self._gate_detail(now)
        return gate, error

    def _gate_detail(self, now: float) -> tuple[dict[str, Any] | None, str | None, str | None, int | None]:
        """(gate status | None, error text | None, error kind | None, gate HTTP status | None).
        Kinds: http (the gate answered non-200), network (no answer), shape (an unusable answer),
        stale (no sample yet, or the last good one is too old)."""
        obs = self.sampler.last
        if obs is None:
            return None, "no gate sample yet", "stale", None
        if obs.error is not None:
            return None, obs.error, obs.kind or "network", obs.http_status
        if now - obs.at > 3 * self.sampler.interval + self.cfg.source_timeout:
            return None, "gate sample is stale", "stale", obs.http_status
        return obs.status, None, None, obs.http_status

    def _routing(self, broker_status: Any, bstatus: dict[str, Any], now: float) -> dict[str, Any]:
        gate, gate_error, gate_error_kind, gate_http_status = self._gate_detail(now)
        g = gate or {}
        if isinstance(broker_status, Failed):
            to, reason = None, "broker_down"
        else:
            rt = bstatus.get("routing_to")
            if rt in ("gpu", "cpu"):
                to = rt
            elif gate is not None:
                to = "gpu" if g.get("available") else "cpu"
            else:
                to = "cpu"  # the broker treats any gate failure as "GPU unavailable"
            reason = _text(g.get("reason"), 64) if gate is not None else None
        uid_match = g.get("uid_match")
        return {
            "to": to,
            "reason": reason,
            "avg30": g.get("avg30") if isinstance(g.get("avg30"), (int, float)) else None,
            "game": _text(g.get("game"), 200),
            "gpu_busy": g.get("gpu_busy") if isinstance(g.get("gpu_busy"), (int, float)) else None,
            "ollama_share": g.get("ollama_share") if isinstance(g.get("ollama_share"), (int, float)) else None,
            "uid_match": uid_match if isinstance(uid_match, bool) else None,
            "threshold": self.cfg.gate_threshold,
            "window_s": self.cfg.gate_window,
            "gate_error": gate_error,
            "gate_error_kind": gate_error_kind,
            "gate_http_status": gate_http_status,
        }

    def _since_for(self, sid: str, status: str, now: float) -> int:
        prev = self._since.get(sid)
        if prev is None or prev[0] != status:
            self._since[sid] = (status, now)
            return int(now)
        return int(prev[1])

    def _services(self, broker_status, webui, containers, docker_state, cmap, timers, agent_services,
                  agent_ollama, agent_disk, now) -> list[dict[str, Any]]:
        def gate_row() -> tuple[str, str]:
            obs = self.sampler.last
            gate, gate_error = self._gate_view(now)
            if gate is not None:
                return "ok", f"available={gate.get('available')}, {gate.get('reason')}"
            if obs is not None and obs.http_status == 503:
                return "warn", gate_error or ""
            if obs is None:
                return "unknown", gate_error or ""
            return "down", gate_error or ""

        def pc_ollama_row() -> tuple[str, str]:
            if isinstance(agent_services, Failed):
                return "unknown", f"host agent: {agent_services.error}"
            return unit_status(agent_services.get("ollama"))

        def cpu_ollama_row() -> tuple[str, str]:
            if isinstance(containers, Failed):
                return "unknown", f"helper: {containers.error}"
            return container_status(cmap.get("ollama-cpu"), docker_state)

        def broker_row() -> tuple[str, str]:
            if isinstance(broker_status, Failed):
                detail = broker_status.error
                if "gpu-broker" in cmap:
                    detail += f"; container {container_status(cmap['gpu-broker'], docker_state)[1]}"
                return "down", detail
            data = broker_status[0] if isinstance(broker_status[0], dict) else {}
            rt = data.get("routing_to")
            return "ok", f"routing to {rt}" if rt in ("gpu", "cpu") else "answering"

        def webui_row() -> tuple[str, str]:
            if isinstance(webui, Failed):
                return "down", webui.error
            if webui != 200:
                return "down", f"HTTP {webui}"
            cst = container_status(cmap["open-webui"], docker_state) if "open-webui" in cmap else None
            return ("warn", cst[1]) if cst and cst[0] == "warn" else ("ok", "healthy")

        def helper_row() -> tuple[str, str]:
            fail = next((c for c in (containers, timers) if isinstance(c, Failed)), None)
            return ("down", fail.error) if fail else ("ok", f"docker {docker_state}")

        def agent_row() -> tuple[str, str]:
            fail = next((c for c in (agent_services, agent_ollama, agent_disk) if isinstance(c, Failed)), None)
            return ("down", fail.error) if fail else ("ok", "answering")

        rows = {"gate": gate_row, "pc-ollama": pc_ollama_row, "cpu-ollama": cpu_ollama_row, "broker": broker_row,
                "webui": webui_row, "helper": helper_row, "agent": agent_row}
        out = []
        for sid in SERVICE_IDS:
            status, detail = safe(f"service {sid}", lambda f=rows[sid]: tuple(f()), ("unknown", BAD_DATA))
            out.append({"id": sid, "status": status, "detail": _text(detail), "since": self._since_for(sid, status, now)})
        return out

    def _models(self, tags, ps, agent_ollama, aliases, broker_status) -> dict[str, Any]:
        def gpu() -> tuple[Any, Any]:
            if isinstance(agent_ollama, Failed):
                return agent_ollama.section(), agent_ollama.section()
            return agent_ollama

        def cpu() -> Any:
            if isinstance(tags, Failed):
                return tags.section()
            data, headers = tags
            models = _list_in(data, "models")
            if headers.get("X-Broker-Warning"):
                return {"error": "the broker can't reach the CPU Ollama"}
            if models is None:
                return {"error": "broker tags reply has no model list"}
            return [_model_entry(m) for m in models if isinstance(m.get("name"), str) and m["name"] not in aliases]

        def cpu_loaded() -> Any:
            if isinstance(ps, Failed):
                return ps.section()
            data, headers = ps
            pl = _list_in(data, "models")
            if headers.get("X-Backend") != "cpu":
                return {"error": "not visible while the broker routes to the GPU"}
            if pl is None:
                return {"error": "broker ps reply has no model list"}
            return [_loaded_entry(m) for m in pl if isinstance(m.get("name"), str)]

        gpu_models, gpu_loaded = safe("gpu models", gpu, ({"error": BAD_DATA}, {"error": BAD_DATA}))
        return {
            "gpu": gpu_models,
            "cpu": safe("cpu models", cpu),
            "loaded": {"gpu": gpu_loaded, "cpu": safe("cpu loaded models", cpu_loaded)},
            "alias": broker_status.section() if isinstance(broker_status, Failed) else safe("aliases", lambda: aliases),
        }

    def _resources(self, aibox, agent_disk, agent_ollama) -> dict[str, Any]:
        def vram() -> Any:
            # null only when the PC positively reports nothing loaded; unknown is an {error}, never null
            # (W renders null as "No model is loaded on the GPU").
            if isinstance(agent_ollama, Failed):
                return agent_ollama.section()
            loaded = agent_ollama[1]
            if not isinstance(loaded, list):
                return loaded  # that half's own {"error"}
            if not loaded:
                return None
            top = max(loaded, key=lambda m: m["size_vram"] or 0)
            return {"model": top["name"], "size_vram": top["size_vram"]}

        return {
            "aibox": aibox.section() if isinstance(aibox, Failed) else safe("ai-box resources", lambda: aibox),
            "pc": agent_disk.section() if isinstance(agent_disk, Failed) else safe("pc disk", lambda: {"disk": agent_disk}),
            "vram": safe("vram", vram, {"error": BAD_DATA}),
        }

    def _alerts(self, data: dict[str, Any]) -> list[dict[str, str]]:
        """Each rule group runs under its own guard; a group that trips on bad data adds one warn alert
        saying so instead of breaking the summary."""

        def services() -> list[dict[str, str]]:
            out = []
            routing = data["routing"]
            for svc in data["services"]:
                sid = svc["id"]
                if sid == "gate":
                    if svc["status"] in ("down", "warn", "unknown") and routing.get("gate_error"):
                        out.append({"id": "gate-unreachable", "level": "warn",
                                    "text": f"The GPU gate can't be read ({routing['gate_error']}); answers go to the ai-box CPU."})
                    continue
                if svc["status"] == "down":
                    level = "crit" if sid in CRIT_WHEN_DOWN else "warn"
                    out.append({"id": f"down:{sid}", "level": level, "text": f"{SERVICE_NAMES[sid]} is down: {svc['detail']}"})
            return out

        def aliases() -> list[dict[str, str]]:
            out = []
            models = data["models"]
            spec_all = models.get("alias")
            if not isinstance(spec_all, dict) or "error" in spec_all:
                return out
            for alias, spec in sorted(spec_all.items()):
                for side in ("gpu", "cpu"):
                    have = models.get(side)
                    want = spec.get(side)
                    if not isinstance(want, str) or not want or not isinstance(have, list):
                        continue
                    names = {norm_model(m["name"]) for m in have if isinstance(m.get("name"), str)}
                    if norm_model(want) not in names:
                        where = "the gaming PC" if side == "gpu" else "the ai-box CPU Ollama"
                        out.append({"id": f"alias-missing:{alias}:{side}", "level": "warn",
                                    "text": f"Alias {alias} needs {want} on {where}, but it isn't there."})
            return out

        def disks() -> list[dict[str, str]]:
            out = []
            for host, label in (("aibox", "ai-box"), ("pc", "gaming PC")):
                section = data["resources"].get(host)
                if not isinstance(section, dict) or not isinstance(section.get("disk"), list):
                    continue
                for d in section["disk"]:
                    total, avail = d.get("total"), d.get("avail")
                    if not _is_int(total) or not _is_int(avail) or total <= 0:
                        continue
                    pct = avail * 100.0 / total
                    if pct < self.cfg.disk_low_pct:
                        level = "crit" if pct < self.cfg.disk_low_pct / 2 else "warn"
                        out.append({"id": f"disk-low:{host}:{d.get('mount')}", "level": level,
                                    "text": f"{label} {d.get('mount')} has {pct:.1f}% free."})
            return out

        def clock() -> list[dict[str, str]]:
            c = data["clock"]
            if isinstance(c, dict) and c.get("synced") is False:
                return [{"id": "clock-unsynced", "level": "warn", "text": "ai-box's clock isn't synchronised (chrony)."}]
            return []

        alerts: list[dict[str, str]] = []
        for name, rule in (("services", services), ("aliases", aliases), ("disks", disks), ("clock", clock)):
            alerts += safe(f"alerts/{name}", rule, [{"id": f"alert-rule-failed:{name}", "level": "warn",
                                                     "text": f"The {name} alert rules couldn't run on bad data from a source."}])
        return alerts


def etag_for(built: Built, identity: dict[str, str] | None) -> str:
    ident = json.dumps(identity, sort_keys=True)
    return '"' + hashlib.sha256(f"{built.core_hash}|{ident}".encode()).hexdigest()[:32] + '"'
