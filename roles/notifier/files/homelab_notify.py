#!/opt/homelab-notify/bin/python3
"""homelab-notify: deliver a Web Push to every subscribed device.

  homelab-notify --title T --body B [--tag TAG] [--test]

Reads the push store written by push-core (Contract 4):
  <push_dir>/vapid.json          {"publicKey", "privateKey" (raw P-256, base64url), "subject"}
  <push_dir>/subscriptions.json  [{"endpoint", "keys": {"p256dh", "auth"}, "label", "created"}]
Payload sent to each device: {"title", "body", "tag", "url": "/"}.

Exit codes: 0 = delivered to at least one device (or suppressed as a duplicate),
            2 = no subscriptions to send to, 1 = error / nothing delivered.

--tag      dedupe: the same tag + title is not resent within 30 min (state in
           /var/lib/homelab-notify/). The tag also travels in the payload, so a phone can
           replace an older notification with the same tag.
--test     send only to subscriptions labelled "test".
Rehearsal: while /etc/homelab/rehearsal exists, titles get a "[REHEARSAL] " prefix and only
           subscriptions labelled "test" receive anything.
Dead subscriptions (HTTP 404/410): push-core is the ONLY writer of subscriptions.json, so this
tool never writes it. It asks push-core to drop them (POST <pushcore_url>/api/unsubscribe,
loopback only, 3 s timeout). Best-effort: if push-core is down (e.g. the boot gate holds
Docker), pruning is skipped silently and the next push that hits the endpoint tries again.

Logs go to journald under the identifier homelab-notify (and to stderr). URLs and control
characters are stripped from every log line (push endpoints are capability URLs).
Settings come from /etc/homelab/notify.json; HOMELAB_NOTIFY_CONFIG points elsewhere (tests).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from urllib.parse import urlparse

EXIT_OK, EXIT_ERROR, EXIT_NO_SUBSCRIPTIONS = 0, 1, 2
DEAD_STATUSES = frozenset({404, 410})
DEFAULT_CONFIG = "/etc/homelab/notify.json"
DEFAULTS = {
    "push_dir": "/srv/homelab/push",
    "state_dir": "/var/lib/homelab-notify",
    "rehearsal_flag": "/etc/homelab/rehearsal",
    "dedupe_seconds": 1800,
    "ttl_seconds": 86400,
    "timeout_seconds": 10,
    "test_label": "test",
    "rehearsal_prefix": "[REHEARSAL] ",
    "default_subject": "mailto:admin@localhost",
    "pushcore_url": "http://127.0.0.1:8085",
    "pushcore_timeout_seconds": 3,
}
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")

Sender = Callable[[dict, str], int]
Unsubscriber = Callable[[str], "int | None"]


def sanitize_text(text: object, max_len: int | None = 160) -> str:
    """Same rule as push-core's sanitizeText(): URLs -> <url>, control chars -> space, cap length."""
    clean = _CTRL_RE.sub(" ", _URL_RE.sub("<url>", str(text))).strip()
    return clean[:max_len] + "…" if max_len and len(clean) > max_len else clean


# --------------------------------------------------------------------------- logging
class Logger:
    def __init__(self, use_syslog: bool = True):
        self.lines: list[str] = []
        self._syslog = None
        if use_syslog:
            try:
                import syslog  # noqa: PLC0415 - Linux only, optional

                syslog.openlog("homelab-notify", syslog.LOG_PID, syslog.LOG_DAEMON)
                self._syslog = syslog
            except ImportError:
                self._syslog = None

    def __call__(self, msg: str, level: str = "info") -> None:
        msg = sanitize_text(msg, max_len=None)  # never let an endpoint URL reach the journal
        self.lines.append(f"{level}: {msg}")
        print(f"homelab-notify: {level}: {msg}", file=sys.stderr, flush=True)
        if self._syslog:
            prio = {"error": self._syslog.LOG_ERR, "warn": self._syslog.LOG_WARNING}.get(level, self._syslog.LOG_INFO)
            self._syslog.syslog(prio, msg)


# --------------------------------------------------------------------------- pure helpers
def load_config(path: str) -> dict:
    cfg = dict(DEFAULTS)
    with contextlib.suppress(FileNotFoundError), open(path, encoding="utf-8") as fh:
        cfg.update(json.load(fh))
    return cfg


