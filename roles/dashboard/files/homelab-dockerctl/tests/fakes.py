"""Shared fakes for the homelab-dockerctl tests: a fake host (systemctl, docker, journalctl, du)
with a fake clock, real socketpairs for the client side, and real pipes for streamed output.
No root, no Docker, no network."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import pathlib
import shutil
import socket
import sys
import tempfile
import threading

HERE = pathlib.Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"

_SPEC = importlib.util.spec_from_file_location("homelab_dockerctl", HERE.parent / "homelab_dockerctl.py")
dockerctl = importlib.util.module_from_spec(_SPEC)
sys.modules["homelab_dockerctl"] = dockerctl  # dataclasses resolve annotations through sys.modules
_SPEC.loader.exec_module(dockerctl)

DOCKER = "/usr/bin/docker"
SYSTEMCTL = "/usr/bin/systemctl"
JOURNALCTL = "/usr/bin/journalctl"


def cid(n: int) -> str:
    return f"{n:064x}"


IMAGES = {
    "sonarr": "lscr.io/linuxserver/sonarr:4.0.17.2952-ls312@sha256:0b5c4803f92456fb9b65bae8375716ea120b4ea17b3cced7da32b63f0085782b",
    "sonarr-old": "lscr.io/linuxserver/sonarr:4.0.16.2944-ls311@sha256:" + "a" * 64,
    "cloudflared": "cloudflare/cloudflared:2026.5.0@sha256:59bab8d3aceec09bf6bdb07d6beca0225ca5cd7ab79436a87ea97978fe1dc4f9",
    "wol-relay": "homelab/wol-relay:2026.07",
}


def container(name: str, n: int, *, project: str, image: str, state: str = "running", health: str | None = "healthy",
              watch: bool = True, restarts: int = 0) -> dict:
    labels = {"com.docker.compose.project": project, "com.docker.compose.service": name}
    if watch:
        labels["homelab.watch"] = "true"
    st = {"Status": state, "Running": state == "running", "ExitCode": 0 if state == "running" else 1,
          "StartedAt": "2026-10-24T06:00:00.123456789Z"}
    if health is not None:
        st["Health"] = {"Status": health, "FailingStreak": 0, "Log": [{"Output": "secret-ish probe output"}]}
    return {
        "Id": cid(n),
        "Name": f"/{name}",
        "RestartCount": restarts,
        "State": st,
        # Things the helper must never pass on:
        "Config": {"Image": image, "Labels": labels, "Env": ["API_KEY=do-not-leak"], "Cmd": ["run"]},
        "Mounts": [{"Source": "/srv/homelab/data/x", "Destination": "/config"}],
        "HostConfig": {"Privileged": False},
    }


def default_containers() -> dict[str, dict]:
    return {
        "sonarr": container("sonarr", 1, project="media", image=IMAGES["sonarr-old"]),
        "jellyfin": container("jellyfin", 2, project="media",
                              image="lscr.io/linuxserver/jellyfin:10.11.9ubu2404-ls33@sha256:" + "b" * 64),
        "cloudflared": container("cloudflared", 3, project="edge", image=IMAGES["cloudflared"], health=None),
        "wol-relay": container("wol-relay", 4, project="utils", image=IMAGES["wol-relay"]),
        "stranger": container("stranger", 5, project="other", image="busybox:1", watch=False),
    }


class FakeProc:
    """A spawned process backed by a real pipe, so the helper's selectors see real fds."""

    _next_pid = 40000

    def __init__(self, argv: list[str], output: bytes = b"", rc: int = 0, hang: bool = False,
                 err_output: bytes | None = None) -> None:
        """err_output=None: stderr merged into stdout; bytes: a separate stderr pipe with that content."""
        self.argv = argv
        self._writers: list[int] = []
        self.stdout = self._pipe(output)
        self.stderr = self._pipe(err_output) if err_output is not None else None
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.returncode: int | None = None
        self.killed = False
        if not hang:
            self.exit(rc)

    def _pipe(self, data: bytes):
        r, w = os.pipe()
        if data:
            os.write(w, data)
        self._writers.append(w)
        return os.fdopen(r, "rb", buffering=0)

    def exit(self, rc: int) -> None:
        for w in self._writers:
            os.close(w)
        self._writers = []
        self.returncode = rc

    def poll(self) -> int | None:
        return self.returncode


