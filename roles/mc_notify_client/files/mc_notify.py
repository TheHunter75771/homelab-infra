#!/usr/bin/python3 -I
"""mc-notify: push a message to Noah's phone through Mission Control (POST /api/notify on ai-box).

  mc-notify --title T [--body B] --tag t [--url U] [--urgent]
  mc-notify --unit UNIT [--title T] [--body B] [--url U] [--urgent]

Python 3 standard library only (B2-CONTRACT.md §5; role mc_notify_client installs it as /usr/local/bin/mc-notify).

Config: JSON at $MC_NOTIFY_CONFIG, else /etc/mc-notify/config.json, else ~/.config/mc-notify/config.json; the first
that exists (a path the caller can't even look at, e.g. inside the root-only /etc/mc-notify, counts as absent). When
$MC_NOTIFY_CONFIG is set, only that file is used.
  {"url": "https://ai-box.<tailnet>.ts.net:8443/api/notify", "token_file": "/etc/mc-notify/token"}
Exactly those two keys. The config file must belong to the caller (or root) and must not be writable by group or
others (whoever can write it can send the token elsewhere). The token file: an absolute path to a regular file (not
a symlink) owned by the caller, mode 0600 or tighter; one token of visible ASCII characters, surrounding whitespace
ignored. Anything else is refused with exit 2.

--unit UNIT  for OnFailure=notify-failure@%n.service: tag "unit-<UNIT>" (sanitized), and when not given, title
             "<UNIT> failed" and body "journalctl -u <UNIT>". --unit and --tag can't be combined.
Tags are sanitized: lowercased, every character outside [a-z0-9._-] becomes "-", cut to 64. The result must start
with a letter or digit (MC1's rule), else exit 2.
Arguments must be valid UTF-8 (exit 2 otherwise). Checked before sending (exit 2), with MC1's own rules: title 1-80
characters after stripping, one line (no control or format characters such as bidi overrides and zero-width
characters, no line breaks or lone surrogates); body at most 300 characters; url at most 512 printable ASCII characters
without backslash, either a path starting with "/" (no "//") or an https:// URL without credentials or "//" after
the scheme, on a *.ts.net host; the request at most 4096 bytes. The JSON holds only title, tag and the optional
fields that were given (body, url, and urgent only as true): MC1 refuses null, unknown keys and wrong types.

Sending: one POST with "Authorization: Bearer <token>", JSON body, TLS verified by the default context, no proxy
(proxy variables are ignored), no redirects (a 3xx is an error; the token never follows it). 10 s in all: the name
lookup, connect, TLS handshake and answer run against one deadline (a slow-drip server can't stretch it), and no
single socket operation may take longer either. The url must
be https://; http://127.0.0.1[:port] only when MC_NOTIFY_ALLOW_INSECURE_LOCAL=1 (tests).

Exit codes: 0 = Mission Control accepted it (HTTP 202; prints its status: sent, deduped or held),
            3 = rate limited (HTTP 429), 2 = usage or config error, 1 = anything else.
The token is never printed, not even on errors: messages never quote the server's answer, an exception's text, an
unknown config key or the token file, and a line holding any 8 consecutive characters of the token is replaced by
"<redacted>" before it is written.
"""

from __future__ import annotations

import argparse
import errno
import http.client
import json
import os
import re
import ssl
import stat
import sys
import threading
import unicodedata
import urllib.error
import urllib.request
from urllib.parse import urlsplit

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_RATE_LIMITED = 0, 1, 2, 3
TIMEOUT_S = 10
SYSTEM_CONFIG = "/etc/mc-notify/config.json"
USER_CONFIG = os.path.join("~", ".config", "mc-notify", "config.json")
CONFIG_KEYS = frozenset({"url", "token_file"})
MAX_CONFIG_BYTES = 64 * 1024
MAX_TOKEN_BYTES = 4096
MAX_REQUEST_BYTES = 4096
MAX_TITLE, MAX_BODY, MAX_TAG, MAX_UNIT = 80, 300, 64, 256
INSECURE_ENV = "MC_NOTIFY_ALLOW_INSECURE_LOCAL"
USER_AGENT = "mc-notify/1"

_TAG_BAD = re.compile(r"[^a-z0-9._-]")
_TAG_OK = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_STATUS_OK = re.compile(r"[a-z][a-z_-]{0,31}\Z")
# MC1's url and title rules (apps/mission-control/server/mission_control/notify.py: _URL_CHARS, _NOT_IN_TITLE,
# and config.py _HOST_RE).
_URL_CHARS = re.compile(r"[\x21-\x5b\x5d-\x7e]{1,512}\Z")
_HOST = re.compile(r"(?=.{1,253}\Z)[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*\.?\Z")
_NOT_IN_TITLE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})  # controls, format (bidi, zero-width), surrogates, breaks
# MC1's 400 answer names the field (B2-CONTRACT.md §1); only these are ever echoed.
_FIELDS = frozenset({"title", "body", "tag", "url", "urgent"})


