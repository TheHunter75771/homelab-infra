"""POST /api/notify (B2): any host or service pushes to Noah's phone through Mission Control's own Web Push.

Senders are tagged servers with no Tailscale login, so the login guard doesn't apply to this one route: a bearer
token per sender is the auth. The config holds only each token's SHA-256 (`notify_senders`); the presented token is
hashed and compared with every sender's hash (hmac.compare_digest, no early exit), so a missing, malformed or wrong
token all take the same path to the same 401.

The handler (app.py `notify`, then `State.notify_accept`) checks, in order: token 401, `allow_from` 403, content type
415, size 413, body 400, too large to push 413, push set up 503, dedupe (202 deduped), rate (429), quiet hours
(202 held), else send (202 sent). An urgent message skips only the quiet hours.

What sw.js shows is bounded here, since it cuts what is longer: the pushed tag is at most 64 characters (a longer
`n.<sender>.<tag>` becomes `n.<sender>.~<hash>`), the held summary's body at most 400 (titles cut with "…").

State (`notify.json` in state_dir, written like alerts.json):
  dedupe   {sha256(sender, tag, title, body): accepted at}: a hash, never the text
  rate     {sender: [accepted at, ...]}: the rolling hour
  held     [{sender, tag, title, at}]: oldest first, at most 50 (the oldest is dropped and counted)
  dropped  how many were dropped from the held queue since the last summary
Logging names the sender, the tag and the outcome only: never a title, body, url or token.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import math
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from typing import Any

from .alerts import MAX_INT, _num
from .config import _HOST_RE, _SENDER_RE, _SHA256_RE, NotifySender
from .util import file_lock, local, read_json, write_json_atomic

log = logging.getLogger("mission_control.notify")

MAX_REQUEST = 4096  # bytes of JSON body
FIELDS = frozenset({"title", "body", "tag", "url", "urgent"})
MAX_TITLE = 80
MAX_TEXT = 300
TAG_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_URL_CHARS = re.compile(r"[\x21-\x5b\x5d-\x7e]{1,512}")  # printable ASCII, no space, no backslash (a browser reads \ as /)
_NOT_IN_TITLE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})  # controls, format (bidi, zero-width), lone surrogates, breaks
HELD_CAP = 50
SUMMARY_LINES = 5
RATE_WINDOW_S = 3600
MAX_DEDUPE = 10000  # entries kept in notify.json (the newest), whatever the rate and window allow
FUTURE_SLACK_S = 300  # a time this far past now is not trusted (a poisoned file, a clock stepped back)
MAX_PUSH_TAG = 64  # sw.js cuts a tag at 64 characters; two cut tags could become one
SUMMARY_BODY_MAX = 400  # sw.js cuts a body at 400 (JavaScript length: UTF-16 code units)


class BadField(ValueError):
    """A body field broke its rule. `field` is a fixed name from this module, safe to return and log."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


@dataclass(frozen=True)
class Message:
    title: str  # stripped
    body: str
    tag: str
    url: str  # "/" when none was given
    urgent: bool


# ---- the request ---------------------------------------------------------------------------------------------
def match_sender(senders: tuple[NotifySender, ...], authorization: list[str]) -> NotifySender | None:
    """The sender whose token hash matches `Authorization: Bearer <token>`, or None. Exactly one header; the scheme
    is case-insensitive (RFC 9110 §11.1); the token is non-empty, printable, no whitespace. Whatever was presented
    is hashed and compared with every sender's hash."""
    presented = b""
    if len(authorization) == 1:
        scheme, _, token = authorization[0].partition(" ")
        if scheme.lower() == "bearer" and token and token.isprintable() and not any(c.isspace() for c in token):
            try:
                presented = token.encode("utf-8", "surrogateescape")
            except UnicodeEncodeError:
                presented = b""
    digest = hashlib.sha256(presented).hexdigest()
    found = None
    for s in senders:  # every one: no early exit
        same = hmac.compare_digest(digest, s.token_sha256)
        if same and presented and found is None:
            found = s
    return found


def last_hop(forwarded_for: list[str]) -> str | None:
    """The last X-Forwarded-For hop (the address `tailscale serve` saw), normalised; None when there is none or it
    isn't an IP literal."""
    if not forwarded_for:
        return None
    hop = ",".join(forwarded_for).rsplit(",", 1)[-1].strip()
    try:
        return str(ipaddress.ip_address(hop))
    except ValueError:
        return None


def from_allowed(sender: NotifySender, forwarded_for: list[str]) -> bool:
    """True when the sender has no `allow_from`, or the last X-Forwarded-For hop is one of its addresses."""
    if not sender.allow_from:
        return True
    hop = last_hop(forwarded_for)
    return hop is not None and hop in sender.allow_from


