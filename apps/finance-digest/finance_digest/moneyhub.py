"""The Money Hub reader: the only code that talks to Money Hub, and it can only read.

Money Hub has no auth, so read-only is enforced here (CONTRACTS section 12.14, section 12.18.2):
* a frozen allowlist of six read paths, each with its own validated query parameters;
* every request is a GET with no body (checked again just before sending);
* the shared opener refuses redirects and ignores proxies; the body is capped and the whole
  request has a deadline.
The sync trigger is not on the allowlist. Nothing here can form another path, method or body.
"""
from __future__ import annotations

import json
import types
import urllib.parse
import urllib.request

from . import httpcore
from .text import valid_month

ALLOWED = types.MappingProxyType(
    {
        "/api/state": frozenset(),
        "/api/accounts": frozenset(),
        "/api/budget": frozenset({"month"}),
        "/api/bills": frozenset({"period"}),
        "/api/debts": frozenset(),
        "/api/payday": frozenset({"period", "check"}),
    }
)
CHECKS = ("1st", "15th")
MAX_BYTES = 2 * 1024 * 1024
DEADLINE_FACTOR = 3


class MoneyHubError(Exception):
    """A read failed; the message is safe to show (no URL, no traceback)."""


def validate_params(path: str, params: dict | None) -> list[tuple[str, str]]:
    params = {} if params is None else params
    if not isinstance(params, dict):
        raise ValueError("query parameters must be a dict")
    allowed = ALLOWED[path]
    out = []
    for key in sorted(params):
        if key not in allowed:
            raise ValueError(f"parameter {key!r} is not allowed for {path}")
        value = params[key]
        if key in ("month", "period"):
            if not valid_month(value):
                raise ValueError(f"{key} must be YYYY-MM")
        elif key == "check":
            if value not in CHECKS:
                raise ValueError("check must be 1st or 15th")
        out.append((key, value))
    return out


class MoneyHub:
    def __init__(self, url: str, timeout_s: int = 10) -> None:
        self._url = url
        self._timeout = min(max(int(timeout_s), 1), 60)

    def get(self, path: str, params: dict | None = None):
        if not isinstance(path, str) or path not in ALLOWED:
            raise ValueError("path is not in the read-only allowlist")
        query = validate_params(path, params)
        try:
            url = httpcore.base_url(self._url, "the Money Hub URL") + path
        except httpcore.HttpFailure as e:
            raise MoneyHubError(f"{e}.") from None
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url, method="GET", headers={"Accept": "application/json", "User-Agent": "finance-digest"}
        )
        if request.get_method() != "GET" or request.data is not None:  # defence in depth
            raise ValueError("the Money Hub reader only sends GET requests without a body")
        try:
            body = httpcore.send(
                request,
                timeout=self._timeout,
                total=DEADLINE_FACTOR * self._timeout,
                max_bytes=MAX_BYTES,
                what=f"Money Hub {path}",
            )
        except httpcore.HttpFailure as e:
            raise MoneyHubError(f"{e}.") from None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise MoneyHubError(f"Money Hub returned a response for {path} that is not JSON.") from None
