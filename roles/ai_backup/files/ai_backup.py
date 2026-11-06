"""Open WebUI nightly backup (docs/CONTRACTS.md §12.28): stage on ai-box, pull to the gaming PC.

Python 3 standard library only. One file, four commands (run with python3 -I -B):

  stage          ai-box, ai-openwebui-backup.service (root, nightly 03:45). Writes
                 <out>/owui-YYYYmmddTHHMMSSZ.tar (root:<group> 0640) holding, in this order:
                   sqlite/webui.db                   online copies (SQLite backup API, source opened
                   sqlite/vector_db/chroma.sqlite3   read-only from the HOST volume path), each passing
                                                     PRAGMA integrity_check
                   uploads.tar                       uploads/
                   vector_db.tar                     vector_db/ minus chroma.sqlite3 (+ its -journal/-wal/
                                                     -shm). The HNSW index files may be mid-write: after a
                                                     restore, run Reindex if a Knowledge search misbehaves.
                   stack.env                         the ai stack's .env (WEBUI_SECRET_KEY), 0600
                   MANIFEST.sha256                   sha256 of every member above; ALWAYS the last member
                 Never: cache/, *.bak-* (anywhere), symlinks (not followed, not stored), special files,
                 other filesystems. Unknown top-level entries of the volume are named in the log.
                 The archive is read back and verified before it is published; then LAST_OK (ISO 8601
                 UTC, 0640) is replaced and all but the newest --keep archives are deleted.
  pull           gaming PC, ai-openwebui-pull.service (root, daily 06:30). Refuses unless --dest is
                 exactly <--mount>/<--subdir> on the real backup drive (findmnt + st_dev + FSROOT, as
                 the AI Library backup, §12.17) and every folder from the mount down to it is root-owned
                 and not group/other-writable (missing ones are created 0700). ai-box is not trusted to
                 be well-behaved (Reviewer X): before fetching, its listing must hold at most
                 --max-remote archives of at most --max-size each, and the drive must keep --min-free-mb
                 free after the transfer. rsync over SSH (rrsync -ro on ai-box, --max-size) mirrors
                 ai-box's archives + LAST_OK into <dest>/.incoming. A new archive whose name is more than
                 --max-future-minutes ahead of this clock is rejected (it would outrank every genuine copy
                 in the name-ordered retention); more than --max-new new archives in one run promotes
                 none. Each new archive's manifest is verified and only then hard-linked into <dest>;
                 fails when ai-box's LAST_OK is older than --max-age-hours. Retention runs only after a
                 fully clean run (rsync exit 0, nothing rejected, no future-dated archive kept, LAST_OK
                 fresh) and deletes only archives beyond the newest --keep whose names are dated more than
                 --keep days ago: a flood of fresh names piles up until the free-space floor refuses.
  verify A...    checks archives: regular-file members only, the expected names, MANIFEST.sha256 last,
                 every hash right.
  restore-check  extracts the newest archive in --dir (or --archive NAME there) to a private temp dir,
                 checks that the inner tars hold only safe names (under uploads/ or vector_db/, no '..',
                 no absolute paths, files and folders only) and runs PRAGMA
                 integrity_check on both databases (plus the inner tars and stack.env). Read-only for
                 the archives; the temp dir is removed.

Exit codes: 0 ok, 1 failed (bad data, rsync, stale LAST_OK, rejected archives), 2 refused (a safety
check failed before anything was written or deleted).
"""
import argparse
import datetime as dt
import fcntl
import grp
import hashlib
import io
import os
import posixpath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse

ARCHIVE_RE = re.compile(r"^owui-(\d{8}T\d{6}Z)\.tar$")
STAMP_FMT = "%Y%m%dT%H%M%SZ"
ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"
MANIFEST = "MANIFEST.sha256"
DB_WEBUI = "sqlite/webui.db"
DB_CHROMA = "sqlite/vector_db/chroma.sqlite3"
UPLOADS_TAR = "uploads.tar"
VECTOR_TAR = "vector_db.tar"
ENV_MEMBER = "stack.env"
# The outer archive, in order. MANIFEST.sha256 is appended last.
MEMBERS = (DB_WEBUI, DB_CHROMA, UPLOADS_TAR, VECTOR_TAR, ENV_MEMBER)
SQLITE_MAGIC = b"SQLite format 3\x00"
SIDECARS = ("-journal", "-wal", "-shm")
# Top-level entries of the volume this script knows about (everything else is reported, not copied).
KNOWN_TOP = {"webui.db", "uploads", "vector_db", "cache"} | {"webui.db" + s for s in SIDECARS}
EXCLUDED_TOP = {"cache"}
BAK_RE = re.compile(r"\.bak-")
RSYNC_OK = {0, 24}  # 24: a file vanished on ai-box during the transfer (its rotation)
WORK_PREFIX = ".work."
STALE_WORK_SECONDS = 12 * 3600
CHUNK = 1 << 20


class Refused(Exception):
    """A safety check failed before anything was written or deleted (exit 2)."""


class Failed(Exception):
    """The run failed (exit 1)."""


def log(msg):
    print(f"ai-backup: {msg}", flush=True)


def utcnow():
    return dt.datetime.now(dt.UTC)


def parse_size(text):
    """rsync-style size: 8G, 512M, 100K, or bytes (K = 1024)."""
    m = re.fullmatch(r"(\d+)([KMGT]?)", text.strip().upper())
    if not m:
        raise Refused(f"bad size {text!r} (use e.g. 8G, 512M or a byte count)")
    return int(m.group(1)) * 1024 ** "_KMGT".index(m.group(2) or "_")


