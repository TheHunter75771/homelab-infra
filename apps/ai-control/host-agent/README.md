# ai-host-agent

Runs on the **gaming PC**. It answers ai-box's AI Control server (module S) with read-only status:
the `ollama` and `gpu-gate` services, their logs, Ollama's models, sudo use by the AI operator
accounts, and disk space. The spec is `docs/CONTRACTS.md` §12.15.3, plus the Lead's rulings of
2026-10-26 (all times are epoch seconds; the sudo `event` field; the `sudo.ws` match). It uses the
Python 3 standard library only, so any Python 3.11 or newer works.

**Nothing changes anything.** It has no POST, no exec of client text, no shell, and no paths
from the client.

## API

Every path needs `Authorization: Bearer <token>`. Without it, the agent answers `401` with
`WWW-Authenticate: Bearer`, the same for every path, so an unauthenticated client learns
nothing. That includes `/healthz` and unknown paths, which return `404` only after auth.
Responses are JSON with `Cache-Control: no-store`. Each response is one request, then close.
Every timestamp is **integer Unix seconds** (or `null`).

| Request | 200 body |
|---|---|
| `GET /healthz` | `{"ok": true}` |
| `GET /v1/services` | `{"services": [Service, Service]}`: `ollama` first, then `gpu-gate` |
| `GET /v1/logs?unit=ollama\|gpu-gate&lines=1..1000` (default 200) | `{"unit", "lines": [str], "truncated": bool}` |
| `GET /v1/ollama` | `{"models": [Model] \| {"error"}, "loaded": [Loaded] \| {"error"}}` |
| `GET /v1/sudo?user=claude\|localai&since=<epoch s>&limit=1..500` (default 100) | `{"user", "entries": [Sudo], "truncated": bool}` |
| `GET /v1/disk` | `{"disk": [{"mount", "total", "avail"} \| {"mount", "error"}]}` |

**`/v1/services`.** Each entry is either `Service` or, if `systemctl` failed for that unit,
`{"id", "unit", "error"}`. The other unit is still reported.