def format_title(title: str, rehearsal: bool, prefix: str = DEFAULTS["rehearsal_prefix"]) -> str:
    return prefix + title if rehearsal and not title.startswith(prefix) else title


def valid_subscription(sub: object) -> bool:
    if not isinstance(sub, dict):
        return False
    keys = sub.get("keys")
    return (
        isinstance(sub.get("endpoint"), str)
        and sub["endpoint"].startswith("https://")
        and isinstance(keys, dict)
        and isinstance(keys.get("p256dh"), str)
        and isinstance(keys.get("auth"), str)
    )


def select_targets(subs: Iterable, *, rehearsal: bool, test_only: bool, test_label: str = "test") -> list[dict]:
    """Rehearsal or --test: only subscriptions labelled `test_label`. Malformed entries are skipped."""
    out = [s for s in subs if valid_subscription(s)]
    if rehearsal or test_only:
        out = [s for s in out if s.get("label") == test_label]
    return out


def dedupe_key(tag: str, title: str) -> str:
    return f"{tag}\x1f{title}"


def prune_dedupe(state: dict, now: float, window: float) -> dict:
    return {k: v for k, v in state.items() if isinstance(v, int | float) and now - v < window}


def is_duplicate(state: dict, key: str, now: float, window: float) -> bool:
    sent = state.get(key)
    return isinstance(sent, int | float) and 0 <= now - sent < window


def build_payload(title: str, body: str, tag: str | None) -> str:
    """The push payload contract shared with push-core's service worker. Exactly these keys.
    Rehearsal is visible in the title prefix."""
    return json.dumps({"title": title, "body": body, "tag": tag, "url": "/"})


def send_all(targets: list[dict], payload: str, sender: Sender, log: Logger) -> tuple[int, set[str], int]:
    """Returns (delivered, dead endpoints, other failures)."""
    delivered, failed, dead = 0, 0, set()
    for sub in targets:
        label = sub.get("label") or "unlabelled"
        try:
            status = sender(sub, payload)
        except Exception as exc:  # noqa: BLE001 - one bad endpoint must not stop the rest
            failed += 1
            # Type only: the message of a requests/urllib3 error embeds the endpoint URL.
            log(f"push to {label} failed: {type(exc).__name__}", "warn")
            continue
        if 200 <= status < 300:
            delivered += 1
        elif status in DEAD_STATUSES:
            dead.add(sub["endpoint"])
            log(f"subscription {label} is gone (HTTP {status}); asking push-core to drop it", "warn")
        else:
            failed += 1
            log(f"push to {label} rejected: HTTP {status}", "warn")
    return delivered, dead, failed


# --------------------------------------------------------------------------- files
def read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def prune_dead(dead: set[str], unsubscribe: Unsubscriber, log: Logger) -> int:
    """Ask push-core to drop dead subscriptions; returns how many it accepted. Stops quietly at
    the first sign that push-core is unreachable (unsubscribe returns None)."""
    removed = 0
    for endpoint in sorted(dead):
        status = unsubscribe(endpoint)
        if status is None:
            return removed
        if 200 <= status < 300:
            removed += 1
        else:
            log(f"push-core refused to unsubscribe a dead subscription (HTTP {status})", "warn")
    if removed:
        log(f"push-core dropped {removed} dead subscription(s)")
    return removed


def make_pushcore_unsubscriber(base_url: str, timeout: float) -> Unsubscriber | None:
    """POST {"endpoint": ...} to push-core's /api/unsubscribe with its CSRF header. Loopback only,
    never through a proxy. Returns the HTTP status, or None when push-core can't be reached."""
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS:
        return None
    url = base_url.rstrip("/") + "/api/unsubscribe"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def unsubscribe(endpoint: str) -> int | None:
        request = urllib.request.Request(  # noqa: S310 - fixed loopback http URL, checked above
            url,
            data=json.dumps({"endpoint": endpoint}).encode("utf-8"),
            method="POST",
            headers={"X-Homelab": "1", "Content-Type": "application/json"},
        )
        try:
            with opener.open(request, timeout=timeout) as resp:
                return int(resp.status)
        except urllib.error.HTTPError as exc:
            exc.close()
            return int(exc.code)
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
            return None  # refused / timed out / reset: push-core is down, skip silently

    return unsubscribe