def stamp_of(name):
    """The UTC time in an archive name (owui-YYYYmmddTHHMMSSZ.tar), or None."""
    m = ARCHIVE_RE.match(name)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), STAMP_FMT).replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def future_dated(name, slack_minutes):
    t = stamp_of(name)
    return t is None or t > utcnow() + dt.timedelta(minutes=slack_minutes)


# ---------------------------------------------------------------------------------------------------
# Small file helpers
# ---------------------------------------------------------------------------------------------------
def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def real_dir(path, label):
    """path must be absolute, without '.'/'..', no symlink anywhere, and a real directory."""
    parts = path.split("/")
    if not path.startswith("/") or "." in parts or ".." in parts:
        raise Refused(f"{label} {path!r} must be an absolute path without '.' or '..'")
    norm = os.path.normpath(path)
    if os.path.realpath(norm) != norm:
        raise Refused(f"{label} {path} goes through a symlink (realpath {os.path.realpath(norm)})")
    try:
        st = os.lstat(norm)
    except FileNotFoundError:
        raise Refused(f"{label} {path} is missing") from None
    if not stat.S_ISDIR(st.st_mode):
        raise Refused(f"{label} {path} is not a directory")
    return norm


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def take_lock(directory):
    fd = os.open(os.path.join(directory, ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise Refused(f"another run holds {directory}/.lock") from None
    return fd


def write_atomic(directory, name, text, mode, gid=-1):
    tmp = os.path.join(directory, f".{name}.tmp")
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, text.encode())
        if gid != -1:
            os.fchown(fd, -1, gid)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, os.path.join(directory, name))
    fsync_dir(directory)


def archives_in(directory):
    """Archive file names in directory (regular files only, never symlinks), oldest first."""
    out = []
    with os.scandir(directory) as it:
        for e in it:
            if ARCHIVE_RE.match(e.name) and e.is_file(follow_symlinks=False):
                out.append(e.name)
    return sorted(out)


def expired(names, keep, min_age_days=0, now=None):
    """The archive names retention deletes: all but the newest `keep` (keep >= 1, so never the newest). With
    min_age_days, only those whose name is dated more than that many days ago (Reviewer X-1b: a flood of fresh
    names can't push the real history out; it piles up until the free-space floor refuses)."""
    if keep < 1:
        raise Refused(f"keep must be at least 1 (got {keep})")
    ordered = sorted(n for n in names if ARCHIVE_RE.match(n))
    out = ordered[:-keep] if len(ordered) > keep else []
    if min_age_days:
        cutoff = (now or utcnow()) - dt.timedelta(days=min_age_days)
        out = [n for n in out if (t := stamp_of(n)) is not None and t < cutoff]
    return out


def apply_retention(directory, keep, protect=None, min_age_days=0):
    names = archives_in(directory)
    gone = expired(names, keep, min_age_days)
    newest = names[-1] if names else None
    for name in gone:
        if name in (newest, protect):  # belt and braces: expired() never returns these
            raise Failed(f"retention picked {name}, the newest archive; refusing")
        os.unlink(os.path.join(directory, name))
        log(f"rotated out {name}")
    if gone:
        fsync_dir(directory)
    return gone


# ---------------------------------------------------------------------------------------------------
# Verify (both hosts)
# ---------------------------------------------------------------------------------------------------
def parse_manifest(text):
    want = {}
    for line in text.splitlines():
        m = re.fullmatch(r"([0-9a-f]{64})  (\S+)", line)
        if not m:
            raise Failed(f"malformed manifest line {line[:80]!r}")
        if m.group(2) in want:
            raise Failed(f"manifest lists {m.group(2)} twice")
        want[m.group(2)] = m.group(1)
    return want


def verify_archive(path):
    """Verify one archive; returns {member: sha256}. Raises Failed with the reason."""
    label = os.path.basename(path)
    got, order, manifest = {}, [], None
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            raise Failed(f"{label}: not a regular file")
        with tarfile.open(path, mode="r:") as tf:
            for m in tf:
                order.append(m.name)
                if not m.isfile():
                    raise Failed(f"{label}: member {m.name!r} is not a regular file")
                if m.name in got or (m.name == MANIFEST and manifest is not None):
                    raise Failed(f"{label}: member {m.name!r} appears twice")
                if m.name != MANIFEST and m.name not in MEMBERS:
                    raise Failed(f"{label}: unexpected member {m.name!r}")
                fh = tf.extractfile(m)
                if m.name == MANIFEST:
                    manifest = fh.read().decode("ascii")
                    continue
                if manifest is not None:
                    raise Failed(f"{label}: {MANIFEST} is not the last member ({m.name} follows it)")
                h = hashlib.sha256()
                while chunk := fh.read(CHUNK):
                    h.update(chunk)
                got[m.name] = h.hexdigest()
    except (tarfile.TarError, OSError, EOFError, UnicodeDecodeError) as exc:
        raise Failed(f"{label}: unreadable ({type(exc).__name__}: {exc})") from None
    if manifest is None:
        raise Failed(f"{label}: no {MANIFEST} (archive cut short?)")
    if order[-1] != MANIFEST:
        raise Failed(f"{label}: {MANIFEST} is not the last member")
    want = parse_manifest(manifest)
    missing = sorted(set(MEMBERS) - set(got))
    unlisted = sorted(set(got) - set(want))
    absent = sorted(set(want) - set(got))
    wrong = sorted(n for n in want if n in got and got[n] != want[n])
    if missing or unlisted or absent or wrong:
        raise Failed(f"{label}: manifest mismatch: missing members {missing}, unlisted {unlisted}, "
                     f"listed but absent {absent}, bad hash {wrong}")
    return got


