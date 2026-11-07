#!/usr/bin/python3 -I
"""aictl-helper: the AI Control dashboard's privileged, read-only helper (docs/CONTRACTS.md §12.15.2).

Socket-activated: aictl-helper.socket (Accept=yes) accepts each connection on /run/aictl-helper.sock
(0660 root:aictl) and starts one aictl-helper@.service instance with the connection on fd 3
(LISTEN_FDS=1) or on stdin (StandardInput=socket). One request per connection, then it closes.

Protocol (the same wire protocol as homelab-dockerctl, §11.2; no streams in P1)
  request   one JSON object on one line, at most 4 KiB, e.g. {"op": "logs", "source": "docker"}.
            Send it with write(), never end(): a half-closed socket reads as "client gone".
  reply     one line, then the helper closes:
            {"ok": true, "op": OP, "data": DATA} or {"ok": false, "error": {"code": CODE, "message": TEXT}}

Rules
  - Only the aictl user may connect: the SO_PEERCRED uid must equal the uid that pwd gives for the
    configured user name. Everyone else, root included, is refused before the request is read.
  - Exactly the P1 operations exist, all read-only: containers, logs, sudo-sessions, sudo-replay,
    sudo-commands, localai-activity, timers.
  - Every value from the client is checked against an allowlist (from the config) or a strict regex
    before it is used. Every subprocess is a fixed argv list (no shell), with a minimal environment,
    a timeout of at most 10 s and a cap on how much output is read.
  - Docker is touched only after `systemctl is-active docker.service` prints "active": touching the
    socket would start Docker.
  - Text from the host (logs, journal messages, sudo command lines and session output) is cleaned of
    ANSI/control sequences and redacted (bearer/API keys, token=, Authorization:, passwords) BEFORE it
    is truncated, so a cut never exposes half a secret's context.
  - sudo sessions are read straight from the I/O log files (log.json or log, timing, ttyout, stdout,
    stderr; gzip, zlib or plain). sudoreplay is never run, so nothing allocates a tty or waits on the
    timing data. Input streams (ttyin, stdin) are never read: they hold keystrokes and the Ansible
    module arguments that travel on stdin.
  - Every request is logged to journald (stderr, SyslogIdentifier=aictl-helper) as op plus the
    validated arguments. Output never goes to the journal.

Configuration: /etc/ai-control/helper.json (AICTL_HELPER_CONFIG overrides it in tests), root-owned and
not writable by group or others. Python 3 stdlib only.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import errno
import fnmatch
import json
import os
import pwd
import re
import selectors
import shlex
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
import traceback
import zlib
from collections.abc import Callable
from typing import Any

CONFIG_ENV = "AICTL_HELPER_CONFIG"
DEFAULT_CONFIG = "/etc/ai-control/helper.json"

MAX_REQUEST_BYTES = 4096
MAX_TIMEOUT = 10.0
LINE_CAP = 2000  # characters per log/journal line, after redaction
MAX_LOG_LINES = 1000
DEFAULT_LOG_LINES = 200
MAX_SESSIONS = 200
DEFAULT_SESSIONS = 50
MAX_JOURNAL_ENTRIES = 500
DEFAULT_JOURNAL_ENTRIES = 100
MAX_REPLAY_BYTES = 262144
DEFAULT_REPLAY_BYTES = 65536
REDACTION_MARGIN = 65536  # raw bytes read past max_bytes, so redaction sees whole secrets at the cut
DEFAULT_SINCE_SECONDS = 86400
MAX_SINCE_SECONDS = 400 * 86400
MAX_OUTPUT_BYTES = 8 * 1024 * 1024  # raw stdout read from any one subprocess
MAX_META_BYTES = 64 * 1024  # log / log.json
MAX_TIMING_BYTES = 4 * 1024 * 1024
MAX_COMPRESSED_READ = 64 * 1024 * 1024
MAX_WALK_DIRS = 100_000
MAX_WALK_DEPTH = 4

CHILD_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
             "SYSTEMD_COLORS": "0", "SYSTEMD_PAGER": "", "SYSTEMD_LESS": "", "NO_COLOR": "1"}

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}\Z")  # container and log source names
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}\Z")
UNIT_PATTERN_RE = re.compile(r"^[A-Za-z0-9@_.:*-]{1,128}\Z")  # config: journalctl -u patterns (globs allowed)
UNIT_NAME_RE = re.compile(r"^[A-Za-z0-9@_.:-]{1,240}\.(?:timer|service)\Z")  # from systemctl output
# Session ids (contract): a six-character TSID, or a relative path; ".." is refused separately.
SESSION_ID_RE = re.compile(r"^[0-9A-Z]{6}\Z|^[0-9A-Za-z/._-]{1,64}\Z")
TSID6_RE = re.compile(r"^[0-9A-Z]{6}\Z")
PATH_PART_RE = re.compile(r"^[0-9A-Za-z._-]{1,64}\Z")
# RFC 3339 as Docker writes it (StartedAt, `docker logs --timestamps`): nanoseconds, Z or an offset.
RFC3339_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})")

# ---- cleaning: terminal sequences and control characters
ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC (title, hyperlinks), BEL or ST terminated
    r"|\x1b[P^_X][^\x1b]*(?:\x1b\\)?"  # DCS, PM, APC, SOS
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI (colours, cursor moves)
    r"|\x1b[()*+][0-9A-Za-z]"  # charset selection
    r"|\x1b[ -/]*[0-~]"  # other two-character escapes
    r"|\x9b[0-?]*[ -/]*[@-~]"  # 8-bit CSI
)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# ---- redaction (applied before any truncation)
# Every rule is linear-time on any input (Reviewer A, round 1): each starts on a literal keyword or on a
# position NOT preceded by a character of its own run (a lookbehind), so no run is scanned twice;
# whitespace and prefixes are bounded; runs followed by more pattern are possessive. Lines are capped
# at REDACT_LINE_CAP before redaction and processed one at a time. The reference cases are in
# tests/redact_cases.json (Y loads the same file).
REDACTED = "[REDACTED]"
REDACT_LINE_CAP = 8192
# A secret-looking NAME is matched by its suffix keyword immediately before [:=]; the rest of the name
# stays in the text untouched, so no prefix has to be matched at all.
_SECRET_KEYWORD = (r"(?:token|secret|secret[_-]?key|passwd|password|passphrase|api[_-]?key|apikey|access[_-]?key"
                   r"|private[_-]?key|auth[_-]?key|client[_-]?secret|ansible_[a-z_]{0,32}pass|_pass|_pwd)")
# A quoted value, with backslash escapes (`"abc\"hunter2"`): the two alternatives can't overlap, and the
# repetition is possessive, so it's linear.
_DQ = r'"(?:[^"\\\n]|\\.)*+"?'
_SQ = r"'(?:[^'\\\n]|\\.)*+'?"
_VALUE = rf"({_DQ}|{_SQ}|[^\s\"']+)"  # an option's value: quoted (escapes included) or a bare word


def _keep_quotes(m: Any) -> str:
    """group(1) kept, the value (group 2) replaced, its quotes kept when it was quoted."""
    value = m.group(2)
    return m.group(1) + (f"{value[0]}{REDACTED}{value[0]}" if value[:1] in ("\"", "'") else REDACTED)
_TOK = r"(?<![A-Za-z0-9_-])"  # a token shape starts at the beginning of its run


def _tok(literal: str) -> str:
    """A token shape's literal start, then the start-of-run check as a fixed-width lookbehind just after
    it: equivalent to _TOK + literal, but the pattern starts with a literal, so the regex engine
    searches for it instead of trying every position (Reviewer A round 3 perf)."""
    return literal + r"(?<![A-Za-z0-9_-]" + literal + ")"
REDACT_RULES: list[tuple[re.Pattern[str], Any]] = [
    # Authorization: Bearer xxx / Basic xxx / raw value (headers, curl -H, logs)
    (re.compile(r"(?i)((?:proxy-(?<!\wproxy-)authorization|authorization(?<!\wauthorization))[\"']?\s{0,8}[:=]\s{0,8}[\"']?)"
                r"(?:(bearer|basic|token|digest)\s{1,8})?(?!\[REDACTED\])[^\s\"',;]+"),
     lambda m: f"{m.group(1)}{m.group(2) + ' ' if m.group(2) else ''}{REDACTED}"),
    (re.compile(r"(?i)(bearer(?<!\wbearer)\s{1,8})(?!\[REDACTED\])[A-Za-z0-9._~+/=-]{4,}"), lambda m: m.group(1) + REDACTED),
    # a flattened YAML block under a secret-named key, `password: | s3cret` / `token: >- abc def`: everything
    # after the indicator to the end of the line (a trailing comment alone, `password: | # x`, is not a value)
    (re.compile(rf"(?i)({_SECRET_KEYWORD}[\"']?\s{{0,8}}:\s{{0,8}}[|>][1-9+-]{{0,2}}[ \t]{{1,8}})"
                r"(?!#|\[REDACTED\])\S[^\n]*"), lambda m: m.group(1) + REDACTED),
    # name=value / name: value / "name": "value" (a quoted value is redacted up to its closing quote)
    # (a YAML "!vault" tag, or a block indicator "|"/">" followed by whitespace or the line end, is left
    # alone: the flattened-block rule above, or the block/vault tracking in redact(), redacts what follows.
    # The exemption looks at most 4 characters ahead, so the rule stays linear; Y mirrors it.)
    (re.compile(rf"(?i)({_SECRET_KEYWORD}[\"']?\s{{0,8}}[:=]\s{{0,8}})"
                r"(?!\[REDACTED\]|!vault\b|[|>][1-9+-]{0,2}(?:[ \t]|$))"
                rf"({_DQ}|{_SQ}|[^\s\"'&,;}}]+)"),
     lambda m: m.group(1) + (f"{m.group(2)[0]}{REDACTED}{m.group(2)[0]}" if m.group(2)[0] in "\"'" else REDACTED)),
    # CLI flags: --password foo, --pass=foo, --db-password foo, --token=foo (not --password-file, --pass-through)
    (re.compile(r"(?i)(--(?:db-password|db-pass|password|passwd|passphrase|pass|token|api-key|apikey|secret|auth-key"
                r"|client-secret)(?:=|\s{1,8}))(?!\[REDACTED\])" rf"({_DQ}|{_SQ}|\S+)"),
     lambda m: m.group(1) + (f"{m.group(2)[0]}{REDACTED}{m.group(2)[0]}" if m.group(2)[0] in "\"'" else REDACTED)),
    # Options that carry a password, matched anywhere on a line that names their command (the trigger
    # checks the command is there; no argument window, so long or many arguments before the option don't
    # hide it). Over-redaction of a same-shaped option of another tool on that line is accepted.
    # curl -u user:pass / -uuser:pass / --user user:pass (the password part only)
    (re.compile(r"(-(?<!\S-)(?:u\s{0,8}+|-user(?:\s{1,8}+|=))[\"']?[^\s:\"']{1,256}+:)(?!\[REDACTED\])[^\s\"']+"),
     rf"\1{REDACTED}"),
    # mysql/mariadb -pPASSWORD (heuristic: -p glued to its value; `-p` alone is the prompt)
    (re.compile(r"(-(?<!\S-)p)(?=\S)(?!\[REDACTED\])" + _VALUE), _keep_quotes),
    # sshpass -p X / -pX
    (re.compile(r"(-(?<!\S-)p\s{0,8}+)(?!\[REDACTED\])" + _VALUE), _keep_quotes),
    # docker login -p X (--password X is a flag, above)
    (re.compile(r"(-(?<!\S-)p\s{0,8}+)(?!\[REDACTED\])" + _VALUE), _keep_quotes),
    # redis-cli -a X
    (re.compile(r"(-(?<!\S-)a\s{0,8}+)(?!\[REDACTED\])" + _VALUE), _keep_quotes),
    # Cookie: / Set-Cookie: values (to the end of the line or a quote)
    # (pairs `name=value` separated by "; "; a value may be quoted, `a="PW"`; an unquoted quote ends the header,
    # as in `-H "Cookie: sid=x" url`. The three alternatives start on different characters: linear.)
    (re.compile(r"(?i)((?:set-(?<![A-Za-z0-9_-]set-)cookie|cookie(?<![A-Za-z0-9_-]cookie))\s{0,8}:\s{0,8})(?!\[REDACTED\])"
                rf"(?:[^\s\"';=]++|=(?:{_DQ}|{_SQ})?|;[ \t]*+)++"),
     lambda m: m.group(1) + REDACTED),
    # credentials in URLs: ://user:pass@
    (re.compile(r"(://[^/\s:@]{0,256}+:)(?!\[REDACTED\])[^@\s/]{1,256}+@"), rf"\1{REDACTED}@"),  # user may be empty
    # an Ansible vault blob written on one line (multi-line blobs: see redact())
    (re.compile(r"(\$ANSIBLE_VAULT;[0-9.]{1,8};[A-Za-z0-9]{1,16}(?:;[^\s;]{1,64})?)(?:\s{1,8}+[0-9a-fA-F]{2,}+)++"),
     rf"\1 {REDACTED}"),
    # well-known token shapes
    (re.compile(_tok(r"tskey-") + r"[A-Za-z0-9-]{8,}"), REDACTED),
    (re.compile(_tok(r"sk-") + r"[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(_tok(r"gh") + r"[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(_tok(r"github_pat_") + r"[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(_tok(r"glpat-") + r"[A-Za-z0-9_-]{20,}"), REDACTED),
    (re.compile(_tok(r"hf_") + r"[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(_tok(r"xox") + r"[abprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(_tok(r"AKIA") + r"[0-9A-Z]{16}(?![0-9A-Za-z])"), REDACTED),
    (re.compile(_tok(r"eyJ") + r"[A-Za-z0-9_-]{8,}+\.[A-Za-z0-9_-]{8,}+\.[A-Za-z0-9_-]{8,}"), REDACTED),
    # Last: a literal "[REDACTED]" in the input never shields text glued to it (`password=[REDACTED]secret`,
    # which the rules above skip as already redacted). Token characters after a marker are absorbed.
    # (a "." right after the marker counts only when a token character follows: `[REDACTED].` ends a sentence)
    (re.compile(r"\[REDACTED\](?:\.(?=[A-Za-z0-9_-]))?[A-Za-z0-9_+=~!$%*#?^\[\]-][A-Za-z0-9_+=~.!$%*#?^\[\]-]*+"),
     REDACTED),
]
# Case-insensitive rules don't use (?i) at match time: it stops the regex engine from skipping ahead to
# possible first characters, which made them ~7x slower. They're compiled without it (their literals are
# lowercase) and matched against an ASCII-lowercased copy of the line, which has the same length, so
# every span maps straight back to the original text; replacements are built from the original. The
# result equals (?i) matching for ASCII; a test checks it on every reference case.
_ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
FOLDED_RULES = frozenset(i for i, (pattern, _) in enumerate(REDACT_RULES) if pattern.flags & re.IGNORECASE)
_FOLDED_PATTERNS = {i: re.compile(REDACT_RULES[i][0].pattern.replace("(?i)", "", 1).replace(r"\[REDACTED\]",
                                                                                             r"\[redacted\]"))
                    for i in FOLDED_RULES}


class _OriginalGroups:
    """A match on the folded copy, read back as the original text."""

    def __init__(self, match: re.Match[str], line: str) -> None:
        self.m, self.line = match, line

    def group(self, i: int = 0) -> str | None:
        start, end = self.m.span(i)
        return None if start < 0 else self.line[start:end]


def _sub_folded(i: int, repl: Any, line: str, fold: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _FOLDED_PATTERNS[i].finditer(fold):
        out.append(line[pos:m.start()])
        out.append(repl(_OriginalGroups(m, line)) if callable(repl) else repl)
        pos = m.end()
    if not out:
        return line
    out.append(line[pos:])
    return "".join(out)


# Each rule runs only on a line whose lowercased text satisfies its trigger: a tuple of groups, every
# group needing one of its substrings (C-speed `in` tests). A dense line pays for the few rules it can
# match, not all of them. Aligned by index with REDACT_RULES; a test checks that redact() with and
# without the triggers agrees on every reference case and pathological input.
_NAME_TRIGGERS = ("token", "secret", "pass", "_pwd", "key")
RULE_TRIGGERS: list[tuple[tuple[str, ...], ...]] = [(t,) if isinstance(t[0], str) else t for t in [
    ("authorization",), ("bearer",), (_NAME_TRIGGERS, (":",), ("|", ">")), (_NAME_TRIGGERS, (":", "=")),
    ("--",), ("curl",), ("mysql", "mariadb"),
    ("sshpass",), (("docker",), ("login",)), ("redis-cli",), ("cookie",), ("://",), ("$ansible_vault",),
    ("tskey-",), ("sk-",), ("gh",), ("github_pat_",), ("glpat-",), ("hf_",), ("xox",), ("akia",), ("eyj",),
    ("[redacted]",),
]]
assert len(RULE_TRIGGERS) == len(REDACT_RULES)
# For these rules the trigger is part of the rule, not just a speed-up: the command must be on the line
# (curl -u, mysql -p, sshpass -p, docker login -p, redis-cli -a). They always check it.
SCOPED_RULES = frozenset({5, 6, 7, 8, 9})
assert all(REDACT_RULES[i][0].pattern.startswith("(-(?<!\\S-)") for i in SCOPED_RULES)


def apply_rules(line: str, use_triggers: bool = True) -> str:
    """The single-line rules, in order, each only where its trigger occurs."""
    low = line.lower()
    fold: str | None = None
    for i, ((pattern, repl), triggers) in enumerate(zip(REDACT_RULES, RULE_TRIGGERS)):
        if (use_triggers or i in SCOPED_RULES) and not all(any(t in low for t in group) for group in triggers):
            continue
        if i in FOLDED_RULES:
            if fold is None:
                fold = line.translate(_ASCII_FOLD)
            new = _sub_folded(i, repl, line, fold)
        else:
            new = pattern.sub(repl, line)
        if new != line:
            line, low, fold = new, new.lower(), None
    return line


PEM_BEGIN_RE = re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----")  # PEM, OpenSSH, PGP
PEM_END_RE = re.compile(r"-----END [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----")
VAULT_HEADER_RE = re.compile(r"\$ANSIBLE_VAULT;[0-9.]{1,8};[A-Za-z0-9]{1,16}(?:;[^\s;]{1,64})?\s*$")
VAULT_BODY_RE = re.compile(r"^(\s*)[0-9a-fA-F]{2,}\s*$")
# A YAML block scalar under a key: `key: |`, `- key: >-`, `"key": |2  # comment`. Anchored at the line
# start and the name class excludes ':' and quotes, so matching is linear. The key counts as secret
# when its name ends in a secret keyword (as in the name=value rule).
# An empty value (`password:` then the value on the next, more-indented line) opens a block too.
YAML_BLOCK_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<dash>-[ ]{1,8})?[\"']?(?P<name>[A-Za-z0-9_.-]{1,128})[\"']?"
                           r"[ ]{0,8}:(?:[ ]{1,8}[|>][1-9+-]{0,2})?[ \t]*(?:#.*)?$")
SECRET_NAME_END_RE = re.compile(rf"(?i){_SECRET_KEYWORD}\Z")


def yaml_secret_block(line: str) -> int | None:
    """The key's column when `line` opens a block scalar under a secret-named key, else None."""
    m = YAML_BLOCK_RE.match(line)
    if not m or not SECRET_NAME_END_RE.search(m.group("name")):
        return None
    return len(m.group("indent")) + len(m.group("dash") or "")