def _one_line(s: str) -> bool:
    """No control or format characters (bidi overrides, zero-width), line breaks or lone surrogates: a title is one
    line of the held summary, and reads as it looks."""
    return not any(unicodedata.category(c) in _NOT_IN_TITLE for c in s)


def url_ok(v: str) -> bool:
    """A same-origin path (`/...`, no `//`), or an `https://` URL with no credentials whose host ends in `.ts.net`.
    Printable ASCII only (anything else percent-encoded) and no backslash, at most 512 characters."""
    if not _URL_CHARS.fullmatch(v):
        return False
    if v.startswith("/"):
        return "//" not in v
    if not v.startswith("https://") or "//" in v.removeprefix("https://"):
        return False
    try:
        u = urllib.parse.urlsplit(v)
        _ = u.port
    except ValueError:
        return False
    if u.username is not None or u.password is not None:
        return False
    host = u.hostname or ""
    return host.endswith(".ts.net") and _HOST_RE.fullmatch(host) is not None


def parse_message(raw: dict[str, Any]) -> Message:
    """The JSON body -> a Message, or BadField naming the first field that broke its rule ("unknown field" for a
    key outside the contract). No field may be null: an absent optional field is left out."""
    if set(raw) - FIELDS:
        raise BadField("unknown field")
    title = raw.get("title")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= MAX_TITLE or not _one_line(title.strip()):
        raise BadField("title")
    body = raw.get("body", "")
    if not isinstance(body, str) or len(body) > MAX_TEXT:
        raise BadField("body")
    tag = raw.get("tag")
    if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
        raise BadField("tag")
    url = raw.get("url", "/")
    if not isinstance(url, str) or not url_ok(url):
        raise BadField("url")
    urgent = raw.get("urgent", False)
    if not isinstance(urgent, bool):
        raise BadField("urgent")
    return Message(title.strip(), body, tag, url, urgent)


def push_tag(sender: str, tag: str) -> str:
    """`n.<sender>.<tag>`, or `n.<sender>.~` + 16 hex of the tag's SHA-256 when that is over 64 characters. The same
    tag always gives the same pushed tag (replace-on-device), and since a tag can't start with "~" a hashed one
    never equals a literal one."""
    full = f"n.{sender}.{tag}"
    if len(full) <= MAX_PUSH_TAG:
        return full
    return f"n.{sender}.~{hashlib.sha256(tag.encode()).hexdigest()[:16]}"


def push_message(sender: str, m: Message) -> dict[str, str]:
    """The house push payload (push.send_all): the tag is per sender, so a newer push replaces the older one."""
    return {"title": f"{sender}: {m.title}", "body": m.body, "tag": push_tag(sender, m.tag), "url": m.url}


def dedupe_key(sender: str, m: Message) -> str:
    return hashlib.sha256(json.dumps([sender, m.tag, m.title, m.body]).encode()).hexdigest()


# ---- quiet hours and the held queue --------------------------------------------------------------------------
def in_quiet_hours(window: tuple[int, int] | None, now: float, tz: str) -> bool:
    """`window` is (start, end) in minutes after local midnight, end excluded; start > end crosses midnight."""
    if window is None:
        return False
    start, end = window
    t = local(now, tz)
    m = t.hour * 60 + t.minute
    return start <= m < end if start < end else m >= start or m < end


def hold(state: dict[str, Any], sender: str, m: Message, now: float) -> dict[str, Any] | None:
    """Queues a message for the summary. Returns the entry dropped to stay within the cap, if any."""
    state["held"].append({"sender": sender, "tag": m.tag, "title": m.title, "at": now})
    if len(state["held"]) > HELD_CAP:
        state["dropped"] += 1
        return state["held"].pop(0)
    return None


