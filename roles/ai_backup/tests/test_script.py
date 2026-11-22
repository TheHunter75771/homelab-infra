"""Unit tests for files/ai_backup.py (docs/CONTRACTS.md §12.28). They bind-mount the fake Open WebUI
volume read-only (as the unit's ProtectSystem=strict does) and mount tmpfs "backup drives", so they run
inside a private user + mount namespace (tests/run.sh: `unshare -rm`); nothing is mounted or written
outside the scratch dir.
    unshare -rm python3 -m unittest test_script      (from tests/; run.sh does this)

Covers: a live WAL database written to during the copy, an idle WAL database on a read-only mount, a
corrupt database and a non-database (no archive, LAST_OK kept), a missing volume/uploads/db, symlinks
(volume, uploads, db, env, entries inside uploads), the exclusions (cache/, *.bak-*, special files),
manifest verification (manifest first, cut short, a flipped byte, an extra member, a link member),
retention on both sides (never the newest; foreign files kept), the pull's backup-drive refusals
(unmounted, wrong fs, wrong path, symlink, another fs, a sibling folder bound on, a stacked mount),
rejected archives, a stale/missing/garbled LAST_OK, rsync failures, the ssh options, the lock, and
restore-check (ok, and a manifest-valid archive whose database is corrupt).
"""
import datetime as dt
import hashlib
import importlib.util
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "files", "ai_backup.py")
CANARY_KEY = "WEBUI_SECRET_KEY=test-canary-0123456789abcdef"


def load_script():
    spec = importlib.util.spec_from_file_location("ai_backup", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AB = load_script()


def in_private_userns():
    try:
        with open("/proc/self/uid_map", encoding="ascii") as fh:
            first = fh.read().split()
    except OSError:
        return False
    return os.getuid() == 0 and first[:3] != ["0", "0", "4294967295"]


def write(path, data, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(data, bytes) else "w") as fh:
        fh.write(data)
    os.chmod(path, mode)


def run(*argv):
    return subprocess.run([sys.executable, "-I", "-B", SCRIPT, *argv], capture_output=True, text=True, check=False)


def mount(*args):
    subprocess.run(["mount", *args], check=True)


def make_wal_db(path, rows=200):
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=wal")
    c.execute("CREATE TABLE chat(id INTEGER PRIMARY KEY, body TEXT)")
    c.execute("CREATE TABLE user(id INTEGER PRIMARY KEY)")
    c.execute("CREATE INDEX chat_body ON chat(body)")
    c.executemany("INSERT INTO chat(body) VALUES (?)", [(f"msg {i}",) for i in range(rows)])
    c.commit()
    return c


def make_rollback_db(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE collections(id TEXT)")
    c.execute("CREATE TABLE embeddings(id INTEGER PRIMARY KEY, v BLOB)")
    c.executemany("INSERT INTO embeddings(v) VALUES (?)", [(os.urandom(64),) for _ in range(300)])
    c.commit()
    c.close()


def corrupt_db(path):
    """A database the backup API copies fine but integrity_check fails: index ia points at ib's pages."""
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE a(x); CREATE INDEX ia ON a(x); CREATE TABLE b(y); CREATE INDEX ib ON b(y);")
    c.executemany("INSERT INTO a VALUES (?)", [(i,) for i in range(500)])
    c.executemany("INSERT INTO b VALUES (?)", [(i * 7,) for i in range(300)])
    c.commit()
    roots = dict(c.execute("SELECT name, rootpage FROM sqlite_master WHERE type='index'").fetchall())
    c.execute("PRAGMA writable_schema=ON")
    c.execute("UPDATE sqlite_master SET rootpage=? WHERE name='ia'", (roots["ib"],))
    c.commit()
    c.close()


def build_archive(path, members, manifest_first=False, manifest=None):
    """A hand-made archive: members = [(name, bytes)]; the manifest is computed unless given."""
    text = manifest if manifest is not None else "".join(
        f"{hashlib.sha256(d).hexdigest()}  {n}\n" for n, d in members)
    entries = list(members)
    entry = ("MANIFEST.sha256", text.encode())
    entries = [entry] + entries if manifest_first else entries + [entry]
    with tarfile.open(path, "w:") as tf:
        for name, data in entries:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))


def stamp(hours_ago):
    return (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours_ago)).strftime("%Y%m%dT%H%M%SZ")


