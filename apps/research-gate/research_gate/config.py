"""The gate's JSON config (``/etc/research-gate/config.json``). Unknown keys are refused, so a
typo cannot silently fall back to a default. There is deliberately no key that relaxes the
address rules (tests inject that; production cannot)."""
from __future__ import annotations

import json
import urllib.parse
from typing import Any

DEFAULTS: dict[str, Any] = {
    "library_root": None,                 # required: the sentinel-checked library mount
    "searxng_url": "http://127.0.0.1:8888",
    "poll_interval": 60,
    "results_per_query": 8,
    "max_bytes": 5 * 1024 * 1024,
    "fetch_timeout": 30,
    "max_redirects": 3,
    "user_agent": "homelab-research-gate/1 (fetches only pages a human approved)",
    "ipv4_only": False,                   # resolve and connect over IPv4 only (the unit is AF_INET-only)
}

# key: (type(s), minimum, maximum)
_NUMBERS = {
    "poll_interval": ((int, float), 5, 86400),
    "results_per_query": ((int,), 1, 50),
    "max_bytes": ((int,), 1024, 100 * 1024 * 1024),
    "fetch_timeout": ((int, float), 1, 300),
    "max_redirects": ((int,), 0, 3),
}


class ConfigError(ValueError):
    pass


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
    for key, (types, lo, hi) in _NUMBERS.items():
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, types) or not lo <= value <= hi:
            raise ConfigError(f"{key} must be a number from {lo} to {hi}")
    url = cfg["searxng_url"]
    parts = urllib.parse.urlsplit(url) if isinstance(url, str) else None
    if (parts is None or parts.scheme not in ("http", "https") or not parts.hostname
            or parts.query or parts.fragment or parts.username or parts.password):
        raise ConfigError("searxng_url must be a plain http(s) URL such as http://127.0.0.1:8888")
    if not isinstance(cfg["ipv4_only"], bool):
        raise ConfigError("ipv4_only must be true or false")
    ua = cfg["user_agent"]
    if not isinstance(ua, str) or not ua or not all(0x20 <= ord(c) < 0x7F for c in ua) or len(ua) > 200:
        raise ConfigError("user_agent must be 1-200 printable ASCII characters")
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