# ---------------------------------------------------------------------------------------------------
# Stage (ai-box)
# ---------------------------------------------------------------------------------------------------
def sqlite_backup(src, dst, deadline_s=900, attempts=3):
    """Consistent online copy of src into dst (a new file), through a read-only source connection,
    then PRAGMA integrity_check on the copy. Raises Failed."""
    try:
        st = os.lstat(src)
    except FileNotFoundError:
        raise Failed(f"database {src} is missing") from None
    if not stat.S_ISREG(st.st_mode):
        raise Failed(f"database {src} is not a regular file (a symlink?); refusing to read it")
    with open(src, "rb") as fh:
        header = fh.read(100)
    if len(header) < 100 or header[:16] != SQLITE_MAGIC:
        raise Failed(f"{src} is not an SQLite database")
    wal = header[18] == 2
    quoted = urllib.parse.quote(src, safe="/")
    for attempt in range(1, attempts + 1):
        # A WAL database that no connection has open has no -wal/-shm. Read-only, SQLite can't create
        # the -shm, so it opens it immutable, and the stat guard below proves no writer touched it.
        idle_wal = wal and not any(os.path.lexists(src + s) for s in ("-wal", "-shm"))
        uri = f"file:{quoted}?mode=ro" + ("&immutable=1" if idle_wal else "")
        before = os.stat(src)
        for p in (dst, dst + "-journal", dst + "-wal", dst + "-shm"):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
        start = time.monotonic()

        def progress(_status, _remaining, _total, start=start):
            if time.monotonic() - start > deadline_s:
                raise TimeoutError(f"the copy took over {deadline_s} s")

        try:
            source = sqlite3.connect(uri, uri=True, timeout=30)
            try:
                target = sqlite3.connect(dst)
                try:
                    # WAL: one step = one read transaction (never blocks the writer). Rollback journal:
                    # steps, so a writer gets the lock between them; a write restarts the copy.
                    source.backup(target, pages=-1 if wal else 4096, sleep=0.02, progress=progress)
                finally:
                    target.close()
            finally:
                source.close()
        except (sqlite3.Error, TimeoutError) as exc:
            raise Failed(f"sqlite backup of {os.path.basename(src)} failed: {exc}") from None
        after = os.stat(src)
        changed = (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns)
        if idle_wal and (changed or any(os.path.lexists(src + s) for s in ("-wal", "-shm"))):
            log(f"{os.path.basename(src)} was opened by a writer during the copy; retrying ({attempt}/{attempts})")
            continue
        break
    else:
        raise Failed(f"{os.path.basename(src)} kept changing during {attempts} copies")
    for s in SIDECARS:
        if os.path.lexists(dst + s):
            raise Failed(f"the copy of {os.path.basename(src)} left a {s} file behind")
    try:
        check = sqlite3.connect(f"file:{urllib.parse.quote(dst, safe='/')}?mode=ro&immutable=1", uri=True)
        try:
            rows = check.execute("PRAGMA integrity_check").fetchall()
        finally:
            check.close()
    except sqlite3.Error as exc:
        raise Failed(f"integrity check of the copy of {os.path.basename(src)} failed: {exc}") from None
    if rows != [("ok",)]:
        raise Failed(f"integrity check failed for {os.path.basename(src)}: {str(rows[:3])[:300]}")
    os.chmod(dst, stat.S_IMODE(st.st_mode))
    return st


class _Exact:
    """Read exactly `size` bytes from fd; a file that shrank while we read it is padded with zeros."""

    def __init__(self, fd, size):
        self.fd, self.left, self.short = fd, size, 0

    def read(self, n=-1):
        if self.left <= 0:
            return b""
        n = self.left if n is None or n < 0 else min(n, self.left)
        data = os.read(self.fd, n)
        if not data:
            self.short += n
            data = b"\0" * n
        self.left -= len(data)
        return data


class TreeStats:
    def __init__(self):
        self.files = self.dirs = self.bytes = 0
        self.skipped = {}

    def skip(self, why, rel):
        self.skipped.setdefault(why, []).append(rel)


