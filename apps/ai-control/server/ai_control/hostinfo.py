"""ai-box host facts: /proc (load, memory), statfs of the configured mounts, and `chronyc -n tracking`."""

from __future__ import annotations

import asyncio
import os
import re
import signal
from typing import Any

from .clients import SourceError


def read_resources(proc_root: str, mounts: tuple[str, ...]) -> dict[str, Any]:
    """Blocking (run it in a thread): load1, memory in bytes, and each mount's total/avail bytes."""
    with open(os.path.join(proc_root, "loadavg"), encoding="ascii") as fh:
        load1 = float(fh.read(256).split()[0])
    mem: dict[str, int] = {}
    with open(os.path.join(proc_root, "meminfo"), encoding="ascii") as fh:
        for line in fh.read(65536).splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].endswith(":") and parts[1].isdigit():
                factor = 1024 if len(parts) > 2 and parts[2] == "kB" else 1
                mem[parts[0][:-1]] = int(parts[1]) * factor
    if "MemTotal" not in mem or "MemAvailable" not in mem:
        raise ValueError("meminfo lacks MemTotal/MemAvailable")
    disks = []
    for mount in mounts:
        try:
            st = os.statvfs(mount)
            disks.append({"mount": mount, "total": st.f_blocks * st.f_frsize, "avail": st.f_bavail * st.f_frsize})
        except OSError:
            disks.append({"mount": mount, "error": "statfs failed"})
    return {
        "load1": load1,
        "mem_total": mem["MemTotal"],
        "mem_avail": mem["MemAvailable"],
        "swap_used": max(0, mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)),
        "disk": disks,
    }


_SYSTIME = re.compile(r"^System time\s*:\s*([0-9.]+)\s+seconds\s+(fast|slow)", re.M)
_LEAP = re.compile(r"^Leap status\s*:\s*(.+?)\s*$", re.M)
_REF = re.compile(r"^Reference ID\s*:\s*\S+\s*\(([^)]*)\)", re.M)


def parse_chrony(text: str) -> dict[str, Any]:
    leap = _LEAP.search(text)
    systime = _SYSTIME.search(text)
    if not leap or not systime:
        raise ValueError("unrecognised chronyc output")
    offset_ms = float(systime.group(1)) * 1000.0
    if systime.group(2) == "slow":
        offset_ms = -offset_ms
    ref = _REF.search(text)
    source = ref.group(1).strip() if ref else None
    return {
        "synced": leap.group(1).lower() != "not synchronised",
        "offset_ms": round(offset_ms, 3),
        "source": (source or None) and source[:128],
    }


async def chrony(chronyc: str, timeout: float) -> dict[str, Any]:
    """`chronyc -n tracking` (fixed argv, no shell), parsed."""
    try:
        proc = await asyncio.create_subprocess_exec(
            chronyc,
            "-n",
            "tracking",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,  # its own process group, so a kill takes any children with it
        )
    except OSError:
        raise SourceError("chronyc not available") from None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        raise SourceError(f"chronyc timed out after {timeout:g}s") from None
    finally:
        # Also on cancellation (the summary's per-source timeout): never leave the child running.
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.shield(proc.wait())
            except asyncio.CancelledError:
                pass
    if proc.returncode != 0:
        raise SourceError(f"chronyc exited {proc.returncode}")
    try:
        return parse_chrony(out[:65536].decode("utf-8", "replace"))
    except ValueError:
        raise SourceError("unrecognised chronyc output") from None


def detect_timezone(configured: str | None) -> str:
    """Config `timezone`, then /etc/timezone, then the /etc/localtime link, then UTC."""
    if configured:
        return configured
    try:
        with open("/etc/timezone", encoding="ascii") as fh:
            tz = fh.read(128).strip()
            if re.fullmatch(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*", tz):
                return tz
    except (OSError, UnicodeDecodeError):
        pass
    try:
        target = os.readlink("/etc/localtime")
        marker = "zoneinfo/"
        if marker in target:
            return target.split(marker, 1)[1]
    except OSError:
        pass
    return "UTC"
