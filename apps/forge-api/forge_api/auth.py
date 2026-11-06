"""The three checks (CONTRACTS §12.25.2): the TCP peer, the bearer secret and, on the tick
endpoints, the approver's Open WebUI user id.

* The peer is the socket's own peer address, never a header (X-Forwarded-For and friends are
  ignored entirely).
* The bearer secret is compared as SHA-256 digests with ``hmac.compare_digest``, so neither the
  content nor the length of what a client sends changes the comparison's timing.
* The approver id is compared the same way (it is not secret, but it costs nothing).
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
from collections.abc import Iterable

from .config import normalise


class PeerCheck:
    def __init__(self, allowed: Iterable[str]) -> None:
        self._allowed = frozenset(normalise(ipaddress.ip_address(a)) for a in allowed)

    def allows(self, peer: object) -> bool:
        """``peer``: the first element of the socket's peer address. Anything unparsable is refused."""
        if not isinstance(peer, str):
            return False
        try:
            addr = normalise(ipaddress.ip_address(peer.split("%", 1)[0]))
        except ValueError:
            return False
        return addr in self._allowed


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).digest()


class BearerAuth:
    """Holds only a digest of the token, never the token itself."""

    def __init__(self, token: str) -> None:
        self._digest = _digest(token)

    def check(self, header_values: list[str]) -> bool:
        """``header_values``: every Authorization header of the request. Exactly one, of the form
        ``Bearer <token>``, is accepted."""
        if len(header_values) != 1:
            return False
        scheme, _, cred = header_values[0].strip().partition(" ")
        same = hmac.compare_digest(_digest(cred.strip()), self._digest)  # runs whatever the scheme
        return same and scheme.lower() == "bearer"


def same_user(given: object, approver: str) -> bool:
    if not isinstance(given, str):
        return False
    return hmac.compare_digest(_digest(given), _digest(approver))
