"""Optional: append the day's entry to a Notion page (CONTRACTS section 12.18.2).

Off unless ``notion.json`` exists (``{"token": ..., "page_id": ...}``, root:fdigest 0640). The only
request is ``PATCH https://api.notion.com/v1/blocks/{page_id}/children`` (Notion's "append block
children"); the host is a constant, so no config can point the token anywhere else. The token is
never logged and never part of an error message. Any failure is reported to the caller, which logs
it and carries on: Notion never fails the run.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import ssl
import stat

from . import httpcore

NOTION_HOST = "api.notion.com"
NOTION_VERSION = "2022-06-28"
MAX_CHILDREN = 100       # Notion: at most 100 blocks per append request, and 100 rows per table
MAX_TEXT = 2000          # Notion: at most 2000 characters per text object
MAX_CONFIG_BYTES = 64 * 1024
MAX_REPLY_BYTES = 1024 * 1024
_TOKEN_RE = re.compile(r"[\x21-\x7e]{1,256}")  # printable ASCII, no spaces: nothing to inject into a header
_PAGE_RE = re.compile(r"[0-9a-fA-F]{32}")


class NotionError(Exception):
    """Notion is misconfigured or failed; the message never holds the token."""


def load_config(path: str) -> tuple[str, str] | None:
    """(token, page_id), or None when the file does not exist (Notion off)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as e:
        raise NotionError(f"the Notion config can't be read ({e.strerror})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotionError("the Notion config is not a regular file")
        raw = os.read(fd, MAX_CONFIG_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_CONFIG_BYTES:
        raise NotionError("the Notion config is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise NotionError("the Notion config is not valid JSON") from None
    if not isinstance(data, dict) or set(data) != {"token", "page_id"}:
        raise NotionError('the Notion config must hold exactly "token" and "page_id"')
    token, page = data["token"], data["page_id"]
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise NotionError("the Notion token is malformed (printable ASCII, no spaces)")
    page_hex = page.replace("-", "") if isinstance(page, str) else ""
    if not _PAGE_RE.fullmatch(page_hex):
        raise NotionError("the Notion page_id must be a page id (32 hex digits, dashes optional)")
    return token, page_hex.lower()


def _rich(text: str) -> list[dict]:
    text = text or " "
    return [{"type": "text", "text": {"content": text[i:i + MAX_TEXT]}} for i in range(0, len(text), MAX_TEXT)][:100]


def _block(kind: str, text: str) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": _rich(text)}}


def to_blocks(day_iso: str, blocks: list[tuple], summary: str | None, note: str, label: str, footer: str) -> list[dict]:
    """The same entry as the library log, as Notion blocks (heading, paragraphs, tables, a quote)."""
    out = [_block("heading_2", day_iso)]
    for b in blocks:
        if b[0] == "para":
            out.append(_block("paragraph", b[1]))
            continue
        _, caption, header, rows = b
        out.append(_block("paragraph", caption))
        width = len(header)
        table_rows = [header] + rows[: MAX_CHILDREN - 1]
        out.append(
            {
                "object": "block",
                "type": "table",
                "table": {
                    "table_width": width,
                    "has_column_header": True,
                    "has_row_header": False,
                    "children": [
                        {"object": "block", "type": "table_row",
                         "table_row": {"cells": [_rich(str(c)) for c in (list(r) + [""] * width)[:width]]}}
                        for r in table_rows
                    ],
                },
            }
        )
        if len(rows) > MAX_CHILDREN - 1:
            out.append(_block("paragraph", f"({len(rows) - (MAX_CHILDREN - 1)} more rows are in the library log.)"))
    out.append(_block("quote", f"{label}: {summary}") if summary else _block("paragraph", note))
    out.append(_block("paragraph", footer))
    return out


def _https(host: str, timeout: float) -> http.client.HTTPConnection:
    return http.client.HTTPSConnection(host, 443, timeout=timeout, context=ssl.create_default_context())


def append(token: str, page_id: str, children: list[dict], *, timeout: float = 20, connect=_https) -> int:
    """Append ``children`` to the page, in requests of at most 100 blocks. Returns the request count.

    ``connect(host, timeout)`` returns an http.client connection; tests pass one to a local fake.
    """
    if not _PAGE_RE.fullmatch(page_id):
        raise NotionError("the Notion page_id is malformed")
    path = f"/v1/blocks/{page_id}/children"
    headers = {
        "Host": NOTION_HOST,
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "finance-digest",
    }
    sent = 0
    for i in range(0, len(children), MAX_CHILDREN):
        body = json.dumps({"children": children[i:i + MAX_CHILDREN]}).encode("utf-8")

        def go(body=body) -> int:
            conn = connect(NOTION_HOST, timeout)
            try:
                conn.request("PATCH", path, body=body, headers=headers)
                response = conn.getresponse()
                status = response.status
                left = MAX_REPLY_BYTES
                while left > 0 and response.read(min(64 * 1024, left)):
                    left -= 64 * 1024
                return status
            except Exception:
                raise httpcore.HttpFailure("Notion is unreachable (connection failed or timed out)") from None
            finally:
                conn.close()

        try:
            status = httpcore.with_deadline(go, timeout + 1, "Notion")
        except httpcore.HttpFailure as e:
            raise NotionError(str(e)) from None
        if not 200 <= status < 300:
            raise NotionError(f"Notion answered HTTP {status}")
        sent += 1
    return sent
