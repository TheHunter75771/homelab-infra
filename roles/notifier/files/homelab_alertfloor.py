#!/usr/bin/python3
"""homelab-alertfloor: the last-resort container alarm (homelab-alertfloor.timer, every 2 min).

For every container labelled homelab.watch=true it pushes once per incident when:
  * health is 'unhealthy' on 2 consecutive checks (counted only while it is running);
  * it is 'exited' or 'dead' without having been stopped on purpose (a docker stop/kill,
    e.g. `docker compose stop`, seen in `docker events` since it last ran);
  * its restart count rose by 3 or more since the previous check.
It pushes "<name> recovered" once the condition clears. If the Docker daemon cannot be reached
on 2 consecutive checks, that is an incident too. A push that fails is retried on the next run.
Nothing happens while the boot gate status is anything but 'ok' (the gate alerts for itself).

State: /var/lib/homelab-alertfloor/state.json. Overrides for tests: HOMELAB_ALERTFLOOR_STATE,
HOMELAB_ALERTFLOOR_BOOTGATE_STATUS, HOMELAB_ALERTFLOOR_NOTIFY, HOMELAB_ALERTFLOOR_DOCKER.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time

LABEL = "homelab.watch=true"
UNHEALTHY_CHECKS = 2
RESTART_RISE = 3
DOCKER_FAIL_CHECKS = 2
FIRST_RUN_LOOKBACK = 600
MAX_LOOKBACK = 3600
DOCKER_KEY = "docker:unreachable"
INTENT_ACTIONS = {"stop": True, "kill": True, "start": False}


def log(msg: str) -> None:
    print(f"homelab-alertfloor: {msg}", flush=True)


# --------------------------------------------------------------------------- pure logic
def intent_marks(events: list[dict]) -> dict[str, bool]:
    """name -> True if the last relevant event says it was stopped on purpose."""
    marks: dict[str, bool] = {}
    for ev in sorted(events, key=lambda e: e.get("timeNano") or e.get("time") or 0):
        action = (ev.get("Action") or ev.get("status") or "").split(":")[0]
        name = ((ev.get("Actor") or {}).get("Attributes") or {}).get("name")
        if name and action in INTENT_ACTIONS:
            marks[name] = INTENT_ACTIONS[action]
    return marks


def assess(prev: dict, containers: list[dict], events: list[dict]) -> tuple[dict, dict[str, tuple[str, str]]]:
    """Fold one observation into per-container memory.

    containers: [{"name", "id", "status", "health", "restart_count", "exit_code"}]
    Returns (tracked, active) where active maps incident key -> (title, body)."""
    marks = intent_marks(events)
    tracked: dict[str, dict] = {}
    active: dict[str, tuple[str, str]] = {}
    for c in containers:
        name, status = c["name"], c["status"]
        p = prev.get(name) or {}
        same = p.get("id") == c["id"]
        intentional = bool(p.get("intentional")) if same else False
        if name in marks:
            intentional = marks[name]
        if status == "running":
            intentional = False

        streak = int(p.get("unhealthy_streak", 0)) if same else 0
        streak = streak + 1 if status == "running" and c.get("health") == "unhealthy" else 0

        rise = c["restart_count"] - int(p["restart_count"]) if same and "restart_count" in p else 0
        looping = bool(p.get("restart_loop")) if same else False
        if rise >= RESTART_RISE:
            looping = True
        elif rise == 0 and status == "running":
            looping = False

        tracked[name] = {
            "id": c["id"],
            "intentional": intentional,
            "unhealthy_streak": streak,
            "restart_count": c["restart_count"],
            "restart_loop": looping,
        }
        if streak >= UNHEALTHY_CHECKS:
            active[f"{name}:unhealthy"] = (f"{name} unhealthy", f"Health check failing on {streak} consecutive checks.")
        if status in ("exited", "dead") and not intentional:
            active[f"{name}:down"] = (
                f"{name} is down",
                f"Container is {status} (exit code {c.get('exit_code', '?')}) and nobody stopped it.",
            )
        if looping:
            active[f"{name}:restarts"] = (
                f"{name} restart loop",
                f"Restart count rose by {rise} since the last check (now {c['restart_count']}).",
            )
    return tracked, active


def reconcile(announced: dict[str, str], active: dict[str, tuple[str, str]], notify) -> dict[str, str]:
    """Push once when an incident opens and once when it clears. A push that fails is kept
    pending and retried next run. notify(tag, title, body) returns homelab-notify's exit code;
    0 (sent or deduped) and 2 (nobody subscribed) both count as done."""
    out = dict(announced)
    for key, (title, body) in sorted(active.items()):
        if key not in out and notify(_tag(key), title, body) in (0, 2):
            out[key] = title
    for key in sorted(set(out) - set(active)):
        subject = key.split(":", 1)[0] if key != DOCKER_KEY else "Docker"
        if notify(_tag(key), f"{subject} recovered", f"Cleared: {out[key]}.") in (0, 2):
            del out[key]
    return out


def _tag(key: str) -> str:
    return "alertfloor:" + key


# --------------------------------------------------------------------------- docker
def _docker(argv: list[str], timeout: float = 60) -> tuple[int, str]:
    exe = os.environ.get("HOMELAB_ALERTFLOOR_DOCKER", "docker")
    try:
        proc = subprocess.run(  # noqa: S603
            [exe, *argv], capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return 127, "docker: not found"
    except subprocess.TimeoutExpired:
        return 124, "docker: timed out"
    return proc.returncode, proc.stdout if proc.returncode == 0 else proc.stderr.strip()


def observe(since: int, until: int) -> tuple[list[dict] | None, list[dict], str]:
    rc, out = _docker(["ps", "--all", "--quiet", "--no-trunc", "--filter", f"label={LABEL}"])
    if rc != 0:
        return None, [], out
    ids = out.split()
    containers: list[dict] = []
    if ids:
        rc, out = _docker(["inspect", *ids])
        if rc != 0:
            return None, [], out
        for item in json.loads(out):
            state = item.get("State") or {}
            containers.append(
                {
                    "name": (item.get("Name") or "").lstrip("/"),
                    "id": item.get("Id", ""),
                    "status": state.get("Status", ""),
                    "health": (state.get("Health") or {}).get("Status", ""),
                    "restart_count": int(item.get("RestartCount") or 0),
                    "exit_code": state.get("ExitCode"),
                }
            )
    rc, out = _docker(
        [
            "events",
            "--since",
            str(since),
            "--until",
            str(until),
            "--filter",
            "type=container",
            "--filter",
            f"label={LABEL}",
            "--format",
            "{{json .}}",
        ],
        timeout=30,
    )
    events = []
    if rc == 0:
        for line in out.splitlines():
            with contextlib.suppress(ValueError):
                events.append(json.loads(line))
    else:
        log(f"docker events failed ({out}); judging stops from the previous state only")
    return containers, events, ""


# --------------------------------------------------------------------------- main
def _read_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _write_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def make_notify():
    exe = os.environ.get("HOMELAB_ALERTFLOOR_NOTIFY", "/usr/local/bin/homelab-notify")

    def notify(tag: str, title: str, body: str) -> int:
        try:
            proc = subprocess.run(  # noqa: S603
                [exe, "--tag", tag, "--title", title, "--body", body],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            log(f"homelab-notify failed: {exc}")
            return 1
        log(f"pushed '{title}' (homelab-notify exit {proc.returncode})")
        return proc.returncode

    return notify


def run(now: float | None = None, observe_fn=observe, notify=None) -> int:
    state_path = os.environ.get("HOMELAB_ALERTFLOOR_STATE", "/var/lib/homelab-alertfloor/state.json")
    gate_path = os.environ.get("HOMELAB_ALERTFLOOR_BOOTGATE_STATUS", "/var/lib/bootgate/status")
    try:
        with open(gate_path, encoding="utf-8") as fh:
            gate = fh.read().strip()
    except FileNotFoundError:
        gate = "unset"
    if gate != "ok":
        log(f"boot gate status is {gate}; skipping (the gate alerts for itself)")
        return 0

    now = int(time.time() if now is None else now)
    state = _read_state(state_path)
    last = state.get("until")
    since = max(int(last), now - MAX_LOOKBACK) if isinstance(last, int | float) else now - FIRST_RUN_LOOKBACK
    containers, events, err = observe_fn(since, now)
    announced = dict(state.get("announced") or {})
    active: dict[str, tuple[str, str]] = {}

    if containers is None:
        streak = int(state.get("docker_fail_streak", 0)) + 1
        state["docker_fail_streak"] = streak
        log(f"docker unreachable ({err}); streak {streak}")
        # keep per-container incidents as they are; only the daemon incident can change
        active = {k: (v, "") for k, v in announced.items() if k != DOCKER_KEY}
        if streak >= DOCKER_FAIL_CHECKS:
            active[DOCKER_KEY] = ("Docker unreachable", f"The Docker daemon did not answer on {streak} checks: {err}")
    else:
        state["docker_fail_streak"] = 0
        tracked, active = assess(state.get("containers") or {}, containers, events)
        names = {c["name"] for c in containers}
        for key in [k for k in announced if k != DOCKER_KEY and k.split(":", 1)[0] not in names]:
            log(f"{key.split(':', 1)[0]} no longer exists; dropping its open incident")
            del announced[key]
        state["containers"] = tracked
        state["until"] = now

    state["announced"] = reconcile(announced, active, notify or make_notify())
    _write_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(run())
