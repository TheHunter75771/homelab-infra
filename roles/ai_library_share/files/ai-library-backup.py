"""Nightly AI Library backup (docs/CONTRACTS.md §12.17.2), run by ai-library-backup.service as root.

Snapshot rotation on the backup drive, inside --dest (exactly <dest-mount>/ai-library):
    next/      this run, built by rsync with --link-dest=current (unchanged files are hard links)
    ready/     next/ once rsync succeeded and the snapshot carries the sentinel (one rename)
    current/   the last good run
    prev/      the run before it, so one bad run can be undone
Rotation, each step one rename: prev -> prev.old, current -> prev, ready -> current, then prev.old is
removed. prev.old is deleted only after ready has become current, so a crash never loses prev:
rotate() is idempotent and every run starts by finishing an interrupted rotation (a leftover next/
is always an unfinished rsync and is deleted; a leftover ready/ is always complete and is kept).
A failed rsync leaves current/ and prev/ untouched.

It refuses (exit 2) before copying anything unless:
  - the source is exactly <source-mount>/ai-library and the dest exactly <dest-mount>/ai-library
    (no '..', no symlink anywhere: realpath equality);
  - both mounts are real mountpoints (findmnt) of an allowed type and hold those folders (an
    unmounted share must never be mirrored as "empty" over the backup);
  - the snapshot names inside the dest are absent or real directories (never symlinks);
  - the source holds the sentinel file and opens as an AI library (Library.open).
rsync failures and a snapshot without the sentinel exit 1. Usage: python3 -I -B ai-library-backup.py --help
"""
import argparse
import fcntl
import os
import posixpath
import shutil
import stat
import subprocess
import sys

NAME = "ai-library"  # both folders are exactly <mount>/ai-library (§12.17, Lead after Reviewer G)
RSYNC_OK = {0, 24}  # 24: some source files vanished during the transfer (the share is live)
SNAPSHOTS = ("next", "ready", "current", "prev", "prev.old")


class Refused(Exception):
    pass


def log(msg):
    print(f"ai-library-backup: {msg}", flush=True)


def findmnt(prog, *args):
    """findmnt -n -r -o TARGET,FSTYPE <args> -> (target, fstype), or None if it finds nothing."""
    res = subprocess.run([prog, "-n", "-r", "-o", "TARGET,FSTYPE", *args],
                         capture_output=True, text=True, check=False)
    lines = res.stdout.splitlines()
    if res.returncode != 0 or not lines:
        return None
    target, _, fstype = lines[0].rpartition(" ")
    # -r escapes spaces in TARGET as \x20; the role's paths have none, so compare as-is.
    return target, fstype


def fsroot(prog, path):
    """FSROOT of the topmost mount AT path (findmnt --mountpoint), or None if path isn't a mountpoint."""
    res = subprocess.run([prog, "-n", "-r", "-o", "FSROOT", "--mountpoint", path],
                         capture_output=True, text=True, check=False)
    lines = res.stdout.splitlines()
    if res.returncode != 0 or not lines:
        return None
    return lines[-1].replace("\\x20", " ")


def exact_path(label, given, mount):
    """The folder must be exactly <mount>/ai-library: absolute, no '.'/'..', no symlink anywhere."""
    for path in (given, mount):
        parts = path.split("/")
        if not path.startswith("/") or "." in parts or ".." in parts:
            raise Refused(f"{label} path {path!r} must be absolute, without '.' or '..'")
    want = os.path.join(os.path.realpath(mount), NAME)
    if os.path.normpath(mount) != os.path.realpath(mount):
        raise Refused(f"{label} mountpoint {mount} goes through a symlink")
    if os.path.normpath(given) != want or os.path.realpath(given) != want:
        raise Refused(f"{label} folder {given} must be exactly {want} (realpath {os.path.realpath(given)})")
    return want


def check_side(prog, label, folder, mount, fstypes):
    found = findmnt(prog, "--mountpoint", mount)
    if found is None:
        raise Refused(f"{label} mountpoint {mount} is not mounted")
    if fstypes and found[1] not in fstypes:
        raise Refused(f"{label} mountpoint {mount} is {found[1]}, expected {' or '.join(fstypes)}")
    try:
        st = os.lstat(folder)
    except FileNotFoundError:
        raise Refused(f"{label} folder {folder} is missing") from None
    if not stat.S_ISDIR(st.st_mode):
        raise Refused(f"{label} folder {folder} is not a real directory")
    # Same filesystem as the mount, by device: under the unit, ReadWritePaths= bind-mounts the folder
    # onto itself, so findmnt -T names the folder as its own mountpoint even though it is the same
    # drive (Integrator, live run). A different filesystem mounted at the folder has another st_dev.
    if os.stat(folder).st_dev != os.stat(mount).st_dev:
        raise Refused(f"{label} folder {folder} is not on the {mount} drive (another filesystem is mounted there)")
    # Same device is not enough: root could bind a SIBLING folder of the drive (e.g. restic/) onto
    # the folder (Reviewer G round 5). A mount at the folder must be a self-bind: its FSROOT is the
    # drive mount's FSROOT plus /ai-library.
    here = fsroot(prog, folder)
    if here is not None:
        want = posixpath.normpath("/" + posixpath.join((fsroot(prog, mount) or "/").strip("/"), NAME))
        if posixpath.normpath("/" + here.strip("/")) != want:
            raise Refused(f"{label} folder {folder} has another folder of the drive mounted on it "
                          f"(FSROOT {here}, want {want})")