def cap_line(line: str, cap: int = REDACT_LINE_CAP) -> tuple[str, bool]:
    """A line at most `cap` characters long. A cut drops the trailing partial word too, so the
    fragment of a secret the cut went through is never shown (unredacted, because it's too short
    for its pattern)."""
    if len(line) <= cap:
        return line, False
    cut = line[:cap]
    ws = max(cut.rfind(" "), cut.rfind("\t"))
    return (cut[:ws + 1] if ws >= 0 else ""), True


def redact_line(line: str) -> str:
    """The single-line rules, on a line already capped at REDACT_LINE_CAP."""
    return _redact_pem(apply_rules(line))[0]


def _redact_pem(line: str, inside: bool = False) -> tuple[str, bool]:
    """Private key blocks on one line, or the part of a multi-line block on this line: each stretch
    of block text on the line becomes one [REDACTED]. Returns (text, still inside a block)."""
    out: list[str] = []
    pos = 0
    if inside:
        out.append(REDACTED)
    while True:
        if inside:
            end = PEM_END_RE.search(line, pos)
            if end is None:
                return "".join(out), True
            pos, inside = end.end(), False
            continue
        begin = PEM_BEGIN_RE.search(line, pos)
        if begin is None:
            out.append(line[pos:])
            return "".join(out), False
        out.append(line[pos:begin.start()] + REDACTED)
        pos, inside = begin.end(), True


