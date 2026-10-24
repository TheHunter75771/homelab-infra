#!/usr/bin/python3 -I
"""homelab-dockerctl: the dashboard's privileged Docker helper (docs/CONTRACTS.md §11.2, ADR 0013).

Socket-activated: homelab-dockerctl.socket (Accept=yes) accepts each connection on
/run/homelab-dockerctl.sock (0660 root:dashboard) and starts one homelab-dockerctl@.service
instance with the connection on stdin. One request per connection, then the connection closes.

Protocol (apps/dashboard/server/src/helper/client.js speaks it)
  request   one JSON object on one line (at most 4 KiB), e.g. {"op": "logs", "name": "sonarr"}.
            Send it with write(), never end(): a half-closed socket reads as "client gone".
  unary     one line, then the helper closes:
            {"ok": true, "op": OP, "data": DATA} or {"ok": false, "error": {"code": CODE, "message": TEXT}}
  streams   logs and update: first {"ok": true, "op": OP, "stream": true, ...} (or the error line
            above, when the request is refused), then one event per line,
              {"type": "line", "ts": RFC3339|null, "text": TEXT, "stream": "stdout"|"stderr"}
              {"type": "step", "step": pulling|recreating|waiting-health|done|failed, "at": ISO, ...}
              {"type": "output", "text": TEXT}   (compose progress during an update)
            and always last {"type": "end", "ok": BOOL, "reason": TEXT, "message"?: TEXT}.
            Closing the client side cancels a log stream; an update always runs to its end.

Rules
  - Only the dashboard user may connect (SO_PEERCRED uid check); everyone else, root included,
    gets "forbidden".
  - Exactly these operations exist: ps, logs, start, stop, restart, update, docker-state,
    last-pull, disk-split, boots. No exec, build, arbitrary paths, other containers, or image
    and volume management.
  - Docker is touched only after `systemctl is-active docker.service` prints "active", checked
    again before every docker command: touching the socket would start Docker and so re-run
    the boot gate.
  - Containers must carry the label homelab.watch=true. Names match a strict pattern and are
    resolved to full container IDs before use. No shell anywhere: every command is an argv list
    with a timeout.
  - Every request is logged to journald (stderr, SyslogIdentifier=homelab-dockerctl). Log
    contents and container output never go to the journal.

Configuration: a JSON file (HOMELAB_DOCKERCTL_CONFIG, default /etc/homelab-dashboard/dockerctl.json)
written by Ansible (role dashboard). Python 3 stdlib only.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import fcntl
import json
import os
import re
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

CONFIG_ENV = "HOMELAB_DOCKERCTL_CONFIG"
DEFAULT_CONFIG = "/etc/homelab-dashboard/dockerctl.json"

MAX_REQUEST_BYTES = 4096
MAX_LINE_BYTES = 16384
MAX_COMPOSE_BYTES = 256 * 1024
MAX_BOOTS = 200
MAX_TAIL = 2000
DEFAULT_TAIL = 200
WATCH_LABEL = "homelab.watch"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}\Z")
STACK_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}\Z")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}\Z")
BOOT_ID_RE = re.compile(r"^[0-9a-f]{32}\Z")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}\Z")
DIR_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}\Z")
# docker logs --since: unix seconds, a relative duration (10m, 2h), or an RFC 3339 date/time.
SINCE_RE = re.compile(
    r"^(?:\d{1,10}|\d{1,6}[smh]|\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2}))?)\Z"
)
TIMESTAMP_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})) ?")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
ACCEPTED_RE = re.compile(r"^Accepted \S+ for (?P<user>\S+) from (?P<addr>\S+) port (?P<port>\d+)\b")
DISCONNECTED_RE = re.compile(r"^Disconnected from user (?P<user>\S+) (?P<addr>\S+) port (?P<port>\d+)\b")

# The whole operation surface. Each op lists the request fields it accepts and their types.
OPS: dict[str, dict[str, type]] = {
    "ps": {},
    "logs": {"name": str, "tail": int, "follow": bool, "since": str},
    "start": {"name": str},
    "stop": {"name": str},
    "restart": {"name": str},
    "update": {"name": str},
    "docker-state": {},
    "last-pull": {},
    "disk-split": {},
    "boots": {},
}
REQUIRED: dict[str, frozenset[str]] = {
    "logs": frozenset({"name"}),
    "start": frozenset({"name"}),
    "stop": frozenset({"name"}),
    "restart": frozenset({"name"}),
    "update": frozenset({"name"}),
}
STREAM_OPS = frozenset({"logs", "update"})
# A start, restart or update of an app in a gate-held stack needs the boot gate status "ok".
GATED_OPS = frozenset({"start", "restart", "update"})


class OpError(Exception):
    """A failure reported to the client as {"ok": false, "error": {"code", "message"}}."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ComposeError(ValueError):
    pass


# --------------------------------------------------------------------------- configuration


