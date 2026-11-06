"""AI Control's local-access guard probe (roles/ai_control, Reviewer Q). Run by tasks/verify_guard.yml as
`python3 -I -B -c <this file> MODE ...` (nothing is installed for it), as root and as an unprivileged user.
Prints one JSON object on stdout and exits 0; the assertions are verify_guard_assert.yml's.

  tcp  '[{"name": n, "host": h, "port": p}, ...]'
       A plain TCP connect to each target, nothing sent. {"n": "ok" | errno name (ECONNREFUSED, ...)}.
  dial SOCKET HOST PORT SNI
       What any local user can do with tailscaled's local API: POST /localapi/v0/dial (Upgrade: ts-dial) for
       HOST:PORT. On "101 Switching Protocols" tailscaled has connected for us, as root; then a TLS handshake (SNI,
       no certificate check: only the answer matters) and `GET /api/summary`. Reports what came back:
       {"dial": 101 | other HTTP status | errno name, "upstream": "reset" | "closed" | "timeout" | "tls: ..." |
        "http 403" | ..., "exposed": true when the answer was a 200 or a JSON body (AI Control answered as Noah)}.
"""
import errno
import json
import socket
import ssl
import sys

TIMEOUT = 5.0


def _err(exc):
    if isinstance(exc, socket.timeout):
        return "timeout"
    code = getattr(exc, "errno", None)
    return errno.errorcode.get(code, type(exc).__name__) if code else type(exc).__name__


def tcp(targets):
    out = {}
    for t in targets:
        family = socket.AF_INET6 if ":" in str(t["host"]) else socket.AF_INET
        s = socket.socket(family, socket.SOCK_STREAM)
        s.settimeout(TIMEOUT)
        try:
            s.connect((t["host"], int(t["port"])))
            out[t["name"]] = "ok"
        except OSError as exc:
            out[t["name"]] = _err(exc)
        finally:
            s.close()
    return out


def _read_head(sock, limit=65536):
    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) < limit:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head, rest


def _status(head):
    parts = head.split(b"\r\n", 1)[0].split()
    return int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None


def dial(path, host, port, sni):
    result = {"dial": None, "upstream": None, "exposed": False}
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    try:
        s.connect(path)
        s.sendall(("POST /localapi/v0/dial HTTP/1.1\r\nHost: local-tailscaled.sock\r\nSec-Tailscale: localapi\r\n"
                   f"Upgrade: ts-dial\r\nConnection: upgrade\r\nDial-Host: {host}\r\nDial-Port: {port}\r\n"
                   "Content-Length: 0\r\n\r\n").encode("ascii"))
        head, rest = _read_head(s)
        result["dial"] = _status(head) if head else "closed"
        if result["dial"] != 101:
            return result
        if rest:
            result["upstream"] = "unexpected bytes before TLS"
            return result
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            tls = ctx.wrap_socket(s, server_hostname=sni)
        except ssl.SSLError as exc:
            result["upstream"] = f"tls: {exc.reason or type(exc).__name__}"
            return result
        except OSError as exc:
            result["upstream"] = "reset" if exc.errno in (errno.ECONNRESET, errno.EPIPE) else _err(exc)
            return result
        tls.sendall(f"GET /api/summary HTTP/1.1\r\nHost: {sni}\r\nConnection: close\r\n\r\n".encode("ascii"))
        head, body = _read_head(tls)
        if not head:
            result["upstream"] = "closed"
            return result
        status = _status(head)
        result["upstream"] = f"http {status}"
        result["exposed"] = status == 200 or body.lstrip()[:1] in (b"{", b"[")
    except ConnectionResetError:
        result["upstream" if result["dial"] == 101 else "dial"] = "reset"
    except OSError as exc:
        result["upstream" if result["dial"] == 101 else "dial"] = _err(exc)
    finally:
        s.close()
    return result


def main(argv):
    if len(argv) == 2 and argv[0] == "tcp":
        print(json.dumps(tcp(json.loads(argv[1]))))
    elif len(argv) == 5 and argv[0] == "dial":
        print(json.dumps(dial(argv[1], argv[2], argv[3], argv[4])))
    else:
        print(json.dumps({"error": "usage: tcp TARGETS_JSON | dial SOCKET HOST PORT SNI"}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
