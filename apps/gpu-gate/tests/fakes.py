"""Fake /sys and /proc trees, a fake clock and a stub Ollama, for tests."""

import json
import os
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PDEV = "0000:2d:00.0"
OLLAMA_UID = 961                                          # the uid the gate and Ollama share
OTHER_UID = 1000 if OLLAMA_UID != 1000 else 1001          # a desktop user's processes
# Gate(..., **AS_OLLAMA): run as a system uid under the default UID_MIN 1000 (no login.defs)
AS_OLLAMA = {"uid": OLLAMA_UID, "login_defs": "/nonexistent/login.defs"}


def fdinfo(client_id, gfx=0, compute=0, pdev=PDEV):
    """An amdgpu DRM fdinfo, in the format read from the real card (Fedora 44, kernel 7.2)."""
    return (
        "pos:\t0\nflags:\t02100002\nmnt_id:\t40\nino:\t750\n"
        "drm-driver:\tamdgpu\ndrm-client-id:\t%d\ndrm-pdev:\t%s\npasid:\t28\n"
        "drm-total-vram:\t74708 KiB\ndrm-memory-gtt: \t8208 KiB\n"
        "drm-engine-gfx:\t%d ns\ndrm-engine-compute:\t%d ns\n" % (client_id, pdev, gfx, compute)
    )


PLAIN_FDINFO = "pos:\t0\nflags:\t02\nmnt_id:\t25\nino:\t1234\n"


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Tree:
    """A tmp dir holding sys/ and proc/."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="gpu-gate-test-")
        self.sys = os.path.join(self.root, "sys")
        self.proc = os.path.join(self.root, "proc")
        os.makedirs(os.path.join(self.sys, "class", "drm"))
        os.makedirs(self.proc)

    def cleanup(self):
        for dirpath, dirnames, filenames in os.walk(self.root):  # undo chmod 000 fixtures
            for n in dirnames + filenames:
                p = os.path.join(dirpath, n)
                if not os.path.islink(p):
                    os.chmod(p, 0o700)
        shutil.rmtree(self.root)

    # --- /sys ---------------------------------------------------------------------------
    def card(self, name="card1", busy=None, pdev=PDEV):
        """cardN/device -> ../../devices/<pdev> like the real symlink; busy=None: no file."""
        dev = os.path.join(self.sys, "devices", pdev)
        os.makedirs(dev, exist_ok=True)
        link = os.path.join(self.sys, "class", "drm", name)
        os.makedirs(link, exist_ok=True)
        if not os.path.exists(os.path.join(link, "device")):
            os.symlink(dev, os.path.join(link, "device"))
        if busy is not None:
            self.set_busy(busy, pdev)
        return os.path.join(link, "device")

    def connector(self, name, pdev=PDEV):
        """card1-DP-1: a connector whose device also points at the GPU."""
        return self.card(name, None, pdev)

    def set_busy(self, busy, pdev=PDEV):
        with open(os.path.join(self.sys, "devices", pdev, "gpu_busy_percent"), "w") as f:
            f.write("%s\n" % busy)

    # --- /proc --------------------------------------------------------------------------
    def proc_(self, pid, comm, exe=None, argv=None, environ="denied", fds=None, ppid=1,
              uid=OTHER_UID, euid=None):
        """A /proc/<pid> with comm, cmdline and stat (world-readable on a real system).

        exe and environ are other users' private files on a real system, so by default they are
        made unreadable (environ "denied", exe a dangling link). The gate must never need them;
        tests also assert it never opens them. environ may be a dict for a readable one.
        """
        base = os.path.join(self.proc, str(pid))
        if os.path.exists(base):
            shutil.rmtree(base)
        os.makedirs(os.path.join(base, "fdinfo"))
        with open(os.path.join(base, "comm"), "w") as f:
            f.write(comm + "\n")
        with open(os.path.join(base, "cmdline"), "wb") as f:
            f.write(b"".join(a.encode() + b"\0" for a in (argv or [])))
        with open(os.path.join(base, "stat"), "w") as f:
            f.write("%d (%s) S %d %d %d 0 -1 4194304 1 0 0 0 0 0\n" % (pid, comm, ppid, pid, pid))
        with open(os.path.join(base, "status"), "w") as f:  # world-readable on a real system
            f.write("Name:\t%s\nState:\tS (sleeping)\nTgid:\t%d\nPid:\t%d\nPPid:\t%d\n"
                    "Uid:\t%d\t%d\t%d\t%d\nGid:\t%d\t%d\t%d\t%d\n"
                    % ((comm, pid, pid, ppid)
                       # Uid: real, effective, saved, fs; euid lets real != effective
                       + (uid, euid if euid is not None else uid) * 2 + (uid,) * 4))
        os.symlink(os.path.join(self.root, "nonexistent", exe or "exe"), os.path.join(base, "exe"))
        path = os.path.join(base, "environ")
        if environ == "denied":
            os.mkdir(path)  # open() fails with an OSError, as for an unreadable file
        elif environ is not None:
            with open(path, "wb") as f:
                f.write(b"".join(("%s=%s" % kv).encode() + b"\0" for kv in environ.items()))
        for fd, text in (fds or {}).items():
            self.set_fd(pid, fd, text)
        return base

    def set_fd(self, pid, fd, text):
        with open(os.path.join(self.proc, str(pid), "fdinfo", str(fd)), "w") as f:
            f.write(text)

    def kill(self, pid):
        shutil.rmtree(os.path.join(self.proc, str(pid)))


class StubOllama:
    """Records requests. `models` is what /api/ps reports; `fail` maps path -> HTTP status."""

    def __init__(self, models=()):
        self.models = list(models)
        self.fail = {}
        self.requests = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def _reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                stub.requests.append(("GET", self.path, None))
                if self.path in stub.fail:
                    return self._reply(stub.fail[self.path], {"error": "boom"})
                if self.path == "/api/ps":
                    return self._reply(200, {"models": [
                        {"name": m, "model": m, "size_vram": 1} for m in stub.models]})
                self._reply(404, {})

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                stub.requests.append(("POST", self.path, body))
                if self.path in stub.fail or body.get("model") in stub.fail:
                    return self._reply(500, {"error": "boom"})
                self._reply(200, {"model": body.get("model"), "done": True, "done_reason": "unload"})

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
