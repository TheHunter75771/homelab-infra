"""Broker configuration: a JSON file, with a default for everything except the three URLs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

DEFAULTS: dict[str, Any] = {
    "listen_host": "0.0.0.0",
    "listen_port": 11434,
    # gpu-gate status cache (seconds) and the gate request timeout (seconds).
    "status_ttl": 5.0,
    "gate_timeout": 1.0,
    # TCP connect timeout to either Ollama backend (seconds).
    "connect_timeout": 3.0,
    # Optional cap on the wait for the GPU backend's first response byte (seconds).
    # null = no cap. Hitting it counts as a GPU failure and fails over to CPU.
    "gpu_first_byte_timeout": None,
    # GPU statuses that count as "failed before the first byte" and fail over to CPU.
    "failover_statuses": [500, 502, 503, 504],
    # Largest request body accepted (MiB). Chat requests with base64 images can be big.
    "max_body_mib": 64,
    # Byte budget for buffered request bodies over 1 MiB (read, rewritten, kept for the GPU->CPU
    # retry until the first response byte). Bodies of 1 MiB or less never wait for it.
    "max_buffered_bytes": 256 * 1024 * 1024,
    # Whole-body read deadline (s); a client still sending after this gets 408.
    "body_read_timeout": 30.0,
    # Longest wait for budget (s) before a large body gets 503.
    "buffer_wait_timeout": 10.0,
    # On SIGTERM: how long in-flight requests get to finish before they are cancelled (seconds).
    "shutdown_timeout": 10.0,
    "log_level": "INFO",
    "aliases": {},
}
REQUIRED = ("gate_url", "gpu_url", "cpu_url")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Alias:
    gpu: str
    cpu: str

    def for_backend(self, backend: str) -> str:
        return self.gpu if backend == "gpu" else self.cpu


@dataclass(frozen=True)
class Config:
    gate_url: str
    gpu_url: str
    cpu_url: str
    listen_host: str = DEFAULTS["listen_host"]
    listen_port: int = DEFAULTS["listen_port"]
    status_ttl: float = DEFAULTS["status_ttl"]
    gate_timeout: float = DEFAULTS["gate_timeout"]
    connect_timeout: float = DEFAULTS["connect_timeout"]
    gpu_first_byte_timeout: float | None = DEFAULTS["gpu_first_byte_timeout"]
    failover_statuses: frozenset[int] = frozenset(DEFAULTS["failover_statuses"])
    max_body_mib: int = DEFAULTS["max_body_mib"]
    max_buffered_bytes: int = DEFAULTS["max_buffered_bytes"]
    body_read_timeout: float = DEFAULTS["body_read_timeout"]
    buffer_wait_timeout: float = DEFAULTS["buffer_wait_timeout"]
    shutdown_timeout: float = DEFAULTS["shutdown_timeout"]
    log_level: str = DEFAULTS["log_level"]
    aliases: dict[str, Alias] = field(default_factory=dict)


def _url(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        raise ConfigError(f"{key} is required and must be an http(s) URL")
    return value.rstrip("/")


def _number(raw: dict[str, Any], key: str, *, allow_none: bool = False, minimum: float = 0.0) -> float | None:
    value = raw.get(key, DEFAULTS[key])
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise ConfigError(f"{key} must be a number >= {minimum}")
    return float(value)


def from_dict(raw: dict[str, Any]) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    unknown = set(raw) - set(DEFAULTS) - set(REQUIRED)
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")

    aliases: dict[str, Alias] = {}
    raw_aliases = raw.get("aliases", {})
    if not isinstance(raw_aliases, dict):
        raise ConfigError("aliases must be an object {name: {gpu: model, cpu: model}}")
    for name, spec in raw_aliases.items():
        if (
            not isinstance(spec, dict)
            or set(spec) != {"gpu", "cpu"}
            or not all(isinstance(spec[k], str) and spec[k] for k in ("gpu", "cpu"))
        ):
            raise ConfigError(f"alias {name!r} must be exactly {{gpu: model, cpu: model}} with non-empty names")
        aliases[name] = Alias(gpu=spec["gpu"], cpu=spec["cpu"])

    statuses = raw.get("failover_statuses", DEFAULTS["failover_statuses"])
    if not isinstance(statuses, list) or not all(isinstance(s, int) and 100 <= s <= 599 for s in statuses):
        raise ConfigError("failover_statuses must be a list of HTTP status codes")

    port = raw.get("listen_port", DEFAULTS["listen_port"])
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("listen_port must be an integer 1-65535")

    ints: dict[str, int] = {}
    for key in ("max_body_mib", "max_buffered_bytes"):
        value = raw.get(key, DEFAULTS[key])
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(f"{key} must be an integer >= 1")
        ints[key] = value
    if ints["max_buffered_bytes"] < ints["max_body_mib"] * 1024 * 1024:
        raise ConfigError("max_buffered_bytes must be at least max_body_mib MiB (one largest body must fit)")

    gate_timeout = _number(raw, "gate_timeout", minimum=0.01)
    connect_timeout = _number(raw, "connect_timeout", minimum=0.01)
    assert gate_timeout is not None and connect_timeout is not None
    return Config(
        gate_url=_url(raw, "gate_url"),
        gpu_url=_url(raw, "gpu_url"),
        cpu_url=_url(raw, "cpu_url"),
        listen_host=str(raw.get("listen_host", DEFAULTS["listen_host"])),
        listen_port=port,
        status_ttl=_number(raw, "status_ttl") or 0.0,
        gate_timeout=gate_timeout,
        connect_timeout=connect_timeout,
        gpu_first_byte_timeout=_number(raw, "gpu_first_byte_timeout", allow_none=True, minimum=0.01),
        failover_statuses=frozenset(statuses),
        max_body_mib=ints["max_body_mib"],
        max_buffered_bytes=ints["max_buffered_bytes"],
        body_read_timeout=_number(raw, "body_read_timeout", minimum=0.01) or 0.0,
        buffer_wait_timeout=_number(raw, "buffer_wait_timeout", minimum=0.01) or 0.0,
        shutdown_timeout=_number(raw, "shutdown_timeout") or 0.0,
        log_level=str(raw.get("log_level", DEFAULTS["log_level"])).upper(),
        aliases=aliases,
    )


def load(path: str) -> Config:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config {path} is not valid JSON: {exc}") from exc
    return from_dict(raw)
