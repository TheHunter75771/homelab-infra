#!/usr/bin/python3
"""homelab-alertfloor: the last-resort container alarm (homelab-alertfloor.timer, every 2 min).

For every container labelled homelab.watch=true it pushes once per incident when:
  * health is 'unhealthy' on 2 consecutive checks (counted only while it is running);
  * it is 'exited' or 'dead' without having been stopped on purpose. Each container (by id)
    keeps a small lifecycle model, replayed IN TIME ORDER from its events and the inspect
    snapshot (stamped when inspect was called): stop/kill/destroy set intent, start and oom
    clear it, die records whether the exit was intentional. A snapshot never overrides a newer
    event, and a running snapshot only clears intent after a die (a start we missed). The
    model persists, so intent survives across runs;
  * its restart count rose by 3 or more since the previous check.
A single unexpected crash that Docker's restart policy recovers (a die without intent, with a
non-zero exit or an OOM, not part of a 3+ restart loop) pushes ONCE "<app> crashed and
restarted" (tag alertfloor:<app>:crash): informational, no incident, no "recovered". A crash
that stays down is the "down" incident instead. Only a fatal kill (SIGKILL/TERM/INT/QUIT or
the container's StopSignal) counts as intent; a HUP/USR1 reload does not.
A "down" is confirmed before it is pushed: the run looks again with a fresh events window, so
a stop that raced the first look, or a container that compose removed, never alerts. The events
window always closes after ps + inspect. A removed container is never judged.
It pushes "<name> recovered" once the condition clears. If the Docker daemon cannot be reached
on 2 consecutive checks, that is an incident too, as is `docker events` failing on 3 (a check
without the event history is skipped rather than guessed). A push that fails is retried on
the next run. Nothing happens while the boot gate status is anything but 'ok'.

State: /var/lib/homelab-alertfloor/state.json. Overrides for tests: HOMELAB_ALERTFLOOR_STATE,
HOMELAB_ALERTFLOOR_BOOTGATE_STATUS, HOMELAB_ALERTFLOOR_NOTIFY, HOMELAB_ALERTFLOOR_DOCKER,
HOMELAB_ALERTFLOOR_EVENTS_MARGIN, HOMELAB_ALERTFLOOR_DOCKER_TIMEOUT.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import signal
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
EVENTS_KEY = "docker:events"
EVENTS_FAIL_CHECKS = 3
SPECIAL_KEYS = {DOCKER_KEY: "Docker", EVENTS_KEY: "Alert floor"}
# Container lifecycle events that decide whether an exit was on purpose. stop/kill/destroy
# (docker stop/kill/rm, compose stop/down/recreate, the dashboard's helper) mean on purpose;
# start begins a new life; an OOM kill is never on purpose; die is the exit itself.
INTENT_EVENTS = {"stop", "kill", "destroy"}
LIFECYCLE_EVENTS = INTENT_EVENTS | {"start", "oom", "die"}
# A kill means "stop it" only with a fatal signal (or the container's own StopSignal). A
# `kill -s HUP`/`USR1` reload is not intent: if the app then dies, that is a crash.
FATAL_SIGNALS = frozenset({int(signal.SIGKILL), int(signal.SIGTERM), int(signal.SIGINT), int(signal.SIGQUIT)})
NOTICE_KEEP_SECONDS = 3600  # an informational push that keeps failing is dropped after this
SNAPSHOT_POINTS = {"running": "seen-up", "paused": "seen-up", "exited": "seen-down", "dead": "seen-down"}


def log(msg: str) -> None:
    print(f"homelab-alertfloor: {msg}", flush=True)


# --------------------------------------------------------------------------- pure logic
def parse_signal(value: object) -> int | None:
    """'15', 'SIGTERM', 'TERM', 'SIGRTMIN+3' -> signal number; None if unknown or absent."""
    text = str(value or "").strip().upper()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if not text.startswith("SIG"):
        text = "SIG" + text
    rt = re.fullmatch(r"SIGRTM(IN|AX)([+-]\d+)?", text)
    if rt:
        return int(signal.SIGRTMIN if rt.group(1) == "IN" else signal.SIGRTMAX) + int(rt.group(2) or 0)
    try:
        return int(signal.Signals[text])
    except KeyError:
        return None


def lifecycle_events(events: list[dict]) -> tuple[dict[str, list], dict[str, list]]:
    """(by container id, by name) -> [(ns, action, {"signal", "exit_code"})]. Real Docker events
    carry the container id, which keeps a recreated container (new id, same name) from
    inheriting the old one's intent; the name is only a fallback for events without an id."""
    by_id: dict[str, list] = {}
    by_name: dict[str, list] = {}
    for ev in events:
        action = (ev.get("Action") or ev.get("status") or "").split(":")[0]
        if action not in LIFECYCLE_EVENTS:
            continue
        ns = int(ev.get("timeNano") or float(ev.get("time") or 0) * 1e9)
        actor = ev.get("Actor") or {}
        cid = actor.get("ID") or ev.get("id")
        attrs = actor.get("Attributes") or {}
        name = attrs.get("name")
        point = (ns, action, {"signal": attrs.get("signal"), "exit_code": attrs.get("exitCode")})
        if cid:
            by_id.setdefault(cid, []).append(point)
        elif name:
            by_name.setdefault(name, []).append(point)
    return by_id, by_name