@dataclasses.dataclass(frozen=True)
class Config:
    allowed_uid: int
    stacks_dir: str = "/srv/homelab/stacks"
    media_mount: str = "/media"
    media_dirs: tuple[str, ...] = ("tv", "movies", "downloads")
    media_sentinel: str = ".homelab-media-sentinel"
    bootgate_status_file: str = "/var/lib/bootgate/status"
    held_stacks: tuple[str, ...] = ("media",)
    backup_pull_user: str = "backuppull"
    cache_dir: str = "/var/cache/homelab-dockerctl"
    docker: str = "/usr/bin/docker"
    systemctl: str = "/usr/bin/systemctl"
    journalctl: str = "/usr/bin/journalctl"
    du: str = "/usr/bin/du"
    nice: str = "/usr/bin/nice"
    ionice: str = "/usr/bin/ionice"
    docker_unit: str = "docker.service"
    # Owner every compose file and its directories must have (root). Tests use their own uid.
    trusted_owner_uid: int = 0
    # Timeouts and limits, in seconds unless named otherwise.
    request_timeout: float = 5
    send_timeout: float = 30
    command_timeout: float = 30
    action_timeout: float = 180
    logs_timeout: float = 60
    follow_max_seconds: float = 1800
    pull_timeout: float = 1200
    up_timeout: float = 300
    health_timeout: float = 300
    health_poll_seconds: float = 2
    health_stable_seconds: float = 5
    journal_timeout: float = 60
    last_pull_days: int = 45
    du_timeout: float = 1800
    disk_split_ttl: float = 86400
    poll_seconds: float = 1.0

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> Config:
        fields = {f.name: f for f in dataclasses.fields(cls)}
        unknown = sorted(set(raw) - set(fields))
        if unknown:
            raise ValueError(f"unknown config keys: {', '.join(unknown)}")
        values: dict[str, Any] = {}
        for key, value in raw.items():
            if key in ("media_dirs", "held_stacks"):
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise ValueError(f"{key} must be a list of strings")
                value = tuple(value)
            values[key] = value
        cfg = cls(**values)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not isinstance(self.allowed_uid, int) or isinstance(self.allowed_uid, bool) or self.allowed_uid <= 0:
            raise ValueError("allowed_uid must be a positive integer (never root)")
        for key in ("stacks_dir", "media_mount", "bootgate_status_file", "cache_dir", "docker", "systemctl",
                    "journalctl", "du", "nice", "ionice"):
            if not os.path.isabs(getattr(self, key)):
                raise ValueError(f"{key} must be an absolute path")
        if not all(DIR_NAME_RE.match(d) for d in self.media_dirs):
            raise ValueError("media_dirs must be plain directory names")
        if not all(STACK_RE.match(s) for s in self.held_stacks):
            raise ValueError("held_stacks must be stack names")
        if not USER_RE.match(self.backup_pull_user):
            raise ValueError("backup_pull_user is not a valid user name")
        if not DIR_NAME_RE.match(self.media_sentinel.lstrip(".")):
            raise ValueError("media_sentinel must be a plain file name")


def load_config(path: str) -> Config:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise TypeError("the config must be a JSON object")
    return Config.from_mapping(raw)


# --------------------------------------------------------------------------- small helpers


