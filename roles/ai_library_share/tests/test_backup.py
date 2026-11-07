"""Unit tests for files/ai-library-backup.py. They mount real tmpfs filesystems as the share and
backup drives, so they run inside a private user + mount namespace (tests/run.sh: `unshare -rm`);
nothing is mounted or written outside the scratch dir. A plain directory stands in for an unmounted
drive. Other backups on the drive (restic, a LUKS header) must survive every case.
    unshare -rm python3 -m unittest test_backup      (from tests/; run.sh does this)
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "files", "ai-library-backup.py")
LIB = os.path.abspath(os.path.join(HERE, "..", "..", "..", "apps", "skill-library"))
SENTINEL = ".ai-library-sentinel"


def load_script():
    spec = importlib.util.spec_from_file_location("ai_library_backup", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def in_private_userns():
    try:
        with open("/proc/self/uid_map", encoding="ascii") as fh:
            first = fh.read().split()
    except OSError:
        return False
    return os.getuid() == 0 and first[:3] != ["0", "0", "4294967295"]


def make_library(root, sentinel=True):
    sys.path.insert(0, LIB)
    from library import Library  # noqa: PLC0415
    Library.init(root)
    if sentinel:
        with open(os.path.join(root, SENTINEL), "w", encoding="utf-8") as fh:
            fh.write("sentinel\n")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def tree(path):
    out = {}
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            out[os.path.relpath(full, path)] = (st.st_ino, st.st_size, os.path.islink(full))
    return out


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not in_private_userns():
            raise RuntimeError("run inside a private user+mount namespace: tests/run.sh (unshare -rm)")

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ai-library-share-bk.")
        self.share = os.path.join(self.base, "share")
        self.backup = os.path.join(self.base, "backup")
        for m in (self.share, self.backup):
            os.mkdir(m)
            subprocess.run(["mount", "-t", "tmpfs", "tmpfs", m], check=True)
        self.src = os.path.join(self.share, "ai-library")
        self.dest = os.path.join(self.backup, "ai-library")
        make_library(self.src)
        os.mkdir(self.dest, 0o700)
        # Other backups that live on the same drive.
        write(os.path.join(self.backup, "restic", "data", "blob"), "precious\n")
        write(os.path.join(self.backup, "luks-header.img"), "luks\n")

    def tearDown(self):
        # Every mount under the scratch dir, deepest first, then the dir itself.
        res = subprocess.run(["findmnt", "-rn", "-o", "TARGET"], capture_output=True, text=True, check=False)
        for target in sorted((t for t in res.stdout.split() if t.startswith(self.base + "/")), reverse=True):
            subprocess.run(["umount", target], check=False)
        shutil.rmtree(self.base, ignore_errors=True)

    def others_intact(self):
        self.assertEqual(read(os.path.join(self.backup, "restic", "data", "blob")), "precious\n")
        self.assertEqual(read(os.path.join(self.backup, "luks-header.img")), "luks\n")

    def run_backup(self, **over):
        args = {
            "--source": self.src, "--source-mount": self.share, "--source-fstype": "tmpfs",
            "--dest": self.dest, "--dest-mount": self.backup, "--dest-fstype": "tmpfs",
            "--lib": LIB, "--sentinel": SENTINEL,
        }
        args.update(over)
        argv = [sys.executable, "-I", "-B", SCRIPT]
        for key, val in args.items():
            if val is not None:
                argv += [key, val]
        return subprocess.run(argv, capture_output=True, text=True, check=False)

    def ok(self, **over):
        res = self.run_backup(**over)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return res

    def assert_refused(self, res, why):
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("REFUSED", res.stdout)
        self.assertIn(why, res.stdout)

    def wrapper(self, body):
        path = os.path.join(self.base, "rsync-wrapper")
        write(path, "#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return path


class Rotation(Base):
    def test_rotation_current_and_prev_with_hard_links_and_no_links_or_specials(self):
        self.ok()
        cur, prev = os.path.join(self.dest, "current"), os.path.join(self.dest, "prev")
        self.assertTrue(os.path.isfile(os.path.join(cur, SENTINEL)))
        self.assertFalse(os.path.exists(prev))

        write(os.path.join(self.src, "00-inbox", "a.txt"), "v1\n")
        os.symlink("/etc/hostname", os.path.join(self.src, "00-inbox", "link"))
        os.mkfifo(os.path.join(self.src, "00-inbox", "fifo"))
        self.ok()
        self.assertTrue(os.path.isfile(os.path.join(cur, "00-inbox", "a.txt")))
        self.assertFalse(os.path.lexists(os.path.join(cur, "00-inbox", "link")), "--no-links copied a symlink")
        self.assertFalse(os.path.lexists(os.path.join(cur, "00-inbox", "fifo")), "--no-specials copied a fifo")
        self.assertFalse(os.path.exists(os.path.join(prev, "00-inbox", "a.txt")))
        self.assertEqual(os.stat(os.path.join(cur, "README.md")).st_ino,
                         os.stat(os.path.join(prev, "README.md")).st_ino)

        write(os.path.join(self.src, "00-inbox", "a.txt"), "version 2\n")
        self.ok()
        self.assertEqual(read(os.path.join(cur, "00-inbox", "a.txt")), "version 2\n")
        self.assertEqual(read(os.path.join(prev, "00-inbox", "a.txt")), "v1\n")
        self.assertEqual(sorted(n for n in os.listdir(self.dest) if n != ".lock"), ["current", "prev"])
        self.others_intact()

    def test_deleted_source_file_leaves_current_but_prev_keeps_it(self):
        write(os.path.join(self.src, "00-inbox", "keep.txt"), "x\n")
        self.ok()
        os.unlink(os.path.join(self.src, "00-inbox", "keep.txt"))
        self.ok()
        self.assertFalse(os.path.exists(os.path.join(self.dest, "current", "00-inbox", "keep.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.dest, "prev", "00-inbox", "keep.txt")))

    # ---- failures after rsync leave current/ and prev/ alone -----------------------------------
    def check_failed_run_leaves_snapshots(self, rsync, want_rc=1):
        self.ok()
        self.ok()
        before = tree(self.dest)
        res = self.run_backup(**{"--rsync": rsync})
        self.assertEqual(res.returncode, want_rc, res.stdout + res.stderr)
        after = {k: v for k, v in tree(self.dest).items() if not k.startswith("next")}
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(os.path.join(self.dest, "ready")))
        self.others_intact()

    def test_rsync_failure_leaves_current_and_prev_untouched(self):
        self.check_failed_run_leaves_snapshots("/bin/false")

    def test_rsync_exit_23_partial_transfer_is_a_failure(self):  # Reviewer G B5
        self.check_failed_run_leaves_snapshots(self.wrapper('rsync "$@"\nexit 23\n'))

    def test_snapshot_without_sentinel_is_not_rotated_in(self):  # Reviewer G B4
        # The share drops mid-run: rsync "succeeds" but the snapshot isn't the library.
        body = 'rsync "$@" || exit $?\nfor a; do d="$a"; done\nrm -f "${d%/}/' + SENTINEL + '"\n'
        self.check_failed_run_leaves_snapshots(self.wrapper(body))


class CrashSafety(Base):
    """A crash at every step of the rotation never loses prev, and the next run finishes it."""

    def build(self, names):
        for name, marker in names.items():
            write(os.path.join(self.dest, name, "marker"), marker)

    def markers(self):
        return {n: read(os.path.join(self.dest, n, "marker")) for n in os.listdir(self.dest)
                if os.path.isfile(os.path.join(self.dest, n, "marker"))}

    def test_crash_at_each_rename_and_during_delete(self):
        mod = load_script()
        real_rename, real_rmtree = mod.rename, mod.shutil.rmtree
        for crash_at in range(4):  # 3 renames, then the delete of prev.old
            with self.subTest(crash_at=crash_at):
                for n in os.listdir(self.dest):
                    real_rmtree(os.path.join(self.dest, n))
                self.build({"prev": "p0", "current": "c1", "ready": "r2"})
                calls = []

                def crashing_rename(dest, old, new, calls=calls, crash_at=crash_at):
                    if len(calls) == crash_at:
                        raise OSError("simulated crash")
                    calls.append((old, new))
                    real_rename(dest, old, new)

                def crashing_rmtree(path, calls=calls, crash_at=crash_at):
                    if crash_at == 3:
                        os.unlink(os.path.join(path, "marker"))  # partly deleted, then the crash
                        raise OSError("simulated crash")
                    real_rmtree(path)

                with mock.patch.object(mod, "rename", crashing_rename), \
                        mock.patch.object(mod.shutil, "rmtree", crashing_rmtree):
                    with self.assertRaises(OSError):
                        mod.rotate(self.dest)
                # At the crash point the two newest snapshots both still exist.
                self.assertIn("c1", self.markers().values())
                self.assertIn("r2", self.markers().values())
                mod.rotate(self.dest)  # what the next run does first
                self.assertEqual(self.markers(), {"prev": "c1", "current": "r2"})
                self.assertEqual(sorted(os.listdir(self.dest)), ["current", "prev"])

    def test_first_rotation_without_prev(self):
        mod = load_script()
        self.build({"current": "c1", "ready": "r2"})
        mod.rotate(self.dest)
        self.assertEqual(self.markers(), {"prev": "c1", "current": "r2"})

    def test_a_full_run_finishes_an_interrupted_rotation_first(self):
        self.ok()
        # Crash after current -> prev.old ... simulate "after step 1": prev.old, current, ready.
        self.build({"prev.old": "p0", "ready": "r2"})
        shutil.rmtree(os.path.join(self.dest, "current"))
        self.build({"current": "c1"})
        self.ok()
        # Recovery made prev=c1, current=r2; this run then made prev=r2, current=<new>.
        self.assertEqual(read(os.path.join(self.dest, "prev", "marker")), "r2")
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "current", SENTINEL)))
        self.assertEqual(sorted(n for n in os.listdir(self.dest) if n != ".lock"), ["current", "prev"])

    def test_leftover_next_is_an_unfinished_rsync_and_is_replaced(self):
        self.ok()
        self.build({"next": "partial"})
        self.ok()
        self.assertFalse(os.path.exists(os.path.join(self.dest, "next")))
        self.assertFalse(os.path.exists(os.path.join(self.dest, "current", "marker")))


class UnitBindMounts(Base):
    """systemd's ReadWritePaths=/ReadOnlyPaths= bind-mount a folder onto itself inside the unit:
    findmnt then reports it as its own mountpoint, but it is still the same drive (Integrator)."""

    def test_dest_bind_mounted_onto_itself_still_backs_up(self):
        subprocess.run(["mount", "--bind", self.dest, self.dest], check=True)
        self.ok()
        self.ok()
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "prev", SENTINEL)))
        self.others_intact()

    def test_source_bind_mounted_onto_itself_still_backs_up(self):
        subprocess.run(["mount", "--bind", self.src, self.src], check=True)
        self.ok()
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "current", SENTINEL)))


class SiblingBinds(Base):
    """Reviewer G round 5: another folder of the same drive bound onto the folder (same st_dev)."""

    def test_sibling_bound_onto_the_dest_is_refused_and_untouched(self):
        self.ok()
        restic = os.path.join(self.backup, "restic")
        before = tree(restic)
        subprocess.run(["mount", "--bind", restic, self.dest], check=True)
        self.assert_refused(self.run_backup(), "another folder of the drive")
        self.assert_refused(self.run_backup(), "another folder of the drive")
        subprocess.run(["umount", self.dest], check=True)
        self.assertEqual(before, tree(restic))
        self.others_intact()

    def test_sibling_library_bound_onto_the_source_is_refused(self):
        stale = os.path.join(self.share, "stale")
        make_library(stale)
        subprocess.run(["mount", "--bind", stale, self.src], check=True)
        self.assert_refused(self.run_backup(), "another folder of the drive")
        self.assertEqual(os.listdir(self.dest), [])

    def test_self_bind_on_a_bind_mounted_drive_passes(self):
        # The drive itself mounted from a subfolder (FSROOT /sub): the self-bind is /sub/ai-library.
        sub = os.path.join(self.base, "sub-drive")
        os.mkdir(sub)
        subprocess.run(["mount", "-t", "tmpfs", "tmpfs", sub], check=True)
        os.makedirs(os.path.join(sub, "sub", "ai-library"))
        subprocess.run(["umount", self.backup], check=True)
        subprocess.run(["mount", "--bind", os.path.join(sub, "sub"), self.backup], check=True)
        subprocess.run(["mount", "--bind", self.dest, self.dest], check=True)
        self.ok()
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "current", SENTINEL)))


class Refusals(Base):
    """Every refusal happens before anything in the backup changes."""

    def check_refusal(self, why, **over):
        self.ok()
        before, before_root = tree(self.dest), sorted(os.listdir(self.backup))
        self.assert_refused(self.run_backup(**over), why)
        self.assertEqual(before, tree(self.dest))
        self.assertEqual(before_root, sorted(os.listdir(self.backup)))
        self.others_intact()

    def test_unmounted_share_with_a_library_on_the_empty_mountpoint(self):
        fake = os.path.join(self.base, "fake-share")
        os.mkdir(fake)
        make_library(os.path.join(fake, "ai-library"))
        self.check_refusal("is not mounted", **{"--source": os.path.join(fake, "ai-library"), "--source-mount": fake})

    def test_unmounted_backup_drive(self):
        fake = os.path.join(self.base, "fake-backup")
        os.makedirs(os.path.join(fake, "ai-library"))
        self.assert_refused(self.run_backup(**{"--dest": os.path.join(fake, "ai-library"), "--dest-mount": fake}),
                            "is not mounted")
        self.assertEqual(os.listdir(os.path.join(fake, "ai-library")), [])

    def test_dest_is_the_backup_mount_root(self):  # Reviewer G T1
        os.makedirs(os.path.join(self.backup, "next"))
        write(os.path.join(self.backup, "next", "x"), "mine\n")
        self.check_refusal("must be exactly", **{"--dest": self.backup})
        self.assertEqual(read(os.path.join(self.backup, "next", "x")), "mine\n")

    def test_dest_with_dotdot_into_another_backup(self):
        self.check_refusal("without '.' or '..'", **{"--dest": self.dest + "/../restic"})

    def test_dest_with_another_name(self):
        os.mkdir(os.path.join(self.backup, "restic2"))
        self.check_refusal("must be exactly", **{"--dest": os.path.join(self.backup, "restic2")})

    def test_source_with_dotdot(self):
        self.check_refusal("without '.' or '..'", **{"--source": self.src + "/../ai-library"})

    def test_source_folder_is_a_symlink(self):
        real = os.path.join(self.share, "real-library")
        os.rename(self.src, real)
        os.symlink(real, self.src)
        self.assert_refused(self.run_backup(), "must be exactly")

    def test_source_on_another_mount(self):
        other = os.path.join(self.base, "other")
        os.makedirs(other)
        subprocess.run(["mount", "-t", "tmpfs", "tmpfs", other], check=True)
        make_library(os.path.join(other, "ai-library"))
        # The mount given is the share, the folder is on another mount: not exactly <mount>/ai-library.
        self.check_refusal("must be exactly", **{"--source": os.path.join(other, "ai-library")})

    def test_nested_mount_inside_the_share(self):
        # <share>/ai-library is itself a different mount (e.g. the drive dropped and something else is there).
        self.ok()
        subprocess.run(["mount", "-t", "tmpfs", "tmpfs", self.src], check=True)
        make_library(self.src)
        self.assert_refused(self.run_backup(), "not on the")

    def test_another_drive_mounted_at_the_dest_folder(self):
        self.ok()
        other = os.path.join(self.base, "other-drive")
        os.mkdir(other)
        subprocess.run(["mount", "-t", "tmpfs", "tmpfs", other], check=True)
        os.mkdir(os.path.join(other, "current"))
        subprocess.run(["mount", "--bind", other, self.dest], check=True)
        self.assert_refused(self.run_backup(), "not on the")
        subprocess.run(["umount", self.dest], check=True)
        self.others_intact()

    def test_wrong_filesystem_type(self):
        self.check_refusal("expected ntfs3", **{"--source-fstype": "ntfs3"})

    def test_non_library_source(self):
        shutil.rmtree(self.src)
        os.mkdir(self.src)
        write(os.path.join(self.src, SENTINEL), "sentinel\n")
        write(os.path.join(self.src, "stuff.txt"), "x\n")
        self.assert_refused(self.run_backup(), "not an AI library")

    def test_library_without_sentinel(self):
        self.ok()
        os.unlink(os.path.join(self.src, SENTINEL))
        before = tree(self.dest)
        self.assert_refused(self.run_backup(), "is missing")
        self.assertEqual(before, tree(self.dest))

    def test_planted_symlink_snapshots_are_refused_and_others_survive(self):  # Reviewer G T2/T3
        for name in ("current", "prev", "next", "ready", "prev.old"):
            with self.subTest(name=name):
                self.ok()
                path = os.path.join(self.dest, name)
                if os.path.isdir(path):
                    shutil.rmtree(path)
                os.symlink("../restic", path)
                self.assert_refused(self.run_backup(), "not a real directory")
                self.others_intact()
                os.unlink(path)

    def test_backup_onto_the_share_itself(self):
        self.check_refusal("overlap", **{"--dest": self.src, "--dest-mount": self.share})

    def test_a_held_lock(self):
        import fcntl  # noqa: PLC0415
        self.ok()
        fd = os.open(os.path.join(self.dest, ".lock"), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assert_refused(self.run_backup(), "holds the lock")
        finally:
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