def iso(hours_ago):
    return (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not in_private_userns():
            raise RuntimeError("run inside a private user+mount namespace: tests/run.sh (unshare -rm)")

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ai-backup-test.")
        self.vol = os.path.join(self.base, "vol")  # the writable volume (the container's view)
        self.ro = os.path.join(self.base, "ro")  # what the unit sees: the same, read-only
        self.out = os.path.join(self.base, "staging")
        self.env = os.path.join(self.base, "stack", ".env")
        os.makedirs(self.out, mode=0o750)
        self.writer = None
        self.build_volume()
        os.mkdir(self.ro)
        mount("--bind", self.vol, self.ro)
        mount("-o", "remount,bind,ro", self.ro)

    def build_volume(self):
        v = self.vol
        os.makedirs(os.path.join(v, "vector_db"))
        self.writer = make_wal_db(os.path.join(v, "webui.db"))  # kept open: -wal and -shm exist
        make_rollback_db(os.path.join(v, "vector_db", "chroma.sqlite3"))
        write(os.path.join(v, "vector_db", "1111-aaaa", "data_level0.bin"), os.urandom(4096))
        write(os.path.join(v, "vector_db", "1111-aaaa", "header.bin"), b"hdr")
        write(os.path.join(v, "uploads", "report.pdf"), b"%PDF-1.7 fake\n")
        write(os.path.join(v, "uploads", "nested", "notes.txt"), "notes\n", 0o600)
        write(os.path.join(v, "uploads", "old.txt.bak-1"), "old\n")
        os.symlink("/etc/hostname", os.path.join(v, "uploads", "escape-link"))
        os.mkfifo(os.path.join(v, "uploads", "a-fifo"))
        write(os.path.join(v, "cache", "embedding", "models", "model.bin"), os.urandom(2048))
        write(os.path.join(v, "webui.db.bak-20261027-eml"), b"stale copy")
        write(os.path.join(v, "surprise.json"), "{}")
        write(self.env, CANARY_KEY + "\n", 0o600)

    def tearDown(self):
        if self.writer is not None:
            self.writer.close()
        res = subprocess.run(["findmnt", "-rn", "-o", "TARGET"], capture_output=True, text=True, check=False)
        for target in sorted((t for t in res.stdout.split() if t.startswith(self.base + "/")), reverse=True):
            subprocess.run(["umount", "-l", target], check=False)
        shutil.rmtree(self.base, ignore_errors=True)

    def stage(self, *extra, volume=None, env=None, out=None, keep="3"):
        return run("stage", "--volume", volume or self.ro, "--env", env or self.env, "--out", out or self.out,
                   "--group", "root", "--keep", keep, "--min-free-mb", "1", *extra)

    def ok(self, res):
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return res

    def archives(self, d=None):
        return AB.archives_in(d or self.out)

    def no_work_left(self):
        self.assertEqual([n for n in os.listdir(self.out) if n.startswith(".work.")], [])


# ------------------------------------------------------------------------------------------------------
class Stage(Base):
    def test_live_wal_db_written_during_the_copy_and_the_archive_contents(self):
        stop = threading.Event()
        written = []

        def keep_writing():
            c = sqlite3.connect(os.path.join(self.vol, "webui.db"), timeout=30)
            i = 0
            while not stop.is_set():
                c.execute("INSERT INTO chat(body) VALUES (?)", (f"live {i}",))
                c.commit()
                i += 1
            written.append(i)
            c.close()

        t = threading.Thread(target=keep_writing)
        t.start()
        try:
            res = self.ok(self.stage())
        finally:
            stop.set()
            t.join()
        self.assertGreater(written[0], 0)
        names = self.archives()
        self.assertEqual(len(names), 1)
        arc = os.path.join(self.out, names[0])
        self.assertEqual(os.stat(arc).st_mode & 0o777, 0o640)
        AB.verify_archive(arc)
        with tarfile.open(arc) as tf:
            members = tf.getnames()
            self.assertEqual(members, list(AB.MEMBERS) + ["MANIFEST.sha256"])  # manifest LAST
            x = os.path.join(self.base, "x")
            for m in tf:
                target = os.path.join(x, m.name)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as fh:
                    fh.write(tf.extractfile(m).read())
        c = sqlite3.connect(os.path.join(x, "sqlite", "webui.db"))
        self.assertEqual(c.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        self.assertGreaterEqual(c.execute("SELECT count(*) FROM chat").fetchone()[0], 200)
        c.close()
        c = sqlite3.connect(os.path.join(x, "sqlite", "vector_db", "chroma.sqlite3"))
        self.assertEqual(c.execute("SELECT count(*) FROM embeddings").fetchone()[0], 300)
        c.close()
        with tarfile.open(os.path.join(x, "uploads.tar")) as tf:
            up = sorted(tf.getnames())
        self.assertEqual(up, ["uploads", "uploads/nested", "uploads/nested/notes.txt", "uploads/report.pdf"])
        with tarfile.open(os.path.join(x, "vector_db.tar")) as tf:
            vec = sorted(tf.getnames())
        self.assertEqual(vec, ["vector_db", "vector_db/1111-aaaa", "vector_db/1111-aaaa/data_level0.bin",
                               "vector_db/1111-aaaa/header.bin"])
        with open(os.path.join(x, "stack.env")) as fh:
            self.assertEqual(fh.read(), CANARY_KEY + "\n")
        # Nothing from cache/ or any *.bak-*, anywhere.
        everything = members + up + vec
        self.assertFalse([n for n in everything if "cache" in n.split("/")], everything)
        self.assertFalse([n for n in everything if ".bak-" in n], everything)
        self.assertIn("left out 1 (symlink): escape-link", res.stdout)
        self.assertIn("left out 1 (special file): a-fifo", res.stdout)
        self.assertIn("not backed up (unknown to this script): surprise.json", res.stdout)
        with open(os.path.join(self.out, "LAST_OK")) as fh:
            self.assertIsNotNone(AB.parse_last_ok(fh.read()))
        self.assertEqual(os.stat(os.path.join(self.out, "LAST_OK")).st_mode & 0o777, 0o640)
        self.no_work_left()

    def test_idle_wal_db_on_a_read_only_mount(self):
        self.writer.close()  # the last connection: SQLite removes -wal and -shm
        self.writer = None
        self.assertFalse(os.path.exists(os.path.join(self.vol, "webui.db-wal")))
        self.ok(self.stage())
        AB.verify_archive(os.path.join(self.out, self.archives()[0]))

    def assert_failed_cleanly(self, res, why, rc=1):
        self.assertEqual(res.returncode, rc, res.stdout + res.stderr)
        self.assertIn(why, res.stdout)
        self.assertEqual(self.archives(), ["owui-20200101T000000Z.tar"])
        with open(os.path.join(self.out, "LAST_OK")) as fh:
            self.assertEqual(fh.read(), "2020-01-01T00:00:00Z\n")
        self.no_work_left()

    def seed_previous(self):
        write(os.path.join(self.out, "owui-20200101T000000Z.tar"), b"previous")
        write(os.path.join(self.out, "LAST_OK"), "2020-01-01T00:00:00Z\n")

    def test_corrupt_db_fails_the_run_and_publishes_nothing(self):
        self.seed_previous()
        os.unlink(os.path.join(self.vol, "vector_db", "chroma.sqlite3"))
        corrupt_db(os.path.join(self.vol, "vector_db", "chroma.sqlite3"))
        self.assert_failed_cleanly(self.stage(), "integrity check failed for chroma.sqlite3")

    def test_not_a_database(self):
        self.seed_previous()
        write(os.path.join(self.vol, "vector_db", "chroma.sqlite3"), b"garbage" * 100)
        self.assert_failed_cleanly(self.stage(), "is not an SQLite database")

    def test_missing_volume_dir(self):
        self.seed_previous()
        self.assert_failed_cleanly(self.stage(volume=os.path.join(self.base, "nope")), "is missing", rc=2)

    def test_missing_uploads_dir(self):
        self.seed_previous()
        shutil.rmtree(os.path.join(self.vol, "uploads"))
        self.assert_failed_cleanly(self.stage(), "uploads/ ", rc=2)

    def test_missing_db(self):
        self.seed_previous()
        os.unlink(os.path.join(self.vol, "vector_db", "chroma.sqlite3"))
        self.assert_failed_cleanly(self.stage(), "is missing")

    def test_volume_through_a_symlink(self):
        self.seed_previous()
        link = os.path.join(self.base, "vol-link")
        os.symlink(self.ro, link)
        self.assert_failed_cleanly(self.stage(volume=link), "goes through a symlink", rc=2)

    def test_uploads_is_a_symlink(self):
        self.seed_previous()
        os.rename(os.path.join(self.vol, "uploads"), os.path.join(self.base, "elsewhere"))
        os.symlink(os.path.join(self.base, "elsewhere"), os.path.join(self.vol, "uploads"))
        self.assert_failed_cleanly(self.stage(), "goes through a symlink", rc=2)

    def test_db_is_a_symlink(self):
        self.seed_previous()
        real = os.path.join(self.base, "other.db")
        make_rollback_db(real)
        os.unlink(os.path.join(self.vol, "vector_db", "chroma.sqlite3"))
        os.symlink(real, os.path.join(self.vol, "vector_db", "chroma.sqlite3"))
        self.assert_failed_cleanly(self.stage(), "is not a regular file (a symlink?)")

    def test_env_is_a_symlink_or_has_no_key(self):
        self.seed_previous()
        link = os.path.join(self.base, "env-link")
        os.symlink(self.env, link)
        self.assert_failed_cleanly(self.stage(env=link), "without following a link", rc=2)
        write(self.env, "OTHER=1\n", 0o600)
        self.assert_failed_cleanly(self.stage(), "has no WEBUI_SECRET_KEY", rc=2)

    def test_lock_held_and_space_refusals(self):
        self.seed_previous()
        fd = AB.take_lock(self.out)
        try:
            self.assert_failed_cleanly(self.stage(), "holds", rc=2)
        finally:
            os.close(fd)
        res = run("stage", "--volume", self.ro, "--env", self.env, "--out", self.out, "--group", "root",
                  "--min-free-mb", str(1 << 30))
        self.assert_failed_cleanly(res, "not enough space", rc=2)

    def test_retention_keeps_the_newest_three_and_foreign_files(self):
        old = [f"owui-{stamp(h)}.tar" for h in (96, 72, 48, 24)]
        for n in old:
            write(os.path.join(self.out, n), b"old")
        write(os.path.join(self.out, "notes.txt"), b"mine")
        write(os.path.join(self.out, ".owui-x.tar.partial"), b"tmp")
        res = self.ok(self.stage())
        names = self.archives()
        self.assertEqual(len(names), 3)
        self.assertEqual(names[:2], old[2:])  # the two newest old ones
        self.assertNotIn(names[-1], old)  # the one just written is kept
        self.assertIn(f"rotated out {old[0]}", res.stdout)
        AB.verify_archive(os.path.join(self.out, names[-1]))
        self.assertTrue(os.path.exists(os.path.join(self.out, "notes.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.out, ".owui-x.tar.partial")))


class StageGuards(Base):
    """Reviewer X-3 survivors on the stage side."""

    def test_another_filesystem_inside_uploads_is_left_out(self):
        other = os.path.join(self.ro, "uploads", "nested")  # mounted over, in the unit's (read-only) view
        mount("-t", "tmpfs", "tmpfs", other)
        write(os.path.join(other, "foreign.bin"), b"not ours")
        res = self.ok(self.stage())
        self.assertIn("left out 1 (other filesystem): nested", res.stdout)
        with tarfile.open(os.path.join(self.out, self.archives()[0])) as tf:
            inner = io.BytesIO(tf.extractfile(AB.UPLOADS_TAR).read())
        with tarfile.open(fileobj=inner) as tf:
            self.assertFalse([n for n in tf.getnames() if n.startswith("uploads/nested")])

    def test_the_stage_reads_its_archive_back_before_publishing(self):
        write(os.path.join(self.out, "LAST_OK"), "2020-01-01T00:00:00Z\n")
        real = AB.write_outer

        def broken(path, srcdir, meta, manifest_text):
            real(path, srcdir, meta, manifest_text)
            with open(path, "r+b") as fh:  # one flipped byte inside the first member's data
                fh.seek(2000)
                byte = fh.read(1)
                fh.seek(2000)
                fh.write(bytes([byte[0] ^ 0xFF]))

        with mock.patch.object(AB, "write_outer", broken):
            rc = AB.main(["stage", "--volume", self.ro, "--env", self.env, "--out", self.out, "--group", "root",
                          "--min-free-mb", "1"])
        self.assertEqual(rc, 1)
        self.assertEqual(self.archives(), [])
        with open(os.path.join(self.out, "LAST_OK")) as fh:
            self.assertEqual(fh.read(), "2020-01-01T00:00:00Z\n")
        self.no_work_left()

    def test_an_idle_wal_db_opened_by_a_writer_mid_copy_is_copied_again(self):
        self.writer.close()  # idle: no -wal/-shm, so the first attempt opens it immutable
        self.writer = None
        db = os.path.join(self.vol, "webui.db")
        real_connect = sqlite3.connect
        state = {"fired": False, "late": None}

        class Source:
            def __init__(self, conn):
                self.conn = conn

            def backup(self, target, **kw):
                if not state["fired"]:  # Open WebUI starts and writes while the copy runs
                    state["fired"] = True
                    state["late"] = real_connect(db)
                    state["late"].execute("INSERT INTO chat(body) VALUES ('written mid-copy')")
                    state["late"].commit()
                return self.conn.backup(target, **kw)

            def close(self):
                self.conn.close()

        def connect(target, *a, **kw):
            conn = real_connect(target, *a, **kw)
            return Source(conn) if kw.get("uri") and "/vol/webui.db?" in str(target) else conn

        out = os.path.join(self.base, "copy.db")
        try:
            with mock.patch.object(AB.sqlite3, "connect", side_effect=connect), \
                    mock.patch.object(AB, "log") as logged:
                AB.sqlite_backup(db, out)
        finally:
            if state["late"] is not None:
                state["late"].close()
        self.assertTrue(state["fired"])
        self.assertIn("retrying", " ".join(str(c) for c in logged.call_args_list))
        c = real_connect(out)
        self.assertEqual(c.execute("SELECT count(*) FROM chat WHERE body='written mid-copy'").fetchone()[0], 1)
        c.close()


class Retention(unittest.TestCase):
    names = tuple(f"owui-2026100{d}T034500Z.tar" for d in range(1, 8))

    def test_expired_never_includes_the_newest(self):
        shuffled = [*self.names[3:], *self.names[:3], "LAST_OK", "owui-garbage.tar"]
        self.assertEqual(AB.expired(shuffled, 3), list(self.names[:4]))
        self.assertEqual(AB.expired(self.names, 1), list(self.names[:-1]))
        self.assertEqual(AB.expired(self.names, 7), [])
        self.assertEqual(AB.expired(self.names, 14), [])
        for keep in range(1, 9):
            self.assertNotIn(self.names[-1], AB.expired(self.names, keep))
        with self.assertRaises(AB.Refused):
            AB.expired(self.names, 0)

    def test_expired_with_an_age_floor_spares_recent_names(self):
        now = dt.datetime(2026, 10, 8, 4, 0, tzinfo=dt.UTC)
        # 7 archives, 1..7 Oct; keep 3 by count, but only those dated more than 3 days before 8 Oct 04:00 go.
        self.assertEqual(AB.expired(self.names, 3, min_age_days=3, now=now), list(self.names[:4]))
        self.assertEqual(AB.expired(self.names, 3, min_age_days=5, now=now), list(self.names[:3]))  # 3 Oct 03:45 < 3 Oct 04:00
        self.assertEqual(AB.expired(self.names, 1, min_age_days=14, now=now), [])
        self.assertEqual(AB.expired(self.names, 3, min_age_days=0, now=now), list(self.names[:4]))


# ------------------------------------------------------------------------------------------------------
class Verify(Base):
    def good_members(self):
        return [(n, f"data of {n}".encode()) for n in AB.MEMBERS]

    def rejected(self, path, why):
        with self.assertRaises(AB.Failed) as cm:
            AB.verify_archive(path)
        self.assertIn(why, str(cm.exception))
        res = run("verify", path)
        self.assertEqual(res.returncode, 1, res.stdout)

    def test_good_archive(self):
        p = os.path.join(self.base, "good.tar")
        build_archive(p, self.good_members())
        self.assertEqual(len(AB.verify_archive(p)), len(AB.MEMBERS))
        self.ok(run("verify", p))

    def test_manifest_first_is_rejected(self):
        p = os.path.join(self.base, "first.tar")
        build_archive(p, self.good_members(), manifest_first=True)
        self.rejected(p, "is not the last member")

    def test_cut_short_at_a_member_boundary(self):
        p = os.path.join(self.base, "good.tar")
        build_archive(p, self.good_members())
        with tarfile.open(p) as tf:
            offset = tf.getmember("MANIFEST.sha256").offset
        cut = os.path.join(self.base, "cut.tar")
        with open(p, "rb") as src, open(cut, "wb") as dst:
            dst.write(src.read(offset))
        self.rejected(cut, "no MANIFEST.sha256")

    def test_cut_short_mid_member(self):
        p = os.path.join(self.base, "good.tar")
        build_archive(p, [(n, os.urandom(50000)) for n in AB.MEMBERS])
        cut = os.path.join(self.base, "cut.tar")
        with open(p, "rb") as src, open(cut, "wb") as dst:
            dst.write(src.read(70000))
        self.rejected(cut, "")

    def test_flipped_byte_is_rejected(self):
        p = os.path.join(self.base, "good.tar")
        build_archive(p, [(n, b"A" * 2000) for n in AB.MEMBERS])
        with tarfile.open(p) as tf:
            off = tf.getmember(AB.UPLOADS_TAR).offset_data
        with open(p, "r+b") as fh:
            fh.seek(off + 10)
            fh.write(b"B")
        self.rejected(p, "bad hash ['uploads.tar']")

    def test_missing_extra_and_odd_members(self):
        p = os.path.join(self.base, "missing.tar")
        build_archive(p, self.good_members()[:-1])
        self.rejected(p, "missing members ['stack.env']")
        p = os.path.join(self.base, "extra.tar")
        build_archive(p, self.good_members() + [("cache/x", b"x")])
        self.rejected(p, "unexpected member 'cache/x'")
        p = os.path.join(self.base, "unlisted.tar")
        members = self.good_members()
        build_archive(p, members, manifest="".join(f"{hashlib.sha256(d).hexdigest()}  {n}\n" for n, d in members[1:]))
        self.rejected(p, "unlisted ['sqlite/webui.db']")
        p = os.path.join(self.base, "link.tar")
        with tarfile.open(p, "w:") as tf:
            ti = tarfile.TarInfo(AB.ENV_MEMBER)
            ti.type, ti.linkname = tarfile.SYMTYPE, "/etc/shadow"
            tf.addfile(ti)
        self.rejected(p, "is not a regular file")


# ------------------------------------------------------------------------------------------------------
FAKE_SSH = """#!/bin/sh
# Stands in for ssh + rrsync -ro on ai-box: records its argv, then runs the remote rsync command in
# $FAKE_REMOTE (the "staging dir" of ai-box), refusing anything that isn't a --sender.
printf '%s\\n' "$*" >> "$FAKE_SSH_LOG"
[ -n "$FAKE_SSH_FAIL" ] && exit 255
while [ "$#" -gt 0 ] && [ "$1" != rsync ]; do shift; done
case " $* " in *" --sender "*) ;; *) echo "fake rrsync: not a read" >&2; exit 1 ;; esac
cd "$FAKE_REMOTE" || exit 1
# FAKE_AFTER_LIST: a file name ai-box adds right after answering the listing (it lies about what it will send).
case " $* " in *" --list-only "*) if [ -n "$FAKE_AFTER_LIST" ]; then
  "$@"; rc=$?
  for n in $FAKE_AFTER_LIST; do cp "$FAKE_SAMPLE" "$FAKE_REMOTE/$n"; done
  exit $rc
fi ;; esac
exec "$@"
"""

FAKE_RSYNC = """#!/bin/sh
# The real rsync, with its argv recorded (one line per call).
printf '%s\\n' "$*" >> "$FAKE_RSYNC_LOG"
case " $* " in *" --list-only "*) exec rsync "$@" ;; esac
# A fetch: FAKE_RSYNC_RC=24 stands in for "a file vanished on ai-box during the transfer".
rsync "$@" || exit $?
exit "${FAKE_RSYNC_RC:-0}"
"""


class Pull(Base):
    def setUp(self):
        super().setUp()
        self.drive = os.path.join(self.base, "drive")
        os.mkdir(self.drive)
        mount("-t", "tmpfs", "tmpfs", self.drive)
        self.dest = os.path.join(self.drive, "ai-box", "openwebui")
        os.makedirs(self.dest, mode=0o700)
        write(os.path.join(self.drive, "restic", "blob"), b"precious")
        self.remote = os.path.join(self.base, "remote")
        os.mkdir(self.remote)
        self.ssh = os.path.join(self.base, "fake-ssh")
        write(self.ssh, FAKE_SSH, 0o755)
        self.ssh_log = os.path.join(self.base, "ssh.log")
        self.rsync = os.path.join(self.base, "fake-rsync")
        write(self.rsync, FAKE_RSYNC, 0o755)
        self.rsync_log = os.path.join(self.base, "rsync.log")
        os.environ.update(FAKE_REMOTE=self.remote, FAKE_SSH_LOG=self.ssh_log, FAKE_RSYNC_LOG=self.rsync_log)
        os.environ.pop("FAKE_SSH_FAIL", None)
        os.environ.pop("FAKE_RSYNC_RC", None)
        os.environ.pop("FAKE_AFTER_LIST", None)
        self.key = os.path.join(self.base, "key")
        self.kh = os.path.join(self.base, "known_hosts")
        write(self.key, "not a real key\n", 0o600)
        write(self.kh, "100.64.0.10 ssh-ed25519 AAAA\n")
        # One real archive from the stage, copied under several names.
        self.ok(self.stage(out=self.remote))
        self.sample = os.path.join(self.base, "sample.tar")
        shutil.copy(os.path.join(self.remote, self.archives(self.remote)[0]), self.sample)
        for n in self.archives(self.remote):
            os.unlink(os.path.join(self.remote, n))

    def put_remote(self, hours_ago_list, last_ok_hours=1):
        names = []
        for h in hours_ago_list:
            n = h if isinstance(h, str) else f"owui-{stamp(h)}.tar"
            shutil.copy(self.sample, os.path.join(self.remote, n))
            names.append(n)
        write(os.path.join(self.remote, "LAST_OK"), iso(last_ok_hours) + "\n")
        return names

    def pull(self, *extra, dest=None, mount_=None, fstype="tmpfs", keep="14", subdir="ai-box/openwebui"):
        return run("pull", "--source", "localhost:", "--key", self.key, "--known-hosts", self.kh,
                   "--ssh", self.ssh, "--dest", dest or self.dest, "--mount", mount_ or self.drive,
                   "--subdir", subdir, "--fstype", fstype, "--keep", keep, "--max-age-hours", "48",
                   "--rsync", self.rsync, "--min-free-mb", "1", *extra)

    def fetches(self):
        """The rsync calls that transferred (not just listed) files."""
        try:
            with open(self.rsync_log) as fh:
                return [line for line in fh.read().splitlines() if "--list-only" not in line]
        except FileNotFoundError:
            return []

    def dest_archives(self):
        return self.archives(self.dest)

    def test_pull_verifies_keeps_and_accumulates_beyond_the_remote(self):
        first = self.put_remote([50, 26, 2])
        self.ok(self.pull())
        self.assertEqual(self.dest_archives(), first)
        with open(os.path.join(self.dest, "LAST_OK")) as fh:
            self.assertIsNotNone(AB.parse_last_ok(fh.read()))
        self.assertTrue(os.path.exists(os.path.join(self.dest, ".last_pull")))
        # ai-box rotates the oldest out and adds a new one: the PC keeps all four.
        os.unlink(os.path.join(self.remote, first[0]))
        newer = self.put_remote([1])
        self.ok(self.pull())
        self.assertEqual(self.dest_archives(), first + newer)
        self.assertEqual(os.stat(os.path.join(self.dest, newer[0])).st_mode & 0o777, 0o600)
        with open(self.ssh_log) as fh:
            argv = fh.read()
        for opt in ("-F none", f"-i {self.key}", "BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes",
                    f"UserKnownHostsFile={self.kh}", "GlobalKnownHostsFile=/dev/null"):
            self.assertIn(opt, argv)
        with open(os.path.join(self.drive, "restic", "blob"), "rb") as fh:
            self.assertEqual(fh.read(), b"precious")
        fetch = self.fetches()
        self.assertTrue(fetch and all("--max-size=8G" in line for line in fetch), fetch)

    # --- Reviewer X-1: future-dated names, the promotion cap ---
    def seed_genuine(self, n=14):
        """n genuine nightly copies, the oldest 13 days old: all inside the 14-day retention floor."""
        names = [f"owui-{stamp(d * 24 + 2)}.tar" for d in range(n - 1, -1, -1)]
        for name in names:
            shutil.copy(self.sample, os.path.join(self.dest, name))
        return names

    def test_future_dated_archives_are_rejected_and_genuine_copies_survive(self):
        genuine = self.seed_genuine()
        newest = self.put_remote([1])
        evil = self.put_remote(["owui-20991231T000000Z.tar", "owui-20991230T000000Z.tar"])
        res = self.pull()
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("dated more than 60 min ahead", res.stdout)
        kept = self.dest_archives()
        self.assertFalse(set(evil) & set(kept))
        self.assertEqual(kept, genuine + newest)  # a failed run never rotates (Reviewer X-1b)
        self.assertFalse(set(evil) & set(os.listdir(os.path.join(self.dest, ".incoming"))))

    def test_slight_clock_skew_is_accepted(self):
        ahead = self.put_remote([-0.5])  # 30 min ahead of this clock: inside the 60 min slack
        self.ok(self.pull())
        self.assertEqual(self.dest_archives(), ahead)
        res = self.pull("--max-future-minutes", "10")
        self.assertEqual(res.returncode, 1, res.stdout)

    def test_a_future_dated_archive_already_kept_blocks_retention(self):
        genuine = self.seed_genuine()
        shutil.copy(self.sample, os.path.join(self.dest, "owui-20991231T000000Z.tar"))
        newest = self.put_remote([1])
        res = self.pull()
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("retention skipped", res.stdout)
        self.assertEqual(self.dest_archives(), genuine + newest + ["owui-20991231T000000Z.tar"])

    # --- Reviewer X-1b: a flood within the clock slack must not wipe the history ---
    def flood(self, run_no, last_ok_hours):
        """ai-box serves 5 valid archives named 10..50 min ahead (inside the 60 min slack), distinct per run."""
        for n in self.archives(self.remote):
            os.unlink(os.path.join(self.remote, n))
        self.put_remote([-(m + run_no) / 60 for m in (10, 20, 30, 40, 50)], last_ok_hours=last_ok_hours)

    def test_e4_three_failing_flood_runs_back_to_back_keep_all_genuine_copies(self):
        genuine = self.seed_genuine()
        for run_no in range(3):  # what Restart=on-failure would do, 5 min apart
            self.flood(run_no, last_ok_hours=80)  # stale LAST_OK: every run exits 1
            res = self.pull()
            self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
            self.assertIn("retention skipped", res.stdout)
            self.assertNotIn("rotated out", res.stdout)
        kept = self.dest_archives()
        self.assertEqual([n for n in genuine if n not in kept], [])
        self.assertEqual(len(kept), 14 + 15)

    def test_three_successful_flood_runs_keep_all_genuine_copies(self):
        genuine = self.seed_genuine()
        for run_no in range(3):
            self.flood(run_no, last_ok_hours=1)
            res = self.ok(self.pull())
            self.assertNotIn("rotated out", res.stdout)
        kept = self.dest_archives()
        self.assertEqual([n for n in genuine if n not in kept], [])
        self.assertEqual(len(kept), 14 + 15)  # piles up until the free-space floor refuses, loudly

    def test_a_failing_run_deletes_nothing_even_when_old_archives_are_due(self):
        for d in range(100, 116):  # 16 archives, all well past the age floor: a clean run would rotate 2+
            shutil.copy(self.sample, os.path.join(self.dest, f"owui-{stamp(d * 24)}.tar"))
        self.put_remote([1], last_ok_hours=80)  # stale LAST_OK: the run fails
        res = self.pull()
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertNotIn("rotated out", res.stdout)
        self.assertEqual(len(self.dest_archives()), 17)

    def test_retention_is_skipped_after_rsync_exit_24(self):
        for d in range(100, 116):
            shutil.copy(self.sample, os.path.join(self.dest, f"owui-{stamp(d * 24)}.tar"))
        self.put_remote([1])
        os.environ["FAKE_RSYNC_RC"] = "24"
        res = self.ok(self.pull())
        self.assertIn("retention skipped this run (rsync exit 24", res.stdout)
        self.assertEqual(len(self.dest_archives()), 17)
        os.environ.pop("FAKE_RSYNC_RC")
        self.ok(self.pull())
        self.assertEqual(len(self.dest_archives()), 14)

    def test_more_files_arriving_than_listed_promotes_none(self):
        genuine = self.seed_genuine()
        self.put_remote([5, 4])
        os.environ.update(FAKE_SAMPLE=self.sample,
                          FAKE_AFTER_LIST=" ".join(f"owui-{stamp(h / 60)}.tar" for h in (5, 10, 15, 20)))
        res = self.pull()
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("ai-box sent 6 files, more than --max-remote 5 (it listed 2)", res.stdout)
        self.assertEqual(self.dest_archives(), genuine)
        self.assertEqual(self.archives(os.path.join(self.dest, ".incoming")), [])

    def test_leftover_partials_in_staging_are_removed(self):
        self.put_remote([2])
        incoming = os.path.join(self.dest, ".incoming")
        write(os.path.join(incoming, ".owui-20261028T034500Z.tar.Ab12Cd"), b"x" * 1000)
        res = self.ok(self.pull())
        self.assertIn("removed a leftover from staging", res.stdout)
        self.assertEqual(sorted(os.listdir(incoming)), sorted(["LAST_OK"] + self.archives(self.remote)))

    def test_refuses_an_oversized_last_ok(self):
        self.put_remote([2])
        write(os.path.join(self.remote, "LAST_OK"), "x" * 5000)
        self.refused(self.pull(), "over --max-size 8G: LAST_OK")

    def test_more_new_archives_than_max_new_promotes_none(self):
        genuine = self.seed_genuine()
        self.put_remote([30, 20, 10, 1])
        res = self.pull("--max-new", "3")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("more than --max-new 3; promoted none", res.stdout)
        self.assertEqual(self.dest_archives(), genuine)
        self.assertFalse(os.path.exists(os.path.join(self.dest, "LAST_OK")))

    # --- Reviewer X-2: bounded transfer ---
    def test_refuses_when_ai_box_lists_too_many_archives(self):
        self.put_remote([30, 26, 20, 10, 5, 2])
        self.refused(self.pull("--max-remote", "5"), "lists 6 archives, more than --max-remote 5")
        self.refused(self.pull("--max-remote", "5"), "Nothing fetched")
        write(os.path.join(self.remote, "owui-evil.tar"), b"x")  # odd names count too
        for n in self.archives(self.remote)[:2]:
            os.unlink(os.path.join(self.remote, n))
        self.refused(self.pull("--max-remote", "4"), "lists 5 archives")

    def test_refuses_an_archive_over_max_size(self):
        self.put_remote([2])
        self.refused(self.pull("--max-size", "1K"), "over --max-size 1K")

    def test_refuses_without_the_free_space_floor(self):
        self.put_remote([2])
        self.refused(self.pull("--min-free-mb", str(1 << 30)), "not enough space on the backup drive")

    def test_parse_size(self):
        self.assertEqual(AB.parse_size("8G"), 8 << 30)
        self.assertEqual(AB.parse_size("512m"), 512 << 20)
        self.assertEqual(AB.parse_size("100"), 100)
        with self.assertRaises(AB.Refused):
            AB.parse_size("8 GB")

    # --- Reviewer X-7: the folder chain on the drive ---
    def test_missing_chain_is_created_root_0700(self):
        shutil.rmtree(os.path.join(self.drive, "ai-box"))
        self.put_remote([2])
        self.ok(self.pull())
        for p in (os.path.join(self.drive, "ai-box"), self.dest):
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o700)

    def test_refuses_a_group_or_other_writable_chain(self):
        self.put_remote([2])
        os.chmod(os.path.join(self.drive, "ai-box"), 0o775)
        self.refused(self.pull(), "must be owned by root and not group/other-writable")
        os.chmod(os.path.join(self.drive, "ai-box"), 0o700)
        os.chmod(self.dest, 0o702)
        self.refused(self.pull(), "must be owned by root and not group/other-writable")

    def test_refuses_a_chain_folder_not_owned_by_root(self):
        real_lstat = os.lstat
        target = os.path.join(self.drive, "ai-box")

        def lstat(path, *a, **kw):
            st = real_lstat(path, *a, **kw)
            if path == target:
                fields = list(st)
                fields[4] = 1000  # st_uid: the desktop user made it
                return os.stat_result(fields)
            return st

        with mock.patch.object(AB.os, "lstat", side_effect=lstat), self.assertRaises(AB.Refused) as cm:
            AB.check_backup_drive("findmnt", self.dest, self.drive, "ai-box/openwebui", ["tmpfs"])
        self.assertIn("must be owned by root", str(cm.exception))

    # --- Reviewer X-3 survivors ---
    def test_an_already_kept_newest_still_moves_last_ok_and_last_pull(self):
        self.put_remote([2])
        self.ok(self.pull())
        os.unlink(os.path.join(self.dest, ".last_pull"))
        fresh = iso(0) + "\n"  # once: a second may tick over before the assert
        write(os.path.join(self.remote, "LAST_OK"), fresh)
        later = time.time() + 120  # ai-box rewrites it a day later in real life: never the same mtime
        os.utime(os.path.join(self.remote, "LAST_OK"), (later, later))
        self.ok(self.pull())  # nothing new: the newest was verified when it was promoted
        self.assertTrue(os.path.exists(os.path.join(self.dest, ".last_pull")))
        with open(os.path.join(self.dest, "LAST_OK")) as fh:
            self.assertEqual(fh.read(), fresh)

    def test_a_future_last_ok_fails(self):
        self.put_remote([2], last_ok_hours=-5)
        res = self.pull()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("is in the future", res.stdout)

    def test_retention_keeps_14_and_never_the_newest(self):
        for h in range(100, 115):  # 15 older archives already on the PC
            shutil.copy(self.sample, os.path.join(self.dest, f"owui-{stamp(h * 24)}.tar"))
        write(os.path.join(self.dest, "README-mine.txt"), b"keep me")
        newest = self.put_remote([1])
        self.ok(self.pull())
        kept = self.dest_archives()
        self.assertEqual(len(kept), 14)
        self.assertEqual(kept[-1], newest[0])
        self.assertTrue(os.path.exists(os.path.join(self.dest, "README-mine.txt")))
        res = self.ok(self.pull(keep="1"))
        self.assertEqual(self.dest_archives(), newest)
        self.assertIn("rotated out", res.stdout)

    def test_corrupt_archive_is_rejected_and_not_kept(self):
        good = self.put_remote([26])
        bad = self.put_remote([2])
        with open(os.path.join(self.remote, bad[0]), "r+b") as fh:
            fh.seek(5000)
            fh.write(b"\xff\xfe")
        res = self.pull()
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("REJECTED", res.stdout)
        self.assertEqual(self.dest_archives(), good)
        self.assertFalse(os.path.exists(os.path.join(self.dest, "LAST_OK")))  # newest not verified
        self.assertNotIn(bad[0], os.listdir(os.path.join(self.dest, ".incoming")))

    def test_archive_cut_short_before_the_manifest_is_rejected(self):
        names = self.put_remote([2])
        with tarfile.open(self.sample) as tf:
            offset = tf.getmember("MANIFEST.sha256").offset
        os.truncate(os.path.join(self.remote, names[0]), offset)
        res = self.pull()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("no MANIFEST.sha256", res.stdout)
        self.assertEqual(self.dest_archives(), [])

    def test_stale_missing_or_garbled_last_ok_fails(self):
        self.put_remote([80], last_ok_hours=80)
        res = self.pull()
        self.assertEqual(res.returncode, 1)
        self.assertIn("h old", res.stdout)
        write(os.path.join(self.remote, "LAST_OK"), "yesterday-ish\n")
        res = self.pull()
        self.assertEqual(res.returncode, 1)
        self.assertIn("not a timestamp", res.stdout)
        os.unlink(os.path.join(self.remote, "LAST_OK"))
        res = self.pull()
        self.assertEqual(res.returncode, 1)
        self.assertIn("missing", res.stdout)

    def test_rsync_failure_leaves_dest_alone(self):
        self.put_remote([2])
        os.environ["FAKE_SSH_FAIL"] = "1"
        res = self.pull()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("rsync exited", res.stdout)
        self.assertEqual(self.dest_archives(), [])

    def test_no_archives_on_ai_box(self):
        write(os.path.join(self.remote, "LAST_OK"), iso(1) + "\n")
        res = self.pull()
        self.assertEqual(res.returncode, 1)
        self.assertIn("no archives", res.stdout)

    # --- the backup-drive checks (refused = exit 2, nothing fetched) ---
    def refused(self, res, why):
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("REFUSED", res.stdout)
        self.assertIn(why, res.stdout)
        self.assertEqual(self.fetches(), [], "rsync fetched despite the refusal")
        incoming = os.path.join(self.dest, ".incoming")
        self.assertEqual(self.archives(incoming) if os.path.isdir(incoming) else [], [])

    def test_refuses_an_unmounted_drive(self):
        self.put_remote([2])
        plain = os.path.join(self.base, "plain")
        os.makedirs(os.path.join(plain, "ai-box", "openwebui"))
        self.refused(self.pull(mount_=plain, dest=os.path.join(plain, "ai-box", "openwebui")), "is not mounted")

    def test_refuses_the_wrong_fs_type(self):
        self.put_remote([2])
        self.refused(self.pull(fstype="ext4"), "is tmpfs, expected ext4")

    def test_refuses_a_dest_that_is_not_exactly_mount_subdir(self):
        self.put_remote([2])
        os.makedirs(os.path.join(self.drive, "restic", "openwebui"))
        self.refused(self.pull(dest=os.path.join(self.drive, "restic", "openwebui")), "must be exactly")
        self.refused(self.pull(dest=self.drive + "/ai-box/../ai-box/openwebui"), "without '.' or '..'")
        self.refused(self.pull(subdir="../x"), "--subdir")

    def test_refuses_a_symlinked_dest(self):
        self.put_remote([2])
        shutil.rmtree(os.path.join(self.drive, "ai-box"))
        os.makedirs(os.path.join(self.drive, "elsewhere", "openwebui"))
        os.symlink(os.path.join(self.drive, "elsewhere"), os.path.join(self.drive, "ai-box"))
        self.refused(self.pull(), "is not a real directory (a symlink or a file)")

    def test_refuses_another_filesystem_on_the_dest(self):
        self.put_remote([2])
        mount("-t", "tmpfs", "-o", "mode=0700", "tmpfs", self.dest)  # root 0700: only the device check catches it
        self.refused(self.pull(), "is not on the")

    def test_refuses_a_sibling_folder_bound_onto_the_dest_but_allows_a_self_bind(self):
        self.put_remote([2])
        mount("--bind", self.dest, self.dest)  # what ReadWritePaths= does under the unit: allowed
        self.ok(self.pull())
        os.unlink(self.ssh_log)
        os.unlink(self.rsync_log)
        subprocess.run(["umount", self.dest], check=True)
        mount("--bind", os.path.join(self.drive, "restic"), self.dest)
        self.refused(self.pull(), "another folder of the drive mounted on it")

    def test_refuses_a_sibling_stacked_on_the_drive_path(self):
        self.put_remote([2])
        os.makedirs(os.path.join(self.drive, "restic", "ai-box", "openwebui"))
        mount("--bind", os.path.join(self.drive, "restic"), self.drive)
        self.refused(self.pull(), "different mounts stacked")

    def test_refuses_when_another_pull_holds_the_lock(self):
        self.put_remote([2])
        fd = AB.take_lock(self.dest)
        try:
            self.refused(self.pull(), "holds")
        finally:
            os.close(fd)


# ------------------------------------------------------------------------------------------------------
class RestoreCheck(Base):
    def test_ok_on_a_staged_archive_and_temp_removed(self):
        self.ok(self.stage())
        res = self.ok(run("restore-check", "--dir", self.out))
        self.assertIn("sqlite/webui.db: integrity_check ok (users 0, chats 200", res.stdout)
        self.assertIn("sqlite/vector_db/chroma.sqlite3: integrity_check ok", res.stdout)
        self.assertIn("WEBUI_SECRET_KEY present", res.stdout)
        self.assertNotIn("test-canary", res.stdout)
        self.assertEqual([n for n in os.listdir(self.out) if n.startswith(".restore-check")], [])

    def test_checks_the_newest_and_fails_on_a_corrupt_db_inside_a_valid_manifest(self):
        self.ok(self.stage())
        with tarfile.open(os.path.join(self.out, self.archives()[0])) as tf:
            members = [(n, tf.extractfile(n).read()) for n in AB.MEMBERS]
        bad = os.path.join(self.base, "bad.db")
        corrupt_db(bad)
        with open(bad, "rb") as fh:
            members[1] = (AB.DB_CHROMA, fh.read())
        build_archive(os.path.join(self.out, "owui-29991231T000000Z.tar"), members)  # the newest
        res = run("restore-check", "--dir", self.out)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("owui-29991231T000000Z.tar: manifest verified", res.stdout)
        self.assertIn("integrity_check", res.stdout)

    def inner_with(self, entries):
        """A manifest-valid archive (the newest) whose uploads.tar holds the given (TarInfo-name, type) entries."""
        self.ok(self.stage())
        with tarfile.open(os.path.join(self.out, self.archives()[0])) as tf:
            members = [(n, tf.extractfile(n).read()) for n in AB.MEMBERS]
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for name, typ in entries:
                ti = tarfile.TarInfo(name)
                ti.type = typ
                if typ == tarfile.SYMTYPE:
                    ti.linkname = "/etc/shadow"
                tf.addfile(ti, io.BytesIO(b"") if typ == tarfile.REGTYPE else None)
        members[2] = (AB.UPLOADS_TAR, buf.getvalue())
        build_archive(os.path.join(self.out, "owui-29991231T000000Z.tar"), members)

    def test_unsafe_inner_names_fail(self):
        for entries, why in (([("uploads/../../etc/cron.d/x", tarfile.REGTYPE)], "unsafe member name"),
                             ([("/etc/passwd", tarfile.REGTYPE)], "unsafe member name"),
                             ([("cache/model.bin", tarfile.REGTYPE)], "unsafe member name"),
                             ([("uploads/link", tarfile.SYMTYPE)], "is not a file or directory")):
            with self.subTest(entries=entries):
                for n in os.listdir(self.out):
                    if n.startswith("owui-"):
                        os.unlink(os.path.join(self.out, n))
                self.inner_with(entries)
                res = run("restore-check", "--dir", self.out)
                self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
                self.assertIn(why, res.stdout)

    def test_drive_check_refuses_an_unmounted_dir(self):
        self.ok(self.stage())
        plain = os.path.join(self.base, "plain")
        os.makedirs(os.path.join(plain, "ai-box"))
        shutil.move(self.out, os.path.join(plain, "ai-box", "openwebui"))
        res = run("restore-check", "--dir", os.path.join(plain, "ai-box", "openwebui"), "--mount", plain,
                  "--fstype", "tmpfs")
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertIn("is not mounted", res.stdout)

    def test_a_named_archive_is_checked_not_the_newest(self):
        self.ok(self.stage())
        good = self.archives()[0]
        write(os.path.join(self.out, "owui-29991231T000000Z.tar"), b"junk, and the newest")
        res = self.ok(run("restore-check", "--dir", self.out, "--archive", good))
        self.assertIn(f"restore-check ok: {good}", res.stdout)
        self.assertEqual(run("restore-check", "--dir", self.out).returncode, 1)
        res = run("restore-check", "--dir", self.out, "--archive", "../" + good)
        self.assertEqual(res.returncode, 2, res.stdout)

    def test_no_archives(self):
        res = run("restore-check", "--dir", self.out)
        self.assertEqual(res.returncode, 1)
        self.assertIn("no archives", res.stdout)


if __name__ == "__main__":
    unittest.main()
