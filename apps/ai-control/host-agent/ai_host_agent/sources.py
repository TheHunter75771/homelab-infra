"""The data behind each endpoint. Every value a client sends is validated against an enum or a
bound and mapped to a constant before it reaches an argv, a path or a URL."""

import datetime
import http.client
import json
import os
import pwd
import re
import socket
import threading
import time

from .runner import ExecError, run

# Client-facing ids -> units (CONTRACTS.md 12.15.3). Nothing else is ever queried.
UNITS = {"ollama": "ollama.service", "gpu-gate": "gpu-gate.service"}
SUDO_USERS = ("claude", "localai")
SHOW_PROPS = ("LoadState,ActiveState,SubState,ActiveEnterTimestamp,NRestarts,MainPID,"
              "ControlGroup,MemoryCurrent")
LOG_LINES_DEFAULT, LOG_LINES_MAX = 200, 1000
SUDO_LIMIT_DEFAULT, SUDO_LIMIT_MAX = 100, 500
SUDO_SCAN = 5000            # newest sudo journal entries examined per request
NAME_MAX = 256              # model names and other short strings
_CGROUP = re.compile(r"/(?:[A-Za-z0-9_.@:+\\-]+/)*[A-Za-z0-9_.@:+\\-]+")
_INT = re.compile(r"[0-9]{1,10}")
_INT_US = re.compile(r"[0-9]{1,17}")  # journald __REALTIME_TIMESTAMP, microseconds
_SUDO_LINE = re.compile(r"\s*([a-z_][a-z0-9_.-]{0,31}\$?) : (.*)", re.DOTALL)
U64_MAX = 2 ** 64 - 1


class BadRequest(ValueError):
    """400 with this (fixed, input-free) message."""


_RFC3339 = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)")


def epoch_of_rfc3339(value):
    """Ollama's `2026-10-26T12:05:00.123456789-04:00` -> integer Unix seconds, else None.
    Every timestamp the agent returns is integer Unix seconds (Lead, 2026-10-26)."""
    if not isinstance(value, str):
        return None
    m = _RFC3339.fullmatch(value)
    if not m:
        return None
    zone = "+00:00" if m.group(3) == "Z" else m.group(3)
    try:
        epoch = int(datetime.datetime.fromisoformat(m.group(1) + zone).timestamp())
    except (ValueError, OverflowError, OSError):  # fraction dropped: whole seconds
        return None
    return epoch if epoch > 0 else None  # Go's zero time (0001-01-01) means "none"


def _clean(value, red, limit=NAME_MAX):
    """A short host-provided string for a response: str only, no controls, the known secrets
    replaced (before the cap, so a cut can't leave part of one), capped."""
    if not isinstance(value, str):
        return None
    return red.known(re.sub(r"[\x00-\x1f\x7f-\x9f]", "", value))[:limit]


