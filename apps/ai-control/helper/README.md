# aictl-helper (module X)

The AI Control dashboard's privileged helper on ai-box. Spec: `docs/CONTRACTS.md` §12.15.2; wire protocol
and structure copied from `homelab-dockerctl` (§11.2). It's one stdlib Python file, `aictl_helper.py`.
P1 is **read-only**: no op changes anything on the host.

## How it runs (for H3, `roles/ai_control/`)

- `aictl-helper.socket`: `ListenStream=/run/aictl-helper.sock`, `SocketUser=root`, `SocketGroup=aictl`,
  `SocketMode=0660`, `Accept=yes`, `MaxConnections=` about 32.
- `aictl-helper@.service`:
  - runs as root;
  - `ExecStart=/usr/bin/python3 -I /opt/ai-control/helper/aictl_helper.py` (or the file itself, which has a `-I` shebang);
  - `StandardInput=socket` or plain fd 3 (both work: the helper takes fd 3 when `LISTEN_FDS=1` is for its own pid, else stdin);
  - `StandardError=journal` and `SyslogIdentifier=aictl-helper`;
  - `RuntimeMaxSec=30`.
  - Hardening that fits a read-only helper: `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`,
    `NoNewPrivileges=yes`, `RestrictAddressFamilies=AF_UNIX`. It needs to read `/var/log/sudo-io` and the journal,
    and to talk to the Docker socket.
- The config is `/etc/ai-control/helper.json`, root:root 0644. The helper **refuses to start a request** if the file
  isn't a regular file owned by the user it runs as (root) or if group or others can write it. The answer is then
  `internal / helper misconfigured`. `helper.example.json` spells out the defaults, which equal the contract's
  allowlists. A missing key takes its default; an unknown key is an error.

## Protocol

It speaks the same protocol as `homelab-dockerctl`, without streams:

- **Request:** one JSON line of at most 4 KiB per connection. Write it, never half-close.
- **Reply:** `{"ok":true,"op","data"}` or `{"ok":false,"error":{"code","message"}}`. The error codes are
  `forbidden`, `bad-request`, `unknown-op`, `not-allowed`, `not-found`, `damaged` (a session whose metadata can't be
  parsed), `docker-inactive`, `timeout`, `failed` and `internal`.

**Peer check:** the `SO_PEERCRED` uid must equal `pwd.getpwnam(config.user).pw_uid`. The default user is `aictl`, and
it's looked up on every connection. Anyone else, including root, gets `forbidden` and is dropped before the request is
read. If the user doesn't exist, everyone is refused.

**Times (Lead, 2026-10-26):** `since` in a request is **integer unix seconds**. Text of any kind, digits included, is
refused. Every time in a reply (`ts`, `started_at`, `next`, `last`) is integer unix seconds, or `null` when unknown or
never. `since` reaches journalctl as `--since=@N` and docker as `--since N`, so no client text is passed to either. A
value more than a day ahead is refused. One older than 400 days is clamped to 400 days, which is longer than any
retained log. Where `since` is optional and omitted, `sudo-commands` and `localai-activity` use the last 24 h, and
`logs` has no lower bound (just the last `lines`).

**Exact `data` shapes.** `int` means integer unix seconds; `T|null` means null is possible.

