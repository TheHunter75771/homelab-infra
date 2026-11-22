"""mission-control serve | briefing --send|--print [--force] | check-config   [--config PATH]
(config path also from env MISSION_CONTROL_CONFIG). Exit codes: 0 ok, 1 failed (briefing not delivered),
2 bad config or push not set up."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from collections.abc import Callable

from . import __version__
from . import alerts as alerts_mod
from . import briefing as briefing_mod
from .config import Config, ConfigError, load
from .net import make_session
from .push import PushError, Sender, SubscriptionStore, load_vapid, make_pywebpush_sender, send_all, subscriptions_path
from .sources import Collector
from .util import local, read_json, write_json_atomic

DEFAULT_CONFIG = "/etc/mission-control/config.json"
log = logging.getLogger("mission_control")


async def compose_now(cfg: Config, clock: Callable[[], float] = time.time) -> dict:
    """Collect and compose. The CLI is a fresh process with no ERAU cache, and it has time: it waits for ERAU up
    to `erau_timeout_s` (a cold ERAU cache takes 7-9 s to rebuild); every other source keeps source_timeout_s."""
    async with make_session(dict(cfg.host_map)) as session:
        # a little longer than the fetch's own timeout, so a timed-out fetch reports its error, not "loading"
        collector = Collector(cfg, session, clock, erau_wait=cfg.erau_timeout_s + 1)
        try:
            snap = await collector.collect()
        finally:
            await collector.close()
    now = clock()
    return briefing_mod.compose(snap, alerts_mod.briefing_alerts(snap, cfg, now), cfg, now)


async def run_briefing(cfg: Config, *, send: bool, force: bool = False, sender: Sender | None = None,
                       clock: Callable[[], float] = time.time, out=sys.stdout) -> int:
    if send and sender is None:
        try:
            vapid = load_vapid(cfg.vapid_file)
            sender = make_pywebpush_sender(vapid, cfg)
        except (PushError, ImportError) as exc:
            print(f"mission-control: push not set up: {exc}", file=sys.stderr)
            return 2
    b = await compose_now(cfg, clock)
    if not send:
        print(briefing_mod.as_text(b), file=out)
        return 0
    assert sender is not None
    marker = os.path.join(cfg.state_dir, "briefing.json")
    try:
        last = read_json(marker, {})
    except ValueError:
        last = {}
    if not force and isinstance(last, dict) and last.get("date") == b["date"]:
        log.info("briefing for %s already sent; not sending again (use --force)", b["date"])
        print(f"briefing for {b['date']} already sent", file=out)
        return 0
    store = SubscriptionStore(subscriptions_path(cfg), cfg.max_subscriptions, clock)
    result = await send_all(store, sender, briefing_mod.briefing_push(b))
    line = (f"briefing {b['date']}: delivered to {result['delivered']}/{result['devices']} device(s), "
            f"{result['failed']} failed, {result['pruned']} expired and removed")
    log.info("%s", line)
    print(line, file=out)
    if result["devices"] == 0:
        return 0
    if result["delivered"] == 0:
        return 1
    write_json_atomic(marker, {"date": b["date"], "sent_at": int(clock())}, 0o600)
    return 0


def check_config(cfg: Config, path: str, out=sys.stdout) -> int:
    problems = []
    try:
        load_vapid(cfg.vapid_file)
    except PushError as exc:
        problems.append(str(exc))
    if not os.path.isdir(cfg.state_dir) or not os.access(cfg.state_dir, os.W_OK | os.X_OK):
        problems.append("state_dir is missing or not writable by this user")
    if not os.path.isfile(os.path.join(cfg.static_dir, "index.html")):
        print("note: static_dir has no index.html yet (the web app isn't installed)", file=out)
    if cfg.ha_url and not os.path.exists(cfg.ha_token_file):
        print("note: no Home Assistant token file yet (the Home card says 'connect HA')", file=out)
    try:
        local(time.time(), cfg.timezone)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"timezone: {type(exc).__name__}")
    for p in problems:
        print(f"mission-control: {p}", file=sys.stderr)
    if problems:
        return 2
    print(f"mission-control: {path} OK", file=out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mission-control", description="Mission Control server (read-only).")
    parser.add_argument("--config", default=os.environ.get("MISSION_CONTROL_CONFIG", DEFAULT_CONFIG),
                        help=f"JSON config (env MISSION_CONTROL_CONFIG, default {DEFAULT_CONFIG})")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the HTTP server and the alert loop")
    b = sub.add_parser("briefing", help="compose today's briefing")
    mode = b.add_mutually_exclusive_group(required=True)
    mode.add_argument("--send", action="store_true", help="push the lock-screen line to every subscribed device")
    mode.add_argument("--print", action="store_true", help="print the full briefing to stdout")
    b.add_argument("--force", action="store_true", help="send even if today's briefing was already sent")
    sub.add_parser("check-config", help="validate the config and the VAPID file, then exit")
    args = parser.parse_args(argv)

    try:
        cfg = load(args.config)
    except ConfigError as exc:
        print(f"mission-control: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(level=cfg.log_level, stream=sys.stderr, format="%(levelname)s %(name)s %(message)s")

    if args.command == "check-config":
        return check_config(cfg, args.config)
    if args.command == "briefing":
        return asyncio.run(run_briefing(cfg, send=args.send, force=args.force))
    from .server import serve

    log.info("mission-control %s starting", __version__)
    asyncio.run(serve(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
