#!/usr/bin/python3
"""bootgate: hold Docker (and every container) until the media drive is verified.

Commands
  check --boot    boot-time gate, run by bootgate.service (docker.service Requires it)
  check --retry   periodic / manual re-run, run by bootgate-retry.service; never reboots
  watch           drop-out watcher, run by bootgate-watch.service every 60 s
  alert           push the failure alert, run by bootgate-alert.service (OnFailure)
  status          print the current state for a human

  --dry-run       log every action that would change something (state files, mounts,
                  USB re-binds, power-cycles, docker) instead of doing it.

Verified means: findmnt reports the expected UUID at the mount point, the sentinel file
exists, and a write + fsync + delete test in the mount point succeeds.

Configuration comes from /etc/default/bootgate (KEY=VALUE, written by Ansible). Environment
variables of the same name override it. BOOTGATE_ROOT prefixes every host path (state, /run,
/dev, /sys, the mount point, the config file); it exists for the test-suite and drills only.

State lives in /var/lib/bootgate: status (ok|waiting|failed|media-lost), attempts (power-cycles
spent on the current incident), usb-port (last USB port the drive was verified on),
history.jsonl (one JSON event per line) and media-stopped (the watcher stopped the media stack).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import UTC, datetime

STATUSES = ("ok", "waiting", "failed", "media-lost")
EVENTS = ("pass", "repair", "powercycle", "fail", "retry-pass", "media-lost")
USB_DEVICE_RE = re.compile(r"^\d+-\d+(\.\d+)*$")
UUID_RE = re.compile(r"^[0-9A-Fa-f-]{8,36}$")
NOTIFY_TAG = "bootgate"
# Push titles (all on tag "bootgate"). The dashboard counts a title as a recovery only if it ENDS
# with a recovery phrase and holds no failure qualifier ("not", "fail...", "n't", ...), so only the
# plain TITLE_BACK resolves the incident; the two qualified "back" titles keep it open.
TITLE_FAILED = "Media drive failed"
TITLE_LOST = "Media drive lost"
TITLE_BACK = "Media drive back"
TITLE_BACK_NO_DOCKER = "Media drive back, Docker did not start"
TITLE_BACK_NO_MEDIA = "Media drive back, media apps did not restart"
HISTORY_MAX_BYTES = 1_000_000
HISTORY_KEEP_LINES = 2000
# After SIGKILL, how long to wait for a child before abandoning it. A process stuck in
# uninterruptible I/O on a hung USB device ignores SIGKILL; the gate must never wait for it.
KILL_GRACE_SECONDS = 5.0
PROBE_RC_SENTINEL_MISSING = 3

DEFAULTS = {
    "BOOTGATE_UUID": "",
    "BOOTGATE_MOUNT": "/media",
    "BOOTGATE_MOUNT_UNIT": "media.mount",
    "BOOTGATE_SENTINEL": ".homelab-media-sentinel",
    "BOOTGATE_WAIT_SECONDS": "720",
    "BOOTGATE_RETRY_WAIT_SECONDS": "30",
    "BOOTGATE_UDEV_WAIT_SECONDS": "15",
    "BOOTGATE_REBIND_WAIT_SECONDS": "90",
    "BOOTGATE_POLL_SECONDS": "2",
    "BOOTGATE_USB_PAUSE_SECONDS": "3",
    "BOOTGATE_WATCH_CONFIRM_SECONDS": "5",
    "BOOTGATE_WRITE_TEST_TIMEOUT": "30",
    "BOOTGATE_FSCK_TIMEOUT": "1800",
    "BOOTGATE_POWERCYCLE_METHOD": "reboot",
    "BOOTGATE_RTCWAKE_SECONDS": "60",
    "BOOTGATE_POWERCYCLE_GRACE_SECONDS": "300",
    "BOOTGATE_RETRY_MINUTES": "5",
    "BOOTGATE_MEDIA_COMPOSE": "/srv/homelab/stacks/media/compose.yaml",
    "BOOTGATE_NOTIFY": "/usr/local/bin/homelab-notify",
    "BOOTGATE_ALERT_TRIES": "5",
    "BOOTGATE_ALERT_RETRY_SECONDS": "30",
}


# --------------------------------------------------------------------------- logging
class Log:
    """Log to stdout. Under systemd (JOURNAL_STREAM set) prefix sd-daemon priorities."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.journal = bool(os.environ.get("JOURNAL_STREAM"))
        self.lines: list[str] = []

    def _emit(self, prio: int, tag: str, msg: str) -> None:
        self.lines.append(f"{tag}: {msg}")
        prefix = f"<{prio}>" if self.journal else f"{tag}: "
        print(prefix + msg, file=self.stream, flush=True)

    def info(self, msg: str) -> None:
        self._emit(6, "info", msg)

    def warn(self, msg: str) -> None:
        self._emit(4, "warn", msg)

    def error(self, msg: str) -> None:
        self._emit(3, "error", msg)