def _exit_code(value: object) -> int | None:
    try:
        return int(str(value))
    except ValueError:
        return None


def fold_lifecycle(model: dict, timeline: list[tuple], stop_signal: int | None = None) -> dict:
    """Replay events and the inspect snapshot IN TIME ORDER over the stored model
    {phase: up|down|None, intent: bool, exit_intentional: bool|None, crash: dict|None}.
    Timeline points are (ns, what) or (ns, what, {"signal", "exit_code"}).

    The snapshot is a point in time like any event, so it can never erase what a NEWER event
    says. This was the live failure: a "running" snapshot taken before a stop/kill/die cleared
    the intent those later events had set, and the next run judged the exit "nobody stopped it".
    A running snapshot clears intent only if it comes after a die: the container came back
    without us seeing its start. Ties go to the event (it can't be newer than the snapshot).

    Only a fatal kill (FATAL_SIGNALS or the container's StopSignal; a kill event without a
    signal attribute is taken as fatal) sets intent. A die without intent, with a non-zero
    exit or after an oom, is a crash: it waits in "crash" and becomes the "notice" (returned
    in the model, not persisted) when the container is up again."""
    m = {
        "phase": model.get("phase"),
        "intent": bool(model.get("intent")),
        "exit_intentional": model.get("exit_intentional"),
        "crash": model.get("crash"),
        "oom": bool(model.get("oom")),
        "notice": None,
    }
    for point in sorted(timeline, key=lambda p: (p[0], p[1].startswith("seen-"))):
        what = point[1]
        info = point[2] if len(point) > 2 else {}
        if what == "kill":
            sig = parse_signal(info.get("signal"))
            if sig is None or sig in FATAL_SIGNALS or sig == stop_signal:
                m["intent"] = True
        elif what in INTENT_EVENTS:
            m["intent"] = True
        elif what == "start" or (what == "seen-up" and m["phase"] == "down"):
            # a new life (seen-up after a die = a start we missed); a waiting crash is now
            # "crashed and restarted"
            if m["crash"]:
                prev = m["notice"] or {"count": 0, "oom": False}
                m["notice"] = {
                    "exit_code": m["crash"]["exit_code"],
                    "oom": prev["oom"] or m["crash"]["oom"],
                    "count": prev["count"] + m["crash"]["count"],
                }
            m.update(phase="up", intent=False, exit_intentional=None, crash=None, oom=False)
        elif what == "seen-up":
            m["phase"] = "up"
        elif what == "oom":
            m.update(intent=False, oom=True)
        elif what == "die":
            code = _exit_code(info.get("exit_code"))
            m.update(phase="down", exit_intentional=m["intent"])
            if not m["intent"] and (m["oom"] or code != 0):
                count = (m["crash"] or {}).get("count", 0) + 1
                m["crash"] = {"exit_code": code, "oom": m["oom"], "count": count}
            m["oom"] = False
        elif what == "seen-down" and m["phase"] != "down":  # exited, but we missed its die
            m.update(phase="down", exit_intentional=m["intent"])
    return m


