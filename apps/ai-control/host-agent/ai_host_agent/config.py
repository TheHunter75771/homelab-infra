"""JSON config with a default for every value. Unknown keys are an error (catches typos)."""

import ipaddress
import json
import os
from urllib.parse import urlsplit

DEFAULTS = {
    "binds": ["127.0.0.1"],          # IP literals; the role adds the gaming PC's tailnet IP
    "port": 11510,
    "token_file": "/etc/ai-host-agent/token",
    "ollama_url": "http://127.0.0.1:11434",  # host: 127.0.0.1 or one of `binds`
    "ollama_timeout": 5,             # seconds, per Ollama call (two calls per /v1/ollama)
    "ollama_max_bytes": 1048576,     # a bigger Ollama reply is refused, not parsed
    "exec_path": "/usr/bin:/bin",    # PATH for systemctl/journalctl (the child env is fixed)
    "exec_timeout": 10,              # seconds, per subprocess (contract: <= 10)
    "exec_max_bytes": 8388608,       # stdout cap per subprocess; the process is killed past it
    "cgroup_root": "/sys/fs/cgroup",
    "mountinfo": "/proc/self/mountinfo",
    "sudo_exes": ["/usr/bin/sudo"],  # journal _EXE of the sudo binaries (Fedora: one)
    "disk_paths": ["/", "/mnt/share"],  # "/" always; the others only while mounted
    "disk_timeout": 2,               # seconds per statfs (a dead network mount can hang it)
    "max_concurrent": 16,            # requests served at once (contract: <= 16); more get 503
    "conn_timeout": 5,               # seconds per socket read/write
    "head_timeout": 2,               # seconds to receive the request line + headers
    "request_deadline": 30,          # seconds from accept to the end of the response
    "log_level": "INFO",
}

# key -> (min, max), inclusive
_NUMBERS = {
    "ollama_timeout": (0.5, 10),
    "exec_timeout": (0.5, 10),
    "disk_timeout": (0.1, 10),
    "conn_timeout": (0.1, 60),
    "head_timeout": (0.1, 60),
    "request_deadline": (1, 300),
}
_INTS = {
    "port": (1, 65535),
    "ollama_max_bytes": (1024, 16 * 1048576),
    "exec_max_bytes": (65536, 64 * 1048576),
    "max_concurrent": (1, 16),
}
_ABS_PATHS = ("token_file", "cgroup_root", "mountinfo")


class ConfigError(ValueError):
    pass


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _abs_path(v):
    return (isinstance(v, str) and v.startswith("/") and "\0" not in v
            and ".." not in v.split("/"))


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
    for k, (lo, hi) in _INTS.items():
        v = cfg[k]
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise ConfigError("%s must be an integer from %s to %s" % (k, lo, hi))
    for k in _ABS_PATHS:
        if not _abs_path(cfg[k]):
            raise ConfigError("%s must be an absolute path" % k)

    binds = cfg["binds"]
    if not isinstance(binds, list) or not binds:
        raise ConfigError("binds must be a non-empty list of IP addresses")
    for b in binds:
        try:
            ipaddress.ip_address(b)
        except (TypeError, ValueError):
            raise ConfigError("binds must be IP addresses, not %r" % (b,)) from None

    paths = cfg["disk_paths"]
    if not isinstance(paths, list) or not paths or not all(_abs_path(p) for p in paths):
        raise ConfigError("disk_paths must be a non-empty list of absolute paths")
    cfg["disk_paths"] = [os.path.normpath(p) for p in paths]

    exes = cfg["sudo_exes"]
    if (not isinstance(exes, list) or not 1 <= len(exes) <= 8
            or not all(_abs_path(e) and e != "/" and not e.endswith("/")
                       and not any(c.isspace() for c in e) for e in exes)
            or len(set(exes)) != len(exes)):
        raise ConfigError("sudo_exes must be 1-8 distinct absolute paths of sudo binaries")

    ep = cfg["exec_path"]
    if not isinstance(ep, str) or not ep or not all(_abs_path(d) for d in ep.split(":")):
        raise ConfigError("exec_path must be absolute directories joined by ':'")

    # Ollama's host: 127.0.0.1, or exactly one of the agent's own bind addresses (the gaming PC's
    # Ollama listens on its tailnet IP only, §12.10). An IP literal, compared as an address:
    # never a hostname, so DNS can't point the agent anywhere else.
    url = cfg["ollama_url"]
    try:
        u = urlsplit(url) if isinstance(url, str) else None
        port = u.port if u else None
        host = ipaddress.ip_address(u.hostname) if u and u.hostname else None
    except ValueError:
        u = host = None
    allowed = {ipaddress.ip_address("127.0.0.1")} | {ipaddress.ip_address(b) for b in binds}
    if (u is None or u.scheme != "http" or host not in allowed or u.username is not None
            or u.password is not None or u.path not in ("", "/") or u.query or u.fragment):
        raise ConfigError("ollama_url must be http://<127.0.0.1 or a binds address>:<port>")
    cfg["ollama_host"] = u.hostname
    cfg["ollama_port"] = port or 80

    level = cfg["log_level"].upper() if isinstance(cfg["log_level"], str) else ""
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError("log_level must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
    cfg["log_level"] = level
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