def tar_tree(root, out, arcroot, skip_top=frozenset()):
    """out = a tar of root as arcroot/...; never follows or stores symlinks, stays on root's
    filesystem, leaves out *.bak-* and the names in skip_top (top level only). Raises Failed."""
    try:
        rst = os.lstat(root)
    except FileNotFoundError:
        raise Failed(f"{root} is missing") from None
    if not stat.S_ISDIR(rst.st_mode):
        raise Failed(f"{root} is not a real directory (a symlink?); refusing to archive it")
    stats = TreeStats()
    with tarfile.open(out, mode="w:", format=tarfile.PAX_FORMAT, encoding="utf-8",
                      errors="surrogateescape") as tf:

        def add_dir(path, arc, st):
            ti = tarfile.TarInfo(arc)
            ti.type, ti.mode, ti.uid, ti.gid, ti.mtime = tarfile.DIRTYPE, stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid, int(st.st_mtime)
            tf.addfile(ti)
            stats.dirs += 1

        def walk(path, rel, top):
            try:
                entries = sorted(os.scandir(path), key=lambda e: e.name)
            except FileNotFoundError:
                stats.skip("vanished", rel or ".")
                return
            for e in entries:
                r = f"{rel}/{e.name}" if rel else e.name
                if BAK_RE.search(e.name):
                    stats.skip("*.bak-*", r)
                    continue
                if top and e.name in skip_top:
                    continue
                try:
                    st = os.lstat(e.path)
                except FileNotFoundError:
                    stats.skip("vanished", r)
                    continue
                if stat.S_ISLNK(st.st_mode):
                    stats.skip("symlink", r)
                    continue
                if st.st_dev != rst.st_dev:
                    stats.skip("other filesystem", r)
                    continue
                if stat.S_ISDIR(st.st_mode):
                    add_dir(e.path, f"{arcroot}/{r}", st)
                    walk(e.path, r, False)
                elif stat.S_ISREG(st.st_mode):
                    try:
                        fd = os.open(e.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOCTTY)
                    except FileNotFoundError:
                        stats.skip("vanished", r)
                        continue
                    except OSError as exc:  # ELOOP: swapped for a symlink since the lstat
                        stats.skip(f"unreadable ({exc.strerror})", r)
                        continue
                    try:
                        fst = os.fstat(fd)
                        if not stat.S_ISREG(fst.st_mode) or fst.st_dev != rst.st_dev:
                            stats.skip("not a regular file", r)
                            continue
                        ti = tarfile.TarInfo(f"{arcroot}/{r}")
                        ti.size, ti.mode, ti.uid, ti.gid, ti.mtime = fst.st_size, stat.S_IMODE(fst.st_mode), fst.st_uid, fst.st_gid, int(fst.st_mtime)
                        reader = _Exact(fd, fst.st_size)
                        tf.addfile(ti, reader)
                        if reader.short:
                            stats.skip("shrank while read (zero-padded)", r)
                        stats.files += 1
                        stats.bytes += fst.st_size
                    finally:
                        os.close(fd)
                else:
                    stats.skip("special file", r)

        add_dir(root, arcroot, rst)
        walk(root, "", True)
    for why, rels in sorted(stats.skipped.items()):
        log(f"{arcroot}: left out {len(rels)} ({why}): {', '.join(rels[:5])}{' ...' if len(rels) > 5 else ''}")
    return stats


def tree_size(root):
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for f in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, f))
            except FileNotFoundError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total


