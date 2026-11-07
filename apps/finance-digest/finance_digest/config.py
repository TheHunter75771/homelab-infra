"""The digest's JSON config (``/etc/finance-digest/config.json``). Unknown keys are refused, so a typo
cannot silently fall back to a default."""
from __future__ import annotations

import json
import zoneinfo
from typing import Any

from . import httpcore

DEFAULT_PATH = "/etc/finance-digest/config.json"

DEFAULTS: dict[str, Any] = {
    "moneyhub_url": None,                       # required: http(s)://host:port
    "broker_url": None,                         # required when ai_summary is true
    "library_root": "/srv/ai-library",
    "timezone": "America/New_York",
    "ai_summary": True,
    "model": "assistant",
    "moneyhub_timeout_s": 10,
    "broker_timeout_s": 120,
    "notion_file": "/etc/finance-digest/notion.json",
    "notion_timeout_s": 20,
    "bills_window_days": 7,
}

# key: (minimum, maximum), whole numbers
_NUMBERS = {
    "moneyhub_timeout_s": (1, 60),
    "broker_timeout_s": (1, 600),
    "notion_timeout_s": (1, 120),
    "bills_window_days": (1, 28),
}


class ConfigError(ValueError):
    pass


def _url(cfg: dict, key: str) -> None:
    try:
        httpcore.base_url(cfg[key], key)
    except httpcore.HttpFailure:
        raise ConfigError(f"{key} must be a plain http(s)://host:port address") from None


def validate(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError("the config must be a JSON object")
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(unknown)}")
    cfg = dict(DEFAULTS)
    cfg.update(raw)
    _url(cfg, "moneyhub_url")
    if not isinstance(cfg["ai_summary"], bool):
        raise ConfigError("ai_summary must be true or false")
    if cfg["ai_summary"] or cfg["broker_url"] is not None:
        _url(cfg, "broker_url")
        if httpcore.same_server(cfg["broker_url"], cfg["moneyhub_url"]):
            raise ConfigError("broker_url must not be the Money Hub address")
    for key, (lo, hi) in _NUMBERS.items():
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise ConfigError(f"{key} must be a whole number from {lo} to {hi}")
    for key in ("library_root", "notion_file"):
        if not isinstance(cfg[key], str) or not cfg[key].startswith("/"):
            raise ConfigError(f"{key} must be an absolute path")
    model = cfg["model"]
    if not isinstance(model, str) or not 1 <= len(model) <= 100 or not all(0x21 <= ord(c) < 0x7F for c in model):
        raise ConfigError("model must be 1-100 printable ASCII characters without spaces")
    try:
        zoneinfo.ZoneInfo(str(cfg["timezone"]))
    except Exception:
        raise ConfigError("timezone must be an IANA zone such as America/New_York") from None
    return cfg


def load(path: str) -> dict[str, Any]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read(256 * 1024)
    except OSError as e:
        raise ConfigError(f"{path} can't be read ({e.strerror})") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ConfigError(f"{path} is not valid JSON") from None
    return validate(data)
