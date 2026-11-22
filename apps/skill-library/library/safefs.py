"""Low-level file operations: errors, atomic writes, and name reservation.

Everything here works on NTFS (ntfs3) as well as ext4: no chmod/chown, no reliance on
POSIX permission bits, no hard links. Writes go to a temporary file in the destination
folder and are renamed into place, so a reader never sees a half-written file.
"""
from __future__ import annotations

import os
import secrets
import shutil
import stat
from pathlib import Path

TMP_PREFIX = ".lib-tmp-"  # temporary files; `check` reports any left behind by a crash


class LibraryError(Exception):
    """An operation was refused or failed; the message is meant for a human."""


class PathRefused(LibraryError):
    """A path would leave the library root, cross a symlink, or collide on case."""


class CapExceeded(LibraryError):
    """The write would take the library to (or past) its soft cap."""


def fsync_dir(path: Path) -> None:
    """Best effort: make a rename durable. Some filesystems (and ntfs3) may not support it."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _open_temp(directory: Path) -> tuple[Path, int]:
    # O_EXCL with a random name; mode 0o666 lets the umask decide (ignored on NTFS anyway).
    while True:
        tmp = directory / f"{TMP_PREFIX}{secrets.token_hex(8)}"
        try:
            return tmp, os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            continue


def _finish(tmp: Path, fd: int, dest: Path, writer) -> None:
    try:
        with os.fdopen(fd, "wb") as fh:
            writer(fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except BaseException:
        # The temp file is ours and never held library content on its own; removing it is
        # cleanup, not a deletion of anything in the library.
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    fsync_dir(dest.parent)


def atomic_write_bytes(dest: Path, data: bytes) -> None:
    """Write ``data`` to ``dest`` via temp file + rename (replaces ``dest`` if it exists)."""
    tmp, fd = _open_temp(dest.parent)
    _finish(tmp, fd, dest, lambda fh: fh.write(data))


class _Limited:
    """A reader that hands out at most ``limit`` bytes of ``fh``."""

    def __init__(self, fh, limit: int) -> None:
        self.fh, self.left = fh, limit

    def read(self, n: int = -1) -> bytes:
        n = self.left if n < 0 else min(n, self.left)
        data = self.fh.read(n) if n > 0 else b""
        self.left -= len(data)
        return data


def atomic_copy(src: Path, dest: Path, max_bytes: int | None = None) -> None:
    """Copy the bytes of ``src`` to ``dest`` via temp file + rename.

    With ``max_bytes`` (the size the caller checked against the cap), a source that turns out
    longer is refused before anything is renamed into place, so a growing file cannot slip
    past the cap (and is not silently truncated either).
    """
    with open(src, "rb") as source:
        reader = source if max_bytes is None else _Limited(source, max_bytes)

        def write(fh) -> None:
            shutil.copyfileobj(reader, fh, 1024 * 1024)
            if max_bytes is not None and source.read(1):
                raise LibraryError(f"{src} grew while it was being copied; try again when it is complete")

        tmp, fd = _open_temp(dest.parent)
        _finish(tmp, fd, dest, write)


def append_text(path: Path, text: str) -> None:
    """Append ``text`` (UTF-8) to an existing regular file, without following a symlink.

    Starts on a new line if the file does not end with one (e.g. after an interrupted append).
    """
    fd = os.open(path, os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise LibraryError(f"{path} is not a regular file")
        if st.st_size and os.pread(fd, 1, st.st_size - 1) != b"\n":
            text = "\n" + text
        data = text.encode("utf-8")
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
    finally:
        os.close(fd)


def casefold_names(directory: Path) -> set[str]:
    return {name.casefold() for name in os.listdir(directory)}


def reserve(directory: Path, names: list[str]) -> bool:
    """Claim every name in ``names`` by creating empty placeholder files with O_EXCL.

    Returns False (and releases what it claimed) if any name exists, compared
    case-insensitively, because NTFS and Windows clients treat ``Foo.md`` and ``foo.md`` as
    the same file. The caller then replaces each placeholder atomically with real content, so
    an existing file is never overwritten, even if two writers race.
    """
    existing = casefold_names(directory)
    if any(name.casefold() in existing for name in names):
        return False
    claimed: list[Path] = []
    try:
        for name in names:
            path = directory / name
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666))
            claimed.append(path)
    except FileExistsError:
        release(claimed)
        return False
    return True


def release(placeholders: list[Path]) -> None:
    """Remove placeholders we created that never received content (still zero bytes)."""
    for path in placeholders:
        try:
            if os.lstat(path).st_size == 0:
                os.unlink(path)
        except FileNotFoundError:
            pass