```text
containers        {"op":"containers"}
  → {"docker": "active"|"inactive"|"failed",
     "containers": [{"name": str, "state": str|"missing", "health": str|null, "started_at": int|null,
                     "restart_count": int|null, "image": str|null}]}
    Allowlisted names only, in config order. "containers" is [] unless docker is "active".

logs              {"op":"logs", "source": str, "lines"?: 1..1000 = 200, "since"?: int}
  → {"source": str, "kind": "unit"|"container", "lines": [{"ts": int|null, "text": str}]}
    Oldest first, at most `lines` entries, each text ≤ 2000 chars (redacted, no control characters).

sudo-sessions     {"op":"sudo-sessions", "user": "claude"|"localai", "limit"?: 1..200 = 50}
  → {"user": str, "sessions": [{"id": str, "ts": int|null, "runas": str, "tty": str, "cwd": str, "command": str}],
     "total": int, "skipped": int, "complete": bool}
    Newest first. "id" is the path form ("00/00/2A"). total = sessions found; skipped = sessions among the newest
    `limit` whose metadata couldn't be parsed (bad or deeply nested log.json with no usable legacy log, a symlinked
    file): they're left out, never failing the list. complete=false if the walk hit its limits or the deadline.

sudo-replay       {"op":"sudo-replay", "user": "claude"|"localai", "id": str, "max_bytes"?: 1..262144 = 65536,
                   "output"?: bool = false}
  → {"id": str, "ts": int|null, "runas": str, "tty": str, "cwd": str, "command": str,
     "output": str|null, "output_truncated": bool, "output_damaged"?: true}
    "output" is null unless "output": true; then plain text of at most max_bytes UTF-8 bytes. output_truncated is
    also true when a line was over 8 KiB (capped before redaction). A session with unparseable metadata is the error
    "damaged".

sudo-commands     {"op":"sudo-commands", "user": "claude"|"localai", "since"?: int, "limit"?: 1..500 = 100}
  → {"user": str, "commands": [{"ts": int|null, "user": str, "runas": str, "cwd": str, "command": str,
                                 "event": "command"|"refused"|"auth_fail", "ambiguous": bool}]}
    Newest first. Rows are sudo lines BY `user` (Lead, after Reviewer A round 2): the query is
    `_EXE=<sudo_exes…> _UID=<user's uid>` (journalctl ORs the _EXE values and ANDs them with _UID), the
    message must start with `user` (sudo pads it: "%8s : "), and Python re-checks _EXE and _UID. _EXE alone
    can be faked from a user namespace; the _UID match keeps other users' forgeries and floods out. A user
    forging rows about itself is an accepted residual risk: the sudo I/O session list is authoritative.
    A user missing from the host is the error "not-found".
    "event" comes from sudo's free-text parts before TTY=, never from the command: "command not allowed" /
    "a password is required" / "user NOT in sudoers" (localai's normal line) → refused;
    "N incorrect password attempt(s)" / "authentication failure" → auth_fail (wins if both); otherwise command.
    PAM's own "pam_unix(sudo:auth): …" lines aren't sudo-format lines and aren't returned.
    "ambiguous" is true when the row's fields can't be trusted to be what sudo meant: its cwd or command holds a
    "; KEY=" sudo-field pattern (TTY, PWD, USER, GROUP, ENV, COMMAND, TSID), a field appears twice, or sudo
    split the line: a "(command continued)" part is its own row (only "command" set, ambiguous), and the first
    part is flagged too when a continuation with the same _PID is in the reply. Parts are not stitched.

localai-activity  {"op":"localai-activity", "since"?: int, "limit"?: 1..500 = 100}
  → {"user": str, "entries": [{"ts": int|null, "unit": str, "comm": str, "message": str}]}
    Newest first; journal entries with _UID = the localai uid.

timers            {"op":"timers"}
  → {"timers": [{"unit": str, "next": int|null, "last": int|null, "last_result": str|null}]}
    Sorted by unit; units matching timer_patterns (ai-*, homelab-*). last_result is the activated service's
    systemd Result ("success", "exit-code", …), null if the timer never ran.
```

**Session `id`** is either a six-character TSID (`00002A`, which becomes `00/00/2A`, as sudoreplay does) or a relative
path matching the contract regex. Every component must match `[0-9A-Za-z._-]{1,64}`, and an empty component, `.`,
`..` or a leading `/` is refused.

**Log sources:** each source maps to fixed `journalctl --unit=` patterns from `log_units`:

- `docker`, `tailscaled`, `chrony` and `ai-control`;
- `aictl-helper`, which maps to `aictl-helper@*.service`;
- `homelab-ai-stack`, which maps to `homelab-ai-stack*`.

A container source must be one of the names in `containers`. Its name is resolved with `docker inspect`, checked
against the exact name, and the log is read by the 64-hex ID.

## Rules the code enforces

- **No shell.** Each command is a fixed argv using the absolute paths from the config. Children run in their own
  process group with stdin on `/dev/null` and a minimal environment (`PATH`, a C.UTF-8 locale, no colours, no pager).
- **Timeouts:** 8 s by default for every command. The config refuses anything above 10 s, and `System.run` also
  clamps any value to 10 s. When a command times out, its whole process group is killed (children included).
- **Overall deadline:** `request_deadline` (10 s, the most the config allows) runs from the start of the connection
  across every recv, every command and the sudo-io reads; each step gets the smaller of its own timeout and what's
  left. A client trickling its request a byte at a time is cut off at the deadline.
- **Output caps:**
  - 8 MiB of stdout from any one command (2 MiB for inspect and list-timers);
  - 2000 characters per log, journal or command line;
  - `max_bytes` for a replay.
- **Docker guard:** before any docker command, `systemctl is-active docker.service` must print `active`. It's checked
  again right before every docker command, never cached. While Docker is inactive, `containers` answers
  `{docker:"inactive", containers:[]}`, and the docker CLI and socket are never touched.
- **Redaction** runs on every piece of host text before it's truncated: log lines, journal messages, sudo command lines
  and session output. **The reference is `tests/redact_cases.json`** (`[{"in","out"}]`, synthetic), which Y loads too.
  - **Linear time** (Reviewer A round 1): every rule starts on a literal keyword or on a position not preceded by a
    character of its own run (lookbehind), so no run is scanned twice; whitespace and prefixes are bounded; runs
    followed by more pattern are possessive. Each line is capped at 8 KiB **before** redaction (a cut drops the
    trailing partial word), and multi-line text is redacted line by line. `test_hardening.py` runs every rule,
    uncapped, on 32 pathological 64 KiB inputs (plus two multi-line YAML ones) and 6 dense-keyword 512 KiB
    inputs, each in under 1 s.
  - **Performance** (Reviewer A rounds 2–3):
    - **Triggers:** a rule runs only on a line containing its trigger substring(s).
    - **Folding:** the case-insensitive rules match a lowercase pattern against an ASCII-lowercased copy of the
      line. The copy has the same length, so spans map back (`str.lower()` would not: `İ` lowercases to two
      characters). It's equal to `(?i)` for ASCII and about 7× faster.
    - **Literal first:** every rule starts with a literal or a literal alternation. A start-of-token or
      start-of-argument check is a fixed-width lookbehind placed just after the literal
      (`hf_(?<![A-Za-z0-9_-]hf_)`, `-(?<!\S-)p`), so the engine searches for the literal instead of trying every
      position.
    - **Tests:** a maximum-size reply (1000 lines × 8 KiB) that is dense for each rule in turn, and one line
      triggering every rule, each redact in under 2 s (worst 0.67 s here). Tests also check that the speed-only
      triggers and the folding never change a result. The request deadline is checked between lines and between
      journal entries, and tests expire it right after journalctl returns.
  - **What it covers:**
    - `Authorization:` headers and `Bearer …`;
    - a value after `=`/`:` whose name ends in token, secret, secret_key (`SECRET_KEY`, `WEBUI_SECRET_KEY`), password,
      passwd, passphrase, api_key, access_key, private_key, auth_key, client_secret, `_pass` (`DB_PASS`, `SMTP_PASS`,
      `ansible_become_pass`) or `_pwd` (`MYSQL_PWD`; plain `PWD=`/`OLDPWD=` are untouched). Quoted values are
      redacted up to the closing quote, backslash escapes included (`"abc\"hunter2"`). A YAML `!vault` tag is left
      alone (its body is redacted instead);
    - flags with `=` or a space-separated value: `--password`, `--pass`, `--passphrase`, `--db-password`,
      `--db-pass`, `--passwd`, `--token`, `--api-key`, `--secret`, `--auth-key`, `--client-secret` (a quoted value
      whole; `--password-file`, `--pass-through`, `--token-file` untouched);
    - **options that carry a password, anywhere on a line that names their command.** This is a **scoped rule**: its
      trigger is part of the rule, not a speed-up. There's no argument window, so many or long arguments before the
      option don't hide it. The option must start an argument (`(?<!\S-)`).
      - `curl -u user:pass`, `-uuser:pass` and `--user user:pass`: the password part only;
      - `-pPASSWORD`, glued to its value, on a `mysql`/`mariadb` line (`-p` alone is the prompt);
      - `sshpass -p X`/`-pX`;
      - `docker login -p X` (the line needs both `docker` and `login`; `--password X` is a flag);
      - `redis-cli -a X`.

      Values can be quoted (`-p 'two words'`, with escapes). **Accepted over-redaction:** a same-shaped option of
      another tool on that line, e.g. `id -u alice:staff` on a curl line, or `docker run -a stdout` on a redis-cli
      line. Without the command on the line nothing changes: `docker run -p 8080:80`, `ssh -p 2222`,
      `docker run -u 1000:1000`.
    - `Cookie:` and `Set-Cookie:` values: `name=value` pairs separated by `; `, a value possibly quoted
      (`a="PW"`). An unquoted quote ends the header, as in `-H "Cookie: sid=x" url`. `cookies: 3` is untouched;
    - a literal `[REDACTED]` never shields text glued to it: `password=[REDACTED]secret` and `[REDACTED]glued`
      become `[REDACTED]` (a `.` right after a marker counts only when a token character follows);
    - URL credentials (`://user:pass@`, and `://:pass@` with an empty user, as in `redis://:pw@host`);
    - Ansible vault blobs: `$ANSIBLE_VAULT;1.x;AES256[;label]` keeps its header; the hex body is redacted, on one line
      or on the following lines;
    - YAML block scalars under a secret-named key (`password: |`, `api_token: >-`, `- token: |2`, quoted keys, a
      trailing comment): the key line is kept, and every following line indented more than the key's column (for
      `- key:`, the column after the dash) becomes `[REDACTED]` at its own indentation. Blank lines stay inside the
      block, and the block ends when indentation drops back to the key's column or less. A non-secret key with `|`
      (`script: |`) is untouched. When the block was already flattened onto one line (`password: | s3cret`,
      `token: >- abc def`), everything after the indicator to the line end is redacted. A secret key with an
      empty value (`password:`) opens a block the same way, for a value on the next, more-indented line. A comment alone
      (`password: | # x`) is kept. The block start is matched anchored at the line start with a bounded name, so it's
      linear. Logs and journal messages are redacted line by line too, and only then flattened to one line;
    - `tskey-…`, `sk-…`, `ghp_`/`gho_`/…, `github_pat_…`, `glpat-…`, `hf_…`, `xox?-…`, `AKIA…`, JWTs, and PEM, OpenSSH
      and PGP private key blocks (`-----BEGIN … PRIVATE KEY[ BLOCK]-----`, on one line or across lines; each line
      touched becomes one `[REDACTED]`).

  A replay reads 64 KiB more than `max_bytes`. When that read was cut, everything after the last whitespace is
  dropped first: escape sequences can strip to nothing and bring the cut fragment inside `max_bytes`, where a
  too-short fragment of a token wouldn't match its pattern. Then it strips, caps lines, redacts line by line and
  truncates, in that order.