def assess(prev: dict, containers: list[dict], events: list[dict]) -> tuple[dict, dict, dict]:
    """Fold one observation into per-container memory.

    containers: [{"name", "id", "status", "health", "restart_count", "exit_code", "seen_ns",
    "stop_signal"}] (seen_ns: when inspect was called, the earliest moment its snapshot can
    describe). Returns (tracked, active, notices): active maps incident key -> (title, body);
    notices are one-off informational pushes ("crashed and restarted") with no incident."""
    by_id, by_name = lifecycle_events(events)
    tracked: dict[str, dict] = {}
    active: dict[str, tuple[str, str]] = {}
    notices: dict[str, tuple[str, str]] = {}
    for c in containers:
        name, status = c["name"], c["status"]
        p = prev.get(name) or {}
        same = p.get("id") == c["id"]
        model: dict = {"phase": None, "intent": False, "exit_intentional": None}
        if same and "phase" in p:
            model = p
        elif same and p.get("intentional"):  # state written before the lifecycle model
            model = {"phase": "down", "intent": True, "exit_intentional": True}
        timeline = list(by_id.get(c["id"], [])) + list(by_name.get(name, []))
        if status in SNAPSHOT_POINTS:
            timeline.append((c.get("seen_ns", math.inf), SNAPSHOT_POINTS[status]))
        model = fold_lifecycle(model, timeline, stop_signal=c.get("stop_signal"))
        notice = model.pop("notice")
        intentional = model["exit_intentional"] is True if model["phase"] == "down" else model["intent"]

        streak = int(p.get("unhealthy_streak", 0)) if same else 0
        streak = streak + 1 if status == "running" and c.get("health") == "unhealthy" else 0

        rise = c["restart_count"] - int(p["restart_count"]) if same and "restart_count" in p else 0
        looping = bool(p.get("restart_loop")) if same else False
        if rise >= RESTART_RISE:
            looping = True
        elif rise == 0 and status == "running":
            looping = False

        # Down only if the snapshot shows it AND the lifecycle agrees: a start newer than the
        # snapshot means the snapshot is already stale, and the next run sees it running.
        down = status in ("exited", "dead") and model["phase"] == "down" and model["exit_intentional"] is not True
        if down or looping:
            # a crash that stays down is the "down" incident; crashes in a loop are the loop
            # incident: no per-crash pushes on top of either
            model["crash"] = None
            notice = None
        if notice:
            oom = " (out of memory)" if notice["oom"] else ""
            code = notice["exit_code"] if notice["exit_code"] is not None else "unknown"
            many = f" {notice['count']} crashes since the last check." if notice["count"] > 1 else ""
            notices[f"{name}:crash"] = (
                f"{name} crashed and restarted",
                f"Exit code {code}{oom}; Docker restarted it.{many}",
            )

        tracked[name] = {
            "id": c["id"],
            "phase": model["phase"],
            "intent": model["intent"],
            "exit_intentional": model["exit_intentional"],
            "crash": model["crash"],
            "oom": model["oom"],
            "intentional": intentional,
            "unhealthy_streak": streak,
            "restart_count": c["restart_count"],
            "restart_loop": looping,
        }
        if streak >= UNHEALTHY_CHECKS:
            active[f"{name}:unhealthy"] = (f"{name} unhealthy", f"Health check failing on {streak} consecutive checks.")
        if down:
            active[f"{name}:down"] = (
                f"{name} is down",
                f"Container is {status} (exit code {c.get('exit_code', '?')}) and nobody stopped it.",
            )
        if looping:
            active[f"{name}:restarts"] = (
                f"{name} restart loop",
                f"Restart count rose by {rise} since the last check (now {c['restart_count']}).",
            )
    return tracked, active, notices


def send_notices(pending: dict, new: dict[str, tuple[str, str]], notify, now: float) -> dict:
    """Informational pushes: sent once, never an incident, never a "recovered". One that fails
    is kept and retried on later runs for up to NOTICE_KEEP_SECONDS. 0 (sent or deduped) and
    2 (nobody subscribed) count as done."""
    queue = dict(pending)
    queue.update({key: {"title": t, "body": b, "ts": now} for key, (t, b) in new.items()})
    left = {}
    for key, notice in sorted(queue.items()):
        if now - float(notice.get("ts", now)) > NOTICE_KEEP_SECONDS:
            continue
        if notify(_tag(key), notice["title"], notice["body"]) not in (0, 2):
            left[key] = notice
    return left