def redact(text: str, check: Callable[[], None] | None = None) -> str:
    """Multi-line text: each line capped at REDACT_LINE_CAP (dropping a cut partial word), then
    redacted. Private key blocks, Ansible vault bodies and YAML block scalars under secret-named keys
    (`password: |` and the more-indented lines after it) are tracked across lines."""
    out: list[str] = []
    in_pem = in_vault = False
    yaml_col: int | None = None  # inside a secret YAML block: the key's column
    for n, raw in enumerate(text.split("\n")):
        if check is not None and n % 16 == 0:
            check()  # the request deadline, between lines of a long text
        line, _ = cap_line(raw)
        if yaml_col is not None:
            body = line.lstrip(" \t")
            indent = len(line) - len(body)
            if not body:
                out.append(line)  # blank lines belong to the block
                continue
            if indent > yaml_col:
                out.append(line[:indent] + REDACTED)
                continue
            yaml_col = None  # indentation dropped back: the block is over
        if in_vault:
            m = VAULT_BODY_RE.match(line)
            if m:
                out.append(m.group(1) + REDACTED)
                continue
            in_vault = False
        if in_pem:
            line, in_pem = _redact_pem(line, inside=True)
            if in_pem:
                out.append(line)
                continue
            # the block ended on this line: the rest of the line gets the normal rules below
        line = apply_rules(line)
        line, in_pem = _redact_pem(line) if "-----" in line else (line, False)
        in_vault = bool(VAULT_HEADER_RE.search(line))
        if not in_pem:
            yaml_col = yaml_secret_block(line)
        out.append(line)
    return "\n".join(out)


def strip_terminal(text: str) -> str:
    """Terminal output -> the plain text a viewer saw: escape sequences removed, CRLF to LF, a lone CR
    returns to the start of the line and later characters overwrite (progress bars), backspace moves
    back one, other control characters (tab and LF kept) are dropped."""
    text = ANSI_RE.sub("", text).replace("\r\n", "\n")
    lines = []
    for line in text.split("\n"):
        if "\r" in line or "\b" in line:
            buf: list[str] = []
            col = 0
            for ch in line:
                if ch == "\r":
                    col = 0
                elif ch == "\b":
                    col = max(0, col - 1)
                else:
                    if col < len(buf):
                        buf[col] = ch
                    else:
                        buf.append(ch)
                    col += 1
            line = "".join(buf)
        lines.append(CONTROL_RE.sub("", line))
    return "\n".join(lines)


