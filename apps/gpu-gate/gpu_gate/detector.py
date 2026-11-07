"""Is a game running? Rules are tried in order; the first rule with any hit decides.

Only world-readable comm, cmdline and stat are used (CONTRACTS.md 12.9). Within a rule, a hit on
a launcher/Wine plumbing process (HELPERS) only names the game when nothing better matched.
Processes matching the `ignore` list never count.
"""

import re

# Steam/Proton/Wine plumbing that isn't the game itself (names the game only as a last resort).
HELPERS = frozenset(
    s.lower()
    for s in (
        "reaper", "srt-bwrap", "pv-adverb", "pressure-vessel", "python3", "steam.exe", "steam",
        "wine", "wine64", "wineserver", "wine-preloader", "wine64-preloader", "start.exe",
        "services.exe", "winedevice.exe", "plugplay.exe", "svchost.exe", "explorer.exe",
        "rpcss.exe", "tabtip.exe", "xalia.exe", "conhost.exe", "crs-handler.exe",
        "steamerrorreporter.exe", "crashpad_handler.exe", "_v2-entry-point", "proton",
        "sh", "bash",
    )
)

_REAPER_APPID = re.compile(r"^AppId=(\d+)$")
_PRISM_INSTANCE = re.compile(r"[/\\]instances[/\\]([^/\\]+)[/\\]", re.IGNORECASE)


def _appid(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n != 0 else None


class Detector:
    def __init__(self, games=(), ignore=()):
        self.games = [g.lower() for g in games]
        self.ignore = [g.lower() for g in ignore]
        self.rules = (
            ("games", self._allow),
            ("steam", self._steam),
            ("wine", self._wine),
            ("gamescope", self._gamescope),
            ("minecraft", self._minecraft),
        )
        self._children = {}

    @staticmethod
    def _any_sub(proc, needles):
        return any(n in name for name in proc.names for n in needles)

    def _allow(self, proc):
        return proc.display if self._any_sub(proc, self.games) else None

    def _descendants(self, proc):
        out, todo, seen = [], [proc.pid], {proc.pid}
        while todo:
            for child in self._children.get(todo.pop(), ()):
                if child.pid not in seen:
                    seen.add(child.pid)
                    out.append(child)
                    todo.append(child.pid)
        return sorted(out, key=lambda p: p.pid)

    def _steam(self, proc):
        # Steam launches every game as "reaper SteamLaunch AppId=<n> -- <game command>".
        if proc.comm != "reaper" or "SteamLaunch" not in proc.argv:
            return None
        appid = None
        for arg in proc.argv:
            if arg == "--":
                break
            m = _REAPER_APPID.match(arg)
            if m:
                appid = _appid(m.group(1))
        if appid is None:
            return None
        for d in self._descendants(proc):  # name it after the first real process under reaper
            if d.display.lower() not in HELPERS and not self._any_sub(d, self.ignore):
                return d.display
        return "steam app %d" % appid

    @staticmethod
    def _wine(proc):
        if any(n.startswith("wine") or n.endswith(".exe") for n in proc.names):
            return proc.display
        return None

    @staticmethod
    def _gamescope(proc):
        return proc.display if any(n.startswith("gamescope") for n in proc.names) else None

    @staticmethod
    def _minecraft(proc):
        if not any(n.startswith("java") for n in proc.names):
            return None
        cmd = " ".join(proc.argv)
        low = cmd.lower()
        if "prismlauncher" in low:
            m = _PRISM_INSTANCE.search(cmd)
            return "minecraft (%s)" % m.group(1) if m else "minecraft"
        return "minecraft" if "minecraft" in low else None

    def detect(self, procs):
        """Return (name, rule) for the running game, or None."""
        self._children = {}
        for p in procs:
            self._children.setdefault(p.ppid, []).append(p)
        procs = [p for p in procs if not self._any_sub(p, self.ignore)]
        for rule, fn in self.rules:
            if rule == "games" and not self.games:
                continue
            best = None
            for proc in procs:
                name = fn(proc)
                if name is None:
                    continue
                if name.lower() not in HELPERS:
                    return name, rule
                if best is None:
                    best = name
            if best is not None:
                return best, rule
        return None
