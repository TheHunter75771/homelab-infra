"""Shared test harness for aictl-helper: a temp host with stub docker/systemctl/journalctl executables
that record their argv, fake sudo I/O log trees, and real socketpairs for the client side.
No root, no Docker, no journal, no network. Every fixture is synthetic."""

from __future__ import annotations

import gzip
import importlib.util
import io
import json
import os
import pathlib
import pwd
import socket
import sys
import tempfile
import threading
import zlib

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "aictl_helper.py"

_SPEC = importlib.util.spec_from_file_location("aictl_helper", SCRIPT)
helper_mod = importlib.util.module_from_spec(_SPEC)
sys.modules["aictl_helper"] = helper_mod  # dataclasses resolve annotations through sys.modules
_SPEC.loader.exec_module(helper_mod)

ME = pwd.getpwuid(os.getuid()).pw_name

# One stub program, installed as docker, systemctl and journalctl. The helper runs children with a
# minimal environment, so the stub finds its scenario next to itself, not through env vars.
STUB = f"""#!{sys.executable} -I
import json, os, sys, time
here = os.path.dirname(os.path.abspath(sys.argv[0]))
name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
with open(os.path.join(here, "calls.jsonl"), "a") as f:
    f.write(json.dumps([name, *args]) + "\\n")
with open(os.path.join(here, "scenario.json")) as f:
    scenario = json.load(f)
def hit(rule):
    return all(any(a == m or a.startswith(m) for a in args) for m in rule.get("match", []))
rule = next((r for r in scenario.get(name, []) if hit(r)), None)
if rule is None:
    sys.stderr.write("stub: no rule for %r\\n" % (args,))
    sys.exit(99)
if rule.get("pidfile"):
    with open(os.path.join(here, rule["pidfile"]), "w") as f:
        f.write(str(os.getpid()))
if rule.get("child_pidfile"):  # a child in the same process group, as compose plugins would be
    child = os.fork()
    if child == 0:
        time.sleep(60)
        os._exit(0)
    with open(os.path.join(here, rule["child_pidfile"]), "w") as f:
        f.write(str(child))
if rule.get("sleep"):
    time.sleep(rule["sleep"])
sys.stdout.write(rule.get("stdout", ""))
sys.stdout.flush()
sys.stderr.write(rule.get("stderr", ""))
sys.exit(rule.get("rc", 0))
"""

CID = {"ollama-cpu": "a" * 64, "gpu-broker": "b" * 64, "open-webui": "c" * 64}


def inspect_obj(name: str, *, state: str = "running", health: str | None = "healthy", restarts: int = 0) -> dict:
    st = {"Status": state, "Running": state == "running", "StartedAt": "2026-10-26T06:00:00.123456789Z"}
    if health is not None:
        st["Health"] = {"Status": health, "Log": [{"Output": "probe said token=PROBE-SECRET"}]}
    return {
        "Id": CID.get(name, "d" * 64), "Name": f"/{name}", "RestartCount": restarts, "State": st,
        # Things the helper must never pass on:
        "Config": {"Image": f"example/{name}:1.0", "Env": ["API_KEY=do-not-leak"], "Labels": {}},
        "Mounts": [{"Source": "/srv/x", "Destination": "/data"}],
    }


def journal_line(ts_usec: int, message: str | list[int], **fields) -> str:
    return json.dumps({"__REALTIME_TIMESTAMP": str(ts_usec), "MESSAGE": message, **fields})


def default_scenario() -> dict:
    return {
        "systemctl": [
            {"match": ["is-active"], "stdout": "active\n"},
            {"match": ["list-timers"], "stdout": json.dumps([
                {"next": 1793055600000000, "left": 1, "last": 1793052054436295, "passed": 1,
                 "unit": "ai-models-pull.timer", "activates": "ai-models-pull.service"},
                {"next": 1793056627861407, "left": 1, "last": None, "passed": None,
                 "unit": "homelab-ai-backup.timer", "activates": "homelab-ai-backup.service"},
                {"next": 1793056627861407, "left": 1, "last": 1793045128410386, "passed": 1,
                 "unit": "fstrim.timer", "activates": "fstrim.service"},
                {"next": 0, "left": 0, "last": 1793045128410386, "passed": 1,
                 "unit": "ai-bad;name.timer", "activates": "x.service"},
            ])},
            {"match": ["show"], "stdout": "Id=ai-models-pull.service\nResult=exit-code\n\n"
                                          "Id=homelab-ai-backup.service\nResult=success\n"},
        ],
        "docker": [
            {"match": ["inspect"], "stdout": json.dumps([inspect_obj("ollama-cpu"),
                                                         inspect_obj("gpu-broker", health=None, restarts=2),
                                                         inspect_obj("open-webui", state="exited", health=None)])},
            {"match": ["logs"], "stdout": "2026-10-26T06:00:00.000000000Z \x1b[32mstarted\x1b[0m\n"
                                          "2026-10-26T06:00:01.000000000Z Authorization: Bearer abcdef0123456789\n",
             "stderr": "2026-10-26T06:00:02.000000000Z warn: api_key=k-123456\n"},
        ],
        "journalctl": [{"stdout": ""}],
    }


