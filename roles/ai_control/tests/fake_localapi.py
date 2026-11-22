"""A stand-in for tailscaled's local API dial (tests/test_ai_control.yml, Reviewer Q): serves ONE connection on a Unix
socket, writes the request head it got to LOG, then answers per MODE:
  refuse   403 (the dial isn't allowed)
  reset    101, then a TCP-style abort (what the guard's reject looks like through tailscaled)
  noah     101, TLS, then 200 with a JSON body (AI Control answering as Noah: the guard failed)
  json403  101, TLS, then 403 with a JSON body (still AI Control's own answer)
  bad502   101, TLS, then 502 with a text body (serve's backend unreachable)
Usage: fake_localapi.py SOCKET MODE CERT KEY LOG"""
import os
import socket
import ssl
import struct
import sys

path, mode, cert, key, log = sys.argv[1:6]
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(path + ".tmp")
os.rename(path + ".tmp", path)
srv.listen(1)
srv.settimeout(20)
conn, _ = srv.accept()
conn.settimeout(10)
buf = b""
while b"\r\n\r\n" not in buf:
    chunk = conn.recv(4096)
    if not chunk:
        break
    buf += chunk
with open(log, "wb") as fh:
    fh.write(buf.split(b"\r\n\r\n", 1)[0])
if mode == "refuse":
    conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 19\r\n\r\ndial access denied\n")
    conn.close()
    sys.exit(0)
conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: ts-dial\r\nConnection: upgrade\r\n\r\n")
if mode == "reset":
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    conn.close()
    sys.exit(0)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.minimum_version = ssl.TLSVersion.TLSv1_2
ctx.load_cert_chain(cert, key)
tls = ctx.wrap_socket(conn, server_side=True)
req = b""
while b"\r\n\r\n" not in req:
    chunk = tls.recv(4096)
    if not chunk:
        break
    req += chunk
status, ctype, body = {
    "noah": ("200 OK", "application/json", b'{"services": []}'),
    "json403": ("403 Forbidden", "application/json", b'{"error": "forbidden"}'),
    "bad502": ("502 Bad Gateway", "text/plain", b"bad gateway\n"),
}[mode]
tls.sendall(b"HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
            % (status.encode(), ctype.encode(), len(body), body))
tls.close()
