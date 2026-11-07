"""Server configuration: one JSON file; every key has a default (the URL defaults are placeholders that the
deploy role overrides)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# /api/logs sources and their picker labels (/api/logs/sources). ai-box ids go to helper X (its allowlist,
# §12.15.2); the two pc: ids go to host agent Y.
DEFAULT_LOG_SOURCES = [
    {"id": "homelab-ai-stack", "label": "ai-box: AI stack startup"},
    {"id": "ollama-cpu", "label": "ai-box: CPU Ollama (container)"},
    {"id": "gpu-broker", "label": "ai-box: GPU broker (container)"},
    {"id": "open-webui", "label": "ai-box: Open WebUI (container)"},
    {"id": "docker", "label": "ai-box: Docker"},
    {"id": "tailscaled", "label": "ai-box: Tailscale"},
    {"id": "chrony", "label": "ai-box: Clock sync (chrony)"},
    {"id": "ai-control", "label": "ai-box: AI Control server"},
    {"id": "aictl-helper", "label": "ai-box: AI Control helper"},
    {"id": "pc:ollama", "label": "Gaming PC: Ollama"},
    {"id": "pc:gpu-gate", "label": "Gaming PC: GPU gate"},
]
# The host agent's /v1/logs units (§12.15.3): the only pc: ids allowed.
PC_LOG_UNITS = {"pc:ollama": "ollama", "pc:gpu-gate": "gpu-gate"}

DEFAULTS: dict[str, Any] = {
    "listen_host": "127.0.0.1",
    "listen_port": 8095,
    # Sources. Placeholders: the role renders the real (tailnet) addresses.
    "gate_url": "http://127.0.0.1:11500",
    "broker_url": "http://127.0.0.1:11434",
    "webui_url": "http://127.0.0.1:8080",
    "helper_socket": "/run/aictl-helper.sock",
    "agent_url": "http://127.0.0.1:11510",
    "agent_token_file": "/etc/ai-control/agent-token",
    "chronyc": "/usr/bin/chronyc",
    "proc_root": "/proc",
    "disk_mounts": ["/"],
    # Web app (module W) files, served at /.
    "static_dir": "/opt/ai-control/web",
    # Gate sampler.
    "gate_samples_path": "/var/lib/ai-control/gate-samples.jsonl",
    "sample_interval": 10.0,
    "persist_interval": 300.0,
    "history_hours": 24.0,
    # The gate's threshold: the gate's /status doesn't report it, so it's configured here (default = gate default).
    "gate_threshold": 50,
    # The gate's averaging window (s), shown as routing.window_s (the gate doesn't report it either).
    "gate_window": 1800,
    # Timeouts (s). Summary sources are capped at 4 s (§12.15.5); API pass-through calls at 10 s.
    "source_timeout": 4.0,
    "request_timeout": 10.0,
    # Summary refresh (s) and how long a built summary is reused by /api/summary.
    "summary_interval": 10.0,
    "summary_ttl": 5.0,
    # SSE /api/events.
    "sse_max_clients": 8,
    "sse_buffer_limit": 1024 * 1024,
    "sse_write_timeout": 30.0,
    "sse_keepalive": 15.0,
    # Alerts.
    "disk_low_pct": 10.0,
    # /api/logs sources on ai-box (forwarded to helper X; X enforces its own allowlist too).
    "log_sources": DEFAULT_LOG_SOURCES,
    "timezone": None,
    "log_level": "INFO",
}

# Always used with fullmatch (a `$` would also accept a trailing newline).
_SOURCE_RE = re.compile(r"[a-z0-9][a-z0-9@._-]{0,63}")
_TZ_RE = re.compile(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    listen_host: str = DEFAULTS["listen_host"]
    listen_port: int = DEFAULTS["listen_port"]
    gate_url: str = DEFAULTS["gate_url"]
    broker_url: str = DEFAULTS["broker_url"]
    webui_url: str = DEFAULTS["webui_url"]
    helper_socket: str = DEFAULTS["helper_socket"]
    agent_url: str = DEFAULTS["agent_url"]
    agent_token_file: str = DEFAULTS["agent_token_file"]
    chronyc: str = DEFAULTS["chronyc"]
    proc_root: str = DEFAULTS["proc_root"]
    disk_mounts: tuple[str, ...] = tuple(DEFAULTS["disk_mounts"])
    static_dir: str = DEFAULTS["static_dir"]
    gate_samples_path: str = DEFAULTS["gate_samples_path"]
    sample_interval: float = DEFAULTS["sample_interval"]
    persist_interval: float = DEFAULTS["persist_interval"]
    history_hours: float = DEFAULTS["history_hours"]
    gate_threshold: float = DEFAULTS["gate_threshold"]
    gate_window: int = DEFAULTS["gate_window"]
    source_timeout: float = DEFAULTS["source_timeout"]
    request_timeout: float = DEFAULTS["request_timeout"]
    summary_interval: float = DEFAULTS["summary_interval"]
    summary_ttl: float = DEFAULTS["summary_ttl"]
    sse_max_clients: int = DEFAULTS["sse_max_clients"]
    sse_buffer_limit: int = DEFAULTS["sse_buffer_limit"]
    sse_write_timeout: float = DEFAULTS["sse_write_timeout"]
    sse_keepalive: float = DEFAULTS["sse_keepalive"]
    disk_low_pct: float = DEFAULTS["disk_low_pct"]
    # ((id, label), ...) in picker order
    log_sources: tuple[tuple[str, str], ...] = field(
        default_factory=lambda: tuple((s["id"], s["label"]) for s in DEFAULT_LOG_SOURCES)
    )
    timezone: str | None = None
    log_level: str = DEFAULTS["log_level"]


def _url(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key, DEFAULTS[key])
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        raise ConfigError(f"{key} must be an http(s) URL")
    return value.rstrip("/")


def _abs_path(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key, DEFAULTS[key])
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
        raise ConfigError(f"{key} must be an absolute path")
    return value


def _number(raw: dict[str, Any], key: str, minimum: float, maximum: float | None = None) -> float:
    value = raw.get(key, DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise ConfigError(f"{key} must be a number >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{key} must be <= {maximum}")
    return float(value)


def _int(raw: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = raw.get(key, DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be an integer {minimum}-{maximum}")
    return value


def from_dict(raw: dict[str, Any]) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")

    mounts = raw.get("disk_mounts", DEFAULTS["disk_mounts"])
    if not isinstance(mounts, list) or not mounts or not all(isinstance(m, str) and m.startswith("/") for m in mounts):
        raise ConfigError("disk_mounts must be a non-empty list of absolute paths")

    sources = raw.get("log_sources", DEFAULTS["log_sources"])
    if not isinstance(sources, list) or not sources:
        raise ConfigError("log_sources must be a non-empty list of {id, label}")
    seen: set[str] = set()
    for s in sources:
        if (
            not isinstance(s, dict)
            or set(s) != {"id", "label"}
            or not isinstance(s["id"], str)
            or not isinstance(s["label"], str)
            or not 0 < len(s["label"]) <= 80
        ):
            raise ConfigError("each log_sources entry must be exactly {id, label} (strings, label 1-80 chars)")
        if s["id"].startswith("pc:"):
            if s["id"] not in PC_LOG_UNITS:
                raise ConfigError(f"log source {s['id']!r}: the host agent serves only {', '.join(PC_LOG_UNITS)}")
        elif not _SOURCE_RE.fullmatch(s["id"]):
            raise ConfigError(f"log source {s['id']!r} must be a unit/container name ([a-z0-9@._-])")
        if s["id"] in seen:
            raise ConfigError(f"log source {s['id']!r} is listed twice")
        seen.add(s["id"])

    tz = raw.get("timezone", None)
    if tz is not None and (not isinstance(tz, str) or not _TZ_RE.fullmatch(tz)):
        raise ConfigError("timezone must be an IANA zone name or null")

    host = raw.get("listen_host", DEFAULTS["listen_host"])
    if not isinstance(host, str) or not host:
        raise ConfigError("listen_host must be a non-empty string")

    return Config(
        listen_host=host,
        listen_port=_int(raw, "listen_port", 1, 65535),
        gate_url=_url(raw, "gate_url"),
        broker_url=_url(raw, "broker_url"),
        webui_url=_url(raw, "webui_url"),
        helper_socket=_abs_path(raw, "helper_socket"),
        agent_url=_url(raw, "agent_url"),
        agent_token_file=_abs_path(raw, "agent_token_file"),
        chronyc=_abs_path(raw, "chronyc"),
        proc_root=_abs_path(raw, "proc_root"),
        disk_mounts=tuple(mounts),
        static_dir=_abs_path(raw, "static_dir"),
        gate_samples_path=_abs_path(raw, "gate_samples_path"),
        sample_interval=_number(raw, "sample_interval", 0.05),
        persist_interval=_number(raw, "persist_interval", 0.05),
        history_hours=_number(raw, "history_hours", 0.5, 24 * 7),
        gate_threshold=_number(raw, "gate_threshold", 1, 100),
        gate_window=_int(raw, "gate_window", 60, 86400),
        source_timeout=_number(raw, "source_timeout", 0.05, 4.0),
        request_timeout=_number(raw, "request_timeout", 0.05, 10.0),
        summary_interval=_number(raw, "summary_interval", 0.05),
        summary_ttl=_number(raw, "summary_ttl", 0.0),
        sse_max_clients=_int(raw, "sse_max_clients", 1, 64),
        sse_buffer_limit=_int(raw, "sse_buffer_limit", 1024, 64 * 1024 * 1024),
        sse_write_timeout=_number(raw, "sse_write_timeout", 0.05),
        sse_keepalive=_number(raw, "sse_keepalive", 0.05),
        disk_low_pct=_number(raw, "disk_low_pct", 0.0, 100.0),
        log_sources=tuple((s["id"], s["label"]) for s in sources),
        timezone=tz,
        log_level=str(raw.get("log_level", DEFAULTS["log_level"])).upper(),
    )


def load(path: str) -> Config:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc.strerror or exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config {path} is not valid JSON: {exc}") from exc
    return from_dict(raw)
