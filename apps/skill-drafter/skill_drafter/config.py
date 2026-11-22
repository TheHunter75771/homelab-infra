"""The drafter's JSON config (``/etc/skill-drafter/config.json``). Unknown keys are refused, so a
typo cannot silently fall back to a default (the research gate's rule)."""
from __future__ import annotations

import json
import urllib.parse
from typing import Any

DEFAULTS: dict[str, Any] = {
    "library_root": None,           # required: the sentinel-checked library mount
    "broker_url": None,             # required: the GPU broker, e.g. http://<ai-box tailnet ip>:11434
    "model": "assistant",           # the broker alias (§12.12 "Models chosen")
    "poll_interval": 120,
    "max_source_chars": 16000,      # prompt budget for all source texts together
    "num_ctx": 16384,               # Ollama context window asked for (prompt + reply must fit)
    "timeout": 1200,                # one broker call, in seconds (the CPU model writes ~8 tok/s)
    "max_attempts": 3,              # per drafting round (CONTRACTS §12.19.1)
}

# key: (types, minimum, maximum)
_NUMBERS = {
    "poll_interval": ((int, float), 5, 86400),
    "max_source_chars": ((int,), 1000, 200_000),
    "num_ctx": ((int,), 2048, 262_144),
    "timeout": ((int, float), 5, 7200),
    "max_attempts": ((int,), 1, 10),
}
_MODEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-/")


class ConfigError(ValueError):
    pass


def check_broker_url(url: object) -> str:
    """``scheme://host[:port]`` of a plain http(s) URL with no path, query, fragment or user info."""
    bad = ConfigError("broker_url must be a plain http(s)://host:port address with no path")
    if not isinstance(url, str):
        raise bad
    try:
        parts = urllib.parse.urlsplit(url.strip())
        _ = parts.port
    except ValueError:
        raise bad from None
    if (parts.scheme not in ("http", "https") or not parts.hostname or "@" in parts.netloc
            or parts.query or parts.fragment or parts.path not in ("", "/")):
        raise bad
    return f"{parts.scheme}://{parts.netloc}"


def validate(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError("the config must be a JSON object")
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(unknown)}")
    cfg = dict(DEFAULTS)
    cfg.update(raw)
    root = cfg["library_root"]
    if not isinstance(root, str) or not root.startswith("/"):
        raise ConfigError("library_root is required and must be an absolute path")
    cfg["broker_url"] = check_broker_url(cfg["broker_url"])
    model = cfg["model"]
    if not isinstance(model, str) or not 1 <= len(model) <= 100 or not set(model) <= _MODEL_CHARS:
        raise ConfigError("model must be 1-100 characters of A-Z a-z 0-9 . _ : - /")
    for key, (types, lo, hi) in _NUMBERS.items():
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, types) or not lo <= value <= hi:
            raise ConfigError(f"{key} must be a number from {lo} to {hi}")
    return cfg


def load(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from None
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    return validate(raw)
