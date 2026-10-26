"""The content hash that pins an approved skill (CONTRACTS §12.12, "hash-pinned approval").

Definition (stable; the sandbox runner must compute the same thing):

    h = sha256()
    for each regular file under the skill folder, sorted by its POSIX path relative to the
    folder (compared as UTF-8 bytes), skipping a top-level CHANGELOG.md:
        h.update(b"file\\0" + path_utf8 + b"\\0" + str(len(content)).encode() + b"\\0" + content)
    hash = "sha256:" + h.hexdigest()

The length prefix makes the encoding unambiguous (no two different trees hash alike by
shifting bytes between a name and a body). Folders contribute only through their files, so an
empty folder does not change the hash. A symlink or any other non-regular entry anywhere in
the tree is refused rather than hashed: an approved skill is plain files only.

``CHANGELOG.md`` at the top level is excluded because the forge writes it *after* approval
(30-skills/<skill>/CHANGELOG.md records the approved hash). A draft may not contain one.
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from library import PathRefused

CHANGELOG = "CHANGELOG.md"
PREFIX = "sha256:"


def tree_files(folder: Path) -> list[tuple[str, Path]]:
    """(relative POSIX path, absolute path) for every regular file, sorted; refuses links."""
    found: list[tuple[str, Path]] = []

    def walk(directory: Path, rel: str) -> None:
        with os.scandir(directory) as it:
            entries = list(it)
        for entry in entries:
            child_rel = f"{rel}{entry.name}"
            st = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                raise PathRefused(f"refusing the symlink {child_rel} in a skill")
            if stat.S_ISDIR(st.st_mode):
                walk(Path(entry.path), child_rel + "/")
            elif stat.S_ISREG(st.st_mode):
                found.append((child_rel, Path(entry.path)))
            else:
                raise PathRefused(f"{child_rel} is not a regular file or folder")

    st = os.lstat(folder)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise PathRefused(f"{folder} is not a real folder")
    walk(folder, "")
    return sorted(found, key=lambda item: item[0].encode("utf-8"))


def file_digests(folder: Path) -> list[tuple[str, str]]:
    """(relative path, sha256 hex) per hashed file, in hash order (for showing Noah what changed)."""
    return [(rel, hashlib.sha256(path.read_bytes()).hexdigest())
            for rel, path in tree_files(folder) if rel != CHANGELOG]


def skill_hash(folder: Path) -> str:
    h = hashlib.sha256()
    for rel, path in tree_files(folder):
        if rel == CHANGELOG:
            continue
        data = path.read_bytes()
        h.update(b"file\0" + rel.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0" + data)
    return PREFIX + h.hexdigest()
