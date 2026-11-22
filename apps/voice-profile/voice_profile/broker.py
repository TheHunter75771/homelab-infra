"""One non-streaming chat request to the GPU broker (``/api/chat``, Ollama-compatible).

Built on ``http.client`` directly: no environment proxy, no redirects (a 3xx is an error), a
capped body and an overall deadline checked between reads. The reply is untrusted text;
``validate`` decides whether any of it is written.
"""
from __future__ import annotations

import http.client
import json
import socket
import ssl
import time
import urllib.parse
from dataclasses import dataclass

CHAT_PATH = "/api/chat"
MAX_REPLY_BYTES = 1024 * 1024
CHUNK = 64 * 1024
TEMPERATURE = 0.2


class BrokerError(Exception):
    """The call failed after the request was sent (or the reply is unusable). Counts as an
    attempt. The message is safe to log: no URL, no body."""


class BrokerUnreachable(BrokerError):
    """Nothing was sent: the connection itself failed (broker down, DNS, refused). Not counted
    as an attempt, so an outage cannot use up a request's retries."""


@dataclass
class Reply:
    content: str
    prompt_eval_count: int | None   # prompt tokens the model read (Ollama), if reported


class Broker:
    def __init__(self, base_url: str, model: str, *, timeout: float, num_ctx: int) -> None:
        parts = urllib.parse.urlsplit(base_url)
        self.scheme = parts.scheme
        self.host = parts.hostname or ""
        self.port = parts.port
        self.model = model
        self.timeout = float(timeout)
        self.num_ctx = num_ctx

    def body(self, system: str, user: str) -> bytes:
        return json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "think": False,
            "options": {"temperature": TEMPERATURE, "num_ctx": self.num_ctx},
        }).encode("utf-8")

    def _connection(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout,
                                               context=ssl.create_default_context())
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def reachable(self) -> bool:
        """Whether a TCP (and TLS) connection to the broker opens; nothing is sent."""
        conn = self._connection()
        conn.timeout = min(self.timeout, 10.0)
        try:
            conn.connect()
            return True
        except (OSError, socket.timeout):
            return False
        finally:
            conn.close()

    def chat(self, system: str, user: str) -> Reply:
        """The model's reply. Raises BrokerUnreachable or BrokerError."""
        deadline = time.monotonic() + self.timeout
        conn = self._connection()
        try:
            try:
                conn.connect()
            except (OSError, socket.timeout) as exc:
                raise BrokerUnreachable(f"broker unreachable ({type(exc).__name__})") from None
            try:
                conn.request("POST", CHAT_PATH, body=self.body(system, user), headers={
                    "Content-Type": "application/json", "Accept": "application/json",
                    "User-Agent": "voice-profile"})
                _remaining(conn, deadline)
                resp = conn.getresponse()
                if resp.status != 200:
                    raise BrokerError(f"broker answered HTTP {resp.status}")
                raw = _read_capped(conn, resp, deadline)
            except BrokerError:
                raise
            except (OSError, http.client.HTTPException) as exc:
                raise BrokerError(f"broker call failed ({type(exc).__name__})") from None
        finally:
            conn.close()
        return parse_reply(raw)


def _remaining(conn: http.client.HTTPConnection, deadline: float) -> None:
    """Shrink the socket timeout to what is left of the overall deadline."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise BrokerError("broker took longer than the timeout; gave up")
    if conn.sock is not None:
        conn.sock.settimeout(left)


def _read_capped(conn: http.client.HTTPConnection, resp: http.client.HTTPResponse, deadline: float) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        _remaining(conn, deadline)
        chunk = resp.read1(CHUNK) if hasattr(resp, "read1") else resp.read(CHUNK)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > MAX_REPLY_BYTES:
            raise BrokerError(f"broker reply is larger than {MAX_REPLY_BYTES} bytes; refused")
        chunks.append(chunk)


def parse_reply(raw: bytes) -> Reply:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BrokerError("broker reply is not JSON") from None
    message = data.get("message") if isinstance(data, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise BrokerError("broker reply has no message text")
    if data.get("done_reason") == "length":
        raise BrokerError("the model stopped at its length limit (profile cut off)")
    count = data.get("prompt_eval_count")
    return Reply(content, count if isinstance(count, int) and not isinstance(count, bool) else None)
