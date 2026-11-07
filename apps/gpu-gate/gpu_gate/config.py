"""JSON config with a default for every value. Unknown keys are an error (catches typos)."""

import json
import re

DEFAULTS = {
    "interval": 5,                 # seconds between samples
    "window": 1800,                # trailing window for avg30, seconds
    "threshold": 50,               # avg30 at or above this (with a game) = unavailable
    "card": None,                  # DRM device dir, e.g. "/sys/class/drm/card1/device"; None = auto
    "sysfs_root": "/sys",
    "proc_root": "/proc",
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_timeout": 5,           # seconds, per Ollama HTTP call
    "ollama_match_uid": True,      # processes with the gate's own real uid are Ollama's
    "ollama_match": ["ollama", "llama-server"],  # plus comm/argv0 substrings (fallback)
    "ollama_engines": ["gfx", "compute"],  # fdinfo drm-engine-<name> counters in ollama_share
    "binds": ["127.0.0.1"],
    "port": 11500,
    "games": [],                   # allow-list: comm/argv0 substrings that always count as a game
    "ignore": [],                  # deny-list: comm/argv0 substrings that never count as a game
    "log_level": "INFO",
}

_ENGINE = re.compile(r"^[a-z0-9_-]+$")  # drm-engine-<name>

# key -> (min, max), inclusive
_NUMBERS = {
    "interval": (0.5, 3600),
    "window": (0.5, 7 * 86400),     # and >= interval (checked below)
    "threshold": (1, 100),
    "ollama_timeout": (0.5, 60),
}
_STR_LISTS = ("ollama_match", "ollama_engines", "binds", "games", "ignore")
_STRS = ("sysfs_root", "proc_root", "ollama_url", "log_level")


class ConfigError(ValueError):
    pass


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate(raw):
    """Merge `raw` over the defaults and validate. Returns a new dict."""
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise ConfigError("unknown config keys: %s" % ", ".join(unknown))
    cfg = dict(DEFAULTS)
    cfg.update(raw)

    for k, (lo, hi) in _NUMBERS.items():
        if not _is_number(cfg[k]) or not lo <= cfg[k] <= hi:
            raise ConfigError("%s must be a number from %s to %s" % (k, lo, hi))
    for k in _STR_LISTS:
        v = cfg[k]
        if not isinstance(v, list) or not all(isinstance(s, str) and s for s in v):
            raise ConfigError("%s must be a list of non-empty strings" % k)
    for k in _STRS:
        if not isinstance(cfg[k], str) or not cfg[k]:
            raise ConfigError("%s must be a non-empty string" % k)
    if not isinstance(cfg["ollama_match_uid"], bool):
        raise ConfigError("ollama_match_uid must be true or false")
    if cfg["card"] is not None and (not isinstance(cfg["card"], str) or not cfg["card"]):
        raise ConfigError("card must be null or a path")
    level = cfg["log_level"].upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError("log_level must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
    cfg["log_level"] = level
    if not cfg["ollama_engines"] or any(not _ENGINE.match(e) for e in cfg["ollama_engines"]):
        raise ConfigError("ollama_engines must name fdinfo engines, e.g. gfx, compute")
    if cfg["window"] < cfg["interval"]:
        raise ConfigError("window must be >= interval")
    port = cfg["port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise ConfigError("port must be an integer 1-65535")
    if not cfg["binds"]:
        raise ConfigError("binds must not be empty")
    if not cfg["ollama_url"].startswith(("http://", "https://")):
        raise ConfigError("ollama_url must be http:// or https://")
    cfg["ollama_url"] = cfg["ollama_url"].rstrip("/")
    return cfg


def load(path):
    """Load and validate a config file; `None` means all defaults."""
    if path is None:
        return validate({})
    with open(path, encoding="utf-8") as f:
        try:
            raw = json.load(f)
        except ValueError as e:
            raise ConfigError("%s: invalid JSON: %s" % (path, e)) from None
    return validate(raw)