def reconcile(announced: dict[str, str], active: dict[str, tuple[str, str]], notify) -> dict[str, str]:
    """Push once when an incident opens and once when it clears. A push that fails is kept
    pending and retried next run. notify(tag, title, body) returns homelab-notify's exit code;
    0 (sent or deduped) and 2 (nobody subscribed) both count as done."""
    out = dict(announced)
    for key, (title, body) in sorted(active.items()):
        if key not in out and notify(_tag(key), title, body) in (0, 2):
            out[key] = title
    for key in sorted(set(out) - set(active)):
        subject = SPECIAL_KEYS.get(key) or key.split(":", 1)[0]
        if notify(_tag(key), f"{subject} recovered", f"Cleared: {out[key]}.") in (0, 2):
            del out[key]
    return out


def _tag(key: str) -> str:
    return "alertfloor:" + key


# --------------------------------------------------------------------------- docker
_MISSING_RE = re.compile(r"No such (?:object|container): (\S+)")


def _docker_timeout() -> float:
    try:
        return max(0.1, float(os.environ.get("HOMELAB_ALERTFLOOR_DOCKER_TIMEOUT", "60")))
    except ValueError:
        return 60.0


def _docker(argv: list[str], timeout: float | None = None) -> tuple[int, str, str]:
    """(exit code, stdout, stderr). 124 = timed out, 127 = no docker binary."""
    timeout = _docker_timeout() if timeout is None else min(timeout, _docker_timeout())
    exe = os.environ.get("HOMELAB_ALERTFLOOR_DOCKER", "docker")
    try:
        proc = subprocess.run(  # noqa: S603
            [exe, *argv], capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return 127, "", "docker: not found"
    except subprocess.TimeoutExpired:
        return 124, "", "docker: timed out"
    return proc.returncode, proc.stdout, proc.stderr.strip()


def _events_margin() -> float:
    """Seconds past 'now' for the end of the events window (absorbs sub-second ordering). The
    docker CLI waits until --until has passed, so this costs at most that long per run."""
    try:
        return max(0.0, float(os.environ.get("HOMELAB_ALERTFLOOR_EVENTS_MARGIN", "1")))
    except ValueError:
        return 1.0


def _container(item: dict) -> dict:
    state = item.get("State") or {}
    return {
        "name": (item.get("Name") or "").lstrip("/"),
        "id": item.get("Id", ""),
        "status": state.get("Status", ""),
        "health": (state.get("Health") or {}).get("Status", ""),
        "restart_count": int(item.get("RestartCount") or 0),
        "exit_code": state.get("ExitCode"),
        "stop_signal": parse_signal((item.get("Config") or {}).get("StopSignal")),
    }


def read_inspect(ids: list[str], rc: int, out: str, err: str) -> tuple[list[dict] | None, set[str], str]:
    """Judge a `docker inspect <ids>` result: (items, removed ids, reason).

    items is None ("Docker can't be trusted right now", handled like unreachable: state, window
    and incidents stay untouched) unless EVERY requested id is accounted for, either returned
    on stdout or, on a non-zero exit, named in stderr as "No such object/container". Only those
    named ids count as removed. A timeout, a missing binary, empty or unparsable stdout, any
    other stderr line, or an id nobody accounts for is never read as "removed".
    """
    if rc in (124, 127):
        return None, set(), err or f"docker inspect exit {rc}"
    try:
        items = json.loads(out)
    except ValueError:
        return None, set(), err or f"docker inspect exit {rc} with no usable output"
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        return None, set(), f"docker inspect exit {rc} with unexpected output"
    removed: set[str] = set()
    if rc != 0:
        lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
        for line in lines:
            match = _MISSING_RE.search(line)
            if not match:
                return None, set(), err  # a real error, not (only) a removal
            removed.add(match.group(1))
        if not removed:
            return None, set(), err or f"docker inspect exit {rc}"
    unaccounted = set(ids) - {i.get("Id") for i in items} - removed
    if unaccounted:
        return None, set(), f"docker inspect did not account for {len(unaccounted)} container(s)"
    return items, removed & set(ids), ""


def observe(since: float) -> tuple[list[dict] | None, list[dict] | None, float, str]:
    """One look at Docker: (containers | None if Docker can't be reached, events | None if the
    event history can't be read, until, error).

    The events window is closed only AFTER ps + inspect. Any stop/kill that produced an
    'exited' seen by inspect was logged before inspect answered, so it is inside the window.
    (Fixing the window at the start of the run missed a compose down that landed in between.)
    A container removed between ps and inspect makes inspect exit 1, name it in stderr ("No such
    object") and still return the others: only the named ids count as removed (never judged).
    Any other inspect failure is treated as Docker unreachable (see read_inspect).
    """
    rc, out, err = _docker(["ps", "--all", "--quiet", "--no-trunc", "--filter", f"label={LABEL}"])
    if rc != 0:
        return None, None, since, err or f"docker ps exit {rc}"
    ids = out.split()
    containers: list[dict] = []
    if ids:
        seen_ns = time.time_ns()  # the snapshot describes some moment after this
        rc, out, err = _docker(["inspect", *ids])
        items, removed, reason = read_inspect(ids, rc, out, err)
        if items is None:
            return None, None, since, reason
        if removed:
            log(f"{len(removed)} container(s) were removed during the check; not judged")
        containers = [dict(_container(item), seen_ns=seen_ns) for item in items]
    until = time.time() + _events_margin()
    rc, out, err = _docker(
        [
            "events",
            "--since",
            f"{since:.6f}",
            "--until",
            f"{until:.6f}",
            "--filter",
            "type=container",
            "--filter",
            f"label={LABEL}",
            "--format",
            "{{json .}}",
        ],
        timeout=30 + _events_margin(),
    )
    if rc != 0:
        return containers, None, until, err or f"docker events exit {rc}"
    events = []
    for line in out.splitlines():
        with contextlib.suppress(ValueError):
            events.append(json.loads(line))
    return containers, events, until, ""


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

    now = time.time() if now is None else now
    state = _read_state(state_path)
    last = state.get("until")
    since = max(float(last), now - MAX_LOOKBACK) if isinstance(last, int | float) else now - FIRST_RUN_LOOKBACK
    prev = state.get("containers") or {}
    announced = dict(state.get("announced") or {})
    active: dict[str, tuple[str, str]] = {}

    notices: dict[str, tuple[str, str]] = {}
    containers, events, until, err = observe_fn(since)
    if containers is not None and events is not None:
        tracked, active, notices = assess(prev, containers, events)
        new_down = sorted(k for k in active if k.endswith(":down") and k not in announced)
        if new_down:
            # Confirm before telling anyone: look again with a fresh events window. A stop/kill
            # that raced the first look is in it now, and a container compose removed is gone.
            log(f"{', '.join(k.split(':', 1)[0] for k in new_down)} looks down; re-checking with a fresh event window")
            containers, events, until, err = observe_fn(since)
            notices = {}
            if containers is not None and events is not None:
                tracked, active, notices = assess(prev, containers, events)

    if containers is None:
        streak = int(state.get("docker_fail_streak", 0)) + 1
        state["docker_fail_streak"] = streak
        log(f"docker unreachable ({err}); streak {streak}")
        # keep per-container incidents as they are; only the daemon incident can change
        active = {k: (v, "") for k, v in announced.items() if k != DOCKER_KEY}
        if streak >= DOCKER_FAIL_CHECKS:
            active[DOCKER_KEY] = ("Docker unreachable", f"The Docker daemon did not answer on {streak} checks: {err}")
    elif events is None:
        # Without the stop/kill history nothing can be judged fairly, so this check is skipped:
        # containers and the window stay as they were, and the next run re-reads the same window.
        streak = int(state.get("events_fail_streak", 0)) + 1
        state["events_fail_streak"] = streak
        log(f"docker events failed ({err}); check skipped, streak {streak}")
        active = {k: (v, "") for k, v in announced.items() if k != EVENTS_KEY}
        if streak >= EVENTS_FAIL_CHECKS:
            active[EVENTS_KEY] = (
                "Alert floor blind",
                f"docker events failed on {streak} checks, so stopped containers can't be judged: {err}",
            )
    else:
        state["docker_fail_streak"] = 0
        state["events_fail_streak"] = 0
        names = {c["name"] for c in containers}
        for key in [k for k in announced if k not in SPECIAL_KEYS and k.split(":", 1)[0] not in names]:
            log(f"{key.split(':', 1)[0]} no longer exists; dropping its open incident")
            del announced[key]
        state["containers"] = tracked
        state["until"] = until

    notify = notify or make_notify()
    state["announced"] = reconcile(announced, active, notify)
    state["notices"] = send_notices(state.get("notices") or {}, notices, notify, now)
    _write_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(run())