# --------------------------------------------------------------------------- config
class Config:
    def __init__(self, env: dict | None = None):
        env = dict(os.environ if env is None else env)
        self.root = env.get("BOOTGATE_ROOT", "").rstrip("/")
        values = dict(DEFAULTS)
        values.update(_read_kv(self.host_path(env.get("BOOTGATE_CONFIG", "/etc/default/bootgate"))))
        values.update({k: v for k, v in env.items() if k in DEFAULTS})
        self.uuid = values["BOOTGATE_UUID"].strip().lower()
        self.mount = values["BOOTGATE_MOUNT"]
        self.mount_unit = values["BOOTGATE_MOUNT_UNIT"]
        self.sentinel = values["BOOTGATE_SENTINEL"]
        self.wait_seconds = float(values["BOOTGATE_WAIT_SECONDS"])
        self.retry_wait_seconds = float(values["BOOTGATE_RETRY_WAIT_SECONDS"])
        self.udev_wait_seconds = float(values["BOOTGATE_UDEV_WAIT_SECONDS"])
        self.rebind_wait_seconds = float(values["BOOTGATE_REBIND_WAIT_SECONDS"])
        self.poll_seconds = max(0.01, float(values["BOOTGATE_POLL_SECONDS"]))
        self.usb_pause_seconds = float(values["BOOTGATE_USB_PAUSE_SECONDS"])
        self.watch_confirm_seconds = float(values["BOOTGATE_WATCH_CONFIRM_SECONDS"])
        self.write_test_timeout = float(values["BOOTGATE_WRITE_TEST_TIMEOUT"])
        self.fsck_timeout = float(values["BOOTGATE_FSCK_TIMEOUT"])
        self.powercycle_method = values["BOOTGATE_POWERCYCLE_METHOD"].strip()
        self.rtcwake_seconds = int(values["BOOTGATE_RTCWAKE_SECONDS"])
        self.powercycle_grace_seconds = float(values["BOOTGATE_POWERCYCLE_GRACE_SECONDS"])
        self.retry_minutes = values["BOOTGATE_RETRY_MINUTES"]
        self.media_compose = values["BOOTGATE_MEDIA_COMPOSE"]
        self.notify_cmd = values["BOOTGATE_NOTIFY"]
        self.alert_tries = max(1, int(values["BOOTGATE_ALERT_TRIES"]))
        self.alert_retry_seconds = float(values["BOOTGATE_ALERT_RETRY_SECONDS"])

    def host_path(self, path: str) -> str:
        """Map an absolute host path into BOOTGATE_ROOT (identity in production)."""
        return self.root + path if self.root else path

    @property
    def state_dir(self) -> str:
        return self.host_path("/var/lib/bootgate")

    @property
    def run_dir(self) -> str:
        return self.host_path("/run/bootgate")

    @property
    def dev_path(self) -> str:
        return self.host_path(f"/dev/disk/by-uuid/{self.uuid}")

    @property
    def sysfs(self) -> str:
        return self.host_path("/sys")

    @property
    def mount_path(self) -> str:
        return self.host_path(self.mount)

    def problems(self) -> list[str]:
        out = []
        if not UUID_RE.match(self.uuid):
            out.append("BOOTGATE_UUID is missing or malformed")
        if not self.mount.startswith("/") or self.mount == "/":
            out.append("BOOTGATE_MOUNT must be an absolute path other than /")
        if self.powercycle_method not in ("reboot", "rtcwake"):
            out.append("BOOTGATE_POWERCYCLE_METHOD must be reboot or rtcwake")
        return out