def check_source_library(src, lib_dir, sentinel):
    sent = os.path.join(src, sentinel)
    try:
        st = os.lstat(sent)
    except FileNotFoundError:
        raise Refused(f"{sent} is missing; is this the real share?") from None
    if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
        raise Refused(f"{sent} is not a regular, non-empty file")
    sys.path.insert(0, lib_dir)
    try:
        from library import Library, LibraryError  # noqa: PLC0415 (the path is only known now)
    except ImportError as exc:
        raise Refused(f"cannot import the library package from {lib_dir}: {exc}") from None
    try:
        Library.open(src)
    except LibraryError as exc:
        raise Refused(f"the source is not an AI library: {exc}") from None


def check_snapshot_names(dest):
    for name in SNAPSHOTS:
        try:
            st = os.lstat(os.path.join(dest, name))
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(st.st_mode):
            raise Refused(f"{dest}/{name} is not a real directory (a symlink or a file); fix it by hand")


def exists(dest, name):
    return os.path.isdir(os.path.join(dest, name)) and not os.path.islink(os.path.join(dest, name))


def fsync_dir(dest):
    dfd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def rename(dest, old, new):
    os.rename(os.path.join(dest, old), os.path.join(dest, new))
    fsync_dir(dest)


def rotate(dest):
    """Finish (or do) the rotation; safe to re-run after a crash at any step."""
    if exists(dest, "ready"):
        if exists(dest, "prev") and exists(dest, "current") and not exists(dest, "prev.old"):
            rename(dest, "prev", "prev.old")
        if exists(dest, "current") and not exists(dest, "prev"):
            rename(dest, "current", "prev")
        if not exists(dest, "current"):
            rename(dest, "ready", "current")
    if exists(dest, "prev.old") and exists(dest, "prev") and exists(dest, "current"):
        shutil.rmtree(os.path.join(dest, "prev.old"))
    if exists(dest, "ready") or exists(dest, "prev.old"):
        raise Refused(f"{dest} is in a state the rotation can't finish ({sorted(os.listdir(dest))}); fix it by hand")


def run(args):
    src = exact_path("source", args.source, args.source_mount)
    dest = exact_path("backup", args.dest, args.dest_mount)
    src_mount, dest_mount = os.path.realpath(args.source_mount), os.path.realpath(args.dest_mount)
    check_side(args.findmnt, "source", src, src_mount, args.source_fstype)
    check_side(args.findmnt, "backup", dest, dest_mount, args.dest_fstype)
    if src == dest or dest.startswith(src + os.sep) or src.startswith(dest + os.sep):
        raise Refused(f"the source {src} and the backup {dest} overlap")
    check_source_library(src, args.lib, args.sentinel)
    check_snapshot_names(dest)

    lock_fd = os.open(os.path.join(dest, ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise Refused("another backup run holds the lock") from None

    rotate(dest)  # finish a rotation a crash interrupted (never loses prev)
    nxt = os.path.join(dest, "next")
    if exists(dest, "next"):
        shutil.rmtree(nxt)  # an unfinished rsync
    current = os.path.join(dest, "current")
    cmd = [args.rsync, "-a", "-x", "--no-links", "--no-specials", "--no-devices", "--delete-after", "--numeric-ids"]
    if exists(dest, "current"):
        cmd.append(f"--link-dest={current}")
    cmd += [src + "/", nxt + "/"]
    log("running " + " ".join(cmd))
    rc = subprocess.run(cmd, check=False).returncode
    if rc not in RSYNC_OK:
        log(f"FAILED: rsync exited {rc}; current/ and prev/ are unchanged, next/ is left for inspection")
        return 1
    if rc == 24:
        log("warning: some source files vanished during the run (rsync 24)")
    # The snapshot must be the real library, not whatever was there if the share dropped mid-run.
    sent = os.path.join(nxt, args.sentinel)
    if not os.path.isfile(sent) or os.path.islink(sent):
        log(f"FAILED: the new snapshot has no {args.sentinel}; current/ and prev/ are unchanged")
        return 1
    rename(dest, "next", "ready")
    rotate(dest)
    log(f"ok: {current} is this run" + (f", {dest}/prev the one before" if exists(dest, "prev") else " (first run)"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="AI Library snapshot backup (CONTRACTS §12.17.2)")
    ap.add_argument("--source", required=True, help="the library folder: exactly <source-mount>/ai-library")
    ap.add_argument("--source-mount", required=True)
    ap.add_argument("--source-fstype", action="append", default=[])
    ap.add_argument("--dest", required=True, help="the backup folder: exactly <dest-mount>/ai-library")
    ap.add_argument("--dest-mount", required=True)
    ap.add_argument("--dest-fstype", action="append", default=[])
    ap.add_argument("--lib", required=True, help="the directory holding the library package")
    ap.add_argument("--sentinel", default=".ai-library-sentinel")
    ap.add_argument("--findmnt", default="findmnt")
    ap.add_argument("--rsync", default="rsync")
    args = ap.parse_args(argv)
    try:
        return run(args)
    except Refused as exc:
        log(f"REFUSED: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
