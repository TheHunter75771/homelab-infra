"""Web Push: the VAPID keys (from a file the role generates), the subscription store (a JSON file, every
read-modify-write under a file lock and written atomically), and the fan-out with pruning.

A subscription whose push service answers 404 or 410 is gone for good and is pruned. Any other failure
(429, 5xx, a network error) keeps it. Endpoint URLs are capabilities for a device, so they are never logged:
a log line names a device by its label and gives the HTTP status only.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from .config import Config
from .util import clean, file_lock, read_json, write_json_atomic

log = logging.getLogger("mission_control.push")

PRUNE_STATUSES = frozenset({404, 410})
OK_STATUSES = frozenset({200, 201, 202, 204})
_B64URL = re.compile(r"[A-Za-z0-9_-]+")
Sender = Callable[[dict[str, Any], str], int]  # (subscription, payload JSON) -> HTTP status; raises on no answer


class PushError(ValueError):
    pass


class VapidConfigError(PushError):
    """py_vapid refused to sign with our VAPID settings: every send would fail the same way."""


SUBJECT_ERROR = "VAPID subject must be https://host (no port or path) or mailto:address"
# py_vapid 1.9.6's `_check_sub` pattern, copied exactly (py_vapid/__init__.py; used with re.match and IGNORECASE).
# py_vapid refuses to sign with a subject that fails it, e.g. an https subject with a port or a path. `load_vapid`
# also asks py_vapid itself (`subject_signs`) when it is installed, so the two can't disagree after an upgrade.
_PY_VAPID_SUB_RE = re.compile(
    r"^(mailto:.+@((localhost|[%\w-]+(\.[%\w-]+)+|([0-9a-f]{1,4}):+([0-9a-f]{1,4})?)))"
    r"|https:\/\/(localhost|[\w-]+\.[\w\.-]+|([0-9a-f]{1,4}:+)+([0-9a-f]{1,4})?)$",
    re.IGNORECASE,
)


def subject_matches(subject: Any) -> bool:
    """The mirrored py_vapid rule, plus no whitespace or control characters."""
    return (isinstance(subject, str) and subject.isprintable() and not any(c.isspace() for c in subject)
            and _PY_VAPID_SUB_RE.match(subject) is not None)


def subject_signs(subject: str) -> bool | None:
    """py_vapid's own verdict: sign a claim with a throwaway key. None when py_vapid isn't installed or the check
    itself breaks (a crypto backend error, a library change): then the mirrored pattern alone guards, and
    startup, check-config and the briefing carry on (Reviewer Z6-1)."""
    try:
        from py_vapid import Vapid, VapidException
    except ImportError:
        return None
    except Exception as exc:  # noqa: BLE001 - a broken install must not stop the server
        log.warning("py_vapid unusable (%s); checking the VAPID subject with the mirrored pattern only",
                    type(exc).__name__)
        return None
    try:
        v = Vapid()
        v.generate_keys()
        v.sign({"sub": subject, "aud": "https://push.example.com"})
    except VapidException:
        return False
    except Exception as exc:  # noqa: BLE001 - type only: never the key or the claim
        log.warning("py_vapid check failed (%s); checking the VAPID subject with the mirrored pattern only",
                    type(exc).__name__)
        return None
    return True


def _b64len(value: Any, nbytes: int) -> bool:
    if not isinstance(value, str) or not _B64URL.fullmatch(value):
        return False
    try:
        return len(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))) == nbytes
    except ValueError:
        return False


def load_vapid(path: str) -> dict[str, str]:
    """vapid.json: {"publicKey": 65-byte P-256 point, "privateKey": 32-byte scalar (both base64url, no padding),
    "subject": "mailto:..." or "https://..."}. The same layout as the house push store."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as exc:
        raise PushError(f"VAPID file not readable ({type(exc).__name__})") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise PushError("VAPID file is not valid JSON") from None
    if not isinstance(raw, dict):
        raise PushError("VAPID file must be a JSON object")
    if not _b64len(raw.get("publicKey"), 65) or not raw["publicKey"].startswith("B"):
        raise PushError("VAPID publicKey must be an uncompressed P-256 point (base64url)")
    if not _b64len(raw.get("privateKey"), 32):
        raise PushError("VAPID privateKey must be 32 bytes (base64url)")
    subject = raw.get("subject")
    if not subject_matches(subject) or subject_signs(subject) is False:
        raise PushError(SUBJECT_ERROR)
    return {"publicKey": raw["publicKey"], "privateKey": raw["privateKey"], "subject": subject}


def validate_subscription(raw: Any, label: Any, cfg: Config) -> dict[str, Any]:
    """A browser PushSubscription.toJSON() -> the stored form. Raises PushError with a safe message."""
    if not isinstance(raw, dict):
        raise PushError("subscription must be an object")
    endpoint = raw.get("endpoint")
    if not isinstance(endpoint, str) or len(endpoint) > 1024:
        raise PushError("subscription endpoint missing or too long")
    try:
        u = urllib.parse.urlsplit(endpoint)
        _ = u.port
    except ValueError:
        raise PushError("subscription endpoint is not a URL") from None
    host = (u.hostname or "").lower()
    if u.scheme != "https" or u.username or u.password or not host:
        raise PushError("subscription endpoint must be an https URL")
    if not any(host == h or host.endswith("." + h) for h in cfg.push_endpoint_hosts):
        raise PushError("subscription endpoint is not a known push service")
    keys = raw.get("keys")
    if not isinstance(keys, dict) or not _b64len(keys.get("p256dh"), 65) or not _b64len(keys.get("auth"), 16):
        raise PushError("subscription keys missing or malformed")
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
        "label": clean(label, 40) or "Device",
    }


