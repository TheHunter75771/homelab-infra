"""systemd socket activation (Reviewer Q, Q-F3).

With a ``forge-api.socket`` unit (``ListenStream=172.20.0.1:879``, ``FreeBind=yes``) systemd
holds the listening socket even while the service is stopped, so no local user can bind
172.20.0.1:879 in the meantime and pose as the service. systemd passes it as fd 3 with
``LISTEN_PID`` (this process) and ``LISTEN_FDS`` (how many). As ``sd_listen_fds`` does, the
variables are honoured only when ``LISTEN_PID`` is this process, and are then removed from the
environment.

The inherited socket is accepted only if it is exactly one listening TCP stream socket bound to
the configured ``bind`` address and ``port``; anything else is refused (the service exits 2) rather
than served. Without the variables the server binds itself (tests, manual runs).
"""
from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import MutableMapping

SD_LISTEN_FDS_START = 3


class ActivationError(ValueError):
    pass


def _same_address(a: str, b: str) -> bool:
    try:
        x, y = ipaddress.ip_address(a.split("%", 1)[0]), ipaddress.ip_address(b)
    except ValueError:
        return False
    if isinstance(x, ipaddress.IPv6Address) and x.ipv4_mapped is not None:
        x = x.ipv4_mapped
    if isinstance(y, ipaddress.IPv6Address) and y.ipv4_mapped is not None:
        y = y.ipv4_mapped
    return x == y


def inherited_socket(bind: str, port: int, *, environ: MutableMapping[str, str] | None = None,
                     pid: int | None = None, fd: int = SD_LISTEN_FDS_START) -> socket.socket | None:
    """The socket systemd passed, checked against ``bind``/``port``; None if none was passed."""
    env = os.environ if environ is None else environ
    listen_pid = env.get("LISTEN_PID")
    listen_fds = env.get("LISTEN_FDS")
    if listen_pid is None and listen_fds is None:
        return None
    if listen_pid != str(os.getpid() if pid is None else pid):
        return None  # meant for another process (sd_listen_fds semantics)
    for key in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        env.pop(key, None)
    if listen_fds != "1":
        raise ActivationError(f"socket activation passed {listen_fds!r} sockets; exactly 1 is expected")
    try:
        sock = socket.socket(fileno=fd)
    except OSError as exc:
        raise ActivationError(f"fd {fd} is not a usable socket ({exc.strerror or exc})") from None
    try:
        if sock.family not in (socket.AF_INET, socket.AF_INET6) or sock.type != socket.SOCK_STREAM:
            raise ActivationError("the passed socket is not a TCP stream socket")
        if sock.getsockopt(socket.SOL_SOCKET, socket.SO_PROTOCOL) != socket.IPPROTO_TCP:
            raise ActivationError("the passed socket is not TCP")
        if not sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
            raise ActivationError("the passed socket is not listening")
        host, got_port = sock.getsockname()[:2]
        if got_port != port or not _same_address(host, bind):
            raise ActivationError(f"the passed socket is bound to {host} port {got_port}, "
                                  f"not the configured {bind} port {port}")
    except BaseException:
        sock.close()
        raise
    return sock
