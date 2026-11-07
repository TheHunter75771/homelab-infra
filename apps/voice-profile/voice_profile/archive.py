"""Archive a file, and write a file only where none exists, through directory file descriptors.

**Archive.** ``archive`` first tries ``Library.archive`` (a rename). In the voice-profile unit,
``40-writing`` and ``90-archive/40-writing`` are separate ReadWritePaths bind mounts, so that rename
fails with ``EXDEV`` (Lead, §12.21); on ``EXDEV`` only, ``copy_archive`` moves the file itself.
``copy_archive`` is also used directly, with an expected identity, for a profile draft that Noah
may be editing (``expect``): there a rename could carry away a file he saved a moment ago.

``copy_archive``, one regular file:

1. open the source folder and the archive folder by walking from the library root one component at
   a time with ``O_DIRECTORY | O_NOFOLLOW`` (a symlinked or swapped folder is refused or, if it is
   swapped after the walk, simply not used: every later step goes through the held folder fds);
2. open the source ``O_NOFOLLOW`` relative to its folder fd; its identity (device, inode, size,
   mtime) must equal ``expect`` when one is given;
3. copy it into a fresh ``O_EXCL | O_NOFOLLOW`` temp file in the archive folder; the source's
   identity must be unchanged after the copy (no torn copy); ``fsync`` the file and the folder;
4. claim the final name with an empty ``O_EXCL`` placeholder (case-insensitive, ``-2``, ``-3``, ...
   like the library), rename the temp file over it, ``fsync`` the folder;
5. ``lstat`` the source name in its folder: it must still be the very file that was copied (same
   identity), or nothing is unlinked (Noah saved it meanwhile: his file stays; the archived copy
   is the old version). Then rename it to a temp name in the same folder (atomic: exactly one
   inode leaves the name), check that inode once more, and only then unlink it; if a save slipped
   in just before the rename, that file is linked back to its name (never over a newer one) and
   nothing is removed. ``fsync`` the folder.

If any step before 5 fails, the temp file and the placeholder are removed and the source is kept.

**Write new.** ``write_new`` writes a file only if its name is absent: a temp file in the folder
(``O_EXCL``), ``fsync``, then ``link(2)`` to the final name, which fails with ``EEXIST`` if anything
appeared there; then the temp name is unlinked. A filesystem without hard links falls back to an
``O_EXCL`` placeholder renamed over (never over anything but the placeholder).
"""
from __future__ import annotations

import errno
import os
import secrets
import stat
from pathlib import PurePosixPath

from library import Library, LibraryError, PathRefused, naming
from library.core import ARCHIVE
from library.safefs import TMP_PREFIX

CHUNK = 1024 * 1024
MAX_NAMES = 10_000
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRFLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | NOFOLLOW
NO_LINKS = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EMLINK}

Identity = tuple[int, int, int, int]


class Changed(LibraryError):
    """The file changed while it was being archived; nothing was removed."""


def identity(st: os.stat_result) -> Identity:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def archive(lib: Library, rel: str) -> list[tuple[str, str]]:
    """Move ``rel`` (a regular file inside the library) to ``90-archive/<same path>``; returns
    ``[(rel, archived rel)]`` like ``Library.archive``."""
    try:
        return lib.archive(rel)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    return copy_archive(lib, rel)


def open_dir(lib: Library, rel_dir: str) -> int:
    """An fd for the library folder ``rel_dir``, walked from the root with O_NOFOLLOW."""
    parts = [p for p in PurePosixPath(rel_dir).parts if p not in ("", ".")]
    if any(p in ("..", "/") or "\\" in p or "\0" in p for p in parts):
        raise PathRefused(f"refusing path {rel_dir!r}")
    fd = os.open(lib.root, DIRFLAGS)
    try:
        for part in parts:
            try:
                nxt = os.open(part, DIRFLAGS, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise PathRefused(f"{rel_dir}: {part} is a symlink or not a folder") from None
                raise
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _copy(src_fd: int, dst_fd: int) -> None:
    while True:
        data = os.read(src_fd, CHUNK)
        if not data:
            return
        while data:
            data = data[os.write(dst_fd, data):]


def _claim(dfd: int, name: str) -> str:
    """An unused name like ``name`` (``-2``, ``-3``, ... compared casefolded), claimed with an empty
    O_EXCL placeholder."""
    taken = {n.casefold() for n in os.listdir(dfd)}
    for n in range(1, MAX_NAMES):
        candidate = naming.numbered(name, n, is_dir=False)
        if candidate.casefold() in taken:
            continue
        try:
            os.close(os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o666, dir_fd=dfd))
        except FileExistsError:
            continue
        return candidate
    raise LibraryError(f"too many archived items named {name}")


def _release(dfd: int, name: str) -> None:
    """Remove a placeholder we created, if it is still empty."""
    try:
        if os.stat(name, dir_fd=dfd, follow_symlinks=False).st_size == 0:
            os.unlink(name, dir_fd=dfd)
    except FileNotFoundError:
        pass


def _unlink_quiet(dfd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dfd)
    except FileNotFoundError:
        pass


