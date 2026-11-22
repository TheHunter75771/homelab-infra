"""OpenPGP fingerprints for role docker's apt key check (docs/CONTRACTS.md §12.8; Python stdlib only).

docker_openpgp_fingerprints(data) -> list of the primary-key fingerprints in an OpenPGP public key file,
upper-case hex, in file order. `data` is the file's text (ASCII armor) or its bytes (armored or binary).

Only primary keys (packet tag 6) are listed; subkeys are bound to them and apt trusts them through them.
A v4 key's fingerprint is SHA-1 over 0x99, the two-byte body length and the key packet body (RFC 4880
§12.2); a v6 key's is SHA-256 over 0x9B, a four-byte length and the body (RFC 9580 §5.5.4). Any other
version is reported as "unsupported-v<N>", which never equals a real fingerprint, so the caller's
comparison fails closed. Malformed input raises ValueError: bad armor, bad CRC, truncated packets, and
any armored text that is not exactly ONE public key block with nothing but whitespace around it (gpg and apt
load every block in a file, so a second block or stray text must never ride along with the pinned key).

docker_openpgp_problem(data, expected, base64_input=False) -> "" when the file holds exactly one primary key
with the expected fingerprint, otherwise a human-readable reason (parse errors included, never raised), for an
assert's fail_msg. base64_input=True takes ansible.builtin.slurp's `content` as is.
"""

import base64
import binascii
import hashlib

_ARMOR_BEGIN = "-----BEGIN PGP PUBLIC KEY BLOCK-----"
_ARMOR_END = "-----END PGP PUBLIC KEY BLOCK-----"


def _crc24(data):
    crc = 0xB704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def _dearmor(text):
    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if not lines or lines[0] != _ARMOR_BEGIN or lines[-1] != _ARMOR_END or len(lines) < 2:
        raise ValueError("not exactly one PGP PUBLIC KEY BLOCK with only whitespace around it")
    body = lines[1:-1]
    if any(line.startswith("-----") for line in body):
        raise ValueError("more than one armor block in the key file")
    # Armor headers ("Key: value") end at the first blank line; without headers the blank line may be absent.
    if "" in body and all(":" in line for line in body[:body.index("")]):
        body = body[body.index("") + 1:]
    checksum = None
    payload = []
    for line in body:
        if not line:
            continue
        if line.startswith("=") and len(line) == 5:
            checksum = line[1:]
            continue
        payload.append(line)
    try:
        data = base64.b64decode("".join(payload), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("armored key is not valid base64: %s" % exc) from None
    if checksum is not None:
        try:
            expected = int.from_bytes(base64.b64decode(checksum, validate=True), "big")
        except (binascii.Error, ValueError) as exc:
            raise ValueError("armor checksum is not valid base64: %s" % exc) from None
        if _crc24(data) != expected:
            raise ValueError("armor checksum mismatch (corrupt key file)")
    return data


def _packets(data):
    pos = 0
    while pos < len(data):
        header = data[pos]
        pos += 1
        if not header & 0x80:
            raise ValueError("byte %d is not an OpenPGP packet header" % (pos - 1))
        if header & 0x40:  # new format
            tag = header & 0x3F
            if pos >= len(data):
                raise ValueError("truncated packet length")
            first = data[pos]
            if first < 192:
                length, pos = first, pos + 1
            elif first < 224:
                if pos + 1 >= len(data):
                    raise ValueError("truncated packet length")
                length, pos = ((first - 192) << 8) + data[pos + 1] + 192, pos + 2
            elif first == 255:
                if pos + 5 > len(data):
                    raise ValueError("truncated packet length")
                length, pos = int.from_bytes(data[pos + 1:pos + 5], "big"), pos + 5
            else:
                raise ValueError("partial body lengths are not valid in a key file")
        else:  # old format
            tag = (header >> 2) & 0x0F
            size = {0: 1, 1: 2, 2: 4}.get(header & 0x03)
            if size is None:
                raise ValueError("indeterminate packet lengths are not valid in a key file")
            if pos + size > len(data):
                raise ValueError("truncated packet length")
            length, pos = int.from_bytes(data[pos:pos + size], "big"), pos + size
        if pos + length > len(data):
            raise ValueError("truncated packet body (tag %d)" % tag)
        yield tag, data[pos:pos + length]
        pos += length


def _fingerprint(body):
    if not body:
        raise ValueError("empty public key packet")
    version = body[0]
    if version == 4:
        return hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).hexdigest().upper()  # nosec B324
    if version == 6:
        return hashlib.sha256(b"\x9b" + len(body).to_bytes(4, "big") + body).hexdigest().upper()
    return "unsupported-v%d" % version


def docker_openpgp_fingerprints(data):
    if isinstance(data, str):
        raw = _dearmor(data)
    else:
        raw = bytes(data)
        if raw.lstrip().startswith(b"-----"):
            raw = _dearmor(raw.decode("ascii"))  # UnicodeDecodeError is a ValueError
        elif b"-----BEGIN " in raw:
            raise ValueError("text before the armor block")
    if not raw:
        raise ValueError("key file is empty")
    return [_fingerprint(body) for tag, body in _packets(raw) if tag == 6]


def docker_openpgp_problem(data, expected, base64_input=False):
    try:
        if base64_input:  # ansible.builtin.slurp content; decoded here so binary junk is reported, not raised
            data = base64.b64decode(data, validate=True)
        found = docker_openpgp_fingerprints(data)
    except (ValueError, TypeError) as exc:
        return "it is not a valid OpenPGP public key file (%s)" % exc
    if found != [str(expected).upper()]:
        return "it holds primary key(s) %s, expected exactly %s" % (found, str(expected).upper())
    return ""


class FilterModule:
    def filters(self):
        return {
            "docker_openpgp_fingerprints": docker_openpgp_fingerprints,
            "docker_openpgp_problem": docker_openpgp_problem,
        }
