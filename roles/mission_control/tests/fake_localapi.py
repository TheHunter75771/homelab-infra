"""Fake tailscaled local API for files/localapi_probe.py (tests/test_mission_control.yml). Serves one Unix socket.
Usage: fake_localapi.py <socket path> <mode> <cert dir>
  refuse  answers 403 to the dial;
  close   upgrades (101) and closes the stream at once (what the guard does to tailscaled's dial: a reset/close);
  leak    upgrades, then speaks TLS with a self-signed certificate and answers the GET with 200 + JSON (what a
          missing guard looks like: serve stamps Noah's login and answers).
Writes "ready" to stdout once listening; serves until killed."""
import datetime
import os
import socket
import ssl
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

path, mode, certdir = sys.argv[1], sys.argv[2], sys.argv[3]
ctx = None
if mode == "leak":
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ai-box.example.ts.net")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    cp, kp = os.path.join(certdir, "c.pem"), os.path.join(certdir, "k.pem")
    open(cp, "wb").write(cert.public_bytes(serialization.Encoding.PEM))
    open(kp, "wb").write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cp, kp)
if os.path.exists(path):
    os.unlink(path)
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(path)
srv.listen(4)
print("ready", flush=True)
while True:
    conn, _ = srv.accept()
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = conn.recv(4096)
        if not chunk:
            break
        head += chunk
    if mode == "refuse" or b"Upgrade: ts-dial" not in head or b"Dial-Port:" not in head:
        conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        conn.close()
        continue
    conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nConnection: upgrade\r\nUpgrade: ts-dial\r\n\r\n")
    if mode == "close":
        conn.close()
        continue
    try:
        tls = ctx.wrap_socket(conn, server_side=True)
        tls.recv(4096)
        body = b'{"cards": {}}'
        tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
        tls.close()
    except (OSError, ssl.SSLError):
        conn.close()