def iso_utc(epoch_seconds: float) -> str:
    return dt.datetime.fromtimestamp(epoch_seconds, tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_text(text: str, limit: int = MAX_LINE_BYTES) -> str:
    """Container/compose output for the client: no ANSI escapes or control characters (tab kept)."""
    text = CONTROL_RE.sub("", ANSI_RE.sub("", text))
    return text[:limit]


def clean_log(text: str, limit: int = 300) -> str:
    """A value that ends up in the journal: one line, printable, bounded."""
    return CONTROL_RE.sub("?", str(text).replace("\n", " ").replace("\t", " "))[:limit]


def peer_credentials(conn: socket.socket) -> tuple[int, int, int]:
    """(pid, uid, gid) of the process on the other end of a Unix socket, from the kernel."""
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return pid, uid, gid


class Log:
    """stderr lines with a syslog priority prefix; systemd files them in the journal."""

    def __init__(self, stream: Any = None) -> None:
        self.stream = stream if stream is not None else sys.stderr

    def _emit(self, priority: int, message: str) -> None:
        try:
            self.stream.write(f"<{priority}>{clean_log(message, 1000)}\n")
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def info(self, message: str) -> None:
        self._emit(6, message)

    def warning(self, message: str) -> None:
        self._emit(4, message)

    def error(self, message: str) -> None:
        self._emit(3, message)


# --------------------------------------------------------------------------- side effects


@dataclasses.dataclass
class Result:
    rc: int
    out: str
    err: str


class System:
    """Every process and clock the helper uses. The tests replace it with a fake."""

    def run(self, argv: list[str], timeout: float) -> Result:
        try:
            proc = subprocess.run(  # argv list, no shell
                argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise OpError("timeout", f"{os.path.basename(argv[0])} did not finish within {timeout:g} s") from exc
        except OSError as exc:
            raise OpError("failed", f"{os.path.basename(argv[0])}: {exc.strerror or exc}") from exc
        return Result(proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace"))

    def spawn(self, argv: list[str], split_stderr: bool = False) -> subprocess.Popen:
        try:
            return subprocess.Popen(  # argv list, no shell
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE if split_stderr else subprocess.STDOUT,
                start_new_session=True,  # its own process group, so a stop also reaches compose plugins
            )
        except OSError as exc:
            raise OpError("failed", f"{os.path.basename(argv[0])}: {exc.strerror or exc}") from exc

    def stop(self, proc: subprocess.Popen, grace: float = 5.0) -> int:
        """Wait briefly for a spawned process, then end its whole process group."""
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        return proc.returncode

    def wait(self, proc: subprocess.Popen, timeout: float) -> int | None:
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def is_mount(self, path: str) -> bool:
        return os.path.ismount(path)

    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


# --------------------------------------------------------------------------- compose files
# The deployed compose files are rendered by Ansible (role stacks) from templates in this repo,
# so a small, strict reader of the YAML block subset they use is enough: block mappings and
# sequences, quoted and plain scalars, flow collections kept as text, anchors, aliases and
# merge keys (<<). It never evaluates anything.


@dataclasses.dataclass
class _Node:
    indent: int
    key: str | None  # None for a sequence item
    value: str  # the raw text after "key:" or "- "
    anchor: str | None = None
    children: list[_Node] = dataclasses.field(default_factory=list)


def _strip_comment(line: str) -> str:
    """Drop a trailing # comment that is not inside a quoted scalar."""
    quote = None
    prev = " "
    i = 0
    while i < len(line):
        ch = line[i]
        if quote == '"':
            if ch == "\\":
                i += 2
                prev = "x"
                continue
            if ch == '"':
                quote = None
        elif quote == "'":
            if ch == "'":
                if line[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                quote = None
        elif ch in "\"'" and prev in " \t[{,:-":
            quote = ch
        elif ch == "#" and prev in " \t":
            return line[:i].rstrip()
        prev = ch
        i += 1
    return line.rstrip()


def _split_anchor(raw: str) -> tuple[str | None, str]:
    raw = raw.strip()
    if raw.startswith("&"):
        parts = raw.split(None, 1)
        return parts[0][1:], (parts[1] if len(parts) > 1 else "")
    return None, raw


def _scalar(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            value = json.loads(raw)
        except ValueError:
            return raw[1:-1]
        return value if isinstance(value, str) else raw[1:-1]
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    return raw


def _split_flow(body: str) -> list[str]:
    """Split the inside of a one-line flow collection at top-level commas."""
    parts, depth, quote, cur = [], 0, None, []
    for ch in body:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _tree(text: str) -> _Node:
    root = _Node(-1, None, "")
    stack = [root]
    for lineno, line in enumerate(text.splitlines(), 1):
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise ComposeError(f"line {lineno}: tab indentation")
        stripped = _strip_comment(line)
        body = stripped.lstrip(" ")
        if not body or body in ("---", "..."):
            continue
        indent = len(stripped) - len(body)
        item = body == "-" or body.startswith("- ")
        if item:
            key, value = None, body[1:].strip()
        elif body[0] in "\"'":
            end = body.find(body[0], 1)
            if end == -1 or not body[end + 1 :].startswith(":"):
                raise ComposeError(f"line {lineno}: unreadable quoted key")
            key, value = body[1:end], body[end + 2 :]
        elif body.endswith(":"):
            key, value = body[:-1], ""
        elif ": " in body:
            key, value = body.split(": ", 1)
        else:
            key, value = None, body  # a continuation line (multi-line scalar); kept, never read
        anchor, value = _split_anchor(value)
        node = _Node(indent, key.strip() if key is not None else None, value, anchor)
        while True:
            top = stack[-1]
            if top.indent < indent:
                break
            # "key:" followed by "- item" at the same indent
            if item and top.indent == indent and top.key is not None and top.value == "" \
                    and all(c.key is None for c in top.children):
                break
            stack.pop()
        stack[-1].children.append(node)
        stack.append(node)
    return root


def _anchors(node: _Node, found: dict[str, _Node]) -> dict[str, _Node]:
    if node.anchor:
        found[node.anchor] = node
    for child in node.children:
        _anchors(child, found)
    return found


def _resolve(node: _Node, anchors: dict[str, _Node]) -> _Node:
    """Follow an alias value (key: *name) to the anchored node."""
    seen = 0
    while node.value.startswith("*") and seen < 10:
        target = anchors.get(node.value[1:].strip())
        if target is None:
            raise ComposeError(f"unknown alias {node.value!r}")
        node, seen = target, seen + 1
    return node


def _mapping(node: _Node, anchors: dict[str, _Node], depth: int = 0) -> dict[str, _Node]:
    """A block mapping's keys, with merge keys (<<: *a or <<: [*a, *b]) applied; explicit keys win."""
    if depth > 10:
        raise ComposeError("merge keys nest too deeply")
    result: dict[str, _Node] = {}
    merges: list[str] = []
    for child in node.children:
        if child.key == "<<":
            value = child.value.strip()
            names = _split_flow(value[1:-1]) if value.startswith("[") and value.endswith("]") else [value]
            merges.extend(names)
        elif child.key is not None:
            result[child.key] = child
    for alias in merges:
        if not alias.startswith("*") or alias[1:] not in anchors:
            raise ComposeError(f"merge key needs an alias, got {alias!r}")
        for key, child in _mapping(anchors[alias[1:]], anchors, depth + 1).items():
            result.setdefault(key, child)
    return result


def _labels(node: _Node | None, anchors: dict[str, _Node]) -> dict[str, str]:
    if node is None:
        return {}
    node = _resolve(node, anchors)
    labels: dict[str, str] = {}
    value = node.value.strip()
    if value.startswith("{") and value.endswith("}"):
        for part in _split_flow(value[1:-1]):
            if ":" in part:
                k, v = part.split(":", 1)
                labels[_scalar(k)] = _scalar(v)
        return labels
    if value.startswith("[") and value.endswith("]"):
        entries = [_scalar(p) for p in _split_flow(value[1:-1])]
    else:
        entries = [_scalar(c.value) for c in node.children if c.key is None]
        for key, child in _mapping(node, anchors).items():
            labels[key] = _scalar(child.value)
    for entry in entries:
        k, _, v = entry.partition("=")
        labels[k.strip()] = v.strip()
    return labels


@dataclasses.dataclass(frozen=True)
class ComposeService:
    image: str | None
    container_name: str | None
    build: bool
    labels: dict[str, str]


@dataclasses.dataclass(frozen=True)
class ComposeFile:
    project: str | None
    services: dict[str, ComposeService]


def parse_compose(text: str) -> ComposeFile:
    root = _tree(text)
    anchors = _anchors(root, {})
    top = {c.key: c for c in root.children if c.key is not None}
    project = _scalar(top["name"].value) if "name" in top and top["name"].value else None
    services: dict[str, ComposeService] = {}
    if "services" in top:
        for svc in top["services"].children:
            if svc.key is None:
                continue
            fields = _mapping(_resolve(svc, anchors), anchors)
            image = _scalar(fields["image"].value) if "image" in fields else None
            cname = _scalar(fields["container_name"].value) if "container_name" in fields else None
            services[_scalar(svc.key)] = ComposeService(
                image=image or None,
                container_name=cname or None,
                build="build" in fields,
                labels=_labels(fields.get("labels"), anchors),
            )
    return ComposeFile(project=project, services=services)


@dataclasses.dataclass(frozen=True)
class Declared:
    """One service from a deployed compose file."""

    name: str  # container name
    stack: str  # directory under stacks_dir
    service: str
    compose_file: str
    pinned: str | None
    local_build: bool
    watch: bool

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "stack": self.stack, "service": self.service,
                "pinned": self.pinned, "local_build": self.local_build}


# --------------------------------------------------------------------------- the reply channel


class Reply:
    """Writes JSON lines to the client. A client that went away never stops a running action."""

    def __init__(self, conn: socket.socket) -> None:
        self.conn = conn
        self.gone = False
        self.started = False

    def send(self, obj: dict[str, Any]) -> bool:
        if self.gone:
            return False
        data = (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self.conn.sendall(data)
        except OSError:  # includes timeouts and a closed peer
            self.gone = True
            return False
        return True

    def ok(self, op: str, data: Any) -> None:
        self.send({"ok": True, "op": op, "data": data})

    def error(self, code: str, message: str) -> None:
        self.send({"ok": False, "error": {"code": code, "message": message}})

    def start_stream(self, op: str, **meta: Any) -> None:
        self.started = True
        self.send({"ok": True, "op": op, "stream": True, **meta})

    def event(self, obj: dict[str, Any]) -> bool:
        return self.send(obj)

    def end(self, ok: bool, reason: str, message: str | None = None) -> None:
        obj: dict[str, Any] = {"type": "end", "ok": ok, "reason": reason}
        if message:
            obj["message"] = message
        self.send(obj)


# --------------------------------------------------------------------------- request handling


def read_request(conn: socket.socket) -> bytes:
    data = b""
    while b"\n" not in data:
        try:
            chunk = conn.recv(MAX_REQUEST_BYTES + 1 - len(data))
        except TimeoutError as exc:
            raise OpError("bad-request", "no complete request line in time") from exc
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_REQUEST_BYTES:
            raise OpError("bad-request", f"request larger than {MAX_REQUEST_BYTES} bytes")
    line = data.split(b"\n", 1)[0].strip()
    if not line:
        raise OpError("bad-request", "empty request")
    return line


def validate_request(raw: bytes) -> dict[str, Any]:
    try:
        req = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise OpError("bad-request", "the request is not valid JSON") from exc
    if not isinstance(req, dict):
        raise OpError("bad-request", "the request must be a JSON object")
    op = req.get("op")
    if not isinstance(op, str) or op not in OPS:
        raise OpError("unknown-op", "unknown operation")
    allowed = OPS[op]
    for key, value in req.items():
        if key == "op":
            continue
        if key not in allowed:
            raise OpError("bad-request", f"field {key!r} is not allowed for {op}")
        want = allowed[key]
        if want is int and (isinstance(value, bool) or not isinstance(value, int)):
            raise OpError("bad-request", f"{key} must be an integer")
        if want is not int and not isinstance(value, want):
            raise OpError("bad-request", f"{key} must be a {want.__name__}")
    missing = REQUIRED.get(op, frozenset()) - set(req)
    if missing:
        raise OpError("bad-request", f"missing field(s): {', '.join(sorted(missing))}")
    if "name" in req and not NAME_RE.match(req["name"]):
        raise OpError("bad-request", "name must match ^[a-z0-9][a-z0-9_.-]{0,62}$")
    if op == "logs":
        tail = req.get("tail", DEFAULT_TAIL)
        if not 0 <= tail <= MAX_TAIL:
            raise OpError("bad-request", f"tail must be between 0 and {MAX_TAIL}")
        req["tail"] = tail
        req.setdefault("follow", False)
        if "since" in req and not SINCE_RE.match(req["since"]):
            raise OpError("bad-request", "since must be unix seconds, a duration like 10m, or an RFC 3339 time")
    return req


def describe(req: dict[str, Any]) -> str:
    parts = [f"op={req['op']}"]
    for key in ("name", "tail", "follow", "since"):
        if key in req:
            parts.append(f"{key}={str(req[key]).lower() if isinstance(req[key], bool) else req[key]}")
    return " ".join(parts)


class Helper:
    def __init__(self, cfg: Config, system: System | None = None, log: Log | None = None) -> None:
        self.cfg = cfg
        self.sys = system or System()
        self.log = log or Log()
        self.handlers: dict[str, Callable[..., Any]] = {
            "ps": self.op_ps,
            "logs": self.op_logs,
            "start": self.op_action,
            "stop": self.op_action,
            "restart": self.op_action,
            "update": self.op_update,
            "docker-state": self.op_docker_state,
            "last-pull": self.op_last_pull,
            "disk-split": self.op_disk_split,
            "boots": self.op_boots,
        }
        assert set(self.handlers) == set(OPS)  # the allowlist and the dispatch table agree

    # ---- connection

    def serve(self, conn: socket.socket) -> str:
        """Handle one connection; returns the outcome code that was logged."""
        started = self.sys.monotonic()
        try:
            pid, uid, _gid = peer_credentials(conn)
        except OSError as exc:
            self.log.error(f"rejected: no peer credentials ({exc.strerror or exc})")
            return "forbidden"
        reply = Reply(conn)
        conn.settimeout(self.cfg.request_timeout)
        if uid != self.cfg.allowed_uid:
            self.log.warning(f"rejected peer uid={uid} pid={pid}: only uid {self.cfg.allowed_uid} may connect")
            reply.error("forbidden", "only the dashboard user may use this socket")
            return "forbidden"
        try:
            req = validate_request(read_request(conn))
        except OpError as exc:
            self.log.warning(f"rejected request from uid={uid} pid={pid}: {exc.code} ({exc.message})")
            reply.error(exc.code, exc.message)
            return exc.code
        op = req["op"]
        self.log.info(f"request peer_uid={uid} peer_pid={pid} {describe(req)}")
        conn.settimeout(self.cfg.send_timeout)
        outcome = "ok"
        try:
            if op in STREAM_OPS:
                outcome = self.handlers[op](req, reply, conn)
            else:
                reply.ok(op, self.handlers[op](req))
        except OpError as exc:
            outcome = exc.code
            if reply.started:
                reply.end(False, exc.code, exc.message)
            else:
                reply.error(exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - one request must never take the reply down silently
            outcome = "internal"
            frame = traceback.extract_tb(exc.__traceback__)[-1]
            self.log.error(f"internal error in {op}: {type(exc).__name__} at line {frame.lineno}")
            if reply.started:
                reply.end(False, "internal", "internal error")
            else:
                reply.error("internal", "internal error")
        elapsed = self.sys.monotonic() - started
        gone = " client-gone" if reply.gone else ""
        self.log.info(f"done {describe(req)} result={outcome}{gone} duration={elapsed:.2f}s")
        return outcome

    # ---- docker guard: the only way any docker command is run

    def docker_unit_state(self) -> str:
        res = self.sys.run([self.cfg.systemctl, "is-active", self.cfg.docker_unit], self.cfg.command_timeout)
        lines = res.out.strip().splitlines()
        return lines[0].strip() if lines else "unknown"

    def require_docker(self) -> None:
        state = self.docker_unit_state()
        if state != "active":
            raise OpError("docker-inactive", f"docker.service is {state}; Docker is not touched until it is active")

    def docker(self, args: list[str], timeout: float) -> Result:
        self.require_docker()
        return self.sys.run([self.cfg.docker, *args], timeout)

    def docker_spawn(self, args: list[str], split_stderr: bool = False) -> subprocess.Popen:
        self.require_docker()
        return self.sys.spawn([self.cfg.docker, *args], split_stderr=split_stderr)

    # ---- container lookup

    def watched(self) -> dict[str, dict[str, Any]]:
        """name -> docker inspect object, for containers labelled homelab.watch=true."""
        res = self.docker(["ps", "--all", "--no-trunc", "--quiet", "--filter", f"label={WATCH_LABEL}=true"],
                          self.cfg.command_timeout)
        if res.rc != 0:
            raise OpError("failed", f"docker ps exited {res.rc}")
        ids = [line.strip() for line in res.out.splitlines() if line.strip()]
        if not all(CONTAINER_ID_RE.match(i) for i in ids):
            raise OpError("failed", "docker ps returned an unexpected container ID")
        if not ids:
            return {}
        res = self.docker(["inspect", "--type", "container", *ids], self.cfg.command_timeout)
        try:
            data = json.loads(res.out) if res.out.strip() else []
        except ValueError as exc:
            raise OpError("failed", "docker inspect returned invalid JSON") from exc
        if not isinstance(data, list) or (res.rc != 0 and not data):
            raise OpError("failed", f"docker inspect exited {res.rc}")
        found: dict[str, dict[str, Any]] = {}
        for c in data:  # a container removed between ps and inspect is simply missing
            labels = (c.get("Config") or {}).get("Labels") or {}
            name = str(c.get("Name", "")).lstrip("/")
            if labels.get(WATCH_LABEL) == "true" and NAME_RE.match(name) and CONTAINER_ID_RE.match(c.get("Id", "")):
                found[name] = c
        return found

    def lookup(self, name: str) -> dict[str, Any]:
        container = self.watched().get(name)
        if container is None:
            raise OpError("not-allowed", f"no container named {name} with label {WATCH_LABEL}=true")
        return container

    def inspect_name(self, name: str) -> dict[str, Any] | None:
        """Exact-name inspect (docker would also match an ID prefix, so the name is checked)."""
        res = self.docker(["inspect", "--type", "container", name], self.cfg.command_timeout)
        if res.rc != 0:
            return None
        try:
            data = json.loads(res.out)
        except ValueError:
            return None
        for c in data if isinstance(data, list) else []:
            if str(c.get("Name", "")) == f"/{name}":
                return c
        return None

    # ---- compose files

    def _trusted(self, path: Path) -> bool:
        try:
            st = os.lstat(path)
        except OSError:
            return False
        return (not stat.S_ISLNK(st.st_mode) and st.st_uid == self.cfg.trusted_owner_uid
                and not st.st_mode & 0o022)

    def declared(self) -> list[Declared]:
        base = Path(self.cfg.stacks_dir)
        if not base.is_dir():
            return []
        if not self._trusted(base):
            self.log.warning(f"{base} is not root-owned or is writable by others; ignoring every compose file")
            return []
        found: list[Declared] = []
        for stack_dir in sorted(base.iterdir()):
            compose = stack_dir / "compose.yaml"
            if not STACK_RE.match(stack_dir.name) or not compose.exists():
                continue
            if not (self._trusted(stack_dir) and self._trusted(compose) and compose.is_file()):
                self.log.warning(f"skipping {compose}: not a root-owned regular file in a root-owned directory")
                continue
            try:
                if compose.stat().st_size > MAX_COMPOSE_BYTES:
                    raise ComposeError("file too large")
                parsed = parse_compose(compose.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ComposeError) as exc:
                self.log.warning(f"skipping {compose}: {clean_log(exc)}")
                continue
            project = parsed.project or stack_dir.name
            for service, svc in sorted(parsed.services.items()):
                name = svc.container_name or f"{project}-{service}-1"
                if not (NAME_RE.match(name) and NAME_RE.match(service)):
                    continue
                found.append(Declared(name=name, stack=stack_dir.name, service=service, compose_file=str(compose),
                                      pinned=svc.image, local_build=svc.build,
                                      watch=svc.labels.get(WATCH_LABEL) == "true"))
        return found

    # ---- boot gate

    def gate_status(self) -> str:
        try:
            words = Path(self.cfg.bootgate_status_file).read_text(encoding="utf-8").split()
        except OSError:
            return "unknown"
        return words[0] if words else "unknown"

    def check_gate(self, stack: str | None) -> None:
        if stack in self.cfg.held_stacks:
            status = self.gate_status()
            if status != "ok":
                raise OpError("held-by-gate", f"the boot gate status is {status}; {stack} apps stay held until it passes")

    # ---- locks (shared by every helper instance)

    @contextlib.contextmanager
    def lock(self, name: str, what: str):
        locks = Path(self.cfg.cache_dir) / "locks"
        locks.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(locks / f"{name}.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OpError("busy", f"{what} is already running") from exc
            yield
        finally:
            os.close(fd)

    # ---- streaming

    @staticmethod
    def _client_closed(conn: socket.socket) -> bool:
        try:
            data = conn.recv(4096)
        except (BlockingIOError, InterruptedError, TimeoutError):
            return False
        except OSError:
            return True
        return data == b""

    def pump(self, proc: subprocess.Popen, on_line: Callable[[bytes, str], None], deadline: float,
             conn: socket.socket | None = None, reply: Reply | None = None) -> str:
        """Feed a process's output lines to on_line(line, "stdout"|"stderr") until every pipe hits
        EOF ("eof"), the deadline ("timeout"), or, when conn is given, the client closes ("client-closed")."""
        sel = selectors.DefaultSelector()
        buffers: dict[int, bytes] = {}
        for pipe, tag in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
            if pipe is not None:
                fd = pipe.fileno()
                os.set_blocking(fd, False)
                sel.register(fd, selectors.EVENT_READ, tag)
                buffers[fd] = b""
        if conn is not None:
            sel.register(conn, selectors.EVENT_READ, "client")
        try:
            while buffers:
                if conn is not None and reply is not None and reply.gone:
                    return "client-closed"
                remaining = deadline - self.sys.monotonic()
                if remaining <= 0:
                    return "timeout"
                for key, _events in sel.select(min(remaining, self.cfg.poll_seconds)):
                    if key.data == "client":
                        if self._client_closed(conn):
                            return "client-closed"
                        continue
                    fd = key.fd
                    try:
                        chunk = os.read(fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        if buffers[fd]:
                            on_line(buffers[fd], key.data)
                        sel.unregister(fd)
                        del buffers[fd]
                        continue
                    *lines, rest = (buffers[fd] + chunk).split(b"\n")
                    for line in lines:
                        on_line(line, key.data)
                    if len(rest) > MAX_LINE_BYTES:
                        on_line(rest, key.data)
                        rest = b""
                    buffers[fd] = rest
            return "eof"
        finally:
            sel.close()

    def run_streamed(self, args: list[str], timeout: float, emit: Callable[[str], None]) -> tuple[int | None, str]:
        """A docker command whose output lines are passed on as they arrive."""
        proc = self.docker_spawn(args)
        reason = "eof"
        try:
            reason = self.pump(proc, lambda raw, _tag: emit(clean_text(raw.decode("utf-8", "replace"))),
                               self.sys.monotonic() + timeout)
            rc = self.sys.wait(proc, 10) if reason == "eof" else None
        finally:
            final = self.sys.stop(proc)
        if reason == "timeout":
            return None, f"timed out after {timeout:g} s"
        rc = final if rc is None else rc
        return rc, "" if rc == 0 else f"exited {rc}"

    # ---- operations

    def op_docker_state(self, _req: dict[str, Any]) -> dict[str, Any]:
        raw = self.docker_unit_state()
        state = raw if raw in ("active", "failed") else "inactive"
        return {"state": state, "unit_state": raw}

    def op_ps(self, _req: dict[str, Any]) -> dict[str, Any]:
        declared = self.declared()
        by_name = {d.name: d for d in declared}
        raw = self.docker_unit_state()
        containers: list[dict[str, Any]] = []
        if raw == "active":
            for name, c in sorted(self.watched().items()):  # watched() re-checks the guard
                st = c.get("State") or {}
                labels = (c.get("Config") or {}).get("Labels") or {}
                decl = by_name.get(name)
                containers.append({
                    "name": name,
                    "state": st.get("Status"),
                    "health": (st.get("Health") or {}).get("Status"),
                    "started_at": st.get("StartedAt"),
                    "restart_count": c.get("RestartCount", 0),
                    "image": (c.get("Config") or {}).get("Image"),
                    "pinned": decl.pinned if decl else None,
                    "stack": decl.stack if decl else labels.get("com.docker.compose.project"),
                })
        return {
            "docker": raw if raw in ("active", "failed") else "inactive",
            "containers": containers,
            # What the deployed compose files declare (no Docker needed), so the dashboard can list
            # every app as "Held by gate" or "Stopped" while Docker is down, and show the pins.
            "declared": [d.public() for d in declared if d.watch],
        }

    def op_logs(self, req: dict[str, Any], reply: Reply, conn: socket.socket) -> str:
        name = req["name"]
        container = self.lookup(name)
        args = ["logs", "--timestamps", "--tail", str(req["tail"])]
        if "since" in req:
            args += ["--since", req["since"]]
        if req["follow"]:
            args.append("--follow")
        args.append(container["Id"])
        proc = self.docker_spawn(args, split_stderr=True)
        limit = self.cfg.follow_max_seconds if req["follow"] else self.cfg.logs_timeout
        reply.start_stream("logs", name=name, follow=req["follow"])

        def on_line(raw: bytes, stream: str) -> None:
            text = raw.decode("utf-8", "replace")
            m = TIMESTAMP_PREFIX_RE.match(text)
            reply.event({"type": "line", "ts": m.group(1) if m else None,
                         "text": clean_text(text[m.end():] if m else text), "stream": stream})

        reason = "eof"
        rc = None
        try:
            reason = self.pump(proc, on_line, self.sys.monotonic() + limit, conn, reply)
            rc = self.sys.wait(proc, 10) if reason == "eof" else None
        finally:
            final = self.sys.stop(proc)
        rc = final if rc is None else rc
        if reason == "client-closed":
            return "client-closed"
        if reason == "timeout":
            if req["follow"]:  # a follow ends after follow_max_seconds by design
                reply.end(True, "max-duration")
                return "ok"
            reply.end(False, "timeout", f"docker logs did not finish within {limit:g} s")
            return "timeout"
        if rc != 0:
            reply.end(False, "failed", f"docker logs exited {rc}")
            return "failed"
        reply.end(True, "eof")
        return "ok"

    def _decl_for(self, name: str, container: dict[str, Any]) -> tuple[Declared | None, str | None]:
        decl = {d.name: d for d in self.declared()}.get(name)
        labels = (container.get("Config") or {}).get("Labels") or {}
        return decl, (decl.stack if decl else labels.get("com.docker.compose.project"))

    def op_action(self, req: dict[str, Any]) -> dict[str, Any]:
        op, name = req["op"], req["name"]
        container = self.lookup(name)
        if op in GATED_OPS:
            _decl, stack = self._decl_for(name, container)
            self.check_gate(stack)
        with self.lock(f"container-{name}", f"an action on {name}"):
            res = self.docker([op, container["Id"]], self.cfg.action_timeout)
        if res.rc != 0:
            first = (res.err.strip().splitlines() or [""])[0]
            raise OpError("failed", clean_log(f"docker {op} exited {res.rc}: {first}", 300))
        after = self.inspect_name(name) or {}
        return {"name": name, "state": (after.get("State") or {}).get("Status")}

    def op_update(self, req: dict[str, Any], reply: Reply, _conn: socket.socket) -> str:
        name = req["name"]
        container = self.lookup(name)
        decl, stack = self._decl_for(name, container)
        if decl is None:
            raise OpError("not-allowed", f"{name} is not declared in a deployed compose file")
        if decl.local_build:
            raise OpError("local-image", f"{name} is built on the host; a deploy updates it, not the dashboard")
        if not decl.pinned:
            raise OpError("not-allowed", f"{name} has no image in its compose file")
        self.check_gate(stack)
        with self.lock(f"container-{name}", f"an action on {name}"):
            # From here on the update runs to the end even if the client disconnects.
            reply.start_stream("update", name=name, pinned=decl.pinned)
            ok, _code, message = self._update(decl, reply)
            if ok:
                reply.end(True, "done")
            else:
                reply.end(False, "failed", message)
        return "ok" if ok else "failed"

    def _step(self, reply: Reply, step: str, **extra: Any) -> None:
        reply.event({"type": "step", "step": step, "at": iso_utc(self.sys.time()), **extra})

    def _update(self, decl: Declared, reply: Reply) -> tuple[bool, str, str]:
        """pulling -> recreating -> waiting-health -> done | failed. Returns (ok, error code, message)."""
        compose = ["compose", "--ansi", "never", "--progress", "plain", "-f", decl.compose_file]

        def emit(text: str) -> None:
            if text.strip():
                reply.event({"type": "output", "text": text})

        def failed(step: str, code: str, why: str) -> tuple[bool, str, str]:
            self._step(reply, "failed", failed_step=step, code=code, message=why)
            return False, code, f"{step}: {why}"

        step = "pulling"
        try:
            self._step(reply, "pulling", image=decl.pinned)
            rc, why = self.run_streamed([*compose, "pull", decl.service], self.cfg.pull_timeout, emit)
            if rc != 0:
                return failed(step, "pull-failed", why)
            step = "recreating"
            self._step(reply, step)
            rc, why = self.run_streamed(
                [*compose, "up", "--detach", "--no-deps", "--no-build", "--pull", "never", decl.service],
                self.cfg.up_timeout, emit)
            if rc != 0:
                return failed(step, "recreate-failed", why)
            step = "waiting-health"
            self._step(reply, step)
            ok, why, image, health = self._wait_health(decl.name)
            if not ok:
                return failed(step, "health-failed", why)
        except OpError as exc:
            return failed(step, exc.code, exc.message)
        self._step(reply, "done", image=image, health=health)
        return True, "", ""

    def _wait_health(self, name: str) -> tuple[bool, str, str | None, str | None]:
        deadline = self.sys.monotonic() + self.cfg.health_timeout
        running_since: float | None = None
        while True:
            c = self.inspect_name(name)
            now = self.sys.monotonic()
            if c is not None:
                st = c.get("State") or {}
                health = (st.get("Health") or {}).get("Status")
                image = (c.get("Config") or {}).get("Image")
                if st.get("Status") in ("exited", "dead"):
                    return False, f"container {st.get('Status')} (exit code {st.get('ExitCode')})", image, health
                if health == "healthy":
                    return True, "", image, health
                if health == "unhealthy":
                    return False, "health check failing", image, health
                if health is None and st.get("Running"):
                    running_since = running_since if running_since is not None else now
                    if now - running_since >= self.cfg.health_stable_seconds:
                        return True, "", image, None
                else:
                    running_since = None
            if now >= deadline:
                return False, f"not healthy after {self.cfg.health_timeout:g} s", None, None
            self.sys.sleep(self.cfg.health_poll_seconds)

    def op_last_pull(self, _req: dict[str, Any]) -> dict[str, Any]:
        user = self.cfg.backup_pull_user
        argv = [self.cfg.journalctl, "--output=json", "--no-pager", "--reverse", "--quiet",
                f"--since=-{int(self.cfg.last_pull_days)}d", "--lines=5000",
                "--output-fields=MESSAGE",
                f"--grep=^(Accepted \\S+ for|Disconnected from user) {user} ",
                "SYSLOG_IDENTIFIER=sshd-session", "SYSLOG_IDENTIFIER=sshd"]
        res = self.sys.run(argv, self.cfg.journal_timeout)
        # journalctl exits 1 when --grep matches nothing; that is "no pull yet", not an error.
        if res.rc not in (0, 1):
            raise OpError("failed", f"journalctl exited {res.rc}")
        return parse_last_pull(res.out.splitlines(), user)

    def op_boots(self, _req: dict[str, Any]) -> dict[str, Any]:
        res = self.sys.run([self.cfg.journalctl, "--list-boots", "--output=json", "--no-pager", "--quiet"],
                           self.cfg.journal_timeout)
        if res.rc != 0:
            raise OpError("failed", f"journalctl exited {res.rc}")
        return {"boots": parse_boots(res.out)}

    # ---- disk split

    def _media_ready(self) -> tuple[bool, str]:
        status = self.gate_status()
        if status != "ok":
            return False, f"gate status {status}"
        mount = self.cfg.media_mount
        if not self.sys.is_mount(mount):
            return False, f"{mount} is not mounted"
        if not os.path.exists(os.path.join(mount, self.cfg.media_sentinel)):
            return False, "media sentinel missing"
        return True, ""

    def _read_split_cache(self, path: Path) -> dict[str, Any] | None:
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("computed_epoch"), (int, float)):
            return None
        if not isinstance(data.get("sizes"), dict):
            return None
        return data

    @staticmethod
    def _split_reply(data: dict[str, Any], cached: bool, stale: bool, reason: str | None = None) -> dict[str, Any]:
        """{"sizes": {"tv": bytes|None, ...}, "computed_at", "cached", "stale", "reason"?}"""
        out = {"sizes": data["sizes"], "computed_at": iso_utc(data["computed_epoch"]), "cached": cached, "stale": stale}
        if reason:
            out["reason"] = reason
        return out

    def op_disk_split(self, _req: dict[str, Any]) -> dict[str, Any]:
        cache_dir = Path(self.cfg.cache_dir)
        cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        cache_file = cache_dir / "disk-split.json"
        cached = self._read_split_cache(cache_file)
        now = self.sys.time()
        if cached and 0 <= now - cached["computed_epoch"] < self.cfg.disk_split_ttl:
            return self._split_reply(cached, cached=True, stale=False)
        ready, why = self._media_ready()
        if not ready:
            if cached:
                return self._split_reply(cached, cached=True, stale=True, reason=why)
            raise OpError("media-unavailable", why)
        try:
            with self.lock("disk-split", "a disk-split computation"):
                sizes = self._du()
                data = {"sizes": sizes, "computed_epoch": int(now)}
                tmp = cache_dir / f".disk-split.{os.getpid()}.tmp"
                tmp.write_text(json.dumps(data), encoding="utf-8")
                os.replace(tmp, cache_file)
        except OpError as exc:
            if exc.code == "busy" and cached:
                return self._split_reply(cached, cached=True, stale=True, reason="computing")
            raise
        return self._split_reply(data, cached=False, stale=False)

    def _du(self) -> dict[str, int | None]:
        paths = {os.path.join(self.cfg.media_mount, d): d for d in self.cfg.media_dirs}
        argv = [self.cfg.ionice, "-c", "3", self.cfg.nice, "-n", "19", self.cfg.du,
                "--summarize", "--one-file-system", "--block-size=1", "--", *paths]
        res = self.sys.run(argv, self.cfg.du_timeout)
        sizes: dict[str, int | None] = dict.fromkeys(self.cfg.media_dirs)
        for line in res.out.splitlines():
            size, _, path = line.partition("\t")
            if path in paths and size.isdigit():
                sizes[paths[path]] = int(size)
        if all(v is None for v in sizes.values()):
            raise OpError("failed", f"du exited {res.rc} without a result")
        return sizes


# --------------------------------------------------------------------------- journal parsing


def parse_last_pull(lines: list[str], user: str) -> dict[str, Any]:
    """journalctl -o json --reverse lines (newest first) -> the last successful pull session:
    {"last_pull": its end (ISO 8601 UTC) or None, "started_at", "duration_seconds", "last_attempt"}.

    Successful = sshd logged "Accepted <method> for USER from ADDR port P" and later
    "Disconnected from user USER ADDR port P" (a clean, client-side close)."""
    closed: dict[tuple[str, str], int] = {}
    last_attempt: int | None = None
    for line in lines:
        try:
            entry = json.loads(line)
            msg = entry.get("MESSAGE")
            ts = int(entry["__REALTIME_TIMESTAMP"])
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if not isinstance(msg, str):
            continue
        m = DISCONNECTED_RE.match(msg)
        if m and m["user"] == user:
            closed.setdefault((m["addr"], m["port"]), ts)
            continue
        m = ACCEPTED_RE.match(msg)
        if m and m["user"] == user:
            if last_attempt is None:
                last_attempt = ts
            end = closed.get((m["addr"], m["port"]))
            if end is not None and end >= ts:
                return {"last_pull": iso_utc(end / 1e6), "started_at": iso_utc(ts / 1e6),
                        "duration_seconds": round((end - ts) / 1e6, 3),
                        "last_attempt": iso_utc(last_attempt / 1e6)}
    return {"last_pull": None, "started_at": None, "duration_seconds": None,
            "last_attempt": iso_utc(last_attempt / 1e6) if last_attempt is not None else None}


def parse_boots(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text) if text.strip() else []
    except ValueError as exc:
        raise OpError("failed", "journalctl --list-boots returned invalid JSON") from exc
    boots = []
    for b in data if isinstance(data, list) else []:
        try:
            boot_id = str(b["boot_id"])
            first, last = int(b["first_entry"]), int(b["last_entry"])
            index = int(b["index"])
        except (KeyError, TypeError, ValueError):
            continue
        if BOOT_ID_RE.match(boot_id):
            boots.append({"index": index, "boot_id": boot_id,
                          "first_entry": iso_utc(first / 1e6), "last_entry": iso_utc(last / 1e6)})
    return boots[-MAX_BOOTS:]


# --------------------------------------------------------------------------- entry point


def close_quietly(conn: socket.socket) -> None:
    """Discard input the client already sent (a refused or extra request) before closing, so the
    client reads the reply and a clean end of stream instead of ECONNRESET."""
    with contextlib.suppress(OSError):
        conn.setblocking(False)
        for _ in range(16):
            if not conn.recv(65536):
                break
    with contextlib.suppress(OSError):
        conn.shutdown(socket.SHUT_RDWR)
    conn.close()


def main() -> int:
    log = Log()
    try:
        conn = socket.socket(fileno=os.dup(0))  # Accept=yes: the accepted connection is stdin
    except OSError:
        log.error("stdin is not a socket; this program is started by homelab-dockerctl.socket")
        return 2
    try:
        if conn.family != socket.AF_UNIX:
            log.error("stdin is not a Unix socket")
            return 2
        try:
            cfg = load_config(os.environ.get(CONFIG_ENV, DEFAULT_CONFIG))
        except (OSError, ValueError, TypeError) as exc:
            log.error(f"cannot load the config: {clean_log(exc)}")
            Reply(conn).error("internal", "helper misconfigured")
            return 1
        Helper(cfg, System(), log).serve(conn)
    finally:
        close_quietly(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
