"""One sample per tick -> Noah's rule -> status; unload Ollama on available->unavailable."""

import logging
import os
import sys
import threading

from . import procfs
from .detector import Detector
from .sampler import OllamaMeter, card_pdev, clamp, find_card, read_busy
from .window import Window

log = logging.getLogger("gpu_gate")

# reason values (stable strings; the README documents them)
NO_GAME = "no_game"                  # game is None                -> available
BELOW_THRESHOLD = "below_threshold"  # avg30 < threshold           -> available
GAME_BUSY = "game_busy"              # game and avg30 >= threshold -> unavailable
NO_SAMPLES = "game_no_samples"       # game and no GPU sample yet  -> unavailable (fail toward the game)


LOGIN_DEFS = "/etc/login.defs"


def uid_min(path=LOGIN_DEFS, default=1000):
    """UID_MIN from login.defs: the first uid handed to login users. As in shadow, the last
    UID_MIN line wins; if its value isn't a number, the default applies."""
    value = None
    for line in (procfs.read_text(path) or "").splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 2 and parts[0] == "UID_MIN":
            value = parts[1]
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def uid_match_guard(uid, login_defs=LOGIN_DEFS):
    """Uid matching assumes the gate's uid is Ollama's alone (a system user such as `ollama`).
    Root or a login user would sweep in the desktop's processes, the game included.
    Returns (enabled, reason)."""
    if uid == 0:
        return False, "uid 0 (root): every root process would count as Ollama's"
    lo = uid_min(login_defs)
    if uid >= lo:
        return False, ("uid %d >= UID_MIN %d (a login user): that user's processes would count "
                       "as Ollama's" % (uid, lo))
    return True, None


def decide(avg30, game, threshold):
    """Noah's rule: available = avg30 < threshold OR game is None. Returns (available, reason)."""
    if game is None:
        return True, NO_GAME
    if avg30 is None:
        return False, NO_SAMPLES
    if avg30 < threshold:
        return True, BELOW_THRESHOLD
    return False, GAME_BUSY


class Gate:
    def __init__(self, cfg, clock, mono, unloader=None, uid=None, login_defs=LOGIN_DEFS):
        """`clock` gives wall time (status ts); `mono` gives seconds for the window, the Ollama
        deltas and staleness. Production passes CLOCK_BOOTTIME so suspend time ages samples out.
        `unloader` is called (no args) on each available->unavailable transition.
        `uid` (default: our own) is the uid matched as Ollama's, subject to uid_match_guard."""
        self.cfg = cfg
        self.clock = clock
        self.mono = mono
        self.unloader = unloader
        self.window = Window(cfg["window"])
        self.detector = Detector(cfg["games"], cfg["ignore"])
        self.card = None
        self.meter = None
        self._available = True  # start as available so a first "unavailable" also unloads
        self._game = None
        self._lock = threading.Lock()
        self._status = None
        self._last_tick = None
        uid = os.getuid() if uid is None else uid
        if cfg["ollama_match_uid"]:
            self.uid_match, self.uid_match_reason = uid_match_guard(uid, login_defs)
            if not self.uid_match:
                print("gpu-gate: WARNING: uid matching off for this run: %s; only the "
                      "ollama_match names identify Ollama" % self.uid_match_reason,
                      file=sys.stderr, flush=True)
        else:
            self.uid_match, self.uid_match_reason = False, "ollama_match_uid is false in the config"
        self.match_uid = uid if self.uid_match else None

    def _ensure_card(self):
        if self.card is None:
            self.card = self.cfg["card"] or find_card(self.cfg["sysfs_root"])
            if self.card is None:
                log.error("no DRM card with gpu_busy_percent under %s", self.cfg["sysfs_root"])
            else:
                log.info("card %s (pdev %s)", self.card, card_pdev(self.card))
            self.meter = OllamaMeter(
                self.cfg["ollama_match"], self.cfg["ollama_engines"],
                card_pdev(self.card) if self.card else None, self.mono,
                match_uid=self.match_uid,
                own_pid=os.getpid(),
            )

    def tick(self):
        self._ensure_card()
        now = self.mono()
        procs = procfs.scan(self.cfg["proc_root"])
        busy = read_busy(self.card) if self.card else None
        share = self.meter.sample(procs)
        if busy is None:
            if self.card:
                log.warning("gpu_busy_percent unreadable at %s", self.card)
            self.window.expire(now)
        else:
            self.window.add(now, clamp(busy - share))
        hit = self.detector.detect(procs)
        game = hit[0] if hit else None
        avg30 = self.window.mean()
        available, reason = decide(avg30, game, self.cfg["threshold"])

        if game != self._game:
            log.info("game: %s -> %s%s", self._game, game, " (rule %s)" % hit[1] if hit else "")
            self._game = game
        if available != self._available:
            log.info("available: %s -> %s (%s, avg30=%s)", self._available, available, reason,
                     None if avg30 is None else round(avg30, 1))
            if not available and self.unloader is not None:
                self.unloader()
            self._available = available

        status = {
            "available": available,
            "reason": reason,
            "avg30": None if avg30 is None else round(avg30, 1),
            "samples": len(self.window),
            "game": game,
            "gpu_busy": busy,
            "ollama_share": round(share, 1),
            "ts": round(self.clock(), 3),
            "uid_match": self.uid_match,
            "uid_match_reason": self.uid_match_reason,
        }
        with self._lock:
            self._status = status
            self._last_tick = now
        return status

    def status(self):
        with self._lock:
            return None if self._status is None else dict(self._status)

    def healthy(self):
        """True when the last successful sample is at most 3 intervals old (/healthz, /status)."""
        with self._lock:
            last = self._last_tick
        return last is not None and self.mono() - last <= 3 * self.cfg["interval"]
