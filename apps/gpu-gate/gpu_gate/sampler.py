"""GPU busy percent (sysfs) and Ollama's share of the gfx engine (DRM fdinfo)."""

import logging
import os
import re

from . import procfs

log = logging.getLogger("gpu_gate.sampler")

_CARD_RE = re.compile(r"^card(\d+)$")  # card1, not the card1-DP-1 connectors
_PCI_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")


def clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def find_card(sysfs_root):
    """The first /sys/class/drm/cardN/device (by N) that has gpu_busy_percent, or None."""
    drm = os.path.join(sysfs_root, "class", "drm")
    cards = sorted(
        (int(m.group(1)), name)
        for name in procfs.listdir(drm)
        for m in [_CARD_RE.match(name)]
        if m
    )
    for _, name in cards:
        dev = os.path.join(drm, name, "device")
        if os.path.exists(os.path.join(dev, "gpu_busy_percent")):
            return dev
    return None


def card_pdev(device_dir):
    """PCI address of the card ("0000:2d:00.0"), or None when it can't be told."""
    base = os.path.basename(os.path.realpath(device_dir))
    return base if _PCI_RE.match(base) else None


def read_busy(device_dir):
    text = procfs.read_text(os.path.join(device_dir, "gpu_busy_percent"))
    try:
        return int(clamp(int(text.strip())))
    except (AttributeError, ValueError):
        return None


def parse_fdinfo(text):
    out = {}
    for line in text.splitlines():
        k, sep, v = line.partition(":")
        if sep:
            out[k.strip()] = v.strip()
    return out


def _ns(value):
    try:
        return int(value.split()[0])
    except (IndexError, ValueError):
        return 0


class OllamaMeter:
    """Percent of wall time Ollama spent on the configured DRM engines since the last sample.

    Each DRM client (an open render node) shows up in the fdinfo of every fd that refers to it,
    in every process that shares it, so totals are keyed by (drm-pdev, drm-client-id) and counted
    once. A client seen for the first time contributes nothing (its counter is lifetime, not
    per-interval); a counter that went backwards counts as 0.
    """

    def __init__(self, match, engines, pdev, mono, match_uid=None, own_pid=None):
        """Ollama's processes: every process whose real uid is `match_uid` (the gate runs as
        `ollama`, so that is Ollama's uid; None turns it off), plus any whose comm or argv0
        contains a `match` name. `own_pid` (the gate itself) never counts."""
        self.match = [m.lower() for m in match]
        self.match_uid = match_uid
        self.own_pid = own_pid
        self.keys = ["drm-engine-" + e for e in engines]
        self.pdev = pdev
        self.mono = mono
        self._prev = None
        self._prev_t = None

    def is_ollama(self, proc):
        if proc.pid == self.own_pid:
            return False
        if self.match_uid is not None and proc.uid() == self.match_uid:
            return True
        return any(m in n for n in proc.names for m in self.match)

    def totals(self, procs):
        totals = {}
        for proc in procs:
            if not self.is_ollama(proc):
                continue
            fdinfo = proc.path("fdinfo")
            for fd in procfs.listdir(fdinfo):
                text = procfs.read_text(os.path.join(fdinfo, fd))
                if not text or "drm-client-id" not in text:
                    continue  # not a DRM fd (or vanished)
                info = parse_fdinfo(text)
                pdev = info.get("drm-pdev")
                if self.pdev and pdev and pdev != self.pdev:
                    continue  # a different GPU
                key = (pdev, info["drm-client-id"])
                if key not in totals:
                    totals[key] = sum(_ns(info.get(k, "0")) for k in self.keys)
        return totals

    def sample(self, procs):
        now = self.mono()
        totals = self.totals(procs)
        share = 0.0
        if self._prev is not None and now > self._prev_t:
            delta = sum(max(0, ns - self._prev[k]) for k, ns in totals.items() if k in self._prev)
            share = clamp(delta / ((now - self._prev_t) * 1e9) * 100.0)
        self._prev, self._prev_t = totals, now
        return share
