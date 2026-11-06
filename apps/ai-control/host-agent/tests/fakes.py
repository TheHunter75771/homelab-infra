"""Stub systemctl/journalctl on a fake PATH (they record their argv and env), a fake Ollama, a
token file and a running agent on 127.0.0.1:0. Every fixture is synthetic."""

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ai_host_agent.auth import BearerAuth
from ai_host_agent.config import validate
from ai_host_agent.server import Agent, AgentHTTPServer

TOKEN = "SYNTHETICtoken0123456789abcdefABCDEF-_x"  # synthetic, 39 chars

# The stub picks its fixture by program and a key from argv: <stubdir>/<key>.out|.rc|.sleep
STUB = r'''#!%(python)s
import json, os, sys, time
d = os.path.dirname(os.path.abspath(__file__))
name = os.path.basename(sys.argv[0])
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": sys.argv, "env": dict(os.environ)}) + "\n")
if name == "journalctl":
    key = "journalctl.sudo" if any(a.startswith("_EXE=") for a in sys.argv) else "journalctl.log"
else:
    key = "systemctl." + sys.argv[-1]
def opt(suffix, default=None):
    p = os.path.join(d, key + suffix)
    if not os.path.exists(p):
        return default
    with open(p, "rb") as f:
        return f.read()
if opt(".spawn"):  # a grandchild in the stub's process group; its pid goes to <key>.pid
    import subprocess
    g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(os.path.join(d, key + ".pid"), "w") as f:
        f.write(str(g.pid))
s = opt(".sleep")
if s:
    time.sleep(float(s))
sys.stdout.buffer.write(opt(".out", b""))
sys.stdout.flush()
sys.exit(int(opt(".rc", b"0")))
'''

SHOW_OLLAMA = (
    "LoadState=loaded\nActiveState=active\nSubState=running\n"
    "ActiveEnterTimestamp=@1790000000\nNRestarts=2\nMainPID=4242\n"
    "ControlGroup=/system.slice/ollama.service\nMemoryCurrent=999\n")
SHOW_GATE = (
    "LoadState=loaded\nActiveState=inactive\nSubState=dead\nActiveEnterTimestamp=\n"
    "NRestarts=0\nMainPID=0\nControlGroup=\nMemoryCurrent=[not set]\n")

TAGS = {"models": [
    {"name": "example-model:9b", "model": "example-model:9b", "modified_at": "2026-10-20T10:00:00Z",
     "size": 6000000000, "digest": "0" * 64,
     "details": {"family": "example", "parameter_size": "9B", "quantization_level": "Q4_K_M"}},
    {"name": "tiny-model:1b", "size": 800000000, "modified_at": "2026-10-21T10:00:00Z",
     "details": {"quantization_level": "Q8_0"}},
]}
PS = {"models": [
    {"name": "example-model:9b", "model": "example-model:9b", "size": 7000000000,
     "size_vram": 6500000000, "expires_at": "2026-10-26T12:05:00Z", "digest": "0" * 64,
     "details": {"family": "example"}},
]}


UIDS = {"claude": 962, "localai": 963}  # synthetic; tests patch sources.user_uid with this


def sudo_entry(ts, message, uid=962, exe="/usr/bin/sudo"):
    """A sudo journal line with the fields seen on the real gaming PC (Lead, 2026-10-26): the
    caller's uid, not 0. The agent asks for MESSAGE, _EXE and _UID (+ journald's __ fields)."""
    return json.dumps({"__REALTIME_TIMESTAMP": str(ts), "__CURSOR": "s=synthetic",
                       "_COMM": "sudo", "_EXE": exe, "_UID": str(uid),
                       "_CAP_EFFECTIVE": "1ffffffffff", "SYSLOG_IDENTIFIER": "sudo",
                       "_TRANSPORT": "syslog", "MESSAGE": message}) + "\n"


class FakeOllama:
    """Serves /api/tags and /api/ps from `self.routes` (path -> (status, bytes)); `delay`
    seconds before answering. Records the paths asked for."""

    def __init__(self, host="127.0.0.1"):
        fake = self
        self.routes = {"/api/tags": (200, json.dumps(TAGS).encode()),
                       "/api/ps": (200, json.dumps(PS).encode())}
        self.delay = 0
        self.paths = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                fake.paths.append(self.path)
                if fake.delay:
                    time.sleep(fake.delay)
                status, body = fake.routes.get(self.path, (404, b"{}"))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer((host, 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, args=(0.05,), daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Env:
    """A tmp dir with bin/ (stubs), cgroup/, mountinfo, a share dir and the token file."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="ai-host-agent-test-")
        self.bin = os.path.join(self.root, "bin")
        self.cgroup = os.path.join(self.root, "cgroup")
        self.share = os.path.join(self.root, "share")
        self.mountinfo = os.path.join(self.root, "mountinfo")
        self.token_file = os.path.join(self.root, "token")
        for d in (self.bin, self.cgroup, self.share):
            os.makedirs(d)
        for name in ("systemctl", "journalctl"):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(STUB % {"python": sys.executable})
            os.chmod(p, 0o755)
        self.write_token(TOKEN)
        self.set_mounts(["/"])
        self.fixture("systemctl.ollama.service.out", SHOW_OLLAMA)
        self.fixture("systemctl.gpu-gate.service.out", SHOW_GATE)
        mem = os.path.join(self.cgroup, "system.slice", "ollama.service")
        os.makedirs(mem)
        with open(os.path.join(mem, "memory.current"), "w") as f:
            f.write("123456\n")

    def write_token(self, text, mode=0o640):
        if os.path.lexists(self.token_file):
            os.remove(self.token_file)
        with open(self.token_file, "w") as f:
            f.write(text)
        os.chmod(self.token_file, mode)

    def fixture(self, name, content):
        with open(os.path.join(self.bin, name), "wb") as f:
            f.write(content.encode() if isinstance(content, str) else content)

    def set_mounts(self, points):
        with open(self.mountinfo, "w") as f:
            for i, p in enumerate(points):
                f.write("%d 1 0:%d / %s rw,relatime shared:1 - ext4 /dev/fake rw\n"
                        % (20 + i, i, p.replace(" ", "\\040")))

    def calls(self):
        p = os.path.join(self.bin, "calls.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(line) for line in f]

    def cfg(self, **over):
        raw = {"token_file": self.token_file, "exec_path": self.bin, "cgroup_root": self.cgroup,
               "mountinfo": self.mountinfo, "disk_paths": ["/", self.share]}
        raw.update(over)
        return validate(raw)

    def cleanup(self):
        shutil.rmtree(self.root)


class Running:
    """An agent serving on 127.0.0.1:0."""

    def __init__(self, cfg, token=TOKEN):
        self.agent = Agent(cfg, token, BearerAuth(token))
        self.srv = AgentHTTPServer("127.0.0.1", 0, self.agent)
        self.port = self.srv.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self.srv.serve_forever, args=(0.05,), daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def get(self, path, token=TOKEN, method="GET", headers=None, data=None):
        """-> (status, headers, raw body bytes)."""
        req = urllib.request.Request(self.base + path, method=method, data=data)
        if token is not None:
            req.add_header("Authorization", "Bearer " + token)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers, e.read()

    def json(self, path, **kw):
        code, _, body = self.get(path, **kw)
        return code, json.loads(body)

    def raw(self, data, timeout=5):
        """Send raw bytes, return everything the server sends back."""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        with s:
            s.sendall(data)
            out = b""
            while True:
                try:
                    chunk = s.recv(65536)
                except ConnectionResetError:
                    break
                if not chunk:
                    break
                out += chunk
        return out


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False
