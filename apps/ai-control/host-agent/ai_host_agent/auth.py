"""The bearer token: read once at start (refused unless the file is safe), compared in
constant time."""

import hashlib
import hmac
import os
import re
import stat

MIN_LEN = 32
MAX_FILE = 4096
# RFC 6750 b64token / RFC 7235 token68
_TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]+")


class TokenError(ValueError):
    """Why the token file was refused. The message never contains the token."""


def load_token(path):
    """Read the token from `path`. Refuses a missing, empty, non-regular or symlinked file, one
    owned by anyone but root or this process's user, and one that is group/world-writable or
    world-readable (the contract's mode is root:aictl-agent 0640)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        raise TokenError("%s: missing" % path) from None
    except OSError as e:
        raise TokenError("%s: cannot open (%s)" % (path, e.strerror)) from None
    try:
        st = os.fstat(fd)  # the opened file itself, not the path: no check/use race
        if not stat.S_ISREG(st.st_mode):
            raise TokenError("%s: not a regular file" % path)
        if st.st_uid not in (0, os.getuid()):
            raise TokenError("%s: owned by uid %d (want root)" % (path, st.st_uid))
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise TokenError("%s: group/world-writable (mode %04o)" % (path, st.st_mode & 0o7777))
        if st.st_mode & stat.S_IROTH:
            raise TokenError("%s: world-readable (mode %04o)" % (path, st.st_mode & 0o7777))
        if st.st_size > MAX_FILE:
            raise TokenError("%s: larger than %d bytes" % (path, MAX_FILE))
        data = os.read(fd, MAX_FILE + 1)
    finally:
        os.close(fd)
    try:
        token = data.decode("ascii").strip()
    except UnicodeDecodeError:
        raise TokenError("%s: not ASCII" % path) from None
    if not token:
        raise TokenError("%s: empty" % path)
    if not _TOKEN.fullmatch(token):
        raise TokenError("%s: one token of A-Z a-z 0-9 . _ ~ + / = - expected" % path)
    if len(token) < MIN_LEN:
        raise TokenError("%s: shorter than %d characters" % (path, MIN_LEN))
    return token


class BearerAuth:
    """Holds a SHA-256 of the token and compares digests with hmac.compare_digest, so neither
    the content nor the length of the presented token changes the comparison's timing."""

    def __init__(self, token):
        self._digest = hashlib.sha256(token.encode("ascii")).digest()

    def check(self, header_values):
        """`header_values`: every Authorization header of the request (latin-1 strings).
        Exactly one, of the form `Bearer <token>`, is accepted."""
        if len(header_values) != 1:
            return False
        scheme, _, cred = header_values[0].strip().partition(" ")
        given = hashlib.sha256(cred.strip().encode("latin-1", "replace")).digest()
        same = hmac.compare_digest(given, self._digest)  # always runs, whatever the scheme
        return same and scheme.lower() == "bearer"