@contextlib.contextmanager
def state_lock(state_dir: str):
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(state_dir, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def save_dedupe(state_dir: str, state: dict) -> None:
    """Atomic (tmp + fsync + rename) write of this tool's own state file."""
    path = os.path.join(state_dir, "dedupe.json")
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(state).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- web push
def make_webpush_sender(vapid: dict, cfg: dict) -> Sender:
    from py_vapid import Vapid  # noqa: PLC0415 - only the real sender needs the venv
    from pywebpush import WebPushException, webpush  # noqa: PLC0415

    key = Vapid.from_string(vapid["privateKey"])
    subject = vapid.get("subject") or cfg["default_subject"]

    def send(sub: dict, payload: str) -> int:
        try:
            resp = webpush(
                subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]},
                data=payload,
                vapid_private_key=key,
                vapid_claims={"sub": subject},  # fresh dict per call: pywebpush stores 'aud' in it
                timeout=cfg["timeout_seconds"],
                ttl=int(cfg["ttl_seconds"]),
                headers={"Urgency": "high"},
            )
        except WebPushException as exc:
            if exc.status_code is not None:
                return int(exc.status_code)
            raise
        return int(resp.status_code)

    return send


# --------------------------------------------------------------------------- main
def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="homelab-notify", description="Send a Web Push to subscribed devices")
    p.add_argument("--title", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--tag", help="dedupe key: same tag + title is not resent within 30 min")
    p.add_argument("--test", action="store_true", help="send only to subscriptions labelled 'test'")
    return p.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    sender: Sender | None = None,
    unsubscribe: Unsubscriber | None = None,
    now: float | None = None,
) -> int:
    args = parse_args(argv)
    log = Logger(use_syslog=os.environ.get("HOMELAB_NOTIFY_SYSLOG", "1") != "0")
    try:
        cfg = load_config(os.environ.get("HOMELAB_NOTIFY_CONFIG", DEFAULT_CONFIG))
        now = time.time() if now is None else now
        rehearsal = os.path.exists(cfg["rehearsal_flag"])
        title = format_title(args.title, rehearsal, cfg["rehearsal_prefix"])
        subs_path = os.path.join(cfg["push_dir"], "subscriptions.json")
        subs = read_json(subs_path, [])
        if not isinstance(subs, list):
            log(f"{subs_path} is not a JSON list", "error")
            return EXIT_ERROR
        targets = select_targets(subs, rehearsal=rehearsal, test_only=args.test, test_label=cfg["test_label"])
        if not targets:
            scope = " labelled 'test'" if (rehearsal or args.test) else ""
            log(f"no subscriptions{scope}; nothing sent: {title}", "warn")
            return EXIT_NO_SUBSCRIPTIONS

        with state_lock(cfg["state_dir"]):
            window = float(cfg["dedupe_seconds"])
            dedupe_path = os.path.join(cfg["state_dir"], "dedupe.json")
            state = read_json(dedupe_path, {})
            state = prune_dedupe(state if isinstance(state, dict) else {}, now, window)
            key = dedupe_key(args.tag, args.title) if args.tag else None
            if key and is_duplicate(state, key, now, window):
                log(f"suppressed duplicate (tag {args.tag}, sent within {window / 60:.0f} min): {title}")
                return EXIT_OK

            if sender is None:
                vapid = read_json(os.path.join(cfg["push_dir"], "vapid.json"), None)
                if not isinstance(vapid, dict) or not vapid.get("privateKey"):
                    log("vapid.json is missing or has no privateKey", "error")
                    return EXIT_ERROR
                sender = make_webpush_sender(vapid, cfg)

            payload = build_payload(title, args.body, args.tag)
            delivered, dead, failed = send_all(targets, payload, sender, log)
            if dead:
                if unsubscribe is None:
                    unsubscribe = make_pushcore_unsubscriber(
                        cfg["pushcore_url"], float(cfg["pushcore_timeout_seconds"])
                    )
                if unsubscribe is None:
                    log("pushcore_url is not a loopback http URL; dead subscriptions left in place", "error")
                else:
                    prune_dead(dead, unsubscribe, log)
            if delivered and key:
                state[key] = now
                save_dedupe(cfg["state_dir"], state)
        log(f"delivered to {delivered}/{len(targets)} device(s), {failed} failed, {len(dead)} gone: {title}")
        return EXIT_OK if delivered else EXIT_ERROR
    except Exception as exc:  # noqa: BLE001
        log(f"error: {type(exc).__name__}: {sanitize_text(exc)}", "error")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
