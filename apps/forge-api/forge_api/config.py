"""The forge API's JSON config (``/etc/forge-api/config.json``) and its bearer-token file.

Unknown keys are refused, so a typo cannot silently fall back to a default. There is no key
that relaxes the peer rule: the allowed peers must be specific, non-loopback addresses that
differ from the bind address, so a process on the host itself is never an allowed peer (tests
build their config dicts directly to use loopback).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
from typing import Any

DEFAULTS: dict[str, Any] = {
    "library_root": None,             # required: the sentinel-checked library mount
    "bind": None,                     # required: the `ai` bridge gateway (or the host address open-webui reaches)
    "port": 879,                      # privileged: bound by systemd's forge-api.socket, never by the service
    "allowed_peers": ["172.20.0.4"],  # open-webui's pinned address on the `ai` network (§12.23)
    "token_file": "/etc/forge-api/token",
    "approver_user_id": None,         # required: Noah's Open WebUI user id
}
REQUIRED = ("library_root", "bind", "approver_user_id")
MAX_PEERS = 8
USER_ID_RE = re.compile(r"[A-Za-z0-9._@-]{1,64}")  # the forge's `by` rule (Forge.tick_*)

TOKEN_MIN = 32
TOKEN_FILE_MAX = 4096
TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/=-]+")  # RFC 6750 b64token


class ConfigError(ValueError):
    """Why the config (or the token file) was refused. Never contains the token."""


def ip_literal(text: object, what: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """A single IPv4/IPv6 address literal (no CIDR, no zone, no hostname), IPv4-mapped IPv6
    reduced to IPv4."""
    if not isinstance(text, str) or not text or text != text.strip() or "/" in text or "%" in text:
        raise ConfigError(f"{what} must be one IPv4 or IPv6 address, e.g. 172.20.0.1")
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        raise ConfigError(f"{what} must be one IPv4 or IPv6 address, not {text!r}") from None
    return normalise(addr)


def normalise(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def validate(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError("the config must be a JSON object")
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(unknown)}")
    cfg = dict(DEFAULTS)
    cfg.update(raw)
    missing = [k for k in REQUIRED if cfg[k] is None]
    if missing:
        raise ConfigError(f"required config key(s) missing: {', '.join(missing)}")

    root = cfg["library_root"]
    if not isinstance(root, str) or not root.startswith("/") or "\0" in root:
        raise ConfigError("library_root must be an absolute path")

    bind = ip_literal(cfg["bind"], "bind")
    if bind.is_unspecified or bind.is_multicast:
        raise ConfigError("bind must be one specific address (never 0.0.0.0 or ::); "
                          "use the `ai` bridge gateway")
    cfg["bind"] = str(bind)

    port = cfg["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("port must be a number from 1 to 65535")

    peers = cfg["allowed_peers"]
    if not isinstance(peers, list) or not 1 <= len(peers) <= MAX_PEERS:
        raise ConfigError(f"allowed_peers must be a list of 1 to {MAX_PEERS} addresses")
    clean = []
    for i, text in enumerate(peers, start=1):
        addr = ip_literal(text, f"allowed_peers entry {i}")
        if addr.is_unspecified or addr.is_multicast or addr.is_loopback:
            raise ConfigError(f"allowed_peers entry {i} ({text}) is unspecified, multicast or "
                              "loopback; list open-webui's pinned address")
        if addr == bind:
            raise ConfigError(f"allowed_peers entry {i} is the bind address itself; the host must "
                              "never count as an allowed peer")
        if str(addr) not in clean:
            clean.append(str(addr))
    cfg["allowed_peers"] = clean

    tf = cfg["token_file"]
    if not isinstance(tf, str) or not tf.startswith("/") or "\0" in tf:
        raise ConfigError("token_file must be an absolute path")

    user = cfg["approver_user_id"]
    if not isinstance(user, str) or not USER_ID_RE.fullmatch(user):
        raise ConfigError("approver_user_id must be 1-64 characters of A-Z a-z 0-9 . _ @ - "
                          "(Noah's Open WebUI user id)")
    return cfg


def load(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh, object_pairs_hook=_no_duplicates)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from None
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    return validate(raw)


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def load_token(path: str) -> str:
    """The bearer secret from ``path`` (root-generated, 0640 root:rgate). Refuses a missing,
    empty, non-regular or symlinked file, one owned by anyone but root or this process's user,
    one that is group/world-writable or world-readable, and a token shorter than 32 characters
    or outside the b64token alphabet. Error messages never contain the file's content."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        raise ConfigError(f"token file {path}: missing") from None
    except OSError as exc:
        raise ConfigError(f"token file {path}: cannot open ({exc.strerror})") from None
    try:
        st = os.fstat(fd)  # the opened file itself: no check/use race
        if not stat.S_ISREG(st.st_mode):
            raise ConfigError(f"token file {path}: not a regular file")
        if st.st_uid not in (0, os.getuid()):
            raise ConfigError(f"token file {path}: owned by uid {st.st_uid} (want root)")
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ConfigError(f"token file {path}: group/world-writable (mode {st.st_mode & 0o7777:04o})")
        if st.st_mode & stat.S_IROTH:
            raise ConfigError(f"token file {path}: world-readable (mode {st.st_mode & 0o7777:04o})")
        if st.st_size > TOKEN_FILE_MAX:
            raise ConfigError(f"token file {path}: larger than {TOKEN_FILE_MAX} bytes")
        data = os.read(fd, TOKEN_FILE_MAX + 1)
    finally:
        os.close(fd)
    try:
        token = data.decode("ascii").strip()
    except UnicodeDecodeError:
        raise ConfigError(f"token file {path}: not ASCII") from None
    if not token:
        raise ConfigError(f"token file {path}: empty")
    if not TOKEN_RE.fullmatch(token):
        raise ConfigError(f"token file {path}: one token of A-Z a-z 0-9 . _ ~ + / = - expected")
    if len(token) < TOKEN_MIN:
        raise ConfigError(f"token file {path}: shorter than {TOKEN_MIN} characters")
    return token
