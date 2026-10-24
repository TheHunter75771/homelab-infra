#!/usr/bin/env python3
"""homelab-heartbeat: off-site liveness watcher for the media server (docs/CONTRACTS.md §8).

Runs once per invocation from homelab-heartbeat.timer (every 2 min) on the edge VPS:

  check  `tailscale ping -c 1 --timeout 5s --until-direct=false <target>` and then a TCP
         connect to port 22 on the address that answered. Both must succeed for "up".
         (--until-direct=false: a reply relayed over DERP still proves the box is alive;
         without it tailscale exits 1 whenever no direct path exists.)
  log    one line per check to <state_dir>/log.jsonl (Contract 8), exactly
         {"ts": ISO 8601 UTC, "state": "up"|"down", "rtt_ms": float|null}
         where state is what THIS check saw and rtt_ms is the tailscale ping latency.
  alert  after `fail_threshold` (3) consecutive failed checks, one Web Push "<name> offline"
         per incident; one "<name> back online" when a check succeeds again. A push that
         reaches no device is retried on the next check; "no subscribed devices" is final.

Pushes go straight from here (pywebpush), using copies of the box's vapid.json and
subscriptions.json that edge.yml syncs into /opt/homelab-heartbeat. Payload is the same JSON
the host notifier sends: {"title", "body", "tag", "url": "/"}.

  --test-push   send one test notification and exit: 0 delivered, 2 no subscribed device,
                1 error (the same codes as homelab-notify)
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

DEFAULT_STATE = {"fails": 0, "down_since": None, "alerted": False, "recovery_pending": False, "recovered_from": None}

PONG_RE = re.compile(
    r"pong from \S+ \((?P<ip>[0-9A-Fa-f.:]+)(?:, \d+)?\).* in (?P<val>[0-9]+(?:\.[0-9]+)?)(?P<unit>ns|us|µs|ms|s)\b"
)
UNIT_MS = {"ns": 1e-6, "us": 1e-3, "µs": 1e-3, "ms": 1.0, "s": 1000.0}


def log(msg):
    print(msg, flush=True)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s):
    return datetime.fromisoformat(s) if s else None


def parse_pong(output):
    """(ip, rtt_ms) from tailscale ping output, or None when there was no pong."""
    m = PONG_RE.search(output or "")
    if not m:
        return None
    return m.group("ip"), round(float(m.group("val")) * UNIT_MS[m.group("unit")], 3)


def tailscale_ping(target, timeout_s, run=subprocess.run):
    cmd = ["tailscale", "ping", "-c", "1", "--timeout", f"{timeout_s}s", "--until-direct=false", target]
    try:
        p = run(cmd, capture_output=True, text=True, timeout=timeout_s + 15)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"tailscale ping: {e}")
        return None
    if p.returncode != 0:
        log(f"tailscale ping: exit {p.returncode}: {(p.stdout + p.stderr).strip()[:200]}")
        return None
    pong = parse_pong(p.stdout)
    if pong is None:
        log(f"tailscale ping: no pong in output: {p.stdout.strip()[:200]}")
    return pong


def tcp_open(host, port, timeout_s, connect=socket.create_connection):
    try:
        connect((host, port), timeout=timeout_s).close()
        return True
    except OSError as e:
        log(f"tcp {host}:{port}: {e}")
        return False


def probe(cfg, ping=tailscale_ping, tcp=tcp_open):
    """(ok, rtt_ms). rtt_ms is None whenever the check failed."""
    pong = ping(cfg["target"], cfg["timeout_s"])
    if pong is None:
        return False, None
    ip, rtt = pong
    if not tcp(ip, cfg["tcp_port"], cfg["timeout_s"]):
        log(f"tailscale answered in {rtt} ms but TCP {cfg['tcp_port']} did not")
        return False, None
    return True, rtt


def human_duration(seconds):
    minutes = max(0, int(round(seconds / 60)))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def local_time(dt, tz_name):
    try:
        from zoneinfo import ZoneInfo

        dt = dt.astimezone(ZoneInfo(tz_name))
    except Exception:  # unknown zone or no tzdata: fall back to UTC
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%H:%M %Z")


def step(state, ok, now, cfg, send):
    """Advance the state machine by one check. `send(title, body)` -> True when the push is
    settled (delivered, or nobody to deliver to); False means retry on the next check."""
    s = dict(DEFAULT_STATE)
    s.update({k: state[k] for k in DEFAULT_STATE if k in state})
    name, threshold = cfg["name"], cfg["fail_threshold"]
    if ok:
        if s["alerted"]:
            s.update(alerted=False, recovery_pending=True, recovered_from=s["down_since"])
        s.update(fails=0, down_since=None)
        if s["recovery_pending"]:
            since = parse_iso(s["recovered_from"])
            took = f" after {human_duration((now - since).total_seconds())} offline" if since else ""
            if send(f"{name} back online", f"Reachable again{took}."):
                s.update(recovery_pending=False, recovered_from=None)
    else:
        s["fails"] += 1
        if s["down_since"] is None:
            s["down_since"] = iso(now)
        s.update(recovery_pending=False, recovered_from=None)  # a new outage supersedes it
        if s["fails"] >= threshold and not s["alerted"]:
            since = parse_iso(s["down_since"])
            body = (
                f"No reply over the tailnet since {local_time(since, cfg['display_tz'])} "
                f"({s['fails']} checks in a row)."
            )
            if send(f"{name} offline", body):
                s["alerted"] = True
    return s


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        log(f"{path}: unreadable ({e}); starting fresh")
        return default


def write_atomic(path, text, mode=0o600):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_log(path, entry, keep_lines, max_bytes):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    if os.path.getsize(path) > max_bytes:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        write_atomic(path, "".join(lines[-keep_lines:]), mode=0o644)


def default_webpush():
    from pywebpush import webpush  # imported lazily: tests run without the venv

    return webpush


DELIVERED, NOBODY, FAILED = "delivered", "nobody", "failed"


def send_push(cfg, title, body, webpush=None):
    """DELIVERED (at least one device took it), NOBODY (no subscribed device) or FAILED."""
    subs = load_json(cfg["subscriptions_file"], None)
    vapid = load_json(cfg["vapid_file"], None)
    if not isinstance(subs, list) or not isinstance(vapid, dict):
        log("push: subscriptions.json or vapid.json missing/invalid")
        return FAILED
    if cfg.get("rehearsal"):
        title = f"[REHEARSAL] {title}"
        subs = [s for s in subs if s.get("label") == "test"]
    subs = [s for s in subs if isinstance(s, dict) and s.get("endpoint") and isinstance(s.get("keys"), dict)]
    if not subs:
        log(f"push: no subscribed devices; not sent: {title}")
        return NOBODY
    if webpush is None:
        webpush = default_webpush()
    payload = json.dumps({"title": title, "body": body, "tag": cfg["tag"], "url": "/"})
    delivered = 0
    for sub in subs:
        try:
            webpush(
                subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]},
                data=payload,
                vapid_private_key=vapid["privateKey"],
                vapid_claims={"sub": vapid["subject"]},  # fresh dict: pywebpush writes "aud" into it
                ttl=cfg["push_ttl_s"],
                timeout=10,
                headers={"Urgency": "high"},
            )
            delivered += 1
        except Exception as e:  # pywebpush raises WebPushException, requests errors, key errors
            # Type and HTTP status only: exception text from pywebpush/requests can carry the
            # endpoint URL, which is a capability for that device and must not reach the journal.
            status = getattr(getattr(e, "response", None), "status_code", None)
            log(f"push to {sub.get('label') or 'device'} failed: {type(e).__name__} (HTTP status {status})")
    log(f"push '{title}': delivered to {delivered}/{len(subs)} device(s)")
    return DELIVERED if delivered else FAILED


def run_check(cfg, now=None, ping=tailscale_ping, tcp=tcp_open, webpush=None):
    now = now or datetime.now(timezone.utc)
    state_path = os.path.join(cfg["state_dir"], "state.json")
    log_path = os.path.join(cfg["state_dir"], "log.jsonl")
    state = load_json(state_path, {})
    if not isinstance(state, dict):
        state = {}
    ok, rtt = probe(cfg, ping=ping, tcp=tcp)
    append_log(
        log_path,
        {"ts": iso(now), "state": "up" if ok else "down", "rtt_ms": rtt},
        cfg["log_keep_lines"],
        cfg["log_max_bytes"],
    )
    new = step(state, ok, now, cfg, lambda title, body: send_push(cfg, title, body, webpush=webpush) != FAILED)
    write_atomic(state_path, json.dumps(new, indent=2) + "\n")
    log(f"{'up' if ok else 'down'} rtt_ms={rtt} fails={new['fails']} alerted={new['alerted']}")
    return new


CONFIG_DEFAULTS = {
    "tcp_port": 22,
    "timeout_s": 5,
    "fail_threshold": 3,
    "name": "mediaserver",
    "tag": "heartbeat",
    "display_tz": "UTC",
    "rehearsal": False,
    "state_dir": "/var/lib/homelab-heartbeat",
    "vapid_file": "/opt/homelab-heartbeat/vapid.json",
    "subscriptions_file": "/opt/homelab-heartbeat/subscriptions.json",
    "push_ttl_s": 86400,
    "log_keep_lines": 21600,
    "log_max_bytes": 3_000_000,
}


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = dict(CONFIG_DEFAULTS, **json.load(f))
    if not cfg.get("target"):
        raise ValueError(f"{path}: 'target' is required")
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="/opt/homelab-heartbeat/config.json")
    ap.add_argument("--test-push", action="store_true", help="send one test notification and exit")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.test_push:
        result = send_push(cfg, f"Test from the {cfg['name']} heartbeat", "Off-site alerts reach this device.")
        return {DELIVERED: 0, NOBODY: 2, FAILED: 1}[result]
    run_check(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
