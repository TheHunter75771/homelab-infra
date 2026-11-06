"""Dial serve's port through tailscaled's local API, the way any local user can (Reviewer Q, Q2-1). Read-only.

Usage: python3 -I -B localapi_probe.py <tailscaled.sock> <server name for TLS SNI> '<targets JSON>'
Targets: [{"name": str, "host": str, "port": int}, ...].

For each target: POST /localapi/v0/dial (Upgrade: ts-dial, Dial-Host, Dial-Port) on the socket. tailscaled dials the
target itself, as root, and after `101 Switching Protocols` the socket is that TCP stream. The probe then tries TLS
(serve is HTTPS; certificate not checked, SNI = the given name) and a GET /api/cards, and reports what came back:
  "refused <status>"   the local API didn't upgrade;
  "socket <errno>"     the local API socket couldn't be opened;
  "closed" / "reset" / "tls <error>" / "timeout" / "error <errno>"   the upgraded stream died before any HTTP
                       answer (the guard);
  "http <status line>" an HTTP answer came back: the connection reached serve. The caller treats it as a leak.
Prints one JSON object {name: result}. It never prints a response body.
"""

import errno
import json
import socket
import ssl
import sys


def dial(sock_path, sni, host, port):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(8)
    try:
        s.connect(sock_path)
    except OSError as exc:
        return "socket %s" % errno.errorcode.get(exc.errno, type(exc).__name__)
    try:
        req = ("POST /localapi/v0/dial HTTP/1.1\r\nHost: local-tailscaled.sock\r\nConnection: upgrade\r\n"
               "Upgrade: ts-dial\r\nDial-Host: %s\r\nDial-Port: %d\r\nDial-Network: tcp\r\nContent-Length: 0\r\n\r\n"
               % (host, port))
        s.sendall(req.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 65536:
            chunk = s.recv(4096)
            if not chunk:
                break
            head += chunk
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101 " not in status + " ":
            return "refused %s" % (status.split(" ", 2)[1] if status.count(" ") else "none")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        tls = ctx.wrap_socket(s, server_hostname=sni)
        tls.sendall(b"GET /api/cards HTTP/1.1\r\nHost: " + sni.encode("ascii") + b"\r\nConnection: close\r\n\r\n")
        answer = tls.recv(4096)
        if not answer:
            return "closed"
        return "http %s" % answer.split(b"\r\n", 1)[0].decode("latin-1")[:60]
    except ConnectionResetError:
        return "reset"
    except ssl.SSLError as exc:
        return "tls %s" % (exc.reason or type(exc).__name__)
    except TimeoutError:
        return "timeout"
    except OSError as exc:
        return "error %s" % errno.errorcode.get(exc.errno, type(exc).__name__)
    finally:
        s.close()


def main(argv):
    sock_path, sni, targets = argv[1], argv[2], json.loads(argv[3])
    print(json.dumps({t["name"]: dial(sock_path, sni, str(t["host"]), int(t["port"])) for t in targets}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