- **Logging:** each request is logged as `request peer_uid= peer_pid= op=… <validated args>`, followed by
  `done … result= duration=`. Raw requests and output are never logged. A refused request is logged with its error
  code only.

## Reading sudo sessions: why there's no sudoreplay

`sudo-sessions` and `sudo-replay` read the I/O log files directly and never run `sudoreplay`:

- **No tty, no timing.** In replay mode sudoreplay goes interactive when it has a terminal, and it reproduces the
  recorded delays unless given `-m 0`. Reading the files means nothing allocates a tty or sleeps. The `timing` file is
  used only for the **order** and byte counts of the output chunks, so stdout and stderr interleave as they were
  written. Its delays are ignored. Without a timing file, the streams are concatenated in the order ttyout, stdout,
  stderr.
- **Formats:** `log.json` is preferred, as sudoreplay prefers it; the legacy `log` file is the fallback. Each stream is
  detected from its first bytes:
  - gzip (sudo's default with `compress_io`), including concatenated members;
  - zlib;
  - plain (`!compress_io`).

  Decompression stops at the cap, so a decompression bomb costs nothing. A corrupt stream returns what decoded and
  sets `output_damaged`. A session still running (no gzip trailer yet) reads normally.
- **Only output streams are read:** `ttyout`, `stdout` and `stderr`. `ttyin` and `stdin` are **never opened**: they
  hold keystrokes and the Ansible module arguments, `no_log` values included (see `roles/operator_account/README.md`).
  `runenv` from `log.json` is never returned either.
- **Terminal output becomes plain text:**
  - CSI, OSC, DCS and other escape sequences, and 8-bit CSI, are removed;
  - CRLF becomes LF;
  - a lone CR returns to the start of the line and later characters overwrite it, so a progress bar shows its final
    state;
  - backspace moves back one character;
  - other control characters are dropped, except tab and LF.
- **No symlinks, at any level.** The user's directory, each component of the session id, and every file inside are
  opened with `O_NOFOLLOW` relative to the previous directory (`dir_fd`). A FIFO can't block a read (`O_NONBLOCK`,
  then a regular-file check).
