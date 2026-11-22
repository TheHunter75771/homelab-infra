"""The voice profile's JSON config (``/etc/voice-profile/config.json``). Unknown keys are refused,
so a typo cannot silently fall back to a default (the drafter's rule)."""
from __future__ import annotations

import json
import urllib.parse
from typing import Any

MiB = 1024 * 1024

DEFAULTS: dict[str, Any] = {
    "library_root": None,             # required: the sentinel-checked library mount
    "broker_url": None,               # required: the GPU broker, e.g. http://<ai-box tailnet ip>:11434
    "model": "assistant",             # the broker alias
    "num_ctx": 16384,                 # Ollama context window asked for (prompt + reply must fit)
    "timeout": 1800,                  # one broker call, in seconds (the CPU model writes ~8 tok/s)
    # caps
    "max_samples": 200,               # .docx files read per run
    "max_docx_bytes": 5 * MiB,        # one .docx on disk (coursework is far smaller)
    "max_zip_entries": 2000,          # entries in one .docx
    "max_uncompressed_bytes": 200 * MiB,  # declared total of all entries
    "max_xml_bytes": 8 * MiB,         # word/document.xml, actually read (memory: MemoryMax=256M)
    "max_ratio": 200,                 # uncompressed / compressed, per entry and in total
    "min_words": 150,                 # a cleaned sample shorter than this is not kept
    "dedupe_jaccard": 0.6,            # 5-word shingles, document level
    "excerpts": 6,                    # excerpts in the prompt (at least 4 are needed)
    "excerpt_chars": 1200,            # one excerpt
}

# key: (types, minimum, maximum)
_NUMBERS = {
    "num_ctx": ((int,), 2048, 262_144),
    "timeout": ((int, float), 5, 7200),
    "max_samples": ((int,), 1, 2000),
    "max_docx_bytes": ((int,), 1024, 25 * MiB),
    "max_zip_entries": ((int,), 10, 100_000),
    "max_uncompressed_bytes": ((int,), 1024, 2048 * MiB),
    "max_xml_bytes": ((int,), 1024, 8 * MiB),
    "max_ratio": ((int, float), 2, 10_000),
    "min_words": ((int,), 1, 100_000),
    "dedupe_jaccard": ((int, float), 0.1, 1.0),
    "excerpts": ((int,), 4, 6),
    "excerpt_chars": ((int,), 200, 4000),
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