def _units(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _cut(s: str, units: int) -> str:
    """`s` in at most `units` UTF-16 code units, ending in "…" when cut; never half a surrogate pair."""
    if _units(s) <= units:
        return s
    out, used = [], 1  # the "…"
    for c in s:
        w = 2 if ord(c) > 0xFFFF else 1
        if used + w > units:
            break
        out.append(c)
        used += w
    return "".join(out).rstrip() + "…"


def summary_push(held: list[dict[str, Any]], dropped: int) -> dict[str, str]:
    """One push for everything held: "Held overnight: N" (N counts the dropped ones too), up to 5 lines (the oldest
    kept first), then "…and K more" for the rest. The body is at most 400 UTF-16 units, so sw.js never cuts it:
    each line gets an equal share of the room (the "more" line and the line breaks come off first), the shortest
    first, so a title that fits leaves its spare room to the longer ones, and a title over its share is cut with
    "…". The sender is never cut (32 + 2 characters, against a share of at least 73)."""
    n = len(held) + dropped
    shown = held[:SUMMARY_LINES]
    prefixes = [f"{h['sender']}: " for h in shown]
    tail = [f"…and {n - len(shown)} more"] if n > len(shown) else []
    room = SUMMARY_BODY_MAX - sum(_units(t) + 1 for t in tail) - max(0, len(shown) - 1)
    lines = [p + h["title"] for p, h in zip(prefixes, shown, strict=True)]
    for k, i in enumerate(sorted(range(len(shown)), key=lambda j: _units(lines[j]))):
        share = room // (len(shown) - k)
        lines[i] = prefixes[i] + _cut(shown[i]["title"], max(1, share - _units(prefixes[i])))
        room -= _units(lines[i])
    return {"title": f"Held overnight: {n}", "body": "\n".join(lines + tail), "tag": "n.held", "url": "/"}


# ---- dedupe and rate -----------------------------------------------------------------------------------------
def prune(state: dict[str, Any], now: float, dedupe_s: float) -> dict[str, Any]:
    """Drops dedupe entries past the window and accept times past the rolling hour, and both when they lie more than
    FUTURE_SLACK_S after now: a clock stepped back must not silence a message or give a false 429."""
    state["dedupe"] = {k: t for k, t in state["dedupe"].items() if -FUTURE_SLACK_S <= now - t < dedupe_s}
    state["rate"] = {s: kept for s, ts in state["rate"].items()
                     if (kept := [t for t in ts if -FUTURE_SLACK_S <= now - t < RATE_WINDOW_S])}
    return state


def retry_after(times: list[float], now: float) -> int:
    """Seconds until the oldest accept in the rolling hour leaves it (1-3600)."""
    return min(RATE_WINDOW_S, max(1, math.ceil(min(times) + RATE_WINDOW_S - now)))


def fresh() -> dict[str, Any]:
    return {"dedupe": {}, "rate": {}, "held": [], "dropped": 0}


# ---- loading the state file: untrusted input, like alerts.json (Reviewer Z2-1) --------------------------------
def _trusted_time(t: Any, now: float | None) -> bool:
    """A sane timestamp (alerts._num) that is not after now + FUTURE_SLACK_S."""
    return _num(t, now) and (now is None or t <= now + FUTURE_SLACK_S)


def _valid_held(h: Any, now: float | None) -> bool:
    return (isinstance(h, dict) and set(h) == {"sender", "tag", "title", "at"}
            and isinstance(h["sender"], str) and _SENDER_RE.fullmatch(h["sender"]) is not None
            and isinstance(h["tag"], str) and TAG_RE.fullmatch(h["tag"]) is not None
            and isinstance(h["title"], str) and 1 <= len(h["title"]) <= MAX_TITLE and _one_line(h["title"])
            and h["title"] == h["title"].strip() and _num(h["at"], now))


def _valid_state(raw: Any, now: float | None = None) -> dict[str, Any]:
    out = fresh()
    if not isinstance(raw, dict):
        return out
    dedupe = raw.get("dedupe")
    if isinstance(dedupe, dict):
        good = [(k, float(t)) for k, t in dedupe.items()
                if isinstance(k, str) and _SHA256_RE.fullmatch(k) and _trusted_time(t, now)]
        out["dedupe"] = dict(sorted(good, key=lambda kv: kv[1])[-MAX_DEDUPE:])
    rate = raw.get("rate")
    if isinstance(rate, dict):
        for s, ts in rate.items():
            if isinstance(s, str) and _SENDER_RE.fullmatch(s) and isinstance(ts, list):
                kept = sorted(float(t) for t in ts if _trusted_time(t, now))[-1000:]
                if kept:
                    out["rate"][s] = kept
    held = raw.get("held")
    if isinstance(held, list):
        # A held message is never dropped for its time (that would silence it); a future one is clamped to now.
        out["held"] = [{**h, "at": float(h["at"] if now is None else min(h["at"], now))}
                       for h in held if _valid_held(h, now)][-HELD_CAP:]
    dropped = raw.get("dropped")
    if out["held"] and isinstance(dropped, int) and not isinstance(dropped, bool) and 0 <= dropped < MAX_INT:
        out["dropped"] = dropped
    return out


class NotifyStore:
    """notify.json in state_dir. The server is its only writer; writes are atomic and under the file lock."""

    def __init__(self, path: str) -> None:
        self.path = path

    def load(self, now: float | None = None) -> dict[str, Any]:
        try:
            return _valid_state(read_json(self.path, {}), now)
        except (ValueError, OverflowError, OSError, TypeError, RecursionError) as exc:
            log.warning("notify state unreadable (%s); starting empty", type(exc).__name__)
            return fresh()

    def save(self, state: dict[str, Any]) -> None:
        with file_lock(self.path):
            write_json_atomic(self.path, state, 0o600)