- **Listing:** the walk goes at most 4 levels deep, follows plain names only, and stops after 100k directories or the
  `read_timeout`, in which case `complete:false`. It ranks sessions by the mtime of their `log`/`log.json`, then reads
  metadata only for the newest `limit`. `total` is the number of sessions found.

## Tests

```sh
python3 -m unittest discover -s apps/ai-control/helper/tests -v     # from the repo root
```

All fixtures are synthetic. There's no root, Docker, journal or network.

`tests/harness.py` sets up three things:
- a temp `bin/` with one stub program installed as `docker`, `systemctl` and `journalctl`. The stub records every
  argv to `calls.jsonl` and answers from `scenario.json` (stdout, stderr, rc, sleep). The helper runs it through the
  **real** `System.run`.
- fake sudo I/O trees.
- a real-socketpair client.

| File | Covers |
|---|---|
| `test_protocol.py` | the peer check: a wrong uid via real SO_PEERCRED is refused and the request stays unread in the socket buffer; root is refused; the user missing; the per-connection lookup. Also the exact op allowlist, field/type/bound validation, `since` (integers only, future refused, ancient clamped), an oversize request, a request at exactly 4096 bytes, a silent client, no raw request in the log, a contained internal error, and the config file (validation, owner, mode, symlink, the example equal to the defaults) |
| `test_ops.py` | `containers` (fields, nothing leaked, missing, inactive/failed/activating never running docker), `logs` (unit and container argv, ANSI, stderr, redaction before the 2000-char cap, refused sources), `sudo-commands` (parsing, the `event` classifier incl. a command that tries to fake one, the `_EXE` matches and forged rows from other executables, grep, unknown users), `localai-activity` (the uid from pwd, a missing user), `timers` (the filter, last_result, bad names, failures), a hung command killed at the timeout (process gone), the 10 s cap, the output cap |
| `test_sudo_io.py` | `sudo-sessions` and `sudo-replay` over gzip, zlib, plain and legacy trees: ANSI-laden output with secrets, ttyin/stdin never opened, timing interleave, redaction before truncation, UTF-8-safe cuts, a decompression bomb, damaged and unfinished streams, path-traversal ids, symlinked session/middle dirs/stream files/user dirs, unknown ids |
| `test_hardening.py` | the reference cases (`redact_cases.json`) and that every rule has one; every rule linear on 26 pathological 64 KiB inputs (uncapped); the 8 KiB line cap dropping a cut word; the partial token at the replay's raw cap; over-long replay lines; damaged metadata (RecursionError, odd shapes, a symlinked log.json, a bad log.json falling back to the legacy log) skipped without failing the list or other replays; the request deadline (one slow command, several commands, a trickling client); a timeout killing the stub's child too; bounded memory on a 200 MiB bomb and `max_length` on every decompress call; a configured user mapping to uid 0 |
| `test_e2e.py` | the real script started like systemd would start it: the connection on stdin, or on fd 3 with `LISTEN_FDS`/`LISTEN_PID`; a writable config refused; not a socket |