class Env:
    """A temp host: bin/ with stubs, an I/O log root, a config, and a Helper wired to the real System."""

    def __init__(self, *, user: str = ME, localai_user: str = ME, **cfg_overrides) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="aictl-helper-test-")
        self.root = pathlib.Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("docker", "systemctl", "journalctl"):
            (self.bin / name).write_text(STUB)
            os.chmod(self.bin / name, 0o755)
        self.iolog = self.root / "sudo-io"
        self.iolog.mkdir(mode=0o700)
        self.scenario = default_scenario()
        self.save_scenario()
        cfg = {
            "user": user,
            "localai_user": localai_user,
            "iolog_root": str(self.iolog),
            "docker": str(self.bin / "docker"),
            "systemctl": str(self.bin / "systemctl"),
            "journalctl": str(self.bin / "journalctl"),
            "request_timeout": 2,
            "send_timeout": 2,
            "command_timeout": 5,
        }
        cfg.update(cfg_overrides)
        self.cfg_raw = cfg
        self.cfg = helper_mod.Config.from_mapping(cfg)
        self.logbuf = io.StringIO()
        self.helper = helper_mod.Helper(self.cfg, helper_mod.System(), helper_mod.Log(self.logbuf))
        # The audited accounts don't exist on a test machine: synthetic uids, everyone else from pwd.
        self.uids = {"claude": 999, "localai": 998}
        real_uid_of = self.helper.sys.uid_of
        self.helper.sys.uid_of = lambda name: self.uids.get(name, real_uid_of(name))

    def close(self) -> None:
        self.tmp.cleanup()

    def save_scenario(self) -> None:
        (self.bin / "scenario.json").write_text(json.dumps(self.scenario))

    def set_rule(self, prog: str, rules: list[dict]) -> None:
        self.scenario[prog] = rules
        self.save_scenario()

    def docker_inactive(self, state: str = "inactive") -> None:
        rules = [r for r in self.scenario["systemctl"] if r.get("match") != ["is-active"]]
        self.set_rule("systemctl", [{"match": ["is-active"], "stdout": f"{state}\n", "rc": 3}, *rules])

    @property
    def calls(self) -> list[list[str]]:
        path = self.bin / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def calls_of(self, prog: str) -> list[list[str]]:
        return [c[1:] for c in self.calls if c[0] == prog]

    @property
    def log(self) -> str:
        return self.logbuf.getvalue()

    def call(self, request, *, raw: bytes | None = None) -> tuple[str, dict | None]:
        """Send one request over a real socketpair, serve it, return (outcome, the reply or None)."""
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        chunks: list[bytes] = []

        def read() -> None:  # concurrently, so a reply bigger than the socket buffer never stalls serve()
            while True:
                try:
                    chunk = client.recv(65536)
                except (ConnectionResetError, OSError):
                    return
                if not chunk:
                    return
                chunks.append(chunk)

        reader = threading.Thread(target=read, daemon=True)
        try:
            client.sendall(raw if raw is not None else (json.dumps(request) + "\n").encode())
            reader.start()
            outcome = self.helper.serve(server)
            server.close()
            reader.join(15)
        finally:
            client.close()
            server.close()
        data = b"".join(chunks)
        lines = data.decode().splitlines()
        assert len(lines) <= 1, f"more than one reply line: {lines!r}"
        return outcome, (json.loads(lines[0]) if lines else None)

    def ok(self, request) -> dict:
        outcome, reply = self.call(request)
        assert outcome == "ok" and reply and reply["ok"], (outcome, reply)
        assert set(reply) == {"ok", "op", "data"} and reply["op"] == request["op"]
        return reply["data"]

    def err(self, request, code: str | None = None) -> dict:
        outcome, reply = self.call(request)
        assert reply is not None and reply["ok"] is False, reply
        if code is not None:
            assert (outcome, reply["error"]["code"]) == (code, code), (outcome, reply)
        return reply["error"]


# --------------------------------------------------------------------------- sudo I/O log fixtures


def encode(data: bytes, mode: str) -> bytes:
    if mode == "gzip":
        return gzip.compress(data)
    if mode == "zlib":
        return zlib.compress(data)
    return data


def make_session(base: pathlib.Path, user: str, rel: str, *, epoch: int, command: list[str],
                 runas: str = "root", cwd: str = "/home/claude", tty: str = "/dev/pts/0",
                 streams: dict[str, bytes] | None = None, timing: list[tuple[int, int]] | None = None,
                 mode: str = "gzip", legacy: bool = False) -> pathlib.Path:
    """One session directory as sudo writes it: log.json (or the legacy log), timing and streams.
    timing: [(event type, byte count)] in write order; delays are written as 0.5 s and must be ignored."""
    d = base / user / rel
    d.mkdir(parents=True, exist_ok=True)
    if legacy:
        (d / "log").write_text(f"{epoch}:{user}:{runas}::{tty}:24:80\n{cwd}\n{' '.join(command)}")
    else:
        (d / "log.json").write_text(json.dumps({
            "timestamp": {"seconds": epoch, "nanoseconds": 5},
            "submituser": user, "runuser": runas, "ttyname": tty, "submitcwd": cwd,
            "command": command[0], "runargv": command, "submithost": "example-host",
            "runenv": ["PATH=/usr/bin", "OLLAMA_API_KEY=env-must-not-appear"], "lines": 24, "columns": 80,
        }))
    for name, data in (streams or {}).items():
        (d / name).write_bytes(encode(data, mode))
    if timing is not None:
        text = "".join(f"{kind} 0.500000 {n}\n" for kind, n in timing)
        (d / "timing").write_bytes(encode(text.encode(), mode))
    os.utime(d / ("log" if legacy else "log.json"), (epoch, epoch))
    return d