class FakeSystem:
    """systemctl, docker, journalctl and du as a scripted host. Every argv is recorded in .calls."""

    def __init__(self, *, docker_state: str = "active", containers: dict | None = None) -> None:
        self.docker_state = docker_state
        self.containers = containers if containers is not None else default_containers()
        self.calls: list[list[str]] = []
        self.procs: list[FakeProc] = []
        self.clock = 1000.0
        self.tick = 0.0  # added to the monotonic clock on every read
        self.wall = 1_792_850_000.0  # 2026-10-24
        self.sleeps: list[float] = []
        self.mounted = True
        # Scripted behaviour.
        self.action_rc = 0
        self.logs_output = b""
        self.logs_err = b""
        self.logs_rc = 0
        self.logs_hang = False
        self.pull_output = b" sonarr Pulling \n \x1b[32m sonarr Pulled\x1b[0m \n"
        self.pull_rc = 0
        self.up_output = b" Container sonarr  Recreate\n Container sonarr  Started\n"
        self.up_rc = 0
        self.health_after_up: list[str | None] = ["starting", "healthy"]
        self.state_after_up = "running"
        self.journal_output = ""
        self.journal_rc = 0
        self.boots_output = "[]"
        self.du_output = ""
        self.du_rc = 0
        self.on_spawn = None  # callback(argv) run before a spawn returns
        self.pending_health: dict[str, list] = {}  # health statuses inspect returns after an "up"

    # ---- clock

    def monotonic(self) -> float:
        self.clock += self.tick
        return self.clock

    def time(self) -> float:
        return self.wall

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += seconds

    def is_mount(self, path: str) -> bool:
        return self.mounted

    # ---- processes

    def docker_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c and c[0] == DOCKER]

    def _inspect(self, refs: list[str]) -> dockerctl.Result:
        out = []
        for ref in refs:
            for name, c in self.containers.items():
                if ref in (name, c["Id"]) or (len(ref) >= 3 and c["Id"].startswith(ref)):
                    out.append(copy.deepcopy(c))
                    if name == ref and name in self.pending_health:
                        seq = self.pending_health[name]
                        health = seq.pop(0) if len(seq) > 1 else seq[0]
                        if health is None:
                            out[-1]["State"].pop("Health", None)
                        else:
                            out[-1]["State"]["Health"] = {"Status": health}
        return dockerctl.Result(0 if out else 1, json.dumps(out), "" if out else "Error: No such container")

    def run(self, argv: list[str], timeout: float) -> dockerctl.Result:
        self.calls.append(list(argv))
        prog = argv[0]
        if prog == SYSTEMCTL:
            state = self.docker_state
            return dockerctl.Result(0 if state == "active" else 3, f"{state}\n" if state else "", "")
        if prog == DOCKER:
            sub = argv[1]
            if sub == "ps":
                ids = [c["Id"] for c in self.containers.values()
                       if c["Config"]["Labels"].get("homelab.watch") == "true"]
                return dockerctl.Result(0, "".join(i + "\n" for i in ids), "")
            if sub == "inspect":
                return self._inspect(argv[4:])
            if sub in ("start", "stop", "restart"):
                if self.action_rc == 0:
                    for c in self.containers.values():
                        if c["Id"] == argv[2]:
                            c["State"]["Status"] = "exited" if sub == "stop" else "running"
                return dockerctl.Result(self.action_rc, "", "" if self.action_rc == 0 else "Error response from daemon: boom\nmore")
            raise AssertionError(f"unexpected docker command {argv}")
        if prog == JOURNALCTL:
            if "--list-boots" in argv:
                return dockerctl.Result(0, self.boots_output, "")
            return dockerctl.Result(self.journal_rc, self.journal_output, "")
        if prog.endswith("ionice"):
            return dockerctl.Result(self.du_rc, self.du_output, "")
        raise AssertionError(f"unexpected command {argv}")

    def spawn(self, argv: list[str], split_stderr: bool = False):
        self.calls.append(list(argv))
        if self.on_spawn:
            self.on_spawn(argv)
        if argv[0] != DOCKER:
            raise AssertionError(f"unexpected spawn {argv}")
        if argv[1] == "logs":
            proc = FakeProc(argv, self.logs_output, self.logs_rc, hang=self.logs_hang,
                            err_output=self.logs_err if split_stderr else None)
        elif argv[1] == "compose" and "pull" in argv:
            proc = FakeProc(argv, self.pull_output, self.pull_rc)
        elif argv[1] == "compose" and "up" in argv:
            proc = FakeProc(argv, self.up_output, self.up_rc)
            if self.up_rc == 0:
                service = argv[-1]
                c = self.containers[service]
                c["Id"] = cid(900 + len(self.procs))
                c["Config"]["Image"] = IMAGES.get(service, c["Config"]["Image"])
                c["State"]["Status"] = self.state_after_up
                c["State"]["Running"] = self.state_after_up == "running"
                self.pending_health = {service: list(self.health_after_up)}
        else:
            raise AssertionError(f"unexpected spawn {argv}")
        self.procs.append(proc)
        return proc

    def stop(self, proc: FakeProc, grace: float = 5.0) -> int:
        if proc.returncode is None:
            proc.killed = True
            proc.exit(-15)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        return proc.returncode

    def wait(self, proc: FakeProc, timeout: float):
        return proc.returncode