class UsageError(Exception):
    """Exit 2. The message never holds the token."""


class _Out:
    """stdout/stderr writer that never lets the token through (belt and braces: no message is built from it)."""

    def __init__(self) -> None:
        self.secret = ""

    def _clean(self, text: str) -> str:
        s = self.secret
        if s and any(s[i:i + 8] in text for i in range(max(1, len(s) - 7))):
            return "<redacted>"
        return text

    def out(self, text: str) -> None:
        print(self._clean(text), file=sys.stdout, flush=True)

    def err(self, text: str) -> None:
        print("mc-notify: " + self._clean(text), file=sys.stderr, flush=True)


OUT = _Out()


# ---- arguments ---------------------------------------------------------------------------------------------------
class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # argparse exits 2 too; keep that, with our prefix
        self.print_usage(sys.stderr)
        raise UsageError(message)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = _Parser(prog="mc-notify", description="Push a message to Noah's phone through Mission Control.",
                epilog="Exit: 0 accepted (prints sent/deduped/held), 3 rate limited, 2 usage or config error, 1 other.")
    p.add_argument("--title", help="1-80 characters (default with --unit: '<unit> failed')")
    p.add_argument("--body", help="at most 300 characters (default with --unit: 'journalctl -u <unit>', else empty)")
    p.add_argument("--tag", help="a newer push with the same tag replaces the older one on the phone")
    p.add_argument("--url", help="what tapping opens: a /path on Mission Control or an https://*.ts.net URL")
    p.add_argument("--urgent", action="store_true", help="send even inside quiet hours")
    p.add_argument("--unit", help="a failed systemd unit (OnFailure=notify-failure@%%n.service)")
    return p.parse_args(argv)


def sanitize_tag(tag: str) -> str:
    return _TAG_BAD.sub("-", tag.lower())[:MAX_TAG]


def _visible(text: str) -> bool:
    return all(33 <= ord(c) <= 126 for c in text)


def url_ok(url: str) -> bool:
    """MC1's rule for the tap target (B2-CONTRACT.md §1; notify.py url_ok): a path, or https on a *.ts.net host."""
    if not _URL_CHARS.match(url):
        return False
    if url.startswith("/"):
        return "//" not in url
    if not url.startswith("https://") or "//" in url[len("https://"):]:
        return False
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return False
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return False
    host = parts.hostname or ""
    return host.endswith(".ts.net") and _HOST.match(host) is not None


def one_line(text: str) -> bool:
    return not any(unicodedata.category(c) in _NOT_IN_TITLE for c in text)