def clean_line(text: str, cap: int = LINE_CAP) -> str:
    """One log/journal line for the client: no escapes or controls, capped at REDACT_LINE_CAP,
    redacted, THEN capped at `cap`."""
    text = text[:4 * REDACT_LINE_CAP]  # bound the cleaning work; each line is capped in redact()
    text = CONTROL_RE.sub("", ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", " "))
    # A multi-line message is redacted line by line (so `password: |` blocks, keys and vault bodies are
    # tracked across its lines), then flattened to one line.
    return redact(text).replace("\n", " ")[:cap]


def truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    return raw[:max_bytes].decode("utf-8", "ignore"), True


def clean_log(text: Any, limit: int = 300) -> str:
    """A value that ends up in the journal: one line, printable, bounded."""
    return CONTROL_RE.sub("?", str(text).replace("\n", " ").replace("\t", " "))[:limit]


def rfc3339_epoch(text: Any) -> tuple[int | None, int]:
    """Docker's RFC 3339 time at the start of text -> (epoch seconds or None, length matched).
    Docker's zero time (0001-01-01, a container that never started) is None."""
    m = RFC3339_RE.match(text) if isinstance(text, str) else None
    if not m:
        return None, 0
    try:
        when = dt.datetime.fromisoformat(m.group(1) + ("+00:00" if m.group(3) == "Z" else m.group(3)))
    except ValueError:
        return None, 0
    end = m.end() + (1 if text[m.end():m.end() + 1] == " " else 0)
    return (int(when.timestamp()) if when.year > 1970 else None), end


def peer_credentials(conn: socket.socket) -> tuple[int, int, int]:
    """(pid, uid, gid) of the process on the other end of a Unix socket, from the kernel."""
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return pid, uid, gid


class OpError(Exception):
    """A failure reported to the client as {"ok": false, "error": {"code", "message"}}."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Log:
    """stderr lines with a syslog priority prefix; systemd files them in the journal."""

    def __init__(self, stream: Any = None) -> None:
        self.stream = stream if stream is not None else sys.stderr

    def _emit(self, priority: int, message: str) -> None:
        try:
            self.stream.write(f"<{priority}>{clean_log(message, 1000)}\n")
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def info(self, message: str) -> None:
        self._emit(6, message)

    def warning(self, message: str) -> None:
        self._emit(4, message)

    def error(self, message: str) -> None:
        self._emit(3, message)


# --------------------------------------------------------------------------- configuration

DEFAULT_LOG_UNITS = {
    "docker": ("docker.service",),
    "tailscaled": ("tailscaled.service",),
    "chrony": ("chrony.service",),
    "ai-control": ("ai-control.service",),
    "aictl-helper": ("aictl-helper@*.service",),
    "homelab-ai-stack": ("homelab-ai-stack*",),
}


@dataclasses.dataclass(frozen=True)
class Config:
    user: str = "aictl"  # the only peer allowed; its uid comes from pwd at connect time
    containers: tuple[str, ...] = ("ollama-cpu", "gpu-broker", "open-webui")
    log_units: dict[str, tuple[str, ...]] = dataclasses.field(default_factory=lambda: dict(DEFAULT_LOG_UNITS))
    sudo_users: tuple[str, ...] = ("claude", "localai")
    localai_user: str = "localai"
    timer_patterns: tuple[str, ...] = ("ai-*", "homelab-*")
    # The sudo binaries whose journal lines count (matched on _EXE, which journald takes from the
    # kernel; _COMM and _UID are the caller's). Classic sudo is sudo.ws on Ubuntu 26.04; sudo-rs stays
    # in so an attempt through it (e.g. by localai) is still audited.
    sudo_exes: tuple[str, ...] = ("/usr/bin/sudo.ws", "/usr/lib/cargo/bin/sudo")
    iolog_root: str = "/var/log/sudo-io"
    docker: str = "/usr/bin/docker"
    systemctl: str = "/usr/bin/systemctl"
    journalctl: str = "/usr/bin/journalctl"
    docker_unit: str = "docker.service"
    request_timeout: float = 5
    send_timeout: float = 10
    command_timeout: float = 8
    read_timeout: float = 8  # budget for walking and reading the sudo I/O logs
    request_deadline: float = 10  # everything from the first recv to the last subprocess

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> Config:
        fields = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(raw) - fields)
        if unknown:
            raise ValueError(f"unknown config keys: {', '.join(unknown)}")
        values: dict[str, Any] = {}
        for key, value in raw.items():
            if key in ("containers", "sudo_users", "timer_patterns", "sudo_exes"):
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise ValueError(f"{key} must be a list of strings")
                value = tuple(value)
            elif key == "log_units":
                if not isinstance(value, dict) or not all(
                        isinstance(v, list) and v and all(isinstance(u, str) for u in v) for v in value.values()):
                    raise ValueError("log_units must map a source name to a non-empty list of unit names")
                value = {k: tuple(v) for k, v in value.items()}
            elif key in ("request_timeout", "send_timeout", "command_timeout", "read_timeout", "request_deadline"):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"{key} must be a number")
            elif not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            values[key] = value
        cfg = cls(**values)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        for key in ("user", "localai_user"):
            if not USER_RE.match(getattr(self, key)):
                raise ValueError(f"{key} is not a valid user name")
        if self.user == "root":
            raise ValueError("user must not be root")
        if not all(USER_RE.match(u) for u in self.sudo_users):
            raise ValueError("sudo_users must be user names")
        if not all(NAME_RE.match(c) for c in self.containers):
            raise ValueError("containers must be container names")
        for source, units in self.log_units.items():
            if not NAME_RE.match(source):
                raise ValueError(f"log source {source!r} is not a valid name")
            if not all(UNIT_PATTERN_RE.match(u) and not u.startswith("-") for u in units):
                raise ValueError(f"log_units[{source}] holds an invalid unit name")
        overlap = set(self.log_units) & set(self.containers)
        if overlap:
            raise ValueError(f"log sources and containers overlap: {', '.join(sorted(overlap))}")
        if not all(UNIT_PATTERN_RE.match(p) and not p.startswith("-") for p in self.timer_patterns):
            raise ValueError("timer_patterns must be unit name globs")
        if not self.sudo_exes or not all(re.match(r"^/[A-Za-z0-9_./-]{1,255}\Z", e) and ".." not in e.split("/")
                                         for e in self.sudo_exes):
            raise ValueError("sudo_exes must be a non-empty list of absolute paths")
        if not UNIT_NAME_RE.match(self.docker_unit):
            raise ValueError("docker_unit must be a unit name")
        for key in ("iolog_root", "docker", "systemctl", "journalctl"):
            path = getattr(self, key)
            if not os.path.isabs(path) or ".." in path.split("/"):
                raise ValueError(f"{key} must be an absolute path")
        for key in ("request_timeout", "send_timeout", "command_timeout", "read_timeout", "request_deadline"):
            value = getattr(self, key)
            if not 0 < value <= MAX_TIMEOUT:
                raise ValueError(f"{key} must be more than 0 and at most {MAX_TIMEOUT:g} s")


def load_config(path: str, owner_uid: int = 0) -> Config:
    """The allowlists are trusted, so the file must be owned by root (owner_uid in tests) and not be
    writable by group or others."""
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != owner_uid or st.st_mode & 0o022:
            raise ValueError("the config must be a regular file owned by root and not group/world writable")
        data = fh.read(MAX_REQUEST_BYTES * 16)
    raw = json.loads(data.decode("utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("the config must be a JSON object")
    return Config.from_mapping(raw)


# --------------------------------------------------------------------------- side effects


@dataclasses.dataclass
class Result:
    rc: int
    out: bytes
    err: bytes
    truncated: bool = False


class System:
    """Every process and clock the helper uses."""

    def run(self, argv: list[str], timeout: float, max_bytes: int = MAX_OUTPUT_BYTES,
            merge_stderr: bool = False) -> Result:
        """Run a fixed argv (no shell, minimal environment, own process group). Reads at most
        max_bytes of stdout; past that the process is ended and the result is marked truncated.
        Past the timeout the whole process group is killed and OpError("timeout") raised."""
        timeout = min(timeout, MAX_TIMEOUT)
        name = os.path.basename(argv[0])
        try:
            proc = subprocess.Popen(  # argv list, no shell
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
                env=dict(CHILD_ENV), close_fds=True, start_new_session=True,
            )
        except OSError as exc:
            raise OpError("failed", f"{name}: {exc.strerror or exc}") from exc
        deadline = time.monotonic() + timeout
        out, err = bytearray(), bytearray()
        truncated = timed_out = False
        sel = selectors.DefaultSelector()
        try:
            for pipe, buf in ((proc.stdout, out), (proc.stderr, err)):
                if pipe is not None:
                    os.set_blocking(pipe.fileno(), False)
                    sel.register(pipe.fileno(), selectors.EVENT_READ, buf)
            while sel.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                for key, _ in sel.select(remaining):
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        sel.unregister(key.fd)
                        continue
                    buf = key.data
                    limit = max_bytes if buf is out else 65536
                    room = limit - len(buf)
                    if room > 0:
                        buf += chunk[:room]
                    if buf is out and len(chunk) > room:
                        truncated = True
                if truncated:
                    break
            if timed_out or truncated:
                self._kill(proc)
            rc = self._wait(proc, max(0.1, deadline - time.monotonic()))
            if rc is None:
                timed_out = True
                self._kill(proc)
                rc = self._wait(proc, 2) or -9
        finally:
            sel.close()
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
        if timed_out:
            raise OpError("timeout", f"{name} did not finish within {timeout:g} s")
        return Result(rc, bytes(out), bytes(err), truncated)

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)

    @staticmethod
    def _wait(proc: subprocess.Popen, timeout: float) -> int | None:
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def uid_of(self, user: str) -> int | None:
        try:
            return pwd.getpwnam(user).pw_uid
        except KeyError:
            return None


# --------------------------------------------------------------------------- requests

# The whole operation surface: the fields each op accepts and their types.
OPS: dict[str, dict[str, tuple[type, ...]]] = {
    "containers": {},
    "logs": {"source": (str,), "lines": (int,), "since": (int,)},
    "sudo-sessions": {"user": (str,), "limit": (int,)},
    "sudo-replay": {"user": (str,), "id": (str,), "max_bytes": (int,), "output": (bool,)},
    "sudo-commands": {"user": (str,), "since": (int,), "limit": (int,)},
    "localai-activity": {"since": (int,), "limit": (int,)},
    "timers": {},
}
REQUIRED: dict[str, frozenset[str]] = {
    "logs": frozenset({"source"}),
    "sudo-sessions": frozenset({"user"}),
    "sudo-replay": frozenset({"user", "id"}),
    "sudo-commands": frozenset({"user"}),
}
# (field, default, minimum, maximum) for the integer fields.
INT_BOUNDS: dict[str, dict[str, tuple[int, int, int]]] = {
    "logs": {"lines": (DEFAULT_LOG_LINES, 1, MAX_LOG_LINES)},
    "sudo-sessions": {"limit": (DEFAULT_SESSIONS, 1, MAX_SESSIONS)},
    "sudo-replay": {"max_bytes": (DEFAULT_REPLAY_BYTES, 1, MAX_REPLAY_BYTES)},
    "sudo-commands": {"limit": (DEFAULT_JOURNAL_ENTRIES, 1, MAX_JOURNAL_ENTRIES)},
    "localai-activity": {"limit": (DEFAULT_JOURNAL_ENTRIES, 1, MAX_JOURNAL_ENTRIES)},
}


def read_request(conn: socket.socket, budget: Callable[[float], float] | None = None,
                 timeout: float = 5) -> bytes:
    """One request line. Each recv waits at most what's left of `timeout` and of the request deadline
    (budget(cap) -> seconds left, at most cap), so a client trickling bytes can't hold the helper."""
    data = b""
    while b"\n" not in data:
        left = budget(timeout) if budget else timeout
        if left <= 0:
            raise OpError("bad-request", "no complete request line in time")
        conn.settimeout(left)
        try:
            chunk = conn.recv(MAX_REQUEST_BYTES + 1 - len(data))
        except TimeoutError as exc:
            raise OpError("bad-request", "no complete request line in time") from exc
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_REQUEST_BYTES:
            raise OpError("bad-request", f"request larger than {MAX_REQUEST_BYTES} bytes")
    line = data.split(b"\n", 1)[0].strip()
    if not line:
        raise OpError("bad-request", "empty request")
    return line


def parse_since(value: int, now: float) -> int:
    """since is integer unix seconds (Lead, 2026-10-26). It is passed to journalctl as @SECONDS and
    to docker logs as SECONDS, never as text. More than a day ahead is refused; older than any
    retained log is clamped to 400 days."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise OpError("bad-request", "since must be integer unix seconds")
    if value > int(now) + 86400:
        raise OpError("bad-request", "since is in the future")
    return max(value, int(now) - MAX_SINCE_SECONDS)


def resolve_session_id(raw: str) -> str:
    """A session id -> a relative path under the user's I/O log directory. "00002A" becomes
    "00/00/2A" (as sudoreplay does); a path id must be plain components, no "..", no leading "/"."""
    if not SESSION_ID_RE.match(raw):
        raise OpError("bad-request", "id must be a 6-character TSID or a relative session path")
    if TSID6_RE.match(raw):
        return f"{raw[0:2]}/{raw[2:4]}/{raw[4:6]}"
    parts = raw.split("/")
    if any(p in ("", ".", "..") or not PATH_PART_RE.match(p) for p in parts) or ".." in raw:
        raise OpError("bad-request", "id must be a relative path of plain components")
    return raw


def validate_request(raw: bytes, cfg: Config, now: float) -> dict[str, Any]:
    try:
        req = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise OpError("bad-request", "the request is not valid JSON") from exc
    if not isinstance(req, dict):
        raise OpError("bad-request", "the request must be a JSON object")
    op = req.get("op")
    if not isinstance(op, str) or op not in OPS:
        raise OpError("unknown-op", "unknown operation")
    allowed = OPS[op]
    for key, value in req.items():
        if key == "op":
            continue
        if key not in allowed:
            raise OpError("bad-request", f"field {key!r} is not allowed for {op}")
        want = allowed[key]
        if isinstance(value, bool) and bool not in want:
            raise OpError("bad-request", f"{key} has the wrong type")
        if not isinstance(value, want):
            raise OpError("bad-request", f"{key} has the wrong type")
    missing = REQUIRED.get(op, frozenset()) - set(req)
    if missing:
        raise OpError("bad-request", f"missing field(s): {', '.join(sorted(missing))}")
    out: dict[str, Any] = {"op": op}
    for key, (default, low, high) in INT_BOUNDS.get(op, {}).items():
        value = req.get(key, default)
        if not low <= value <= high:
            raise OpError("bad-request", f"{key} must be between {low} and {high}")
        out[key] = value
    if "source" in allowed:
        source = req["source"]
        if not NAME_RE.match(source) or (source not in cfg.log_units and source not in cfg.containers):
            raise OpError("not-allowed", "source is not an allowlisted unit or container")
        out["source"] = source
    if "user" in allowed:
        user = req["user"]
        if user not in cfg.sudo_users:
            raise OpError("not-allowed", "user is not an audited account")
        out["user"] = user
    if op == "sudo-replay":
        out["id"] = resolve_session_id(req["id"])
        out["output"] = req.get("output", False)
    if "since" in allowed:
        if "since" in req:
            out["since"] = parse_since(req["since"], now)
        elif op != "logs":
            out["since"] = int(now) - DEFAULT_SINCE_SECONDS
    return out


def describe(req: dict[str, Any]) -> str:
    """The validated request as one log line (never the raw request)."""
    parts = [f"op={req['op']}"]
    for key in ("source", "user", "id", "lines", "limit", "since", "max_bytes", "output"):
        if key in req:
            value = req[key]
            parts.append(f"{key}={str(value).lower() if isinstance(value, bool) else value}")
    return " ".join(parts)


class Reply:
    def __init__(self, conn: socket.socket) -> None:
        self.conn = conn
        self.gone = False

    def send(self, obj: dict[str, Any]) -> None:
        data = (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self.conn.sendall(data)
        except OSError:
            self.gone = True

    def ok(self, op: str, data: Any) -> None:
        self.send({"ok": True, "op": op, "data": data})

    def error(self, code: str, message: str) -> None:
        self.send({"ok": False, "error": {"code": code, "message": message}})


# --------------------------------------------------------------------------- sudo I/O logs


class StreamReader:
    """Reads one I/O log file (gzip, zlib or plain, detected from its first bytes), refusing
    symlinks, returning at most `cap` decoded bytes. Never decompresses past the cap."""

    def __init__(self, deadline: Callable[[], bool]) -> None:
        self.expired = deadline

    def read(self, dir_fd: int, name: str, cap: int) -> tuple[bytes, bool, bool] | None:
        """(data, truncated, damaged), or None when the file doesn't exist."""
        try:
            fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise OpError("failed", f"cannot open {name}: {exc.strerror}") from exc
        with os.fdopen(fd, "rb", buffering=0) as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise OpError("failed", f"{name} is not a regular file")
            head = fh.read(2)
            if head[:2] == b"\x1f\x8b":
                return self._inflate(fh, head, cap, 16 + zlib.MAX_WBITS)
            if len(head) == 2 and head[0] == 0x78 and (head[0] * 256 + head[1]) % 31 == 0:
                data, truncated, damaged = self._inflate(fh, head, cap, zlib.MAX_WBITS)
                if not (damaged and not data):
                    return data, truncated, damaged
                fh.seek(0)  # plain text that merely starts like a zlib header ("x^", "x\x01")
                head = b""
            data = head + fh.read(max(0, cap + 1 - len(head)))
            return data[:cap], len(data) > cap, False

    def _inflate(self, fh: Any, head: bytes, cap: int, wbits: int) -> tuple[bytes, bool, bool]:
        """Decompress at most cap + 1 bytes (so truncation is detectable), member after member."""
        out = bytearray()
        dec = zlib.decompressobj(wbits)
        buf = head
        consumed = len(head)
        try:
            while len(out) <= cap:
                if self.expired():
                    raise OpError("timeout", "reading the session took too long")
                if not buf:
                    buf = fh.read(65536)
                    consumed += len(buf)
                    if not buf or consumed > MAX_COMPRESSED_READ:
                        break
                out += dec.decompress(buf, cap + 1 - len(out))
                buf = dec.unconsumed_tail
                if dec.eof:  # a further member may follow (concatenated gzip streams)
                    buf = dec.unused_data + buf
                    dec = zlib.decompressobj(wbits)
        except zlib.error:
            return bytes(out[:cap]), len(out) > cap, True
        return bytes(out[:cap]), len(out) > cap, False


class SessionDir:
    """One session directory under /var/log/sudo-io/<user>, opened without following symlinks at
    any level (each component is opened with O_NOFOLLOW relative to the previous one)."""

    def __init__(self, root: str, rel: str) -> None:
        self.fd = -1
        fds: list[int] = []
        try:
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
            fds.append(fd)
            for part in rel.split("/"):
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd)
                fds.append(fd)
        except OSError as exc:
            for f in fds:
                os.close(f)
            # ENOTDIR, ELOOP: a file or a symlink where a directory should be
            if isinstance(exc, FileNotFoundError) or exc.errno in (errno.ENOTDIR, errno.ELOOP):
                raise OpError("not-found", "no such session") from exc
            raise OpError("failed", f"cannot open the session: {exc.strerror}") from exc
        for f in fds[:-1]:
            os.close(f)
        self.fd = fds[-1]

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def parse_session_meta(reader: StreamReader, dir_fd: int) -> dict[str, Any] | None:
    """log.json (preferred, as sudoreplay does) or the legacy log file -> session metadata.
    None when the directory has neither file. OpError("damaged") when what's there can't be parsed
    (bad JSON, nesting deep enough for a RecursionError, a wrong shape, a symlink), so a caller can
    skip this one session and carry on."""
    damaged = False
    try:
        got = reader.read(dir_fd, "log.json", MAX_META_BYTES)
        if got is not None:
            meta = _meta_from_json(got[0])
            if meta is not None:
                return meta
            damaged = True
        got = reader.read(dir_fd, "log", MAX_META_BYTES)
        if got is not None:
            meta = _meta_from_legacy(got[0])
            if meta is not None:
                return meta
            damaged = True
    except OpError as exc:
        if exc.code == "timeout":
            raise
        damaged = True
    if damaged:
        raise OpError("damaged", "the session's metadata is unreadable")
    return None


