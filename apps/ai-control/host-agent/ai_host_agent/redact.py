"""Keeps secrets out of what the agent returns.

One rule set (Lead, 2026-10-26): a port of helper X's reference rules in
apps/ai-control/helper/aictl_helper.py ("redaction"), proved equal by loading X's
tests/redact_cases.json in tests/test_redact.py. Change the rules there first, then here.

On top of X's rules, Y keeps two extras: the agent's own token is replaced wherever it appears
(`Redactor.known`), and every 200 body gets a last pass for it (`Redactor.scrub`).

Order: escapes and controls are stripped, the token is replaced, the line is capped at
REDACT_LINE_CAP (a cut drops the partial word it went through), redacted, and only then cut to
the response cap. Every rule is linear-time (see X's notes below); a per-rule substring
prefilter and ASCII folding instead of (?i) keep a dense maximum-size reply near 2 s.
The rules block below is long-lined because it is X's text, copied verbatim (Python 3.11+)."""

import re
from typing import Any

MASK = "[REDACTED]"
REDACT_LINE_CAP = 8192   # characters per line before redaction
LINE_MAX = 2000          # characters per line in a response, after redaction

# ---- cleaning: terminal sequences and control characters (as X)
ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC (title, hyperlinks), BEL or ST terminated
    r"|\x1b[P^_X][^\x1b]*(?:\x1b\\)?"  # DCS, PM, APC, SOS
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI (colours, cursor moves)
    r"|\x1b[()*+][0-9A-Za-z]"  # charset selection
    r"|\x1b[ -/]*[0-~]"  # other two-character escapes
    r"|\x9b[0-?]*[ -/]*[@-~]"  # 8-bit CSI
)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# ---- the rules: VERBATIM from apps/ai-control/helper/aictl_helper.py (X's redaction section,
# from _SECRET_KEYWORD to redact_line/_redact_pem). Re-copy it whole when X changes; the
# tests compare every pattern, flag, trigger and fold with X's.
REDACTED = MASK
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


def redact_lines(lines):
    """X's redact() loop, as a generator over consecutive lines (a journal tail): each capped at
    REDACT_LINE_CAP (dropping a cut partial word), then redacted. Private key blocks, Ansible vault
    bodies and YAML block scalars under secret-named keys are tracked across lines."""
    in_pem = in_vault = False
    yaml_col = None  # inside a secret YAML block: the key's column
    for raw in lines:
        line, _ = cap_line(raw)
        if yaml_col is not None:
            body = line.lstrip(" \t")
            indent = len(line) - len(body)
            if not body:
                yield line  # blank lines belong to the block
                continue
            if indent > yaml_col:
                yield line[:indent] + REDACTED
                continue
            yaml_col = None  # indentation dropped back: the block is over
        if in_vault:
            m = VAULT_BODY_RE.match(line)
            if m:
                yield m.group(1) + REDACTED
                continue
            in_vault = False
        if in_pem:
            line, in_pem = _redact_pem(line, inside=True)
            if in_pem:
                yield line
                continue
            # the block ended on this line: the rest of the line gets the normal rules below
        line = apply_rules(line)
        line, in_pem = _redact_pem(line) if "-----" in line else (line, False)
        in_vault = bool(VAULT_HEADER_RE.search(line))
        if not in_pem:
            yaml_col = yaml_secret_block(line)
        yield line


def redact(text):
    """Multi-line text, exactly as X's redact()."""
    return "\n".join(redact_lines(text.split("\n")))


def strip_controls(text):
    return CONTROL_RE.sub("", ANSI_RE.sub("", text))


class Redactor:
    """X's rules plus the agent's known secrets (its token), replaced wherever they appear."""

    def __init__(self, secrets=()):
        self._secrets = [s for s in secrets if isinstance(s, str) and len(s) >= 8]

    def known(self, text):
        """Only the known secret values (also for short structured fields)."""
        for s in self._secrets:
            if s in text:
                text = text.replace(s, MASK)
        return text

    def scrub(self, value):
        """A JSON-able response body with the known secrets replaced in every string: the last
        line of defence, so no endpoint can return the token whatever the host says."""
        if isinstance(value, str):
            return self.known(value)
        if isinstance(value, list):
            return [self.scrub(v) for v in value]
        if isinstance(value, dict):
            return {self.scrub(k): self.scrub(v) for k, v in value.items()}
        return value

    def _clean(self, raw):
        """One line of host text: bounded, escapes and controls out (line breaks become
        spaces), known secrets out. Stripping first rejoins a secret split by an escape."""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        text = raw[:4 * REDACT_LINE_CAP]  # bound the cleaning work; the redaction cap follows
        text = ANSI_RE.sub("", text).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        return self.known(CONTROL_RE.sub("", text))

    def line(self, raw, limit=LINE_MAX):
        """One standalone line (a sudo command): clean, cap, redact, then cut to `limit`."""
        text, _ = cap_line(self._clean(raw))
        return redact_line(text)[:limit]

    def lines(self, raws, limit=LINE_MAX):
        """Consecutive lines (a journal tail), so a key block spanning lines is redacted whole."""
        return [line[:limit] for line in redact_lines(self._clean(r) for r in raws)]