def copy_archive(lib: Library, rel: str, *, expect: Identity | None = None) -> list[tuple[str, str]]:
    """The fd-based move (see the module doc)."""
    path = PurePosixPath(rel)
    if not path.parts or path.parts[0] == ARCHIVE:
        raise LibraryError(f"{rel} is already archived")
    lib.abs_path(rel)                                   # the library's own checks first
    dest_rel = f"{ARCHIVE}/{path.parent.as_posix()}"
    lib.ensure_dir(dest_rel)
    sfd = open_dir(lib, path.parent.as_posix())
    try:
        dfd = open_dir(lib, dest_rel)
        try:
            return _move(lib, sfd, dfd, path.name, rel, dest_rel, expect)
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)


def _move(lib: Library, sfd: int, dfd: int, name: str, rel: str, dest_rel: str,
          expect: Identity | None) -> list[tuple[str, str]]:
    try:
        src_fd = os.open(name, os.O_RDONLY | NOFOLLOW, dir_fd=sfd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PathRefused(f"{rel} is a symlink") from None
        raise
    tmp = f"{TMP_PREFIX}{secrets.token_hex(8)}"
    claimed: str | None = None
    renamed = False
    try:
        try:
            before = os.fstat(src_fd)
            if not stat.S_ISREG(before.st_mode):
                raise LibraryError(f"{rel} is not a regular file")
            if expect is not None and identity(before) != expect:
                raise Changed(f"{rel} changed before it could be archived; nothing removed")
            dst_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o666, dir_fd=dfd)
            try:
                _copy(src_fd, dst_fd)
                os.fsync(dst_fd)
            finally:
                os.close(dst_fd)
            if identity(os.fstat(src_fd)) != identity(before):
                raise Changed(f"{rel} changed while it was being copied; nothing removed")
        finally:
            os.close(src_fd)
        os.fsync(dfd)
        claimed = _claim(dfd, name)
        os.replace(tmp, claimed, src_dir_fd=dfd, dst_dir_fd=dfd)   # same folder, only over our placeholder
        renamed = True
        os.fsync(dfd)
    except BaseException:
        _unlink_quiet(dfd, tmp)
        if claimed is not None and not renamed:
            _release(dfd, claimed)
        raise
    archived = f"{dest_rel}/{claimed}"
    try:
        now = os.stat(name, dir_fd=sfd, follow_symlinks=False)
    except FileNotFoundError:
        now = None
    if now is None or identity(now) != identity(before):
        raise Changed(f"{rel} changed after it was copied to {archived}; it was not removed")
    # unlink(2) by name is not conditional: a save landing between the check above and the unlink
    # would be lost. So take the name away atomically first (exactly one inode moves), check that
    # it is the very file copied, and only then unlink it; if it is not, put it back.
    grave = f"{TMP_PREFIX}{secrets.token_hex(8)}"
    os.rename(name, grave, src_dir_fd=sfd, dst_dir_fd=sfd)
    taken = os.stat(grave, dir_fd=sfd, follow_symlinks=False)
    if identity(taken) != identity(before):
        _put_back(sfd, grave, name, rel)
        raise Changed(f"{rel} was saved again while it was being archived; it was kept")
    os.unlink(grave, dir_fd=sfd)
    os.fsync(sfd)
    return [(rel, archived)]


def _put_back(dfd: int, tmp: str, name: str, rel: str) -> None:
    """Give ``tmp`` its name back, never over a file that appeared there meanwhile."""
    try:
        try:
            os.link(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
        except OSError as exc:
            if exc.errno not in NO_LINKS:
                raise
            os.close(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o666, dir_fd=dfd))
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            os.fsync(dfd)
            return
    except FileExistsError:
        raise Changed(f"{rel} was saved twice while it was being archived; the earlier save is kept as "
                      f"{PurePosixPath(rel).parent}/{tmp}") from None
    os.unlink(tmp, dir_fd=dfd)
    os.fsync(dfd)


def write_new(lib: Library, rel: str, data: bytes) -> None:
    """Write ``rel`` only if no file of that name (any case) exists; FileExistsError otherwise."""
    path = PurePosixPath(rel)
    lib.abs_path(rel)
    dfd = open_dir(lib, path.parent.as_posix())
    tmp = f"{TMP_PREFIX}{secrets.token_hex(8)}"
    placeholder = False
    try:
        if any(n.casefold() == path.name.casefold() for n in os.listdir(dfd)):
            raise FileExistsError(errno.EEXIST, f"{rel} exists")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o666, dir_fd=dfd)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, path.name, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
        except OSError as exc:
            if exc.errno not in NO_LINKS:
                raise
            # No hard links here: an O_EXCL placeholder, then a rename over it (only over it).
            os.close(os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o666, dir_fd=dfd))
            placeholder = True
            os.replace(tmp, path.name, src_dir_fd=dfd, dst_dir_fd=dfd)
            placeholder = False
        _unlink_quiet(dfd, tmp)
        os.fsync(dfd)
    except BaseException:
        _unlink_quiet(dfd, tmp)
        if placeholder:
            _release(dfd, path.name)
        raise
    finally:
        os.close(dfd)