def _meta_from_json(raw: bytes) -> dict[str, Any] | None:
    try:
        info = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(info, dict):
        return None
    try:
        ts = info.get("timestamp") or {}
        seconds = ts.get("seconds") if isinstance(ts, dict) else None
        argv = info.get("runargv")
        if isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv):
            command = shlex.join(argv)
            if isinstance(info.get("command"), str) and not argv[0].startswith("/"):
                command = shlex.join([info["command"], *argv[1:]])
        else:
            command = _s(info.get("command"))
        return {
            "epoch": _epoch(seconds),
            "user": _s(info.get("submituser")),
            "runas": _s(info.get("runuser")),
            "tty": _s(info.get("ttyname")),
            "cwd": _s(info.get("runcwd") or info.get("submitcwd")),
            "command": command,
        }
    except Exception:  # noqa: BLE001 - any odd shape is one damaged session, never a failed request
        return None


def _meta_from_legacy(raw: bytes) -> dict[str, Any] | None:
    lines = raw.decode("utf-8", "replace").split("\n")
    first = lines[0].split(":")
    if len(first) < 5:
        return None
    try:
        epoch = _epoch(float(first[0]))
    except ValueError:
        epoch = None
    return {"epoch": epoch, "user": first[1], "runas": first[2], "tty": first[4],
            "cwd": lines[1] if len(lines) > 1 else "", "command": lines[2] if len(lines) > 2 else ""}


