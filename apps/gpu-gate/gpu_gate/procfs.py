"""Tolerant /proc reading. Processes vanish and other users' files may be unreadable:
every read returns None instead of raising.

Only world-readable files are read for other users' processes: comm, cmdline and stat
(CONTRACTS.md 12.9). Never exe links, never environ. The sampler additionally reads the
world-readable status (real uid, lazily) and fdinfo, the latter only for Ollama's processes,
which run as the gate's own uid.
"""

import os


def read_bytes(path, limit=1 << 20):
    try:
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:  # FileNotFoundError, PermissionError, ProcessLookupError (ESRCH), ...
        return None


def read_text(path):
    data = read_bytes(path)
    return None if data is None else data.decode("utf-8", "replace")


def listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


def basename_any(path):
    """Basename that understands both / and \\ (Wine argv0 is a Windows path)."""
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def parse_ppid(stat):
    """ppid from /proc/<pid>/stat ("pid (comm) state ppid ..."; comm may contain ") ")."""
    try:
        return int(stat.rpartition(")")[2].split()[1])
    except (AttributeError, IndexError, ValueError):
        return None


class Proc:
    """One process snapshot, from world-readable files only."""

    __slots__ = ("pid", "root", "comm", "argv", "ppid", "_uid")

    def __init__(self, pid, root, comm, argv, ppid=None):
        self.pid = pid
        self.root = root
        self.comm = comm
        self.argv = argv    # list of str, [] for kernel threads or when unreadable
        self.ppid = ppid
        self._uid = False

    def uid(self):
        """Real uid from /proc/<pid>/status ("Uid:\treal\teffective\tsaved\tfs"), or None.
        Read lazily: only the Ollama meter needs it, game detection never does."""
        if self._uid is False:
            self._uid = None
            for line in (read_text(self.path("status")) or "").splitlines():
                if line.startswith("Uid:"):
                    try:
                        self._uid = int(line.split()[1])
                    except (IndexError, ValueError):
                        pass
                    break
        return self._uid

    @property
    def argv0(self):
        return basename_any(self.argv[0]) if self.argv else ""

    @property
    def names(self):
        """Lower-cased identifiers used for substring matching: comm and argv0's basename."""
        return [n.lower() for n in (self.comm, self.argv0) if n]

    @property
    def display(self):
        """comm, unless the kernel truncated it (15 chars) and argv0 has the full name."""
        a0 = self.argv0
        if a0 and len(a0) > len(self.comm) and a0.startswith(self.comm):
            return a0
        return self.comm

    def path(self, *parts):
        return os.path.join(self.root, str(self.pid), *parts)


def scan(proc_root):
    """Snapshot every readable process, in pid order."""
    pids = sorted(int(n) for n in listdir(proc_root) if n.isdigit())
    out = []
    for pid in pids:
        base = os.path.join(proc_root, str(pid))
        comm = read_text(os.path.join(base, "comm"))
        if comm is None:  # vanished
            continue
        raw = read_bytes(os.path.join(base, "cmdline")) or b""
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        ppid = parse_ppid(read_text(os.path.join(base, "stat")))
        out.append(Proc(pid, proc_root, comm.rstrip("\n"), argv, ppid))
    return out
