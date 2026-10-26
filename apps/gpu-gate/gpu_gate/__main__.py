"""Entrypoint: python3 -m gpu_gate --config /etc/gpu-gate/config.json [--once [--samples N]]"""

import argparse
import json
import logging
import signal
import sys
import threading
import time

from . import __version__
from .config import ConfigError, load
from .gate import Gate
from .ollama import OllamaClient
from .server import GateHTTPServer

log = logging.getLogger("gpu_gate")


def boottime():
    """Seconds on CLOCK_BOOTTIME: like monotonic, but keeps counting through suspend, so a
    suspended PC's samples age out of the window instead of resuming as if no time passed."""
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def build_gate(cfg, unloader=None):
    return Gate(cfg, time.time, boottime, unloader=unloader)


def safe_tick(gate):
    """One sample; a failure is logged and leaves the last good status in place (it goes
    stale, and /status + /healthz answer 503 once it's older than 3 intervals)."""
    try:
        gate.tick()
        return True
    except Exception:  # noqa: BLE001 - a bad sample must not kill the gate
        log.exception("tick failed")
        return False


def run_loop(gate, interval, stop, now=time.monotonic):
    """Tick every `interval` until `stop` is set. Scheduling uses `now` (Event.wait is monotonic)."""
    deadline = now() + interval
    while not stop.wait(max(0.0, deadline - now())):
        safe_tick(gate)
        deadline += interval
        if deadline < now():  # overran (or resumed from suspend): don't burst to catch up
            deadline = now() + interval


def run_once(gate, samples, interval, sleep=time.sleep):
    """Take `samples` samples `interval` apart and return the last status. One sample can't
    show ollama_share: a DRM client's first sighting counts 0, so the default is 2."""
    status = gate.tick()
    for _ in range(samples - 1):
        sleep(interval)
        status = gate.tick()
    return status


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gpu-gate", description=__doc__)
    ap.add_argument("--config", help="JSON config file (default: built-in defaults)")
    ap.add_argument("--once", action="store_true",
                    help="sample, print the status JSON to stdout and exit (no HTTP, no unload)")
    ap.add_argument("--samples", type=int, default=2, metavar="N",
                    help="with --once: samples to take, one interval apart (default 2; "
                         "ollama_share needs at least 2)")
    args = ap.parse_args(argv)
    if args.samples < 1:
        ap.error("--samples must be >= 1")

    try:
        cfg = load(args.config)
    except (OSError, ConfigError) as e:
        print("gpu-gate: config: %s" % e, file=sys.stderr)
        return 2
    # stderr (journald keeps both streams), so --once's stdout is the JSON alone
    logging.basicConfig(stream=sys.stderr, level=cfg["log_level"],
                        format="%(levelname)s %(name)s: %(message)s")

    if args.once:
        status = run_once(build_gate(cfg), args.samples, cfg["interval"])
        print(json.dumps(status, indent=2))
        return 0

    client = OllamaClient(cfg["ollama_url"], cfg["ollama_timeout"])

    def unload():
        threading.Thread(target=client.unload_all, name="unload", daemon=True).start()

    gate = build_gate(cfg, unloader=unload)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    safe_tick(gate)  # /status has data before the first request (if the sample works)
    servers = []
    try:
        for host in cfg["binds"]:
            srv = GateHTTPServer(host, cfg["port"], gate)
            threading.Thread(target=srv.serve_forever, name="http-" + host, daemon=True).start()
            servers.append(srv)
            log.info("gpu-gate %s listening on %s:%d", __version__, host, cfg["port"])
    except OSError as e:
        log.error("bind failed: %s", e)
        for srv in servers:
            srv.shutdown()
        return 1

    run_loop(gate, cfg["interval"], stop)

    log.info("stopping")
    for srv in servers:
        srv.shutdown()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