def _epoch(value: Any) -> int | None:
    """A session start time -> int epoch seconds; NaN, infinities, negatives and non-numbers -> None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        seconds = int(value)
    except (ValueError, OverflowError):
        return None
    return seconds if 0 < seconds < 2**40 else None


def _s(value: Any) -> str:
    return value if isinstance(value, str) else ""


def public_session(sid: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Session metadata for the client: plain, redacted text."""
    return {
        "id": sid,
        "ts": meta.get("epoch"),
        "runas": clean_line(meta.get("runas") or "", 64),
        "tty": clean_line(meta.get("tty") or "", 64),
        "cwd": clean_line(meta.get("cwd") or "", 512),
        "command": clean_line(meta.get("command") or "", LINE_CAP),
    }


# Output streams only; ttyin and stdin are never read.
OUTPUT_STREAMS = {1: "stdout", 2: "stderr", 4: "ttyout", 6: "ttyout"}


def interleave(timing: bytes | None, streams: dict[str, bytes]) -> bytes:
    """Put the output streams back in the order they were written, using the timing file only for
    order and byte counts (its delays are ignored). Without a usable timing file: ttyout, stdout,
    stderr one after the other."""
    if timing:
        pos = dict.fromkeys(streams, 0)
        out = bytearray()
        ok = False
        for line in timing.decode("ascii", "replace").splitlines():
            parts = line.split()
            if len(parts) < 3 or not parts[0].isdigit():
                continue
            kind = int(parts[0])
            name = OUTPUT_STREAMS.get(kind)
            if name is None or name not in streams or not parts[2].isdigit():
                continue
            n = int(parts[2])
            out += streams[name][pos[name]:pos[name] + n]
            pos[name] += n
            ok = True
        if ok:
            for name, data in streams.items():  # anything the timing file didn't cover (capped reads)
                out += data[pos[name]:]
            return bytes(out)
    return b"".join(streams[n] for n in ("ttyout", "stdout", "stderr") if n in streams)


# --------------------------------------------------------------------------- the helper