class SubscriptionStore:
    """subscriptions.json in state_dir: a JSON list. Both the server and `briefing --send` change it, so every
    change is read-modify-write under the file lock, then an atomic replace (one writer at a time)."""

    def __init__(self, path: str, max_items: int = 20, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.max_items = max_items
        self.clock = clock

    def all(self) -> list[dict[str, Any]]:
        try:
            data = read_json(self.path, [])
        except (ValueError, OSError) as exc:
            log.error("subscriptions file unreadable (%s)", type(exc).__name__)
            return []
        if not isinstance(data, list):
            log.error("subscriptions file is not a list")
            return []
        return [s for s in data if isinstance(s, dict) and isinstance(s.get("endpoint"), str)
                and isinstance(s.get("keys"), dict)]

    def _write(self, subs: list[dict[str, Any]]) -> None:
        write_json_atomic(self.path, subs, 0o600)

    def add(self, sub: dict[str, Any]) -> None:
        with file_lock(self.path):
            subs = [s for s in self.all() if s["endpoint"] != sub["endpoint"]]
            if len(subs) >= self.max_items:
                raise PushError(f"too many devices (at most {self.max_items}); remove one first")
            subs.append({**sub, "created_at": int(self.clock())})
            self._write(subs)

    def remove(self, endpoint: str) -> bool:
        with file_lock(self.path):
            subs = self.all()
            keep = [s for s in subs if s["endpoint"] != endpoint]
            if len(keep) == len(subs):
                return False
            self._write(keep)
            return True

    def prune(self, endpoints: set[str]) -> int:
        if not endpoints:
            return 0
        with file_lock(self.path):
            subs = self.all()
            keep = [s for s in subs if s["endpoint"] not in endpoints]
            if len(keep) != len(subs):
                self._write(keep)
            return len(subs) - len(keep)


def make_pywebpush_sender(vapid: dict[str, str], cfg: Config) -> Sender:
    """The real sender: pywebpush (pinned in versions.yml), run in a worker thread by `send_all`."""
    from py_vapid import Vapid, VapidException
    from pywebpush import WebPushException, webpush

    key = Vapid.from_string(vapid["privateKey"])

    def send(sub: dict[str, Any], payload: str) -> int:
        try:
            resp = webpush(
                subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]},
                data=payload,
                vapid_private_key=key,
                vapid_claims={"sub": vapid["subject"]},  # a fresh dict per call: pywebpush writes "aud" into it
                timeout=cfg.push_timeout_s,
                ttl=cfg.push_ttl_s,
                headers={"Urgency": "high"},
            )
        except WebPushException as exc:
            status = exc.status_code  # pywebpush 2.x: the push service's HTTP status, or None
            if isinstance(status, int):
                return status
            raise PushError("push service gave no HTTP answer") from None
        except VapidException:  # our subject or key: not the device's fault, and the same for every device
            raise VapidConfigError("VAPID config invalid") from None
        return int(resp.status_code)

    return send


async def send_all(store: SubscriptionStore, sender: Sender, message: dict[str, str], *, only: str | None = None
                   ) -> dict[str, int]:
    """Push `message` to every subscription (or only to endpoint `only`). Prunes 404/410. Returns counts:
    devices, delivered, failed, pruned."""
    payload = json.dumps(message, separators=(",", ":"))
    subs = [s for s in store.all() if only is None or s["endpoint"] == only]
    gone: set[str] = set()
    delivered = failed = 0
    for sub in subs:
        label = clean(sub.get("label"), 40) or "device"
        try:
            status = await asyncio.to_thread(sender, sub, payload)
        except asyncio.CancelledError:
            raise
        except VapidConfigError:
            failed += 1
            log.error("push to %s failed: VAPID config invalid (%s)", label, SUBJECT_ERROR)
            continue
        except Exception as exc:  # noqa: BLE001 - exception text can carry the endpoint URL: type only
            failed += 1
            log.warning("push to %s failed: %s (no HTTP answer)", label, type(exc).__name__)
            continue
        if status in OK_STATUSES:
            delivered += 1
        elif status in PRUNE_STATUSES:
            gone.add(sub["endpoint"])
            log.info("push to %s: HTTP %d, subscription expired; removing it", label, status)
        else:
            failed += 1
            log.warning("push to %s failed: HTTP %d (kept)", label, status)
    pruned = store.prune(gone)
    return {"devices": len(subs), "delivered": delivered, "failed": failed, "pruned": pruned}


def subscriptions_path(cfg: Config) -> str:
    return os.path.join(cfg.state_dir, "subscriptions.json")