def _utf8(text: str) -> bool:
    """Python reads argv bytes that aren't UTF-8 as lone surrogates (surrogateescape); they can't be sent as JSON."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def build_message(args: argparse.Namespace) -> dict:
    for name in ("title", "body", "tag", "url", "unit"):
        value = getattr(args, name)
        if value is not None and not _utf8(value):
            raise UsageError(f"--{name} isn't valid UTF-8 (check the shell's locale or the bytes passed)")
    if args.unit is not None:
        unit = args.unit
        if not unit or len(unit) > MAX_UNIT or not _visible(unit):
            raise UsageError("--unit must be a unit name (1-256 visible characters, no spaces)")
        if args.tag is not None:
            raise UsageError("give --tag or --unit, not both (--unit sets the tag)")
        tag = sanitize_tag("unit-" + unit)
        if args.title is None:
            room = MAX_TITLE - len(" failed")
            title = (unit if len(unit) <= room else unit[: room - 1] + "…") + " failed"
        else:
            title = args.title
        body = ("journalctl -u " + unit)[:MAX_BODY] if args.body is None else args.body
    else:
        if args.title is None or args.tag is None:
            raise UsageError("--title and --tag are required (or --unit)")
        title, body, tag = args.title, args.body, sanitize_tag(args.tag)
    title = title.strip()
    if not 0 < len(title) <= MAX_TITLE:
        raise UsageError("--title must be 1-80 characters after stripping spaces")
    if not one_line(title):
        raise UsageError("--title must be one line (no control or format characters, no line breaks)")
    if body is not None and len(body) > MAX_BODY:
        raise UsageError("--body must be at most 300 characters")
    if not _TAG_OK.match(tag):
        raise UsageError("the tag must start with a letter or digit after sanitizing (a-z 0-9 . _ -, at most 64)")
    if args.url is not None and not url_ok(args.url):
        raise UsageError("--url must be a path starting with / (no //) or an https:// URL without credentials on a "
                         ".ts.net host: at most 512 printable ASCII characters, no backslash")
    # Only what was given: MC1 answers 400 to a null, a wrong type or an unknown key.
    msg = {"title": title, "tag": tag}
    if body is not None:
        msg["body"] = body
    if args.url is not None:
        msg["url"] = args.url
    if args.urgent:
        msg["urgent"] = True
    return msg


# ---- config and token --------------------------------------------------------------------------------------------
def config_path() -> str:
    env = os.environ.get("MC_NOTIFY_CONFIG")
    if env:
        return env
    if os.path.lexists(SYSTEM_CONFIG):
        return SYSTEM_CONFIG
    return os.path.expanduser(USER_CONFIG)


def _open_checked(path: str, *, follow: bool, what: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | (0 if follow else os.O_NOFOLLOW)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise UsageError(f"{what} {path} doesn't exist") from None
    except OSError as exc:
        if not follow and exc.errno == errno.ELOOP:
            raise UsageError(f"{what} {path} is a symlink; point the config at the real file") from None
        raise UsageError(f"can't open {what} {path} ({exc.strerror or type(exc).__name__})") from None
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise UsageError(f"{what} {path} isn't a regular file")
    return fd, st


def _read_fd(fd: int, limit: int) -> bytes:
    chunks, size = [], 0
    try:
        while size <= limit:
            chunk = os.read(fd, limit + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def load_config(path: str) -> dict:
    fd, st = _open_checked(path, follow=True, what="config")
    if st.st_uid not in (os.geteuid(), 0) or st.st_mode & 0o022:
        os.close(fd)
        raise UsageError(f"config {path} must belong to you or root and not be writable by group or others")
    raw = _read_fd(fd, MAX_CONFIG_BYTES)
    if len(raw) > MAX_CONFIG_BYTES:
        raise UsageError(f"config {path} is larger than 64 KiB")
    try:
        cfg = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UsageError(f"config {path} isn't valid JSON ({type(exc).__name__})") from None
    if not isinstance(cfg, dict):
        raise UsageError(f"config {path} must be a JSON object")
    keys = set(cfg)
    if keys != CONFIG_KEYS:
        # An unknown key is never echoed (it may be a token pasted into the config); only how many there are.
        missing, unknown = sorted(CONFIG_KEYS - keys), len(keys - CONFIG_KEYS)
        raise UsageError(f"config {path} must have exactly url and token_file"
                         + (f"; missing {', '.join(missing)}" if missing else "")
                         + (f"; {unknown} unknown key{'s' if unknown > 1 else ''}" if unknown else ""))
    url, token_file = cfg["url"], cfg["token_file"]
    if not isinstance(url, str) or not isinstance(token_file, str):
        raise UsageError(f"config {path}: url and token_file must be strings")
    check_endpoint(url)
    if not os.path.isabs(token_file) or "\0" in token_file:
        raise UsageError(f"config {path}: token_file must be an absolute path")
    return {"url": url, "token_file": token_file}


def check_endpoint(url: str) -> None:
    if not _visible(url) or len(url) > 1024:
        raise UsageError("config url has spaces, control or non-ASCII characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UsageError("config url doesn't parse (host or port)") from None
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UsageError("config url must not carry credentials")
    if parts.fragment or "#" in url:
        raise UsageError("config url must not have a fragment")
    if url.startswith("https://") and parts.hostname:
        return
    if (url.startswith("http://") and os.environ.get(INSECURE_ENV) == "1" and parts.hostname == "127.0.0.1"
            and parts.netloc in ("127.0.0.1", f"127.0.0.1:{port}")):
        return
    raise UsageError("config url must be https://<host>/... (http://127.0.0.1 only with "
                     f"{INSECURE_ENV}=1, for tests)")


def load_token(path: str) -> str:
    fd, st = _open_checked(path, follow=False, what="token file")
    if st.st_mode & 0o077:
        os.close(fd)
        raise UsageError(f"token file {path} is mode {stat.S_IMODE(st.st_mode):04o}; it must be 0600 or tighter "
                         f"(chmod 600 {path})")
    if st.st_uid != os.geteuid():
        os.close(fd)
        raise UsageError(f"token file {path} must belong to the user running mc-notify")
    raw = _read_fd(fd, MAX_TOKEN_BYTES)
    if len(raw) > MAX_TOKEN_BYTES:
        raise UsageError(f"token file {path} is larger than 4 KiB")
    try:
        token = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        raise UsageError(f"token file {path} holds non-ASCII bytes") from None
    if not token or not _visible(token):
        raise UsageError(f"token file {path} must hold one token of visible ASCII characters")
    return token


# ---- sending -----------------------------------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # urllib's signature
        return None  # urllib then raises HTTPError(3xx); the Authorization header goes nowhere else


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )


def _json_field(raw: bytes, key: str) -> str:
    try:
        value = json.loads(raw.decode("utf-8")).get(key)
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return ""
    return value if isinstance(value, str) else ""


def _reason(exc: BaseException) -> str:
    """What failed, without any exception text (it can quote headers, i.e. the token)."""
    reason = getattr(exc, "reason", exc)
    if not isinstance(reason, BaseException):
        return type(exc).__name__
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "TLS certificate verification failed"
    if isinstance(reason, ssl.SSLError):
        return "TLS error (" + type(reason).__name__ + ")"
    if isinstance(reason, TimeoutError):
        return f"timed out after {TIMEOUT_S} s"
    if isinstance(reason, OSError) and reason.strerror:
        return reason.strerror.lower() + " (" + type(reason).__name__ + ")"
    return type(reason).__name__


def send(url: str, token: str, message: dict) -> int:
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_REQUEST_BYTES:
        raise UsageError("the message is larger than 4096 bytes")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
        "User-Agent": USER_AGENT,
    })
    # urllib's timeout bounds each socket operation, not the whole exchange, and not the name lookup at all. So the
    # exchange runs in a daemon thread and this one waits TIMEOUT_S for it in total; a thread still stuck in DNS or
    # a slow-drip answer is abandoned when the process exits.
    box: dict = {}

    def work() -> None:
        try:
            box["answer"] = _exchange(req)
        except BaseException as exc:  # noqa: BLE001 - handed to the waiting thread
            box["error"] = exc

    worker = threading.Thread(target=work, name="mc-notify-request", daemon=True)
    worker.start()
    worker.join(TIMEOUT_S)
    if worker.is_alive():
        OUT.err(f"couldn't reach Mission Control: timed out, no answer within {TIMEOUT_S} s (lookup, connect, TLS "
                "and answer together)")
        return EXIT_ERROR
    exc = box.get("error")
    if isinstance(exc, (urllib.error.URLError, OSError, http.client.HTTPException, ValueError)):
        OUT.err("couldn't reach Mission Control: " + _reason(exc))
        return EXIT_ERROR
    if exc is not None:
        raise exc
    return _answer(*box["answer"])


def _exchange(req: urllib.request.Request) -> tuple[int, bytes, str]:
    """One POST: (HTTP code, at most 4096 bytes of the answer, its Retry-After). Raises on no answer."""
    try:
        with _opener().open(req, timeout=TIMEOUT_S) as resp:
            return resp.status, resp.read(MAX_REQUEST_BYTES), ""
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read(MAX_REQUEST_BYTES)
        except (OSError, http.client.HTTPException):
            pass
        return exc.code, raw, exc.headers.get("Retry-After", "") if exc.headers else ""


def _answer(code: int, raw: bytes, retry_after: str) -> int:
    if code == 202:
        status = _json_field(raw, "status")
        OUT.out(status if _STATUS_OK.match(status) else "accepted")
        return EXIT_OK
    if code == 429:
        wait = retry_after.strip()
        hint = f"; retry after {wait} s" if wait.isdigit() and len(wait) <= 6 else ""
        OUT.err("rate limited by Mission Control" + hint)
        return EXIT_RATE_LIMITED
    field = _json_field(raw, "error")
    known = {
        400: "Mission Control refused the message" + (f" (field: {field})" if field in _FIELDS else ""),
        401: "Mission Control refused the token (check token_file against the sender's token in the vault)",
        403: "Mission Control doesn't accept this sender from this address (allow_from)",
        405: "Mission Control has no POST /api/notify at this url",
        413: "the message is too large for Mission Control",
        415: "Mission Control wants application/json",
        503: "push isn't set up on Mission Control (no VAPID keys or subscriptions)",
    }
    detail = known.get(code, "unexpected answer" if code < 300 or code >= 400 else "redirects aren't followed")
    OUT.err(f"HTTP {code}: {detail}")
    return EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    try:
        message = build_message(parse_args(argv))
        cfg = load_config(config_path())
        token = load_token(cfg["token_file"])
        OUT.secret = token
        return send(cfg["url"], token, message)
    except UsageError as exc:
        OUT.err(str(exc))
        return EXIT_USAGE
    except KeyboardInterrupt:
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - never a traceback: its text could quote the token
        OUT.err("unexpected error (" + type(exc).__name__ + ")")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