class Helper:
    def __init__(self, cfg: Config, system: System | None = None, log: Log | None = None) -> None:
        self.cfg = cfg
        self.sys = system or System()
        self.log = log or Log()
        self.deadline_at = float("inf")  # set per request by serve()
        self.handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "containers": self.op_containers,
            "logs": self.op_logs,
            "sudo-sessions": self.op_sudo_sessions,
            "sudo-replay": self.op_sudo_replay,
            "sudo-commands": self.op_sudo_commands,
            "localai-activity": self.op_localai_activity,
            "timers": self.op_timers,
        }
        assert set(self.handlers) == set(OPS)  # the allowlist and the dispatch table agree

    # ---- connection

    def serve(self, conn: socket.socket) -> str:
        """Handle one connection; returns the outcome code that was logged."""
        started = self.sys.monotonic()
        self.deadline_at = started + self.cfg.request_deadline
        try:
            pid, uid, _gid = peer_credentials(conn)
        except OSError as exc:
            self.log.error(f"rejected: no peer credentials ({exc.strerror or exc})")
            return "forbidden"
        reply = Reply(conn)
        conn.settimeout(self.cfg.request_timeout)
        allowed_uid = self.sys.uid_of(self.cfg.user)
        if allowed_uid is None or allowed_uid == 0:
            self.log.error(f"rejected peer uid={uid} pid={pid}: user {self.cfg.user} does not exist (or is root)")
            reply.error("forbidden", "only the aictl user may use this socket")
            return "forbidden"
        if uid != allowed_uid:
            self.log.warning(f"rejected peer uid={uid} pid={pid}: only uid {allowed_uid} ({self.cfg.user}) may connect")
            reply.error("forbidden", "only the aictl user may use this socket")
            return "forbidden"
        try:
            recv_started = self.sys.monotonic()
            recv_end = recv_started + self.cfg.request_timeout

            def recv_budget(cap: float) -> float:
                return min(cap, recv_end - self.sys.monotonic(), self.deadline_at - self.sys.monotonic())

            req = validate_request(read_request(conn, recv_budget, self.cfg.request_timeout), self.cfg,
                                   self.sys.time())
        except OpError as exc:
            self.log.warning(f"rejected request from uid={uid} pid={pid}: {exc.code} ({exc.message})")
            reply.error(exc.code, exc.message)
            return exc.code
        op = req["op"]
        self.log.info(f"request peer_uid={uid} peer_pid={pid} {describe(req)}")
        conn.settimeout(self.cfg.send_timeout)
        outcome = "ok"
        try:
            reply.ok(op, self.handlers[op](req))
        except OpError as exc:
            outcome = exc.code
            reply.error(exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - one request must never take the reply down silently
            outcome = "internal"
            frame = traceback.extract_tb(exc.__traceback__)[-1]
            self.log.error(f"internal error in {op}: {type(exc).__name__} at line {frame.lineno}")
            reply.error("internal", "internal error")
        elapsed = self.sys.monotonic() - started
        gone = " client-gone" if reply.gone else ""
        self.log.info(f"done {describe(req)} result={outcome}{gone} duration={elapsed:.2f}s")
        return outcome

    # ---- commands

    def check_deadline(self) -> None:
        """Between lines of a long reply: past the request deadline, stop with a timeout."""
        if self.sys.monotonic() > self.deadline_at:
            raise OpError("timeout", f"the request deadline ({self.cfg.request_deadline:g} s) has passed")

    def budget(self, cap: float) -> float:
        """Seconds a step may take: its own cap, never past the request deadline."""
        return min(cap, self.deadline_at - self.sys.monotonic())

    def run(self, argv: list[str], *, max_bytes: int = MAX_OUTPUT_BYTES, merge_stderr: bool = False) -> Result:
        timeout = self.budget(self.cfg.command_timeout)
        if timeout <= 0.05:
            raise OpError("timeout", f"the request deadline ({self.cfg.request_deadline:g} s) has passed")
        return self.sys.run(argv, timeout, max_bytes=max_bytes, merge_stderr=merge_stderr)

    def docker_state(self) -> str:
        res = self.run([self.cfg.systemctl, "is-active", self.cfg.docker_unit], max_bytes=256)
        lines = res.out.decode("utf-8", "replace").strip().splitlines()
        return lines[0].strip() if lines else "unknown"

    def docker(self, args: list[str], **kw: Any) -> Result:
        """The only way a docker command runs: the guard is checked right before it."""
        state = self.docker_state()
        if state != "active":
            raise OpError("docker-inactive", f"docker.service is {clean_log(state, 32)}; Docker is not touched")
        return self.run([self.cfg.docker, *args], **kw)

    def journal(self, args: list[str], matches: list[str]) -> list[dict[str, Any]]:
        argv = [self.cfg.journalctl, "--output=json", "--no-pager", "--quiet", *args, *matches]
        res = self.run(argv)
        # journalctl exits 1 when --grep matches nothing; that's an empty answer, not an error.
        if res.rc not in (0, 1) and not res.out:
            raise OpError("failed", f"journalctl exited {res.rc}")
        entries = []
        for line in res.out.decode("utf-8", "replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # e.g. the last line of a capped read
            if isinstance(entry, dict):
                entries.append(entry)
        return entries

    # ---- containers

    def inspect(self, names: list[str]) -> dict[str, dict[str, Any]]:
        res = self.docker(["inspect", "--type", "container", "--", *names], max_bytes=2 * 1024 * 1024)
        try:
            data = json.loads(res.out) if res.out.strip() else []
        except ValueError as exc:
            raise OpError("failed", "docker inspect returned invalid JSON") from exc
        if not isinstance(data, list):
            raise OpError("failed", "docker inspect returned an unexpected shape")
        found = {}
        for c in data:
            name = str((c or {}).get("Name", "")).lstrip("/") if isinstance(c, dict) else ""
            if name in names:  # exact names only (docker would also match an ID prefix)
                found[name] = c
        return found

    def op_containers(self, _req: dict[str, Any]) -> dict[str, Any]:
        state = self.docker_state()
        if state != "active":
            return {"docker": "failed" if state == "failed" else "inactive", "containers": []}
        names = list(self.cfg.containers)
        found = self.inspect(names) if names else {}
        containers = []
        for name in names:
            c = found.get(name)
            if c is None:
                containers.append({"name": name, "state": "missing", "health": None, "started_at": None,
                                   "restart_count": None, "image": None})
                continue
            st = c.get("State") or {}
            containers.append({
                "name": name,
                "state": st.get("Status"),
                "health": (st.get("Health") or {}).get("Status"),
                "started_at": rfc3339_epoch(st.get("StartedAt"))[0],
                "restart_count": c.get("RestartCount", 0),
                "image": (c.get("Config") or {}).get("Image"),
            })
        return {"docker": "active", "containers": containers}

    # ---- logs

    def op_logs(self, req: dict[str, Any]) -> dict[str, Any]:
        source, lines = req["source"], req["lines"]
        if source in self.cfg.log_units:
            args = ["--reverse", f"--lines={lines}", "--output-fields=MESSAGE,_SYSTEMD_UNIT"]
            if "since" in req:
                args.append(f"--since=@{req['since']}")
            for unit in self.cfg.log_units[source]:
                args.append(f"--unit={unit}")
            entries = self.journal(args, [])
            out = []
            for e in reversed(entries[:lines]):  # oldest first, like a tail
                self.check_deadline()
                out.append({"ts": _journal_ts(e), "text": clean_line(_message(e))})
            return {"source": source, "kind": "unit", "lines": out}
        # a container: resolve the exact name first, then read its log by ID
        found = self.inspect([source])
        c = found.get(source)
        if c is None or not re.match(r"^[0-9a-f]{64}\Z", str(c.get("Id", ""))):
            raise OpError("not-found", f"container {source} does not exist")
        args = ["logs", "--timestamps", "--tail", str(lines)]
        if "since" in req:
            args += ["--since", str(req["since"])]
        args.append(c["Id"])
        res = self.docker(args, merge_stderr=True)
        if res.rc != 0 and not res.out:
            raise OpError("failed", f"docker logs exited {res.rc}")
        text_lines = res.out.decode("utf-8", "replace").split("\n")
        if res.truncated and len(text_lines) > 1:
            text_lines = text_lines[:-1]
        out = []
        for raw in text_lines:
            if raw == "":
                continue
            ts, cut = rfc3339_epoch(raw)
            self.check_deadline()
            out.append({"ts": ts, "text": clean_line(raw[cut:])})
        return {"source": source, "kind": "container", "lines": out[-lines:]}

    # ---- sudo sessions

    def _deadline(self) -> Callable[[], bool]:
        end = self.sys.monotonic() + self.budget(self.cfg.read_timeout)
        return lambda: self.sys.monotonic() > end

    def _user_root(self, user: str) -> str:
        return os.path.join(self.cfg.iolog_root, user)

    def op_sudo_sessions(self, req: dict[str, Any]) -> dict[str, Any]:
        """Walk /var/log/sudo-io/<user> (no symlinks, depth <= 4, plain names only), take the newest
        sessions by the mtime of their log file, then read the metadata of those only."""
        user, limit = req["user"], req["limit"]
        expired = self._deadline()
        reader = StreamReader(expired)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            root_fd = os.open(self._user_root(user), flags)
        except FileNotFoundError:
            return {"user": user, "sessions": [], "total": 0, "complete": True}
        except OSError as exc:
            raise OpError("failed", f"cannot open the I/O log directory: {exc.strerror}") from exc
        found: list[tuple[float, str]] = []
        state = {"visited": 0, "complete": True}

        def walk(dir_fd: int, rel: str, depth: int) -> None:
            if expired() or state["visited"] >= MAX_WALK_DIRS:
                state["complete"] = False
                return
            state["visited"] += 1
            with os.scandir(dir_fd) as it:
                entries = list(it)
            if rel:
                for meta_name in ("log.json", "log"):
                    try:
                        st = os.stat(meta_name, dir_fd=dir_fd, follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISREG(st.st_mode):
                        found.append((st.st_mtime, rel))
                        return  # a session directory has no sessions below it
            if depth >= MAX_WALK_DEPTH:
                return
            for e in sorted(entries, key=lambda x: x.name):
                if not PATH_PART_RE.match(e.name) or e.name in (".", "..") or not e.is_dir(follow_symlinks=False):
                    continue
                try:
                    child = os.open(e.name, flags, dir_fd=dir_fd)
                except OSError:
                    continue
                try:
                    walk(child, f"{rel}/{e.name}" if rel else e.name, depth + 1)
                finally:
                    os.close(child)

        try:
            walk(root_fd, "", 0)
        finally:
            os.close(root_fd)
        found.sort(reverse=True)
        sessions = []
        skipped = 0
        for _mtime, rel in found[:limit]:
            if expired():
                state["complete"] = False
                break
            try:
                session = SessionDir(self._user_root(user), rel)
            except OpError:
                continue  # removed since the walk
            try:
                meta = parse_session_meta(reader, session.fd)
            except OpError as exc:
                if exc.code == "timeout":
                    state["complete"] = False
                    break
                skipped += 1  # a damaged session never fails the list
                continue
            finally:
                session.close()
            if meta is None:
                skipped += 1
                continue
            sessions.append((meta.get("epoch") or 0, rel, meta))
        sessions.sort(key=lambda s: (s[0], s[1]), reverse=True)
        return {"user": user, "sessions": [public_session(rel, meta) for _, rel, meta in sessions],
                "total": len(found), "skipped": skipped, "complete": state["complete"]}

    def op_sudo_replay(self, req: dict[str, Any]) -> dict[str, Any]:
        user, rel, max_bytes = req["user"], req["id"], req["max_bytes"]
        expired = self._deadline()
        reader = StreamReader(expired)
        session = SessionDir(self._user_root(user), rel)
        try:
            meta = parse_session_meta(reader, session.fd)
            if meta is None:
                raise OpError("not-found", "no such session")
            data = public_session(rel, meta)
            data["output"] = None
            data["output_truncated"] = False
            if not req["output"]:
                return data
            raw_cap = max_bytes + REDACTION_MARGIN
            streams: dict[str, bytes] = {}
            damaged = False
            raw_truncated = False
            for name in ("ttyout", "stdout", "stderr"):
                got = reader.read(session.fd, name, raw_cap)
                if got is not None:
                    streams[name] = got[0]
                    raw_truncated |= got[1]
                    damaged |= got[2]
            timing = reader.read(session.fd, "timing", MAX_TIMING_BYTES)
            joined = interleave(timing[0] if timing else None, streams)
        finally:
            session.close()
        raw_truncated |= len(joined) > raw_cap
        raw = joined[:raw_cap]
        if raw_truncated:
            # The cut may go through a secret, leaving a fragment too short for its pattern: drop
            # everything after the last whitespace (escape sequences can shrink to nothing, so the
            # fragment could otherwise land inside max_bytes).
            last = max(raw.rfind(b" "), raw.rfind(b"\n"), raw.rfind(b"\t"), raw.rfind(b"\r"))
            raw = raw[:last + 1] if last >= 0 else b""
        # Order matters: strip terminal sequences, cap each line, redact line by line, then truncate.
        plain = strip_terminal(raw.decode("utf-8", "replace"))
        line_capped = any(len(line) > REDACT_LINE_CAP for line in plain.split("\n"))
        text, cut = truncate_utf8(redact(plain, self.check_deadline), max_bytes)
        data["output"] = text
        data["output_truncated"] = cut or raw_truncated or line_capped
        if damaged:
            data["output_damaged"] = True
        return data

    # ---- journal audits

    def op_sudo_commands(self, req: dict[str, Any]) -> dict[str, Any]:
        """sudo's journal lines BY the audited user (Lead, after Reviewer A round 2): _EXE is a configured
        sudo binary AND _UID is the audited user's uid (journalctl ORs the _EXE matches and ANDs them
        with _UID), and the user named at the start of the message is the audited user. Python checks
        all three again. _EXE alone can be faked from a user namespace; the _UID match keeps other
        users' forgeries and floods out. A user forging rows about itself remains possible (accepted;
        the sudo I/O session list is the authoritative record)."""
        user, limit = req["user"], req["limit"]
        uid = self.sys.uid_of(user)
        if uid is None:
            raise OpError("not-found", f"user {user} does not exist")
        # user is from the allowlist (USER_RE), so it is safe inside the pattern. sudo pads the name
        # to 8 characters ("%8s : "), hence the leading spaces.
        args = ["--reverse", f"--since=@{req['since']}", f"--lines={limit}",
                "--output-fields=MESSAGE,_EXE,_UID,_PID", "--case-sensitive=true", f"--grep=^\\s*{user} : "]
        entries = self.journal(args, [*(f"_EXE={exe}" for exe in self.cfg.sudo_exes), f"_UID={int(uid)}"])
        rows: list[tuple[dict[str, Any], dict[str, Any], str]] = []
        continued_pids: set[str] = set()
        for e in entries:
            self.check_deadline()
            if e.get("_EXE") not in self.cfg.sudo_exes or str(e.get("_UID")) != str(uid):
                continue  # belt and braces: journalctl was asked for exactly these
            parsed = parse_sudo_message(_message(e))
            if parsed is None or parsed["user"] != user:
                continue
            pid = str(e.get("_PID", ""))
            if parsed["continued"] and pid:
                continued_pids.add(pid)
            rows.append((e, parsed, pid))
            if len(rows) >= limit:
                break
        out = []
        for e, parsed, pid in rows:
            # A row is ambiguous when its fields can't be trusted to be what sudo meant: a cwd or command
            # holding a "; KEY=" sudo-field pattern, a repeated field, or a line split into parts.
            # (the parser flags a "(command continued)" part itself; its first part is found by _PID here)
            ambiguous = parsed["ambiguous"] or (bool(pid) and pid in continued_pids and not parsed["continued"])
            out.append({"ts": _journal_ts(e), "user": parsed["user"], "runas": clean_line(parsed["runas"], 64),
                        "cwd": clean_line(parsed["cwd"], 512), "command": clean_line(parsed["command"]),
                        "event": parsed["event"], "ambiguous": bool(ambiguous)})
        return {"user": user, "commands": out}

    def op_localai_activity(self, req: dict[str, Any]) -> dict[str, Any]:
        uid = self.sys.uid_of(self.cfg.localai_user)
        if uid is None:
            raise OpError("not-found", f"user {self.cfg.localai_user} does not exist")
        limit = req["limit"]
        args = ["--reverse", f"--since=@{req['since']}", f"--lines={limit}",
                "--output-fields=MESSAGE,_SYSTEMD_UNIT,_COMM,_UID"]
        entries = self.journal(args, [f"_UID={int(uid)}"])
        out = []
        for e in entries[:limit]:
            self.check_deadline()
            if str(e.get("_UID", uid)) != str(uid):
                continue
            out.append({"ts": _journal_ts(e), "unit": clean_line(_s(e.get("_SYSTEMD_UNIT")), 256),
                        "comm": clean_line(_s(e.get("_COMM")), 64), "message": clean_line(_message(e))})
        return {"user": self.cfg.localai_user, "entries": out}

    # ---- timers

    def op_timers(self, _req: dict[str, Any]) -> dict[str, Any]:
        res = self.run([self.cfg.systemctl, "list-timers", "--all", "--output=json", "--no-pager"],
                       max_bytes=2 * 1024 * 1024)
        if res.rc != 0:
            raise OpError("failed", f"systemctl list-timers exited {res.rc}")
        try:
            data = json.loads(res.out) if res.out.strip() else []
        except ValueError as exc:
            raise OpError("failed", "systemctl list-timers returned invalid JSON") from exc
        timers = []
        for t in data if isinstance(data, list) else []:
            if not isinstance(t, dict):
                continue
            unit, activates = t.get("unit"), t.get("activates")
            if not isinstance(unit, str) or not UNIT_NAME_RE.match(unit):
                continue
            if not any(fnmatch.fnmatchcase(unit, p) for p in self.cfg.timer_patterns):
                continue
            if not isinstance(activates, str) or not UNIT_NAME_RE.match(activates):
                activates = None
            timers.append({"unit": unit, "activates": activates, "next": _usec(t.get("next")),
                           "last": _usec(t.get("last"))})
        results: dict[str, str] = {}
        services = sorted({t["activates"] for t in timers if t["activates"]})
        if services:
            res = self.run([self.cfg.systemctl, "show", "--property=Id,Result", "--", *services],
                           max_bytes=256 * 1024)
            if res.rc == 0:
                results = parse_show(res.out.decode("utf-8", "replace"))
        out = []
        for t in sorted(timers, key=lambda x: x["unit"]):
            result = results.get(t["activates"] or "") if t["last"] else None
            out.append({"unit": t["unit"], "next": t["next"], "last": t["last"],
                        "last_result": clean_line(result, 32) if result else None})
        return {"timers": out}


# --------------------------------------------------------------------------- parsing helpers


def _message(entry: dict[str, Any]) -> str:
    msg = entry.get("MESSAGE")
    if isinstance(msg, list) and all(isinstance(b, int) and 0 <= b < 256 for b in msg):
        return bytes(msg).decode("utf-8", "replace")  # journald's encoding of non-UTF-8 messages
    return msg if isinstance(msg, str) else ""


def _journal_ts(entry: dict[str, Any]) -> int | None:
    """__REALTIME_TIMESTAMP (microseconds, as a string) -> epoch seconds."""
    try:
        usec = int(entry["__REALTIME_TIMESTAMP"])
    except (KeyError, TypeError, ValueError):
        return None
    return usec // 1_000_000 if usec > 0 else None


def _usec(value: Any) -> int | None:
    """systemctl's microsecond timestamps -> epoch seconds; 0/null (never) -> None."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value // 1_000_000


SUDO_LINE_RE = re.compile(r"^\s*(?P<user>[a-z_][a-z0-9_-]{0,31}) : (?P<rest>.*)\Z", re.S)


SUDO_FIELD_IN_TEXT_RE = re.compile(r";[ \t]{0,8}(?:TTY|PWD|USER|GROUP|ENV|COMMAND|TSID)=")
SUDO_CONTINUED = "(command continued)"


def parse_sudo_message(msg: str) -> dict[str, Any] | None:
    """`claude : TTY=pts/0 ; PWD=/home/claude ; USER=root ; COMMAND=/usr/bin/apt update` -> fields.
    COMMAND is last and may itself contain " ; ", so everything after "COMMAND=" is the command.
    A long line is split by sudo into parts; the later ones read `claude : (command continued) …` and
    come back as a row with only `command` set, flagged continued."""
    m = SUDO_LINE_RE.match(msg)
    if not m:
        return None
    rest = m.group("rest")
    if rest.startswith(SUDO_CONTINUED):
        return {"user": m.group("user"), "runas": "", "cwd": "", "command": rest[len(SUDO_CONTINUED):].lstrip(),
                "event": "command", "continued": True, "ambiguous": True}
    command = ""
    cm = re.search(r"(?:^|\s;\s)COMMAND=", rest)
    if cm:
        command = rest[cm.end():]
        rest = rest[:cm.start()]
    fields: dict[str, str] = {}
    repeated = False
    notes: list[str] = []  # the free-text parts before TTY=, e.g. "command not allowed"
    for part in rest.split(" ; "):
        key, sep, value = part.strip().partition("=")
        if sep and key.isupper():
            repeated |= key in fields
            fields[key] = value
        elif part.strip():
            notes.append(part.strip())
    if not command and "USER" not in fields:
        return None
    cwd = fields.get("PWD", "")
    ambiguous = repeated or bool(SUDO_FIELD_IN_TEXT_RE.search(command) or SUDO_FIELD_IN_TEXT_RE.search(cwd))
    return {"user": m.group("user"), "runas": fields.get("USER", ""), "cwd": cwd, "command": command,
            "event": classify_sudo_event(notes), "continued": False, "ambiguous": ambiguous}


# sudo's own wording for a refusal or a failed authentication (Lead, 2026-10-26). Matched against the
# free-text parts of the line only, never against the command, so a command can't fake its event.
SUDO_REFUSED_RE = re.compile(r"\bcommand not allowed\b|\ba password is required\b|\buser NOT in sudoers\b", re.I)
SUDO_AUTH_FAIL_RE = re.compile(r"\bincorrect password attempts?\b|\bauthentication failure\b", re.I)


def classify_sudo_event(notes: list[str]) -> str:
    """"command" | "refused" | "auth_fail" from the free-text parts of a sudo log line. A line naming
    both (e.g. wrong passwords, then not allowed) is auth_fail, the more alarming of the two."""
    text = " ; ".join(notes)
    if SUDO_AUTH_FAIL_RE.search(text):
        return "auth_fail"
    if SUDO_REFUSED_RE.search(text):
        return "refused"
    return "command"


def parse_show(text: str) -> dict[str, str]:
    """`systemctl show --property=Id,Result a b` blocks -> {unit: result}."""
    results: dict[str, str] = {}
    for block in text.split("\n\n"):
        props = dict(line.partition("=")[::2] for line in block.splitlines() if "=" in line)
        if props.get("Id"):
            results[props["Id"]] = props.get("Result", "")
    return results


# --------------------------------------------------------------------------- entry point


def close_quietly(conn: socket.socket) -> None:
    """Discard input the client already sent before closing, so it reads the reply and a clean end of
    stream instead of ECONNRESET."""
    with contextlib.suppress(OSError):
        conn.setblocking(False)
        for _ in range(16):
            if not conn.recv(65536):
                break
    with contextlib.suppress(OSError):
        conn.shutdown(socket.SHUT_RDWR)
    conn.close()


def connection_fd() -> int:
    """systemd passes an Accept=yes connection as fd 3 (LISTEN_FDS=1 for this pid) and, with
    StandardInput=socket, also as stdin."""
    if os.environ.get("LISTEN_FDS") == "1" and os.environ.get("LISTEN_PID") == str(os.getpid()):
        return 3
    return 0


def main() -> int:
    log = Log()
    try:
        conn = socket.socket(fileno=os.dup(connection_fd()))
    except OSError:
        log.error("no socket on fd 3 or stdin; this program is started by aictl-helper.socket")
        return 2
    try:
        if conn.family != socket.AF_UNIX:
            log.error("the connection is not a Unix socket")
            return 2
        try:
            # The file must belong to the user the helper runs as: root in production.
            cfg = load_config(os.environ.get(CONFIG_ENV, DEFAULT_CONFIG), os.geteuid())
        except (OSError, ValueError, TypeError) as exc:
            log.error(f"cannot load the config: {clean_log(exc)}")
            Reply(conn).error("internal", "helper misconfigured")
            return 1
        Helper(cfg, System(), log).serve(conn)
    finally:
        close_quietly(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