def _read_kv(path: str) -> dict:
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                    val = val[1:-1]
                values[key.strip()] = val
    except FileNotFoundError:
        pass
    return values


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- state store
class Store:
    """Files under /var/lib/bootgate. Every write is atomic and fsynced (the attempts file is
    what stops a boot loop, so it has to survive the power-cycle that follows it)."""

    def __init__(self, cfg: Config, log: Log, dry_run: bool = False):
        self.cfg = cfg
        self.log = log
        self.dry_run = dry_run
        self.dir = cfg.state_dir

    def _path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def _read(self, name: str) -> str | None:
        try:
            with open(self._path(name), encoding="utf-8") as fh:
                return fh.read().strip()
        except FileNotFoundError:
            return None

    def _write(self, name: str, content: str) -> None:
        if self.dry_run:
            self.log.info(f"DRY-RUN would write {name}={content.strip()!r}")
            return
        os.makedirs(self.dir, mode=0o755, exist_ok=True)
        path = self._path(name)
        tmp = f"{path}.tmp.{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        _fsync_dir(self.dir)

    # status ---------------------------------------------------------------
    def status(self) -> str | None:
        return self._read("status")

    def set_status(self, status: str) -> None:
        assert status in STATUSES, status
        self._write("status", status + "\n")

    # attempts -------------------------------------------------------------
    def attempts(self) -> int:
        """Missing file = 0 (nothing spent yet). Unreadable or garbage = 1, which forbids a
        power-cycle: when in doubt, never reboot."""
        raw = self._read("attempts")
        if raw is None:
            return 0
        try:
            return max(0, int(raw))
        except ValueError:
            self.log.warn(f"attempts file holds {raw!r}; treating it as 1 (no power-cycle)")
            return 1

    def set_attempts(self, value: int) -> int | None:
        """Write and read back. Returns the value on disk, or None if it could not be stored."""
        try:
            self._write("attempts", f"{value}\n")
        except OSError as exc:
            self.log.error(f"could not write attempts: {exc}")
            return None
        if self.dry_run:
            return value
        try:
            return int(self._read("attempts") or "")
        except (OSError, ValueError):
            return None

    # usb port -------------------------------------------------------------
    def usb_port(self) -> str | None:
        raw = self._read("usb-port")
        return raw if raw and USB_DEVICE_RE.match(raw) else None

    def set_usb_port(self, port: str) -> None:
        self._write("usb-port", port + "\n")

    # media-stopped marker ---------------------------------------------------
    def media_stopped(self) -> bool:
        return os.path.exists(self._path("media-stopped"))

    def set_media_stopped(self, on: bool) -> None:
        if on:
            self._write("media-stopped", utc_now_iso() + "\n")
        elif self.media_stopped():
            if self.dry_run:
                self.log.info("DRY-RUN would remove media-stopped")
                return
            os.unlink(self._path("media-stopped"))

    # history --------------------------------------------------------------
    def history(self, event: str, detail: str, wait_seconds: float, boot_id: str) -> dict:
        assert event in EVENTS, event
        entry = {
            "ts": utc_now_iso(),
            "boot_id": boot_id,
            "event": event,
            "detail": detail,
            "wait_seconds": round(float(wait_seconds), 1),
        }
        line = json.dumps(entry) + "\n"
        if self.dry_run:
            self.log.info(f"DRY-RUN would append history: {line.strip()}")
            return entry
        os.makedirs(self.dir, mode=0o755, exist_ok=True)
        path = self._path("history.jsonl")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        self._trim_history(path)
        return entry

    def _trim_history(self, path: str) -> None:
        try:
            if os.path.getsize(path) <= HISTORY_MAX_BYTES:
                return
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()[-HISTORY_KEEP_LINES:]
            self._write("history.jsonl", "".join(lines))
        except OSError as exc:
            self.log.warn(f"could not trim history: {exc}")

    def last_events(self, n: int = 10) -> list[dict]:
        try:
            with open(self._path("history.jsonl"), encoding="utf-8") as fh:
                lines = fh.readlines()[-n:]
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            with contextlib.suppress(ValueError):
                out.append(json.loads(line))
        return out

    # lock -----------------------------------------------------------------
    @contextlib.contextmanager
    def lock(self, blocking: bool):
        """Serialise gate runs. Yields True when held, False when busy (non-blocking)."""
        if self.dry_run:
            yield True
            return
        os.makedirs(self.dir, mode=0o755, exist_ok=True)
        fd = os.open(self._path(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            os.close(fd)


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- the host
class PowerCycleRequested(Exception):
    """Raised by test hosts to model 'the machine went down here'."""


class Host:
    """Everything that touches the real machine. Gate logic only talks to this interface, so
    tests can swap in a fake. Mutating calls honour --dry-run."""

    def __init__(self, cfg: Config, log: Log, dry_run: bool = False):
        self.cfg = cfg
        self.log = log
        self.dry_run = dry_run
        self.last_abandoned_pid: int | None = None

    # plumbing -------------------------------------------------------------
    def _run(self, argv: list[str], timeout: float = 120, mutate: bool = False) -> tuple[int, str]:
        """Run argv (never a shell) with a hard bound on how long the gate can be held.

        Output goes to an unlinked temp file, not a pipe, so there is no reader to block on.
        On timeout the child is SIGKILLed and given KILL_GRACE_SECONDS to die; a child that is
        stuck in uninterruptible I/O (a hung USB disk) ignores SIGKILL, so it is abandoned and
        the call fails. subprocess.run() cannot do this: after kill() it waits without a bound.
        """
        if mutate and self.dry_run:
            self.log.info("DRY-RUN would run: " + " ".join(argv))
            return 0, ""
        self.last_abandoned_pid = None
        name = os.path.basename(argv[0])
        with tempfile.TemporaryFile() as out:
            try:
                proc = subprocess.Popen(  # noqa: S603 - argv lists only, never a shell
                    argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT
                )
            except FileNotFoundError:
                return 127, f"{name}: not found"
            except OSError as exc:
                return 126, f"{name}: {exc.strerror or exc}"
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    self.last_abandoned_pid = proc.pid
                    self.log.error(
                        f"{name} (pid {proc.pid}) ignored SIGKILL: stuck in uninterruptible I/O; abandoning it"
                    )
                    return 124, f"{name}: hung for {timeout:.0f}s and could not be killed (abandoned pid {proc.pid})"
                return 124, f"{name}: timed out after {timeout:.0f}s (killed)"
            out.seek(0)
            text = out.read().decode("utf-8", "replace")
        return rc, text.strip()

    def _self(self, *args: str) -> list[str]:
        """argv to run one of this script's internal helpers in a child process."""
        return [sys.executable, os.path.abspath(__file__), *args]

    def _write_sysfs(self, path: str, value: str) -> str | None:
        """sysfs writes (USB unbind/bind, port disable) can block in the kernel while a hung
        device is torn down, so they run in a bounded child too."""
        if self.dry_run:
            self.log.info(f"DRY-RUN would write {value!r} to {path}")
            return None
        rc, out = self._run(self._self("_write-sysfs", path, value), timeout=30)
        return None if rc == 0 else (out or f"{path}: rc={rc}")

    # time -----------------------------------------------------------------
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def boot_id(self) -> str:
        try:
            with open(self.cfg.host_path("/proc/sys/kernel/random/boot_id"), encoding="ascii") as fh:
                return fh.read().strip()
        except OSError:
            return "unknown"

    def booting(self) -> bool:
        """True only while systemd is still bringing the system up. Power-cycles are allowed
        only then, so an Ansible run, a docker restart or a socket activation can never
        reboot the host."""
        _, out = self._run(["systemctl", "is-system-running"], timeout=15)
        return out.strip().splitlines()[-1:] in (["initializing"], ["starting"])

    # the drive ------------------------------------------------------------
    def device_present(self) -> bool:
        return os.path.exists(self.cfg.dev_path)

    def device_majmin(self) -> str | None:
        """'major:minor' of the block device behind /dev/disk/by-uuid/<uuid>, if there is one."""
        try:
            st = os.stat(self.cfg.dev_path)
        except OSError:
            return None
        if not stat.S_ISBLK(st.st_mode):
            return None
        return f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"

    def mount_info(self) -> dict | None:
        """Top-most mount at the mount point as {'UUID', 'SOURCE', 'MAJ:MIN'}, or None."""
        rc, out = self._run(
            ["findmnt", "-n", "-P", "-o", "UUID,SOURCE,MAJ:MIN", "--mountpoint", self.cfg.mount], timeout=15
        )
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if rc != 0 or not lines:
            return None
        return dict(re.findall(r'([\w:]+)="([^"]*)"', lines[-1]))

    def start_mount(self) -> tuple[bool, str]:
        rc, out = self._run(["systemctl", "start", "--no-ask-password", self.cfg.mount_unit], timeout=120, mutate=True)
        return rc == 0, out or f"systemctl start {self.cfg.mount_unit}: rc={rc}"

    def stop_mount(self) -> str:
        rc, out = self._run(["systemctl", "stop", "--no-ask-password", self.cfg.mount_unit], timeout=60, mutate=True)
        if rc == 0 and (self.dry_run or self.mount_info() is None):
            return f"stopped {self.cfg.mount_unit}"
        rc2, out2 = self._run(["umount", "-l", self.cfg.mount], timeout=30, mutate=True)
        return f"systemctl stop rc={rc} ({out}); umount -l rc={rc2} ({out2})"

    def probe_drive(self) -> tuple[bool, str]:
        """Sentinel stat, then write + fsync + delete, all in ONE child process: the gate itself
        never touches the drive's filesystem, so a hung USB device can only hang the child.
        While an abandoned probe is still stuck, no new one is started (they would pile up)."""
        stuck = self._stuck_probe()
        if stuck:
            return False, f"an earlier probe of the drive (pid {stuck}) is still stuck in I/O; not starting another"
        argv = self._self("_probe", self.cfg.mount_path, self.cfg.sentinel)
        if self.dry_run:
            argv.append("--no-write")
        rc, out = self._run(argv, timeout=self.cfg.write_test_timeout)
        if self.last_abandoned_pid:
            self._remember_stuck(self.last_abandoned_pid)
        if rc == 0:
            return True, "dry-run: sentinel checked, write test skipped" if self.dry_run else "ok"
        if rc == PROBE_RC_SENTINEL_MISSING:
            return False, f"sentinel {self.cfg.sentinel} is missing"
        if rc == 124:
            return False, f"drive probe hung: {out}"
        return False, f"write test failed: {out or f'rc={rc}'}"

    def _stuck_file(self) -> str:
        return os.path.join(self.cfg.run_dir, "stuck-probe")

    def _stuck_probe(self) -> int | None:
        try:
            with open(self._stuck_file(), encoding="ascii") as fh:
                pid = int(fh.read().strip())
        except (OSError, ValueError):
            return None
        try:
            with open(self.cfg.host_path(f"/proc/{pid}/cmdline"), "rb") as fh:
                if b"_probe" in fh.read():
                    return pid
        except OSError:
            pass
        if not self.dry_run:  # it finally died (or the pid is someone else's now)
            with contextlib.suppress(OSError):
                os.unlink(self._stuck_file())
        return None

    def _remember_stuck(self, pid: int) -> None:
        if self.dry_run:
            return
        try:
            os.makedirs(self.cfg.run_dir, exist_ok=True)
            with open(self._stuck_file(), "w", encoding="ascii") as fh:
                fh.write(f"{pid}\n")
        except OSError as exc:
            self.log.warn(f"could not record the stuck probe: {exc}")

    def udev_retrigger(self) -> str:
        rc1, out1 = self._run(
            ["udevadm", "trigger", "--action=add", "--subsystem-match=block"], timeout=60, mutate=True
        )
        rc2, out2 = self._run(["udevadm", "settle", "--timeout=30"], timeout=45, mutate=True)
        return f"udevadm trigger rc={rc1}{' ' + out1 if out1 else ''}; settle rc={rc2}{' ' + out2 if out2 else ''}"

    def usb_port_live(self) -> str | None:
        """USB port id (e.g. '2-2') of the device behind /dev/disk/by-uuid/<uuid>, from sysfs."""
        if not self.device_present():
            return None
        name = os.path.basename(os.path.realpath(self.cfg.dev_path))
        syspath = os.path.realpath(os.path.join(self.cfg.sysfs, "class", "block", name))
        port = None
        for part in syspath.split(os.sep):
            if USB_DEVICE_RE.match(part):
                port = part  # keep the deepest one: the drive, not a hub in front of it
        return port

    def usb_rebind(self, port: str) -> tuple[bool, str]:
        """Re-bind the drive's USB device (unbind/bind). If the device has dropped off the bus
        entirely, power-cycle its hub port via the port's 'disable' attribute instead."""
        devices = os.path.join(self.cfg.sysfs, "bus", "usb", "devices")
        driver = os.path.join(self.cfg.sysfs, "bus", "usb", "drivers", "usb")
        if os.path.exists(os.path.join(devices, port)):
            err1 = self._write_sysfs(os.path.join(driver, "unbind"), port)
            self.sleep(self.cfg.usb_pause_seconds)
            err2 = self._write_sysfs(os.path.join(driver, "bind"), port)
            if err2:
                return False, f"usb {port}: unbind {err1 or 'ok'}; bind failed: {err2}"
            return True, f"usb {port}: unbind {(err1 + ' (ignored)') if err1 else 'ok'}; bind ok"
        disable = os.path.join(devices, *usb_port_connector(port), "disable")
        if os.path.exists(disable):
            err1 = self._write_sysfs(disable, "1")
            self.sleep(self.cfg.usb_pause_seconds)
            err2 = self._write_sysfs(disable, "0")
            if err1 or err2:
                return False, f"usb {port}: not on the bus; port power-cycle failed: {err1 or err2}"
            return True, f"usb {port}: not on the bus; hub port power-cycled (disable 1 -> 0)"
        return False, f"usb {port}: neither the device nor its hub port is in sysfs"

    def fsck_readonly(self) -> tuple[int, str]:
        """fsck -n: read-only, answers 'no' to every question. Never -y / -p / -a."""
        return self._run(["fsck", "-n", self.cfg.dev_path], timeout=self.cfg.fsck_timeout)

    def power_cycle(self) -> str:
        if self.cfg.powercycle_method == "rtcwake":
            argv = ["rtcwake", "-m", "off", "-s", str(self.cfg.rtcwake_seconds)]
        else:
            argv = ["systemctl", "reboot", "--no-ask-password"]
        rc, out = self._run(argv, timeout=60, mutate=True)
        if self.dry_run:
            return "dry-run: power-cycle not performed"
        if rc != 0:
            return f"{' '.join(argv)} failed rc={rc}: {out}"
        # On success systemd kills us during shutdown. If we are still here after the grace
        # period, the power-cycle did not happen; the caller then fails closed (no retry).
        self.sleep(self.cfg.powercycle_grace_seconds)
        return f"{' '.join(argv)} returned but the host is still up after {self.cfg.powercycle_grace_seconds:.0f}s"

    # docker / notify ---------------------------------------------------------
    def start_docker(self) -> tuple[bool, str]:
        rc, out = self._run(["systemctl", "start", "--no-ask-password", "docker.service"], timeout=300, mutate=True)
        return rc == 0, out or f"rc={rc}"

    def compose_available(self) -> bool:
        return os.path.isfile(self.cfg.host_path(self.cfg.media_compose))

    def compose(self, action: str) -> tuple[bool, str]:
        verb = ["up", "-d"] if action == "up" else ["stop"]
        rc, out = self._run(["docker", "compose", "-f", self.cfg.media_compose, *verb], timeout=300, mutate=True)
        return rc == 0, out or f"rc={rc}"

    def notify(self, title: str, body: str) -> int:
        rc, out = self._run(
            [self.cfg.notify_cmd, "--tag", NOTIFY_TAG, "--title", title, "--body", body],
            timeout=120,
            mutate=True,
        )
        if out:
            self.log.info(f"homelab-notify: {out}")
        return rc

    def clear_rerun_trigger(self) -> bool:
        path = os.path.join(self.cfg.run_dir, "rerun")
        if not os.path.lexists(path):
            return False
        if self.dry_run:
            self.log.info(f"DRY-RUN would remove {path}")
            return True
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        return True


def usb_port_connector(port: str) -> tuple[str, str]:
    """('2-0:1.0', 'usb2-port2') for '2-2'; ('2-1:1.0', '2-1-port4') for '2-1.4'."""
    if "." in port:
        hub, num = port.rsplit(".", 1)
        return f"{hub}:1.0", f"{hub}-port{num}"
    bus, num = port.split("-", 1)
    return f"{bus}-0:1.0", f"usb{bus}-port{num}"


def probe(directory: str, sentinel: str, write: bool = True) -> int:
    """Child-process half of Host.probe_drive(). Exit 0 = ok, 3 = sentinel missing, 1 = I/O
    error. Any of these syscalls may block forever on a hung device; that is why this runs in a
    child the gate can abandon."""
    try:
        os.stat(os.path.join(directory, sentinel))
    except FileNotFoundError:
        print(f"sentinel {sentinel} is missing", file=sys.stderr)
        return PROBE_RC_SENTINEL_MISSING
    except OSError as exc:
        print(f"stat of the sentinel failed: {exc.strerror or exc}", file=sys.stderr)
        return 1
    return probe_write(directory) if write else 0


def write_sysfs(path: str, value: str) -> int:
    """Child-process half of Host._write_sysfs()."""
    try:
        with open(path, "w", encoding="ascii") as fh:
            fh.write(value)
    except OSError as exc:
        print(f"{path}: {exc.strerror or exc}", file=sys.stderr)
        return 1
    return 0


def probe_write(directory: str) -> int:
    """Create, fsync and delete a file. fsync forces real I/O, so a dead device fails here."""
    path = os.path.join(directory, ".bootgate-write-test")
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, b"bootgate write test\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.unlink(path)
        _fsync_dir(directory)
    except OSError as exc:
        print(f"{exc.filename or directory}: {exc.strerror or exc}", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------- the gate
class Gate:
    def __init__(self, cfg: Config, host, store: Store, log: Log):
        self.cfg = cfg
        self.host = host
        self.store = store
        self.log = log
        self._boot_id: str | None = None

    @property
    def boot_id(self) -> str:
        if self._boot_id is None:
            self._boot_id = self.host.boot_id()
        return self._boot_id

    def event(self, name: str, detail: str, waited: float = 0.0) -> None:
        log = self.log.warn if name in ("repair", "powercycle", "fail", "media-lost") else self.log.info
        log(f"[{name}] {detail}")
        try:
            self.store.history(name, detail, waited, self.boot_id)
        except OSError as exc:  # a full or read-only /var must not flip the gate's verdict
            self.log.error(f"could not append history: {exc}")

    def set_status(self, status: str) -> None:
        try:
            self.store.set_status(status)
        except OSError as exc:
            self.log.error(f"could not write status {status}: {exc}")

    # building blocks -----------------------------------------------------------
    def wait_for_device(self, limit: float) -> tuple[bool, float]:
        start = self.host.now()
        announced = False
        while True:
            waited = self.host.now() - start
            if self.host.device_present():
                if announced:
                    self.log.info(f"media device appeared after {waited:.1f}s")
                return True, waited
            if waited >= limit:
                return False, waited
            if not announced:
                self.log.info(f"waiting up to {limit:.0f}s for {self.cfg.dev_path}")
                announced = True
            self.host.sleep(min(self.cfg.poll_seconds, max(0.0, limit - waited)))

    def mount_state(self) -> tuple[str, str]:
        """('ok' | 'none' | 'foreign' | 'stale', detail) for the top-most mount at the mount point.
        'stale': the right UUID, but not the device that now carries it (the drive dropped off
        and came back as a new sdX while the dead mount stayed behind)."""
        info = self.host.mount_info()
        if info is None:
            return "none", f"nothing is mounted on {self.cfg.mount}"
        uuid = (info.get("UUID") or "").strip().lower()
        source = info.get("SOURCE") or "?"
        if uuid != self.cfg.uuid:
            return "foreign", f"{self.cfg.mount} holds {source} (UUID {uuid or 'none'}), not the media drive"
        have, want = info.get("MAJ:MIN"), self.host.device_majmin()
        if have and want and have != want:
            return "stale", f"{self.cfg.mount} is a stale mount of {source} ({have}); the drive is now {want}"
        return "ok", "mounted"

    def ensure_mounted(self) -> tuple[bool, str]:
        """Start the mount unit. A stale mount or a foreign filesystem at the mount point is
        unmounted first; a correct mount is never touched."""
        state, detail = self.mount_state()
        if state == "ok":
            return True, "already mounted"
        if state != "none":
            self.log.warn(f"{detail}; unmounting it")
            self.log.info(self.host.stop_mount())
        if not self.host.device_present():
            return False, "media device is not present"
        return self.host.start_mount()

    def verify(self) -> tuple[bool, str]:
        state, detail = self.mount_state()
        if state != "ok":
            return False, detail
        ok, msg = self.host.probe_drive()
        if not ok:
            return False, msg
        return True, "verified (UUID, sentinel, write test)"

    def mount_and_verify(self) -> tuple[bool, str]:
        ok, msg = self.ensure_mounted()
        if not ok:
            return False, f"mount failed: {msg}"
        return self.verify()

    def remember_usb_port(self) -> None:
        try:
            port = self.host.usb_port_live()
            if port and port != self.store.usb_port():
                self.log.info(f"media drive is on USB port {port}")
                self.store.set_usb_port(port)
        except OSError as exc:
            self.log.warn(f"could not record the USB port: {exc}")

    def succeed(self, event: str, detail: str, waited: float) -> None:
        self.store.set_attempts(0)
        self.set_status("ok")
        self.event(event, detail, waited)
        self.remember_usb_port()

    # the repair ladder -----------------------------------------------------------
    def repair_ladder(self, waited: float, why: str) -> tuple[bool, str]:
        # 1. udev re-trigger + settle, then a short wait for the device
        msg = self.host.udev_retrigger()
        present, _ = self.wait_for_device(self.cfg.udev_wait_seconds)
        if present:
            ok, why = self.mount_and_verify()
            self.event("repair", f"udev re-trigger: {msg}; {'passed' if ok else why}", waited)
            if ok:
                return True, "udev re-trigger"
        else:
            self.event("repair", f"udev re-trigger: {msg}; device still absent", waited)

        # 2. USB re-bind of the port the drive was last verified on
        port = self.store.usb_port() or self.host.usb_port_live()
        if port:
            if self.host.mount_info() is not None:
                self.log.info("unmounting before the USB re-bind: " + self.host.stop_mount())
            _, msg = self.host.usb_rebind(port)
            present, _ = self.wait_for_device(self.cfg.rebind_wait_seconds)
            self.event("repair", f"usb re-bind: {msg}; device {'present' if present else 'still absent'}", waited)
        else:
            self.event("repair", "usb re-bind skipped: no USB port recorded for the drive yet", waited)

        # 3. retry the mount
        if self.host.device_present():
            ok, why = self.mount_and_verify()
            self.event("repair", f"mount retry: {'passed' if ok else why}", waited)
            if ok:
                return True, "usb re-bind + mount retry"
        else:
            why = "media device is not present"
            self.event("repair", "mount retry skipped: device absent", waited)

        # 4. fsck -n: read-only diagnostics for the human; it never changes the disk
        if self.host.device_present() and self.host.mount_info() is None:
            rc, out = self.host.fsck_readonly()
            tail = " | ".join(out.splitlines()[-3:])[:400]
            self.event("repair", f"fsck -n rc={rc}: {tail or 'no output'}", waited)
            if rc not in (0, 124, 127):
                why += f"; fsck -n reports problems (rc={rc}), run e2fsck by hand"
        else:
            self.event("repair", "fsck -n skipped: device absent or still mounted", waited)
        return False, why

    # commands ----------------------------------------------------------------------
    def check_boot(self) -> int:
        booting = self.host.booting()
        limit = self.cfg.wait_seconds if booting else self.cfg.retry_wait_seconds
        if not booting:
            self.log.info("not a boot-time run: short wait, and no power-cycle whatever happens")
        with self.store.lock(blocking=True):
            self.set_status("waiting")
            present, waited = self.wait_for_device(limit)
            if present:
                ok, why = self.mount_and_verify()
            else:
                ok, why = False, f"media device did not appear within {limit:.0f}s"
            if ok:
                self.succeed("pass", why + (" (late arrival)" if waited >= self.cfg.poll_seconds else ""), waited)
                return 0
            self.log.warn(why)

            ok, how = self.repair_ladder(waited, why)
            if ok:
                self.succeed("pass", f"verified after repair ({how})", waited)
                return 0
            why = how

            attempts = self.store.attempts()
            if attempts >= 1:
                why += f"; attempts={attempts}, so no power-cycle (one per incident)"
            elif not booting:
                why += "; not a boot-time run, so no power-cycle"
            elif self.store.set_attempts(1) != 1:
                why += "; could not record attempts=1, so no power-cycle (boot-loop guard)"
            else:
                self.event(
                    "powercycle",
                    f"{why}; power-cycling once via {self.cfg.powercycle_method}",
                    waited,
                )
                result = self.host.power_cycle()  # normally does not come back
                why += f"; {result}"
            self.set_status("failed")
            self.event("fail", why + "; docker stays down", waited)
            return 1

    def check_retry(self) -> int:
        if self.host.clear_rerun_trigger():
            self.log.info("manual re-run requested")
        with self.store.lock(blocking=False) as held:
            if not held:
                self.log.info("another gate run is in progress; nothing to do")
                return 0
            status = self.store.status()
            restore_only = False
            if status in ("failed", "media-lost", "waiting"):
                if status == "waiting":
                    self.log.warn("status is 'waiting' but no gate run holds the lock; treating it as failed")
                present, waited = self.wait_for_device(self.cfg.retry_wait_seconds)
                ok, why = self.mount_and_verify() if present else (False, "media device is not present")
                if not ok:
                    self.log.info(f"retry: still failing ({why}); status stays {status}")
                    return 0
                self.succeed("retry-pass", f"was {status}; {why}", waited)
            elif status == "ok" and self.store.media_stopped():
                restore_only = True
                self.log.info("the media stack was stopped by the watcher; restarting it")
            else:
                self.log.info(f"status is {status or 'unset'}; nothing to retry")
                return 0
            restore_media = self.store.media_stopped() or status == "media-lost"

        # Outside the lock: starting docker.service may start bootgate.service, which takes it.
        notes = []
        media_ok = True
        docker_ok, msg = self.host.start_docker()
        notes.append("docker running" if docker_ok else f"docker start FAILED: {msg[-300:]}")
        if restore_media and docker_ok:
            if self.host.compose_available():
                media_ok, msg = self.host.compose("up")
                notes.append("media apps restarted" if media_ok else f"media apps restart FAILED: {msg[-300:]}")
                if media_ok:
                    self.store.set_media_stopped(False)  # else: kept, so the next retry tries again
            else:
                notes.append("media stack not deployed")
                self.store.set_media_stopped(False)
        if not docker_ok:
            title = TITLE_BACK_NO_DOCKER
        elif not media_ok:
            title = TITLE_BACK_NO_MEDIA
        else:
            title = TITLE_BACK  # only when Docker is up AND the media apps (if stopped) restarted
        body = ("Verified again" if not restore_only else "Drive verified") + "; " + "; ".join(notes) + "."
        self.host.notify(title, body)
        return 0 if docker_ok and media_ok else 1

    def watch(self) -> int:
        with self.store.lock(blocking=False) as held:
            if not held:
                return 0
            if self.store.status() != "ok":
                return 0
            ok, why = self.verify()
            if ok:
                self.remember_usb_port()
                return 0
            self.log.warn(f"watch: {why}; re-checking in {self.cfg.watch_confirm_seconds:.0f}s")
            self.host.sleep(self.cfg.watch_confirm_seconds)
            ok, why = self.verify()
            if ok:
                self.log.info("watch: transient, verified on re-check")
                return 0
            self.set_status("media-lost")
            have_stack = self.host.compose_available()
            if have_stack:
                self.store.set_media_stopped(True)
            self.event("media-lost", why)
        if have_stack:
            stop_ok, msg = self.host.compose("stop")
            stopped = "media apps stopped" if stop_ok else f"stopping the media apps FAILED: {msg}"
        else:
            stopped = "media stack not deployed"
        self.host.notify(
            TITLE_LOST,
            f"Media drive lost; {stopped}. {why}. Retrying every {self.cfg.retry_minutes} min.",
        )
        return 0

    def alert(self) -> int:
        last_fail = next((e for e in reversed(self.store.last_events(20)) if e.get("event") == "fail"), None)
        detail = last_fail.get("detail", "") if last_fail else "no failure recorded"
        body = (
            f"Docker and every container are held down. {detail}. "
            f"Retrying every {self.cfg.retry_minutes} min; after fixing the drive, "
            "touch /run/bootgate/rerun to retry now."
        )
        rc = 1
        for attempt in range(1, self.cfg.alert_tries + 1):
            rc = self.host.notify(TITLE_FAILED, body)
            if rc != 1:
                break
            if attempt < self.cfg.alert_tries:
                self.log.warn(f"alert delivery failed (try {attempt}); retrying in {self.cfg.alert_retry_seconds:.0f}s")
                self.host.sleep(self.cfg.alert_retry_seconds)
        return rc

    def status_report(self) -> int:
        print(f"status:   {self.store.status() or 'unset'}")
        print(f"attempts: {self.store.attempts()}")
        print(f"usb-port: {self.store.usb_port() or 'unknown'}")
        print(f"media stack stopped by the watcher: {'yes' if self.store.media_stopped() else 'no'}")
        for entry in self.store.last_events(10):
            print(json.dumps(entry))
        return 0


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bootgate", description="Media drive boot gate")
    p.add_argument("--dry-run", action="store_true", help="log changes instead of making them")
    sub = p.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="verify (and repair) the media drive")
    mode = check.add_mutually_exclusive_group(required=True)
    mode.add_argument("--boot", action="store_true")
    mode.add_argument("--retry", action="store_true")
    for name in ("watch", "alert", "status"):
        sub.add_parser(name)
    probe_p = sub.add_parser("_probe")  # internal: run in a child by Host.probe_drive()
    probe_p.add_argument("directory")
    probe_p.add_argument("sentinel")
    probe_p.add_argument("--no-write", action="store_true")
    sysfs_p = sub.add_parser("_write-sysfs")  # internal: run in a child by Host._write_sysfs()
    sysfs_p.add_argument("path")
    sysfs_p.add_argument("value")
    for sp in sub.choices.values():
        sp.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    return p


def main(argv: list[str] | None = None, host=None, log: Log | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "_probe":
        return probe(args.directory, args.sentinel, write=not args.no_write)
    if args.command == "_write-sysfs":
        return write_sysfs(args.path, args.value)
    log = log or Log()
    try:
        cfg = Config()
        problems = cfg.problems()
        if problems:
            for problem in problems:
                log.error(problem)
            return 1  # fail closed: docker stays down, the alert fires
        dry = bool(args.dry_run)
        store = Store(cfg, log, dry_run=dry)
        gate = Gate(cfg, host or Host(cfg, log, dry_run=dry), store, log)
        if args.command == "check":
            return gate.check_boot() if args.boot else gate.check_retry()
        return {"watch": gate.watch, "alert": gate.alert, "status": gate.status_report}[args.command]()
    except Exception:  # noqa: BLE001 - fail closed on anything unexpected
        log.error("unexpected error:\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