class Env:
    """A temp host layout (stacks, gate status, media mount, cache) plus a helper wired to fakes."""

    def __init__(self, *, allowed_uid: int | None = None, gate: str = "ok", **cfg_overrides) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dockerctl-test-")
        root = pathlib.Path(self.tmp.name)
        os.chmod(root, 0o755)
        self.root = root
        self.stacks = root / "stacks"
        shutil.copytree(FIXTURES / "stacks", self.stacks)
        for p in [self.stacks, *self.stacks.rglob("*")]:
            os.chmod(p, 0o755 if p.is_dir() else 0o644)
        self.media = root / "media"
        for d in ("tv", "movies", "downloads"):
            (self.media / d).mkdir(parents=True)
        (self.media / ".homelab-media-sentinel").write_text("")
        self.status = root / "bootgate-status"
        self.set_gate(gate)
        self.cache = root / "cache"
        cfg = {
            "allowed_uid": os.getuid() if allowed_uid is None else allowed_uid,
            "stacks_dir": str(self.stacks),
            "media_mount": str(self.media),
            "bootgate_status_file": str(self.status),
            "cache_dir": str(self.cache),
            "trusted_owner_uid": os.getuid(),
            "request_timeout": 2,
            "send_timeout": 2,
            "poll_seconds": 0.01,
            "health_poll_seconds": 2,
            "health_timeout": 60,
            "health_stable_seconds": 5,
        }
        cfg.update(cfg_overrides)
        self.cfg = dockerctl.Config.from_mapping(cfg)
        self.sys = FakeSystem()
        self.logbuf = io.StringIO()
        self.helper = dockerctl.Helper(self.cfg, self.sys, dockerctl.Log(self.logbuf))

    def set_gate(self, status: str) -> None:
        self.status.write_text(f"{status}\n")

    def close(self) -> None:
        self.tmp.cleanup()

    @property
    def log(self) -> str:
        return self.logbuf.getvalue()

    def call(self, request, *, raw: bytes | None = None) -> tuple[str, list[dict]]:
        """Send one request over a real socketpair, serve it, return (outcome, reply lines)."""
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.sendall(raw if raw is not None else (json.dumps(request) + "\n").encode())
            outcome = self.helper.serve(server)
            server.close()
            data = b""
            while True:
                try:
                    chunk = client.recv(65536)
                except ConnectionResetError:  # the server closed with our extra input unread
                    break
                if not chunk:
                    break
                data += chunk
        finally:
            client.close()
            server.close()
        return outcome, [json.loads(line) for line in data.decode().splitlines()]

    def call_threaded(self, request) -> tuple[socket.socket, threading.Thread, dict]:
        """Serve in a thread; the test drives the client end (e.g. closes it mid-stream)."""
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        result: dict = {}

        def run() -> None:
            try:
                result["outcome"] = self.helper.serve(server)
            finally:
                server.close()

        client.sendall((json.dumps(request) + "\n").encode())
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return client, thread, result


def read_lines(client: socket.socket, count: int, timeout: float = 5.0) -> list[dict]:
    client.settimeout(timeout)
    buf = b""
    while buf.count(b"\n") < count:
        chunk = client.recv(65536)
        if not chunk:
            break
        buf += chunk
    return [json.loads(line) for line in buf.decode().splitlines()[:count]]