`Service` has these fields:
- `id`: `"ollama"` or `"gpu-gate"`.
- `unit`: `"ollama.service"` or `"gpu-gate.service"`.
- `load`, `active`, `sub`: strings or `null`.
- `since`: epoch seconds of `ActiveEnterTimestamp`, or `null`.
- `restarts`: an int or `null`.
- `main_pid`: an int, or `null` when it's 0.
- `memory_bytes`: an int or `null`. It is read from the unit cgroup's `memory.current`, so it
  covers every process of the unit (Ollama's `llama-server` workers too). systemd's
  `MemoryCurrent` is the fallback.

**`/v1/logs`.**
- **Command:** `journalctl -u <unit> -n N -o short-iso --no-pager`, oldest first. The lines are
  journal text, so their timestamps are the text's own `short-iso`.
- **Lines:** each line has control and ANSI sequences stripped, is capped at 8 KiB, **redacted,
  then** cut to 2000 characters. The tail is redacted as one stream, so a private key, vault
  body or YAML secret block spread over several journal lines is masked whole.
- **`truncated`:** true when the output passed `exec_max_bytes` and was cut. The cut-off partial
  line is dropped, never returned.

**`/v1/ollama`.** It calls `<ollama_url>/api/tags` and `/api/ps`. On the gaming PC,
`ollama_url` is the tailnet IP, because Ollama listens only there (§12.10). Each part fails on its
own, as `{"error": "unreachable"|"timeout"|"too large"|"bad response"|"http <n>"}`.
- `Model`: `{"name", "size": int|null, "quant": str|null, "modified": epoch|null}`.
  `quant` is Ollama's `details.quantization_level`.
- `Loaded`: `{"name", "size_vram": int|null, "expires_at": epoch|null}`.

Every other Ollama field (digest, family, ...) is dropped. Names have controls stripped and are
capped at 256 characters.

**`/v1/sudo`.** It reads `journalctl _EXE=/usr/bin/sudo [_EXE=… per sudo_exes] _UID=<uid of user> -o json --output-fields=MESSAGE,_EXE,_UID -r -n 5000 --no-pager [--since @<s>]`.
- **Matching (Lead, after Reviewer A round 2):** journalctl ORs the `_EXE` values and ANDs
  `_UID`.
  - `_EXE` alone is not enough: it can be faked through an unprivileged user namespace (a
    bind-mounted "sudo"; Fedora allows those for Steam and Flatpak).
  - `_UID` is journald's own record of the sender, so another user's forged rows, or a flood
    meant to push real rows out of the 5000-entry window, never match.
  - The uid comes from `getpwnam(user)`. A user unknown on this host returns `502`
    `{"error": "user not found on this host"}`.
- **Checked again in Python:** each row's `_EXE` must be in `sudo_exes`, its `_UID` must be the
  user's, and the line must start with the audited user (`user : …`).
  - There is no `_COMM` match, because a process sets its own comm.
  - There is no `_UID=0` match, because real sudo lines carry the caller's uid.
  - **Accepted residual risk:** a user forging rows about itself. The sudo I/O session list
    stays the authoritative record.
- **`since`:** integer epoch seconds, at most one day ahead of now (later is `400`).
- **Which entries:** the user's own sudo lines (`user : [note ; ]TTY=… ; PWD=… ; USER=… ;
  COMMAND=…`), newest first, up to `limit`.
- **`Sudo` fields:** `{"ts": epoch|null, "user", "runas": str|null, "cwd": str|null, "command",
  "event", "ambiguous": bool}`. `cwd` and `command` are redacted.
- **`ambiguous`:** true when the line can't be split reliably, so the UI should say "check the
  session recording". That is:
  - the cwd or the command contains a `; KEY=` sudo-field pattern (`TTY`, `PWD`, `USER`,
    `GROUP`, `ENV`, `COMMAND`, `TSID`);
  - a field name appears twice;
  - the row is a `(command continued)` piece. Such a piece is kept as its own row, with
    `runas`/`cwd` `null`, the piece as `command` and `event` `command`;
  - the row is the start of a command that sudo split into continued pieces.
- **`event`:** decided from sudo's free-text notes only, never from a field value or the
  command:
  - `auth_fail` for "incorrect password attempt(s)" or "authentication failure". This wins
    when a line also names a refusal, as in X;
  - otherwise `refused` for "command not allowed", "a password is required" or "user NOT in
    sudoers";
  - otherwise `command`.
- **`truncated`:** true when fewer than `limit` were found **and** the 5000-entry scan (or the
  byte cap) was exhausted, so older matches may exist.
- **Not rows:** pam_unix lines. sudo logs its own line for the same event.

**`/v1/disk`.** `/` is always reported. Every other `disk_paths` entry (default `/mnt/share`)
is reported only while it is a mount point in `/proc/self/mountinfo`. Reading that file never
touches the filesystem, so a dead network mount can't hang the check. `statvfs` runs in a
worker thread with `disk_timeout`. A hung path reports `"statfs timed out"` and is not retried
until the stuck call returns, so it costs at most one thread per path.

**Errors:**

| Status | When |
|---|---|
| `400` | A bad request line or header, or a target that isn't origin-form. Also: a parameter that is unknown, repeated, outside its enum or bound, or not decimal. For `since`, only integer epoch seconds are accepted. The error message never echoes the input |
| `401` | No token, a wrong token, another scheme, or more than one `Authorization` header |
| `404` | An unknown path (after auth) |
| `405` | Any method but GET (with `Allow: GET`). This is checked before auth, and nothing runs |
| `408` | The request line and headers didn't arrive within `head_timeout` (2 s) overall, even when a client trickles them |
| `414` / `431` | The request line, or the line plus headers, exceeds **8 KiB** (8192 bytes including CRLFs). 8192 bytes is served; 8193 is refused |
| `502` | `journalctl` failed or timed out (`{"error": "journalctl timed out"}`), or the sudo user doesn't exist on this host |
| `503` | More than `max_concurrent` (16) requests are in flight: `{"error": "busy"}`, `Retry-After: 1` |

## Limits

- **Concurrency:** at most 16 requests are served at once, counted across all binds. The 17th
  and later get `503` from a reject pool of the same size, which reads at most 8 KiB for up to
  1 s, answers and closes. With that pool full too, a connection is closed on accept. That
  bounds the agent's threads at about 2 × 16 handlers + 16 deadline timers + the statfs workers.
- **Timeouts:**
  - 5 s per socket operation (`conn_timeout`);
  - `head_timeout` 2 s for the request head;
  - `request_deadline` 30 s from accept to the last response byte. The socket is then shut down,
    so a slow reader can't hold a slot.
- **Subprocesses:**
  - They run with fixed argv lists and a timeout of `exec_timeout` (≤ 10 s, enforced by the
    config). They never use a shell.
  - They get `stdin` from `/dev/null`, `stderr` dropped, and their own session, so a timeout
    kills the whole group.
  - Their environment is **fixed**: `PATH=<exec_path>`, `LC_ALL=C.UTF-8` and systemd's
    no-colour/no-pager variables. Nothing is inherited from the agent's environment.
  - The program is one of `systemctl` and `journalctl`, found on `exec_path` (default
    `/usr/bin:/bin`).
  - stdout is capped at `exec_max_bytes` (8 MiB).
- **Ollama:** `http.client` to the `ollama_url` host only. The config accepts `http://` with
  `127.0.0.1` or exactly one of the agent's own `binds` addresses, as an IP literal. It refuses
  hostnames (so DNS can't redirect it), https, userinfo, paths and queries. The client ignores
  proxy variables and doesn't follow redirects. Each call has a `ollama_timeout` deadline and
  an `ollama_max_bytes` (1 MiB) cap.

## Secrets

- **Token file:** read once at start. The agent **refuses to start** (exit 2) if the file is:
  - missing, empty, a symlink or not a regular file;
  - owned by anyone but root (or the agent's own user);
  - **group- or world-writable**, or **world-readable**;
  - not one token68 string of at least 32 characters.

  The intended mode is `root:aictl-agent 0640`. The error message names the file and the reason,
  never the content.
- **Comparison:** the agent keeps only the token's SHA-256. It compares
  `hmac.compare_digest(sha256(presented), sha256(token))` on every attempt, whatever the scheme.
  Both sides are 32 bytes, so neither the content nor the length of a guess changes the timing.
- **Redaction: one rule set with helper X** (Lead, 2026-10-26). `ai_host_agent/redact.py` is
  a port of X's rules in `apps/ai-control/helper/aictl_helper.py`, masking with `[REDACTED]`.
  Change X first, then port. `tests/test_redact.py` keeps the two equal:
  - it loads X's `tests/redact_cases.json` at test time and checks every case through every
    path the agent uses;
  - it imports X's module read-only and asserts every pattern is identical, in the same order.

  The order per line is fixed:
  1. escapes and controls are stripped;
  2. the agent's own token is replaced;
  3. the line is capped at 8 KiB, and a cut drops the partial word it went through;
  4. the line is redacted;
  5. it is cut to the response cap.

  Every rule is linear-time. The tests run each rule on 64 KiB pathological lines and on
  512 KiB dense-keyword lines, uncapped, under 1 s each.

  Y's extras on top of X's rules: the token is replaced wherever it appears. As a last pass,
  every string of every 200 body has the token value replaced. Short fields (state names,
  model names) replace it before their cap.
- **Logs** go to stderr, one line per request: `GET /v1/logs 200`. Only the method, a **known**
  route (an unknown path logs as `-`, since a client could put the token in it) and the status
  are logged. The query string and headers never are. http.server's own request logging is off.

## Run

```sh
./ai-host-agent --config config.example.json            # launcher; works under python3 -I
./ai-host-agent --config /etc/ai-host-agent/config.json --check   # validate config + token, exit
curl -sH "Authorization: Bearer $(sudo cat /etc/ai-host-agent/token)" http://127.0.0.1:11510/v1/services | jq .
```

**Config:** `config.example.json` lists every key with its default. A missing key takes its
default, and unknown keys are rejected.
- `ollama_url`: `http://<127.0.0.1 or a binds address>:<port>`. The role sets it to the
  tailnet IP, where the PC's Ollama listens.
- `binds` must be IP literals. The role sets `["127.0.0.1", "{{ gaming_pc_tailnet_ip }}"]`.
  Binds use `IP_FREEBIND`, so the tailnet address can be bound before tailscaled has it at boot.
- **Bounds:**
  - `max_concurrent` 1–16;
  - `exec_timeout` and `ollama_timeout` 0.5–10 s;
  - `request_deadline` 1–300 s;
  - `exec_max_bytes` 64 KiB–64 MiB;
  - `ollama_max_bytes` 1 KiB–16 MiB.
- `sudo_exes`: 1–8 distinct absolute paths of the sudo binaries, default `["/usr/bin/sudo"]`
  (the Fedora gaming PC). H3 sets it per host.
- `cgroup_root`, `mountinfo` and `exec_path` exist for tests.

## Test

```sh
python3 -m unittest -v        # from apps/ai-control/host-agent
```

The tests run a real server on `127.0.0.1:0`:
- **Stubs:** stub `systemctl` and `journalctl` on a fake `PATH`. They record their argv and
  environment, and play fixtures with exit codes and delays.
- **Fake Ollama:** an HTTP server that can be slow, oversized or broken.
- **Fake system files:** a fake cgroup tree and mountinfo.
- **Entrypoint:** `test_main.py` runs the real entrypoint as a child process, covering token
  refusals, serving, stderr logging and SIGTERM.

All fixtures are synthetic: model names, the token, users and timestamps.

## systemd unit (for role H3 `ai_host_agent`; not installed by this app)

The role writes this to `/etc/systemd/system/ai-host-agent.service`. It installs the code at
`/opt/ai-host-agent/` and the config at `/etc/ai-host-agent/config.json` (root 0644). It writes
the token to `/etc/ai-host-agent/token` (`root:aictl-agent 0640`, dir `root:aictl-agent 0750`).
It creates the user `aictl-agent` (system, nologin, no home), whose only supplementary group is
`systemd-journal`. The firewalld policy opens 11510 to ai-box only.

```ini
[Unit]
Description=ai-host-agent: read-only AI status for ai-box (CONTRACTS.md 12.15.3)
After=network-online.target tailscaled.service ollama.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -I /opt/ai-host-agent/ai-host-agent --config /etc/ai-host-agent/config.json
Restart=on-failure
RestartSec=5

# --- Identity: no privileges --------------------------------------------------------------------
User=aictl-agent
Group=aictl-agent
# journalctl reads the system journal through this group. Not PrivateUsers=: that would
# unmap the group and blind journalctl.
SupplementaryGroups=systemd-journal
CapabilityBoundingSet=
AmbientCapabilities=
NoNewPrivileges=yes
SystemCallFilter=@system-service
# Not ~@resources: journalctl raises its own RLIMIT_NOFILE to open every journal file.
SystemCallFilter=~@privileged
SystemCallArchitectures=native
SystemCallErrorNumber=EPERM

# --- Sandbox -----------------------------------------------------------------------------------
# Everything is read-only; the agent writes nothing (logs go to journald via stderr).
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
# It needs only /proc/self (mountinfo); other users' processes stay hidden.
ProtectProc=invisible
ProcSubset=pid
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
# cgroupfs read-only: memory.current is only read.
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
# AF_UNIX for systemctl's D-Bus call and journald's stdout stream
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
UMask=0077
# Bounded by design (16 handlers + 16 rejects + timers + children); this is the backstop.
TasksMax=128
MemoryMax=512M
# Network exposure is firewalld's job (policy: 11510 from ai-box only). Optional defence in
# depth, templated by the role (identifiers never go in the repo):
# IPAddressDeny=any
# IPAddressAllow=localhost <ai-box tailnet IP>

[Install]
WantedBy=multi-user.target
```

The role must verify, after deploy, as the role's own checks:
- `systemd-analyze verify` on the unit;
- `sudo -u aictl-agent journalctl -u ollama -n 1` returns a line, which proves the group works;
- `systemctl show -p ActiveState ollama.service` works as `aictl-agent`, since an unprivileged
  `show` goes over D-Bus with no polkit;
- `GET /v1/services` and `GET /v1/logs?unit=ollama` work **under the unit**. The syscall filter
  and `ProtectProc=invisible` can only be proven on the host. If the second one breaks
  `systemctl`, relax it to `ProtectProc=default` (it is defence in depth, not a contract item).
