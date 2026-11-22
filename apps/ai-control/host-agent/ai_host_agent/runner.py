"""Runs systemctl/journalctl with a fixed argv: no shell, a fixed environment, a timeout and an
output cap. Callers build argv only from constants and validated, enum-mapped values."""

import os
import selectors
import shutil
import signal
import subprocess
import time

PROGRAMS = frozenset({"systemctl", "journalctl"})


class ExecError(Exception):
    """A short, fixed message safe to return to the client (no output, no paths)."""


class Result:
    def __init__(self, stdout, truncated):
        self.stdout = stdout
        self.truncated = truncated


def child_env(path):
    # Nothing inherited from the agent's environment.
    return {"PATH": path, "LC_ALL": "C.UTF-8", "SYSTEMD_COLORS": "0", "SYSTEMD_PAGER": "cat",
            "SYSTEMD_LESS": "", "SYSTEMD_URLIFY": "0"}


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # its own session: children die too
    except OSError:
        pass
    proc.wait()


def run(program, args, *, path, timeout, max_bytes):
    """Run `program` (one of PROGRAMS, found on `path`) with `args`. Returns a Result; if the
    output passes `max_bytes` the process is killed and the output kept up to the cap
    (`truncated`). Raises ExecError on not found, timeout or a non-zero exit."""
    if program not in PROGRAMS:
        raise ValueError("program not allowed: %r" % program)
    exe = shutil.which(program, path=path)
    if exe is None:
        raise ExecError("%s not found" % program)
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen([exe, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=child_env(path), cwd="/",
                            close_fds=True, start_new_session=True)
    buf = bytearray()
    truncated = False
    try:
        fd = proc.stdout.fileno()
        with selectors.DefaultSelector() as sel:
            sel.register(fd, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _kill(proc)
                    raise ExecError("%s timed out" % program)
                if not sel.select(remaining):
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > max_bytes:
                    del buf[max_bytes:]
                    truncated = True
                    _kill(proc)
                    break
        if not truncated:
            try:
                rc = proc.wait(max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                _kill(proc)
                raise ExecError("%s timed out" % program) from None
            if rc != 0:
                raise ExecError("%s failed (exit %d)" % (program, rc))
    finally:
        proc.stdout.close()
        if proc.returncode is None:
            _kill(proc)
    return Result(bytes(buf), truncated)