def _int_or_none(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _lines(stdout, truncated):
    lines = stdout.decode("utf-8", "replace").split("\n")
    if truncated:
        lines.pop()  # killed mid-line: drop the fragment (it may be half a secret)
    elif lines and lines[-1] == "":
        lines.pop()
    return lines


# --- query parameters ------------------------------------------------------------------------

def int_param(query, key, default, lo, hi):
    raw = query.get(key)
    if raw is None or raw == "":
        return default
    if not _INT.fullmatch(raw) or not lo <= int(raw) <= hi:
        raise BadRequest("%s must be an integer from %d to %d" % (key, lo, hi))
    return int(raw)


def enum_param(query, key, allowed):
    raw = query.get(key)
    if raw not in allowed:
        raise BadRequest("%s must be one of: %s" % (key, ", ".join(allowed)))
    return raw


SINCE_AHEAD_MAX = 86400     # seconds; a later `since` is refused (Lead, as X)


def since_param(query, now=time.time):
    """`since`: empty/absent, or integer Unix seconds (Lead, 2026-10-26), at most one day
    ahead of now. journalctl only ever gets `--since @<digits>`."""
    raw = query.get("since")
    if raw is None or raw == "":
        return None
    if not _INT.fullmatch(raw):
        raise BadRequest("since must be integer Unix seconds")
    if int(raw) > int(now()) + SINCE_AHEAD_MAX:
        raise BadRequest("since is more than a day in the future")
    return int(raw)


# --- /v1/services ----------------------------------------------------------------------------

def _since(value):
    if not value:
        return None
    if value.startswith("@") and _INT.fullmatch(value[1:]):
        return int(value[1:])
    return None  # not `@<seconds>` (a systemctl without --timestamp=unix): unknown


def _cgroup_memory(cfg, control_group, memory_current):
    """memory.current of the unit's cgroup (all its processes); MemoryCurrent as a fallback."""
    if control_group and _CGROUP.fullmatch(control_group) and \
            not {".", ".."} & set(control_group.split("/")):
        try:
            with open(os.path.join(cfg["cgroup_root"], control_group.lstrip("/"),
                                   "memory.current"), "rb") as f:
                raw = f.read(32).strip()
            if raw.isdigit():
                return int(raw)
        except OSError:
            pass
    if memory_current and memory_current.isdigit() and int(memory_current) != U64_MAX:
        return int(memory_current)
    return None


def _service(cfg, red, sid, unit):
    res = run("systemctl", ["show", "--timestamp=unix", "--property=" + SHOW_PROPS, "--", unit],
              path=cfg["exec_path"], timeout=cfg["exec_timeout"], max_bytes=65536)
    props = {}
    for line in _lines(res.stdout, res.truncated):
        k, sep, v = line.partition("=")
        if sep:
            props[k] = v
    pid = props.get("MainPID", "")
    restarts = props.get("NRestarts", "")
    return {
        "id": sid,
        "unit": unit,
        "load": _clean(props.get("LoadState"), red, 32),
        "active": _clean(props.get("ActiveState"), red, 32),
        "sub": _clean(props.get("SubState"), red, 32),
        "since": _since(props.get("ActiveEnterTimestamp")),
        "restarts": int(restarts) if _INT.fullmatch(restarts) else None,
        "main_pid": int(pid) if _INT.fullmatch(pid) and int(pid) > 0 else None,
        "memory_bytes": _cgroup_memory(cfg, props.get("ControlGroup"),
                                       props.get("MemoryCurrent")),
    }


def services(cfg, red):
    out = []
    for sid, unit in UNITS.items():
        try:
            out.append(_service(cfg, red, sid, unit))
        except ExecError as e:
            out.append({"id": sid, "unit": unit, "error": str(e)})
    return {"services": out}


# --- /v1/logs --------------------------------------------------------------------------------

def logs(cfg, redactor, query):
    sid = enum_param(query, "unit", tuple(UNITS))
    n = int_param(query, "lines", LOG_LINES_DEFAULT, 1, LOG_LINES_MAX)
    res = run("journalctl", ["-u", UNITS[sid], "-n", str(n), "-o", "short-iso", "--no-pager"],
              path=cfg["exec_path"], timeout=cfg["exec_timeout"], max_bytes=cfg["exec_max_bytes"])
    # as one stream, so a private key block spanning journal lines is redacted whole
    lines = redactor.lines(_lines(res.stdout, res.truncated))
    return {"unit": sid, "lines": lines[-n:], "truncated": res.truncated}


# --- /v1/sudo --------------------------------------------------------------------------------

def classify_sudo(notes):
    """The event of a sudo line from its free-text notes (the segments before TTY=/PWD=/...,
    never a field value). Lead, 2026-10-26."""
    text = " ; ".join(notes).lower()
    # auth_fail wins over refused (Lead, as X pins it): wrong passwords, then "not allowed",
    # is the more alarming of the two.
    if "incorrect password attempt" in text or "authentication failure" in text:
        return "auth_fail"
    if any(n in text for n in ("command not allowed", "a password is required",
                               "not in sudoers")):
        return "refused"
    return "command"


# sudo's own field names. In a cwd or command, `; KEY=` means the line can't be split reliably
# (a path or argument can imitate a field); so does a field name appearing twice.
SUDO_FIELDS = ("TTY", "PWD", "USER", "GROUP", "ENV", "COMMAND", "TSID")
_FIELD_INSIDE = re.compile(r";\s*(?:" + "|".join(SUDO_FIELDS) + r")=")
CONTINUED = "(command continued)"


def parse_sudo(message):
    """sudo's journal line `  user : [note ; ]TTY=… ; PWD=… ; USER=… ; COMMAND=…`
    -> (user, {"TTY", "PWD", "USER", "COMMAND"}, [notes], ambiguous) or None. COMMAND is last
    and may itself contain ' ; ', so it's split off first. pam_unix lines don't match (sudo
    logs its own line for the same event). `(command continued)` pieces are parsed elsewhere."""
    m = _SUDO_LINE.fullmatch(message)
    if not m:
        return None
    user, rest = m.groups()
    if rest.startswith(CONTINUED):
        return None
    fields = {}
    head, sep, command = rest.partition(" ; COMMAND=")
    if not sep:
        if rest.startswith("COMMAND="):
            head, command = "", rest[len("COMMAND="):]
        else:
            return None
    fields["COMMAND"] = command
    notes = []
    seen = []
    for part in head.split(" ; ") if head else ():
        k, sep, v = part.partition("=")
        if sep and re.fullmatch(r"[A-Z_]+", k):
            seen.append(k)
            if k in ("TTY", "PWD", "USER"):
                fields[k] = v
        else:
            notes.append(part)
    ambiguous = (len(seen) != len(set(seen))
                 or bool(_FIELD_INSIDE.search(fields.get("PWD", "")))
                 or bool(_FIELD_INSIDE.search(command)))
    return user, fields, notes, ambiguous


def user_uid(name):
    """The audited user's uid on this host, or None (unknown, or root)."""
    try:
        uid = pwd.getpwnam(name).pw_uid
    except KeyError:
        return None
    return uid or None


def _message(entry):
    msg = entry.get("MESSAGE")
    if isinstance(msg, list):  # journald's JSON for non-UTF-8 data: a list of byte values
        try:
            msg = bytes(msg).decode("utf-8", "replace")
        except (TypeError, ValueError):
            return None
    return msg if isinstance(msg, str) else None


def sudo(cfg, redactor, query):
    user = enum_param(query, "user", SUDO_USERS)
    since = since_param(query)
    limit = int_param(query, "limit", SUDO_LIMIT_DEFAULT, 1, SUDO_LIMIT_MAX)
    uid = user_uid(user)
    if uid is None:
        raise ExecError("user not found on this host")
    # _EXE=<sudo...> AND _UID=<the audited user> (Lead, after Reviewer A round 2). _EXE alone
    # can be faked through an unprivileged user namespace (a bind-mounted "sudo"), but _UID is
    # the sender's real uid, set by journald: another user's forgeries or floods never match.
    # Both are checked again below, and the line must name the audited user. Accepted residual
    # risk: a user forging rows about itself (the sudo I/O session list stays authoritative).
    exes = cfg["sudo_exes"]
    args = ["_EXE=" + exe for exe in exes] + ["_UID=%d" % uid]
    args += ["-o", "json", "--output-fields=MESSAGE,_EXE,_UID", "-r", "-n", str(SUDO_SCAN),
             "--no-pager"]
    if since is not None:
        args += ["--since", "@%d" % since]
    res = run("journalctl", args, path=cfg["exec_path"], timeout=cfg["exec_timeout"],
              max_bytes=cfg["exec_max_bytes"])
    raw_lines = _lines(res.stdout, res.truncated)
    entries = []
    continued = False  # newest first: a command's continuation pieces come before its start
    for raw in raw_lines:
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("_EXE") not in exes \
                or entry.get("_UID") != str(uid):
            continue
        msg = _message(entry)
        m = _SUDO_LINE.fullmatch(msg) if msg else None
        if not m or m.group(1) != user:
            continue  # the line must name the audited user itself
        ts = entry.get("__REALTIME_TIMESTAMP")
        row = {"ts": int(ts) // 1000000 if isinstance(ts, str) and _INT_US.fullmatch(ts) else None,
               "user": user}
        if m.group(2).startswith(CONTINUED):  # kept as its own, flagged row
            continued = True
            row.update(runas=None, cwd=None,
                       command=redactor.line(m.group(2)[len(CONTINUED):].lstrip()),
                       event="command", ambiguous=True)
        else:
            parsed = parse_sudo(msg)
            if not parsed:
                continue
            _, f, notes, ambiguous = parsed
            row.update(runas=_clean(f.get("USER"), redactor, 64),
                       cwd=redactor.line(f["PWD"]) if "PWD" in f else None,
                       command=redactor.line(f["COMMAND"]), event=classify_sudo(notes),
                       ambiguous=ambiguous or continued)
            continued = False
        entries.append(row)
        if len(entries) >= limit:
            break
    scanned_all = len(raw_lines) >= SUDO_SCAN
    return {"user": user, "entries": entries,
            "truncated": len(entries) < limit and (res.truncated or scanned_all)}


# --- /v1/ollama ------------------------------------------------------------------------------

class _OllamaError(Exception):
    pass


def _ollama_get(cfg, path):
    """GET 127.0.0.1:11434<path> -> parsed JSON. http.client, not urllib: no proxy variables,
    no redirects. The whole call (connect, headers, body) has one deadline."""
    timeout = cfg["ollama_timeout"]
    deadline = time.monotonic() + timeout
    conn = http.client.HTTPConnection(cfg["ollama_host"], cfg["ollama_port"], timeout=timeout)
    try:
        conn.request("GET", path, headers={"Accept": "application/json"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise _OllamaError("http %d" % resp.status)
        cap = cfg["ollama_max_bytes"]
        body = bytearray()
        while True:
            if time.monotonic() > deadline:
                raise _OllamaError("timeout")
            chunk = resp.read1(65536)
            if not chunk:
                break
            body += chunk
            if len(body) > cap:
                raise _OllamaError("too large")
    except socket.timeout:
        raise _OllamaError("timeout") from None
    except (OSError, http.client.HTTPException):
        raise _OllamaError("unreachable") from None
    finally:
        conn.close()
    try:
        data = json.loads(body)
    except ValueError:
        raise _OllamaError("bad response") from None
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise _OllamaError("bad response")
    return [m for m in data["models"] if isinstance(m, dict)]


def _tags(cfg, red):
    out = []
    for m in _ollama_get(cfg, "/api/tags"):
        details = m.get("details") if isinstance(m.get("details"), dict) else {}
        out.append({"name": _clean(m.get("name"), red), "size": _int_or_none(m.get("size")),
                    "quant": _clean(details.get("quantization_level"), red, 32),
                    "modified": epoch_of_rfc3339(m.get("modified_at"))})
    return out


def _ps(cfg, red):
    return [{"name": _clean(m.get("name"), red), "size_vram": _int_or_none(m.get("size_vram")),
             "expires_at": epoch_of_rfc3339(m.get("expires_at"))}
            for m in _ollama_get(cfg, "/api/ps")]


def ollama(cfg, red):
    out = {}
    for key, fn in (("models", _tags), ("loaded", _ps)):
        try:
            out[key] = fn(cfg, red)
        except _OllamaError as e:
            out[key] = {"error": str(e)}
    return out


# --- /v1/disk --------------------------------------------------------------------------------

def mount_points(mountinfo):
    """Mount points from /proc/self/mountinfo (field 5, octal escapes decoded). Reading this
    never touches the mounted filesystems, so a dead network mount can't hang it."""
    points = set()
    try:
        with open(mountinfo, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split(" ")
                if len(parts) > 4:
                    points.add(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)),
                                      parts[4]))
    except OSError:
        pass
    return points


class StatFS:
    """statvfs in a worker thread with a timeout. A hung call keeps its path marked busy (and
    its one thread) until the kernel returns; no second thread is started for that path."""

    def __init__(self, timeout, statvfs=os.statvfs):
        self.timeout = timeout
        self._statvfs = statvfs
        self._busy = set()
        self._lock = threading.Lock()

    def __call__(self, path):
        with self._lock:
            if path in self._busy:
                raise TimeoutError
            self._busy.add(path)
        result = {}

        def work():
            try:
                result["st"] = self._statvfs(path)
            except OSError as e:
                result["err"] = e
            finally:
                with self._lock:
                    self._busy.discard(path)

        t = threading.Thread(target=work, name="statfs", daemon=True)
        t.start()
        t.join(self.timeout)
        if t.is_alive():
            raise TimeoutError
        if "err" in result:
            raise result["err"]
        return result["st"]


def disk(cfg, statfs):
    mounted = mount_points(cfg["mountinfo"])
    out = []
    for path in cfg["disk_paths"]:
        if path != "/" and path not in mounted:
            continue  # "if mounted": an unmounted /mnt/share would report the root fs
        try:
            st = statfs(path)
        except TimeoutError:
            out.append({"mount": path, "error": "statfs timed out"})
            continue
        except OSError:
            out.append({"mount": path, "error": "statfs failed"})
            continue
        out.append({"mount": path, "total": st.f_blocks * st.f_frsize,
                    "avail": st.f_bavail * st.f_frsize})
    return {"disk": out}