def read_env(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise Refused(f"the stack env file {path} is missing") from None
    except OSError as exc:
        raise Refused(f"the stack env file {path} can't be opened without following a link: {exc.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"the stack env file {path} is not a regular file")
        with os.fdopen(os.dup(fd), "rb") as fh:
            data = fh.read()
    finally:
        os.close(fd)
    if not re.search(rb"(?m)^WEBUI_SECRET_KEY=\S+", data):
        raise Refused(f"the stack env file {path} has no WEBUI_SECRET_KEY line")
    return data, st


def clean_stale_work(out):
    now = time.time()
    with os.scandir(out) as it:
        for e in it:
            if (e.name.startswith(WORK_PREFIX) and e.is_dir(follow_symlinks=False)
                    and now - e.stat(follow_symlinks=False).st_mtime > STALE_WORK_SECONDS):
                shutil.rmtree(e.path)
                log(f"removed a stale work dir {e.name}")


def write_outer(path, srcdir, meta, manifest_text):
    """The outer tar: MEMBERS in order, then MANIFEST.sha256 (last)."""
    with tarfile.open(path, mode="w:", format=tarfile.PAX_FORMAT) as tf:
        for name in MEMBERS:
            full = os.path.join(srcdir, name)
            st = os.stat(full)
            ti = tarfile.TarInfo(name)
            mode, uid, gid = meta[name]
            ti.size, ti.mode, ti.uid, ti.gid, ti.mtime = st.st_size, mode, uid, gid, int(st.st_mtime)
            with open(full, "rb") as fh:
                tf.addfile(ti, fh)
        data = manifest_text.encode("ascii")
        ti = tarfile.TarInfo(MANIFEST)
        ti.size, ti.mode, ti.mtime = len(data), 0o644, int(time.time())
        tf.addfile(ti, io.BytesIO(data))


def stage(args):
    if args.keep < 1:
        raise Refused(f"--keep must be at least 1 (got {args.keep})")
    vol = real_dir(args.volume, "the Open WebUI volume")
    out = real_dir(args.out, "the staging dir")
    try:
        gid = grp.getgrnam(args.group).gr_gid
    except KeyError:
        raise Refused(f"group {args.group} does not exist") from None
    env_data, env_st = read_env(args.env)
    for sub in ("uploads", "vector_db"):
        real_dir(os.path.join(vol, sub), f"the volume's {sub}/")

    lock = take_lock(out)
    try:
        clean_stale_work(out)
        unknown = sorted(n for n in os.listdir(vol) if n not in KNOWN_TOP and not BAK_RE.search(n))
        if unknown:
            log(f"note: not backed up (unknown to this script): {', '.join(unknown)}")

        need = sum(os.path.getsize(p) for p in (os.path.join(vol, "webui.db"), os.path.join(vol, "vector_db", "chroma.sqlite3"))
                   if os.path.isfile(p))
        need += tree_size(os.path.join(vol, "uploads")) + tree_size(os.path.join(vol, "vector_db"))
        sv = os.statvfs(out)
        free = sv.f_bavail * sv.f_frsize
        want = 2 * need + args.min_free_mb * (1 << 20)
        if free < want:
            raise Refused(f"not enough space in {out}: {free >> 20} MiB free, need {want >> 20} MiB")

        now = utcnow()
        stamp, iso = now.strftime(STAMP_FMT), now.strftime(ISO_FMT)
        final = os.path.join(out, f"owui-{stamp}.tar")
        if os.path.lexists(final):
            raise Refused(f"{os.path.basename(final)} already exists")

        work = tempfile.mkdtemp(prefix=WORK_PREFIX, dir=out)
        try:
            m = os.path.join(work, "m")
            os.makedirs(os.path.join(m, "sqlite", "vector_db"))
            meta = {}
            st = sqlite_backup(os.path.join(vol, "webui.db"), os.path.join(m, DB_WEBUI), args.sqlite_deadline)
            meta[DB_WEBUI] = (stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid)
            st = sqlite_backup(os.path.join(vol, "vector_db", "chroma.sqlite3"), os.path.join(m, DB_CHROMA), args.sqlite_deadline)
            meta[DB_CHROMA] = (stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid)
            up = tar_tree(os.path.join(vol, "uploads"), os.path.join(m, UPLOADS_TAR), "uploads")
            meta[UPLOADS_TAR] = (0o600, 0, 0)
            vec = tar_tree(os.path.join(vol, "vector_db"), os.path.join(m, VECTOR_TAR), "vector_db",
                           skip_top=frozenset("chroma.sqlite3" + s for s in ("",) + SIDECARS))
            meta[VECTOR_TAR] = (0o600, 0, 0)
            fd = os.open(os.path.join(m, ENV_MEMBER), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(env_data)
            meta[ENV_MEMBER] = (0o600, env_st.st_uid, env_st.st_gid)

            manifest = "".join(f"{sha256_file(os.path.join(m, n))}  {n}\n" for n in MEMBERS)
            tmp_tar = os.path.join(work, "out.tar")
            write_outer(tmp_tar, m, meta, manifest)
            with open(tmp_tar, "rb") as fh:
                os.fsync(fh.fileno())
            verify_archive(tmp_tar)  # read back: manifest last, every hash right
            os.chown(tmp_tar, -1, gid)
            os.chmod(tmp_tar, 0o640)
            os.rename(tmp_tar, final)
            fsync_dir(out)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        write_atomic(out, "LAST_OK", iso + "\n", 0o640, gid)
        apply_retention(out, args.keep, protect=os.path.basename(final))
        size = os.path.getsize(final)
        log(f"backup ok: {os.path.basename(final)} ({size >> 20} MiB): uploads {up.files} files, "
            f"vector_db {vec.files} files, {len(archives_in(out))} archive(s) kept")
    finally:
        os.close(lock)
    return 0


# ---------------------------------------------------------------------------------------------------
# Backup drive checks (gaming PC; the AI Library backup's checks, §12.17)
# ---------------------------------------------------------------------------------------------------
def findmnt_rows(prog, path, columns):
    res = subprocess.run([prog, "-n", "-r", "-o", columns, "--mountpoint", path],
                         capture_output=True, text=True, check=False)
    if res.returncode != 0:
        return []
    return [line.split(" ") for line in res.stdout.splitlines() if line]


def unescape(field):
    return field.replace("\\x20", " ")


def check_backup_drive(prog, dest, mount, subdir, fstypes, create=False):
    """dest must be exactly <mount>/<subdir> on the real backup drive, and every folder from the mount down
    to dest root-owned and not group/other-writable (with create=True, missing ones are made root 0700, but
    only once the drive itself checked out). Returns the normalized dest."""
    parts = subdir.split("/")
    if subdir.startswith("/") or not subdir or "." in parts or ".." in parts or "" in parts:
        raise Refused(f"--subdir {subdir!r} must be a relative path without '.', '..' or empty parts")
    dparts = dest.split("/")
    if not dest.startswith("/") or "." in dparts or ".." in dparts:
        raise Refused(f"the destination {dest!r} must be an absolute path without '.' or '..'")
    mount_n = real_dir(mount, "the backup drive mountpoint")
    want = posixpath.join(mount_n, subdir)
    if os.path.normpath(dest) != want:
        raise Refused(f"the destination {dest} must be exactly {want}")
    rows = findmnt_rows(prog, mount_n, "TARGET,FSTYPE,SOURCE,FSROOT")
    if not rows:
        raise Refused(f"{mount_n} is not mounted (is the backup drive there?)")
    if fstypes and rows[-1][1] not in fstypes:
        raise Refused(f"{mount_n} is {rows[-1][1]}, expected {' or '.join(fstypes)}")
    # Stacked mounts at the drive path (a sibling bound over it): every one must be the same drive root.
    if len({(r[2], r[3]) for r in rows}) != 1:
        raise Refused(f"{mount_n} has different mounts stacked on it: {[(r[2], unescape(r[3])) for r in rows]}")
    mdev = os.stat(mount_n).st_dev
    path = mount_n
    for part in parts:
        path = posixpath.join(path, part)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            if not create:
                raise Refused(f"{path} is missing") from None
            os.mkdir(path, 0o700)
            fsync_dir(posixpath.dirname(path))
            log(f"created {path} (root 0700)")
            st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):
            raise Refused(f"{path} is not a real directory (a symlink or a file); fix it by hand")
        # Same drive by device, for the destination and every folder between it and the mount.
        if st.st_dev != mdev:
            raise Refused(f"{path} is not on the {mount_n} drive (another filesystem is mounted there)")
        # /mnt/backup itself belongs to the desktop user: a folder below it that isn't root's (or that others
        # can write) could have been swapped in by that user.
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise Refused(f"{path} must be owned by root and not group/other-writable "
                          f"(uid {st.st_uid}, mode {stat.S_IMODE(st.st_mode):04o}); fix it by hand")
        # A mount AT the folder (systemd's ReadWritePaths= self-bind) must be that same folder of the drive.
        here = findmnt_rows(prog, path, "FSROOT")
        if here:
            root = unescape(rows[-1][3]).strip("/")
            expect = posixpath.normpath("/" + posixpath.join(root, posixpath.relpath(path, mount_n)))
            for r in here:
                if posixpath.normpath("/" + unescape(r[0]).strip("/")) != expect:
                    raise Refused(f"{path} has another folder of the drive mounted on it (FSROOT {unescape(r[0])}, want {expect})")
    return real_dir(want, "the destination")


def ensure_private_dir(path):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        os.mkdir(path, 0o700)
        return
    if not stat.S_ISDIR(st.st_mode):
        raise Refused(f"{path} is not a real directory (a symlink or a file); fix it by hand")


# ---------------------------------------------------------------------------------------------------
# Pull (gaming PC)
# ---------------------------------------------------------------------------------------------------
def parse_last_ok(text):
    try:
        return dt.datetime.strptime(text.strip(), ISO_FMT).replace(tzinfo=dt.UTC)
    except ValueError:
        return None


LIST_RE = re.compile(r"^([-dlpscb][-rwxsStT]{9})\s+([\d,]+)\s+\S+\s+\S+\s+(.+)$")


def remote_listing(args, ssh):
    """[(name, size)] of what the pull would fetch (the same filters), from rsync --list-only."""
    cmd = [args.rsync, "--list-only", "--no-h", "--include=/owui-*.tar", "--include=/LAST_OK", "--exclude=*",
           "--timeout=120", "-e", ssh, args.source]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode not in RSYNC_OK:
        raise Failed(f"listing ai-box's archives failed (rsync exited {res.returncode}): {res.stderr.strip()[:200]}")
    out = []
    for line in res.stdout.splitlines():
        m = LIST_RE.match(line)
        if not m:
            raise Failed(f"unexpected listing line from ai-box: {line[:120]!r}")
        if m.group(3) == ".":
            continue
        out.append((m.group(3), int(m.group(2).replace(",", "")), m.group(1)[0]))
    return out


def pull(args):
    if args.keep < 1:
        raise Refused(f"--keep must be at least 1 (got {args.keep})")
    if args.max_remote < 1 or args.max_new < 1:
        raise Refused("--max-remote and --max-new must be at least 1")
    max_size = parse_size(args.max_size)
    for p in (args.key, args.known_hosts):
        if not os.path.isfile(p) or os.path.islink(p):
            raise Refused(f"{p} is missing or not a regular file")
    if any(ch.isspace() for ch in args.key + args.known_hosts + args.ssh):
        raise Refused("the key, known_hosts and ssh paths must not contain whitespace")
    dest = check_backup_drive(args.findmnt, args.dest, args.mount, args.subdir, args.fstype, create=True)
    lock = take_lock(dest)
    try:
        stage_dir = os.path.join(dest, ".incoming")
        ensure_private_dir(stage_dir)
        # rsync's partial temp files (a transfer cut off, e.g. by LimitFSIZE) are excluded by the filters, so
        # --delete-after never removes them: anything in staging that isn't an archive or LAST_OK goes now.
        with os.scandir(stage_dir) as it:
            for e in it:
                if not (ARCHIVE_RE.match(e.name) or e.name == "LAST_OK") and not e.is_dir(follow_symlinks=False):
                    os.unlink(e.path)
                    log(f"removed a leftover from staging: {e.name[:80]}")
        ssh = (f"{args.ssh} -F none -i {args.key} -o BatchMode=yes -o IdentitiesOnly=yes"
               f" -o StrictHostKeyChecking=yes -o UserKnownHostsFile={args.known_hosts}"
               f" -o GlobalKnownHostsFile=/dev/null -o ConnectTimeout=20 -o ServerAliveInterval=30")

        # 1. Bound the transfer before fetching anything (Reviewer X-2): count, sizes, free space.
        listing = remote_listing(args, ssh)
        files = [(n, size) for n, size, typ in listing if n != "LAST_OK" or typ != "-"]
        if len(files) > args.max_remote:
            raise Refused(f"ai-box lists {len(files)} archives, more than --max-remote {args.max_remote}; "
                          "is its retention broken? Nothing fetched")
        big = [n for n, size in files if size > max_size]
        if big or any(size > 4096 for n, size, typ in listing if n == "LAST_OK"):
            raise Refused(f"ai-box offers files over --max-size {args.max_size}: {', '.join(big) or 'LAST_OK'}; nothing fetched")
        need = 0
        for n, size in files:
            have = os.path.join(stage_dir, n)
            if not (os.path.isfile(have) and not os.path.islink(have) and os.path.getsize(have) == size):
                need += size
        sv = os.statvfs(dest)
        free = sv.f_bavail * sv.f_frsize
        if free < need + args.min_free_mb * (1 << 20):
            raise Refused(f"not enough space on the backup drive: {free >> 20} MiB free, the pull needs {need >> 20} MiB "
                          f"plus the {args.min_free_mb} MiB floor; nothing fetched")

        # 2. Fetch (rsync's own --max-size bounds a listing that changed in between).
        cmd = [args.rsync, "-rtp", "--delete-after", "--no-links", "--no-specials", "--no-devices",
               "--chmod=D0700,F0600", "--timeout=600", f"--max-size={args.max_size}",
               "--include=/owui-*.tar", "--include=/LAST_OK", "--exclude=*",
               "-e", ssh, args.source, stage_dir + "/"]
        log("running " + " ".join(cmd[:-4]) + " ... " + " ".join(cmd[-2:]))
        rc = subprocess.run(cmd, check=False).returncode
        if rc not in RSYNC_OK:
            raise Failed(f"rsync exited {rc}; {dest} left as it was")
        if rc == 24:
            log("note: a file vanished on ai-box during the transfer (its rotation)")

        # The listing was ai-box's word; count what actually arrived (it may have changed in between).
        arrived = [e for e in os.listdir(stage_dir) if e != "LAST_OK"]
        if len(arrived) > args.max_remote:
            for e in arrived:
                if not os.path.lexists(os.path.join(dest, e)):
                    os.unlink(os.path.join(stage_dir, e))
            raise Failed(f"ai-box sent {len(arrived)} files, more than --max-remote {args.max_remote} (it listed "
                         f"{len(files)}); promoted none, retention skipped")
        staged = archives_in(stage_dir)
        if not staged:
            raise Failed(f"no archives on ai-box yet; {dest} left as it was")
        newest, newest_ok, rejected, promoted = staged[-1], False, [], []

        # 3. Future-dated names never get in (Reviewer X-1): they would outrank every genuine copy.
        ahead = [n for n in staged if future_dated(n, args.max_future_minutes)]
        for name in ahead:
            log(f"REJECTED {name}: dated more than {args.max_future_minutes} min ahead of this clock")
            rejected.append(name)
            os.unlink(os.path.join(stage_dir, name))
        new = [n for n in staged if n not in ahead and not os.path.lexists(os.path.join(dest, n))]
        if len(new) > args.max_new:
            raise Failed(f"ai-box offers {len(new)} new archives, more than --max-new {args.max_new}; promoted none "
                         f"(a normal day brings 1; a week off at most ai-box's keep)")

        # 4. Verify and promote.
        for name in staged:
            if name in ahead:
                continue
            src, dst = os.path.join(stage_dir, name), os.path.join(dest, name)
            if os.path.lexists(dst):
                if os.path.islink(dst) or not os.path.isfile(dst):
                    raise Refused(f"{dst} is not a regular file; fix it by hand")
                newest_ok |= name == newest  # verified when it was promoted
                continue
            try:
                verify_archive(src)
            except Failed as exc:
                log(f"REJECTED {exc}")
                rejected.append(name)
                os.unlink(src)  # fetched again next run
                continue
            tmp = os.path.join(dest, f".{name}.tmp")
            if os.path.lexists(tmp):
                os.unlink(tmp)
            os.link(src, tmp)
            os.rename(tmp, dst)
            promoted.append(name)
            newest_ok |= name == newest
        if promoted:
            fsync_dir(dest)
            log(f"verified and kept: {', '.join(promoted)}")

        kept_ahead = [n for n in archives_in(dest) if future_dated(n, args.max_future_minutes)]

        last_ok_path = os.path.join(stage_dir, "LAST_OK")
        try:
            with open(last_ok_path, encoding="ascii", errors="replace") as fh:
                last_ok_text = fh.read(64)
        except FileNotFoundError:
            last_ok_text = ""
        if newest_ok:
            if last_ok_text:
                write_atomic(dest, "LAST_OK", last_ok_text if last_ok_text.endswith("\n") else last_ok_text + "\n", 0o600)
            write_atomic(dest, ".last_pull", utcnow().strftime(ISO_FMT) + "\n", 0o600)
        # 5. Every failure is decided before retention: retention runs only after a fully clean run (Reviewer
        #    X-1b: a failing run that still rotated could be repeated by Restart=on-failure to empty the history).
        if rejected:
            raise Failed(f"{len(rejected)} archive(s) rejected and not kept: {', '.join(rejected)}; retention skipped")
        if kept_ahead:
            raise Failed(f"future-dated archive(s) in {dest}: {', '.join(kept_ahead)}; remove by hand; retention skipped")
        last_ok = parse_last_ok(last_ok_text)
        if last_ok is None:
            raise Failed(f"ai-box's LAST_OK is missing or not a timestamp ({last_ok_text.strip()[:40]!r})")
        age_h = (utcnow() - last_ok).total_seconds() / 3600
        if age_h < -1:
            raise Failed(f"ai-box's LAST_OK ({last_ok_text.strip()}) is in the future: check the clocks")
        if age_h > args.max_age_hours:
            raise Failed(f"ai-box's last good backup ({last_ok_text.strip()}) is {age_h:.0f} h old "
                         f"(limit {args.max_age_hours} h); retention skipped. Is ai-openwebui-backup.timer running?")
        # 6. Retention: only now, only after rsync exit 0, and never an archive dated within the last --keep days.
        if rc == 0:
            apply_retention(dest, args.keep, min_age_days=args.keep)
        else:
            log("retention skipped this run (rsync exit 24: ai-box rotated during the transfer)")
        log(f"pull ok: newest {newest}, {len(archives_in(dest))} archive(s) kept, ai-box LAST_OK {last_ok_text.strip()}")
    finally:
        os.close(lock)
    return 0


# ---------------------------------------------------------------------------------------------------
# restore-check
# ---------------------------------------------------------------------------------------------------
def count(conn, table):
    try:
        return conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    except sqlite3.Error:
        return "n/a"


def check_inner_tar(path, arcroot):
    files = total = 0
    with tarfile.open(path, mode="r:") as tf:
        for m in tf:
            name = m.name
            if name.startswith("/") or ".." in name.split("/") or (name != arcroot and not name.startswith(arcroot + "/")):
                raise Failed(f"{os.path.basename(path)}: unsafe member name {name!r}")
            if m.isdir():
                continue
            if not m.isfile():
                raise Failed(f"{os.path.basename(path)}: member {name!r} is not a file or directory")
            fh = tf.extractfile(m)
            while fh.read(CHUNK):
                pass
            files += 1
            total += m.size
    return files, total


def restore_check(args):
    directory = real_dir(args.dir, "the archive dir")
    if args.mount:
        check_backup_drive(args.findmnt, directory, args.mount, args.subdir, args.fstype)
    if args.archive:
        if "/" in args.archive or args.archive in (".", ".."):
            raise Refused(f"--archive {args.archive!r} must be a file name in --dir")
        names = [args.archive]
    else:
        names = archives_in(directory)
    if not names:
        raise Failed(f"no archives in {directory}")
    newest = os.path.join(directory, names[-1])
    verify_archive(newest)
    log(f"{names[-1]}: manifest verified")
    tmp = tempfile.mkdtemp(prefix=".restore-check.", dir=args.tmpdir or directory)
    try:
        with tarfile.open(newest, mode="r:") as tf:
            for m in tf:
                if m.name not in MEMBERS:
                    continue
                target = os.path.join(tmp, m.name)
                os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
                with tf.extractfile(m) as src, open(target, "xb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK)
        for member in (DB_WEBUI, DB_CHROMA):
            path = os.path.join(tmp, member)
            conn = sqlite3.connect(f"file:{urllib.parse.quote(path, safe='/')}?mode=ro&immutable=1", uri=True)
            try:
                rows = conn.execute("PRAGMA integrity_check").fetchall()
                if rows != [("ok",)]:
                    raise Failed(f"{member}: integrity_check: {str(rows[:3])[:300]}")
                if member == DB_WEBUI:
                    detail = f"users {count(conn, 'user')}, chats {count(conn, 'chat')}, knowledge {count(conn, 'knowledge')}"
                else:
                    detail = f"collections {count(conn, 'collections')}, embeddings {count(conn, 'embeddings')}"
            finally:
                conn.close()
            log(f"{member}: integrity_check ok ({detail})")
        for member, root in ((UPLOADS_TAR, "uploads"), (VECTOR_TAR, "vector_db")):
            files, total = check_inner_tar(os.path.join(tmp, member), root)
            log(f"{member}: {files} files, {total >> 20} MiB, all readable")
        with open(os.path.join(tmp, ENV_MEMBER), "rb") as fh:
            if not re.search(rb"(?m)^WEBUI_SECRET_KEY=\S+", fh.read()):
                raise Failed(f"{ENV_MEMBER} has no WEBUI_SECRET_KEY")
        log(f"{ENV_MEMBER}: WEBUI_SECRET_KEY present")
        log(f"restore-check ok: {names[-1]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


# ---------------------------------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Open WebUI backup (docs/CONTRACTS.md §12.28)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stage", help="ai-box: write one archive, LAST_OK, rotate")
    s.add_argument("--volume", required=True, help="the open-webui volume's host path (_data)")
    s.add_argument("--env", required=True, help="the ai stack's .env")
    s.add_argument("--out", required=True, help="the staging dir")
    s.add_argument("--group", required=True, help="group that may read the archives (the pull account)")
    s.add_argument("--keep", type=int, default=3)
    s.add_argument("--min-free-mb", type=int, default=1024)
    s.add_argument("--sqlite-deadline", type=int, default=900, help="seconds one database copy may take")

    drive = argparse.ArgumentParser(add_help=False)
    drive.add_argument("--mount", default="", help="the backup drive's mountpoint")
    drive.add_argument("--subdir", default="ai-box/openwebui")
    drive.add_argument("--fstype", action="append", default=[])
    drive.add_argument("--findmnt", default="findmnt")

    p = sub.add_parser("pull", parents=[drive], help="gaming PC: fetch, verify, keep")
    p.add_argument("--source", required=True, help="user@host: (rrsync limits the path)")
    p.add_argument("--key", required=True)
    p.add_argument("--known-hosts", required=True)
    p.add_argument("--dest", required=True)
    p.add_argument("--keep", type=int, default=14)
    p.add_argument("--max-age-hours", type=int, default=48)
    p.add_argument("--max-remote", type=int, default=5, help="refuse when ai-box lists more archives")
    p.add_argument("--max-new", type=int, default=5, help="promote none when more archives are new")
    p.add_argument("--max-size", default="8G", help="largest archive accepted (rsync size syntax)")
    p.add_argument("--min-free-mb", type=int, default=20480, help="free space the drive keeps after a pull")
    p.add_argument("--max-future-minutes", type=int, default=60, help="clock slack for archive names")
    p.add_argument("--rsync", default="rsync")
    p.add_argument("--ssh", default="ssh")

    v = sub.add_parser("verify", help="verify archives")
    v.add_argument("archives", nargs="+")

    r = sub.add_parser("restore-check", parents=[drive], help="extract the newest archive and check it")
    r.add_argument("--dir", required=True)
    r.add_argument("--tmpdir", default="")
    r.add_argument("--archive", default="", help="check this archive (a file name in --dir), not the newest")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "stage":
            return stage(args)
        if args.cmd == "pull":
            if not args.mount:
                raise Refused("pull needs --mount (the backup drive)")
            return pull(args)
        if args.cmd == "restore-check":
            return restore_check(args)
        bad = 0
        for a in args.archives:
            try:
                n = len(verify_archive(a))
                log(f"{os.path.basename(a)}: verified {n} members")
            except Failed as exc:
                log(f"REJECTED {exc}")
                bad += 1
        return 1 if bad else 0
    except Refused as exc:
        log(f"REFUSED: {exc}")
        return 2
    except Failed as exc:
        log(f"FAILED: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
