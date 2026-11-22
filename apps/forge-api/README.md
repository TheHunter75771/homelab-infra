# forge-api: the Skill Forge over HTTP for Open WebUI (module F)

Spec: `docs/CONTRACTS.md` §12.25 (row F), with the forge rules of §12.12 and the research-gate
pattern of §12.16. Stdlib Python (3.12+, like the library), `http.server` with one thread per
connection. It imports `forge` and `library` from `apps/skill-library` and goes through the forge's
own API and text checks for everything. It never approves anything by itself: the tick endpoints
only pass on a choice Noah made in an Open WebUI confirmation dialog (module T), and they require
Noah's Open WebUI user id. The forge writes the tick and its audit line (`approve-chat by=<id>` in
the request's `state.md`) under its own lock.

## Run

```
./forge-api --config /etc/forge-api/config.json serve           # the service (binds, or uses systemd's socket)
./forge-api --config config.example.json check-config            # 0 ok, 2 refused (never prints the token)
./forge-api --config ... check-config --require-library          # also 2 if the library does not open
python3 -m unittest discover -s tests -t .                       # from apps/forge-api
```

**Socket activation** (Reviewer Q, Q-F3): when systemd passes a socket (`LISTEN_PID` is this
process and `LISTEN_FDS=1`), `serve` uses fd 3 instead of binding. It is refused (exit 2) unless it
is exactly one listening TCP stream socket bound to the configured `bind` and `port`. The variables
are removed from the environment once read. Without them, the server binds itself (tests, manual
runs).

The launcher finds `forge` and `library` beside it (the installed layout, `/opt/forge-api/`) or in
`../skill-library` (a repo checkout), and works under `python3 -I -B`. `serve` exits 2 if the config
or the token file is refused and 3 if it can't listen. An unmounted library doesn't stop it: every
call answers 503 until the share is back.

## Config

JSON. Unknown keys and duplicate keys are refused.

| Key | Default | Rule |
|---|---|---|
| `library_root` | required | absolute path: the sentinel-checked library mount |
| `bind` | required | one IPv4 or IPv6 literal; never `0.0.0.0`/`::` or multicast. Deploy: the `ai` bridge gateway (see below) |
| `port` | `879` | 1 to 65535. Production uses 879, a privileged port that only systemd's socket unit binds (see Deploying) |
| `allowed_peers` | `["172.20.0.4"]` | 1 to 8 address literals (no CIDR). Loopback, unspecified, multicast and the bind address itself are refused, so the host is never an allowed peer |
| `token_file` | `/etc/forge-api/token` | absolute path |
| `approver_user_id` | required | Noah's Open WebUI user id: 1 to 64 of `A-Z a-z 0-9 . _ @ -` (the forge's `by` rule) |

**Token file:** one token of the b64token alphabet (`A-Z a-z 0-9 . _ ~ + / = -`), at least 32
characters. It is read once at start, through `O_NOFOLLOW`, and refused if it isn't a regular file,
if it's owned by anyone but root or the service user, if it's group- or world-writable, or if it's
world-readable. The intended mode is `0640 root:rgate`. Only a SHA-256 digest is kept in memory.

## Checks on every request (in this order, before any body is read)

1. **Peer, on accept** (Reviewer Q, Q-F1): the TCP peer address (from the socket, never from a
   header) must be in `allowed_peers`. This is checked right after `accept()`, before the
   connection takes one of the service's slots. A refused peer is answered **403**
   `{"error": "forbidden"}` by a separate pool of 16. That pool gives it at most 1 s to send its
   request line, which is read, dropped and never parsed or logged. With that pool full, the
   connection is closed at once. So refused peers can never use up the slots an allowed client
   needs. The handler repeats the check. `X-Forwarded-For`, `X-Real-IP` and `Forwarded` are
   ignored.
2. **Bearer secret:** exactly one `Authorization: Bearer <token>` header. Digests are compared with
   `hmac.compare_digest`, so the comparison takes the same time whatever is sent. Otherwise **401**
   with `WWW-Authenticate: Bearer`.
3. **Route and method:** the fixed patterns below are matched against the raw target; it is never
   percent-decoded and never used as a file path. Query strings are refused (400). An unknown path
   is 404, and a wrong method is 405 with `Allow`.
4. **Body (POST):** `Content-Type: application/json` (`charset=utf-8` optional; else **415**); exactly
   one `Content-Length` and no `Transfer-Encoding` (else **411**); at most **64 KiB** (else **413**,
   unread); strict UTF-8 and strict JSON (no duplicate keys, no `NaN`/`Infinity`, an object at the
   top level; else **400**). A body that doesn't arrive in time gets **408**.
5. **Tick endpoints only:** `user` must equal `approver_user_id` (constant-time). Otherwise **403**,
   and the forge is never called.

No interim `100 Continue` is sent before these checks. Every reply is JSON with
`Cache-Control: no-store`, `X-Content-Type-Options: nosniff` and `Connection: close`. Errors are
`{"error": "<short reason>"}`, with no stack trace. A refusal by the forge's own checks quotes the
forge's reason, cleaned of control characters and clipped to 400 characters.

**Limits:** at most 16 connections at once (more are closed on accept), 10 s per socket read or
write, and 30 s for a whole connection (the socket is then shut down, so a trickling client can't
hold a thread). The forge's lock file is shared with the research gate and Noah's CLI. A busy
forge is retried for about 5 s, then answered **503**.

Writes inside this process are serialised by one lock. Waiting for it is bounded at 5 s
(Reviewer Q, Q-F2): after that the call is answered **503** and the forge is never called, so a
write can't start long after its client gave up. **A write that has started always runs to the
end.** A client cut off by the 30 s connection deadline, or one that disconnects mid-call, may
still find its tick applied. The research tool tells Noah "may have been applied; check
research_status" in that case. The forge's own record (`state.md`, `approve-chat by=<id>`) is
always the truth.

## Endpoints

| Method and path | Body | Result |
|---|---|---|
| `POST /requests` | `{"title", "why", "queries": [{"query", "why"}]}` | **201** `{"rid", "proposed", "status"}`. `forge.new` + `propose_searches`, every search unticked |
| `GET /requests` | | `{"requests": [{"rid", "where", "status", "title", "searches": {"approved", "listed"}, "sources": {...}, "ingested"}], "total", "unreadable"}`, newest first, at most 200 |
| `GET /requests/{rid}` | | `{"rid", "where", "status", "title", "searches": [{"n", "query", "why", "approved"}], "sources": [{"n", "url", "domain", "title", "approved"}], "ingested": [{"n", "url", "path"}], "pending_searches", "pending_sources", "malformed_lines"}` |
| `POST /requests/{rid}/tick-searches` | `{"queries": [...], "user"}` | `{"rid", "results": [{"query", "result", "reason"?}], "counts": {"ticked", "already", "not-found"}}` |
| `POST /requests/{rid}/tick-sources` | `{"urls": [...], "user"}` | the same, with `"url"` (canonical form) |
| `GET /items/{rid}/{n}` | | `{"rid", "n", "url", "path", "kind": "text", "text", "truncated", "bytes"}`; for a PDF, `"kind": "pdf"` with an empty `text` and a note (no extraction yet) |

`where` is `35-forge` or `95-shelf`. A shelved request can be read, but ticking it gets 409. `n` is
1-based and follows the order of `ingested:` in `state.md`.

**Input rules (§12.25 caps, plus the forge's own checks):**
- `title`: at most 120 characters; the forge's plain-text rule (`check_ascii`: letters, digits,
  space and `. , : ; ? ' " / + = % @ - ( )`, and no `why:`).
- `why`: at most 2000 characters; the forge's justification rule (`check_why`). It is written to
  `request.md` with a provenance line saying it came from chat, in the assistant's words, not
  Noah's.
- `queries`: 1 to **10**. Each `query` is at most **200** characters and passes `check_query`. Each
  query's `why` is at most 300 characters and passes `check_why`.
- Tick `queries`: 1 to 10, each at most 200 characters, passing `check_query`. Tick `urls`: 1 to
  **30**, each at most 2048 characters, passing `canonical_url`.
- Duplicates are dropped, keeping the first. One bad item refuses the whole call (400) before
  anything is written. Unknown fields are refused, and nothing has an "approved" flag.

**Tick results** come from the forge (`Forge.tick_searches/tick_sources(rid, items, by=<approver>)`)
through a strict adapter (`forge_api/ticks.py`). Every item asked for must be answered exactly once
with `ticked`, `already` or `not-found`; anything else is a 500 that claims nothing. `reason`
explains a `not-found` and never quotes the file. A forge refusal (wrong state, a file edited
mid-call) is 409. An installed forge without the tick methods gets 501.

**Items** are found only through the forge's own record: the n-th `ingested:` entry in the request's
`state.md` (written by `record_ingest`). The record must be `<canonical url> -> 10-sources/<topic>/<name>`.
The item's header (`<item>.md`) must carry `source:` equal to that URL and `forge:` equal to that
request; otherwise the item is refused with 409 and not read. Every path goes through
`Library.abs_path`, which confines it to the root and refuses `..` and any symlinked component, and
the file is opened with `O_NOFOLLOW` as a regular file. At most 256 KiB is read. CR/LF and Unicode
line separators become `\n`, and control (Cc except `\n` and `\t`), format (bidi, zero-width, BOM),
surrogate, private-use and unassigned characters are removed. The result is clipped to **20 KB**
(UTF-8, at a character boundary), and `truncated` says whether anything was cut. The text is
untrusted web content; module T frames it as data.

## Logging

Logs go to stderr (the journal). Each request gets one line: peer, method (only a known HTTP method
word, else `-`), route template (plus `rid=` when it's a valid request id), status and duration.
The log never contains a header, a body, a raw path or query, a client-sent user id, or
`http.server`'s request line. A 5xx's detail and traceback go to the log, never to the client.
`serve` installs a formatter that replaces the token anywhere in a formatted record, tracebacks
included, as a second line of defence.

## Deploying (notes for H8, `roles/forge_api/`)

**Layout** (like research_gate): code at `/opt/forge-api/`, holding `forge-api`, `forge_api/` and a
copy of `apps/skill-library/{forge,library}`, root-owned and read-only. Config at
`/etc/forge-api/config.json` (`0640 root:rgate`). The token is at `/etc/forge-api/token`
(`0640 root:rgate`), root-generated, e.g. `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`
(43 characters). Use `no_log: true` on every task that touches it, and never rotate it on converge.
Noah pastes it into module T's Valve.

**Address and port (Lead decision, Reviewer S):** the `ai` bridge's gateway, **`172.20.0.1`**, on
privileged port **879**. **Production always uses the socket unit below:** systemd (root) binds
879, and the service, running as `rgate` with no capabilities, only inherits it. Without the
socket, a plain bind on a port below 1024 fails for a non-root user (`serve` exits 3, "cannot
listen"). That is expected; never give the service `CAP_NET_BIND_SERVICE` to work around it. A
privileged port also means no unprivileged local user can ever bind it, even if the socket unit
is down.
- **How open-webui reaches it:** open-webui (172.20.0.4) reaches the host through that address. The
  packet goes container → bridge → the host's INPUT chain. It isn't forwarded, so `DOCKER-USER`
  (a FORWARD chain) never sees it, and no NAT happens: the peer the service sees is 172.20.0.4.
- **FreeBind:** the socket unit's `FreeBind=yes` lets systemd bind 172.20.0.1 before Docker has
  created the bridge at boot. The server sets `IP_FREEBIND` itself only on the plain-bind
  fallback.
- **`host.docker.internal`:** don't use it. With `host-gateway` it resolves to `docker0`
  (172.17.0.1), not the `ai` bridge.
- **Firewall (UFW):** allow only `in on <ai bridge interface> proto tcp from 172.20.0.4 to
  172.20.0.1 port 879`. Keep the default deny for everything else, so a LAN or tailnet host can't
  reach the port even under the weak-host model. The interface is `br-<network id>` unless the
  compose network pins `com.docker.network.bridge.name` (see open questions).
- **systemd IP filter:** `IPAddressDeny=any` plus `IPAddressAllow=172.20.0.4/32` on the unit. It
  filters the unit's sockets in both directions, so the service can't open any outbound
  connection, and the host itself (172.20.0.1, loopback) is dropped before the app sees it. Note:
  this also makes H8's "from the host gets 403" check impossible as written. With the filter, a
  host call times out at the socket instead of getting 403. Either test the 403 with the IP filter
  relaxed in a `systemd-run` copy, or accept a drop as the expected result (see open questions).
- **Never published:** the port is never published on the tailnet or the LAN, and `tailscale
  serve` is never used for it.

**Socket unit (Reviewer Q):** deploy `forge-api.socket` with `ListenStream=172.20.0.1:879`,
`FreeBind=yes`, `Accept=no` and `Service=forge-api.service`.
Enable the socket rather than the service. systemd then holds 172.20.0.1:879 for as long as the
socket unit is up, including while the service is stopped or restarting. No local user can bind
it and pose as the service to open-webui, which would collect the token from the tool's requests.
The service refuses any other inherited socket: one listening TCP socket on exactly the configured
`bind` and `port` (172.20.0.1, 879), or it exits 2. Put the IP filter
(`IPAddressDeny=any`, `IPAddressAllow=172.20.0.4/32`) on the **socket** unit as well as the
service. Per systemd.resource-control(5), a service's own IP access list does **not** apply to
sockets passed in by activation; the socket unit's list applies to its socket. The Integrator
proves it holds: a host and a LAN connection to 879 must get no answer. With socket activation,
the `SocketBind*` and `IP_FREEBIND` notes for the service no longer matter, because the socket
unit binds.

**Unit hardening** (copy the research-gate unit; the differences are marked):
- **Identity and start:** `User=rgate`, `Group=rgate`, `Type=exec`. `ExecStart=/usr/bin/python3 -I -B
  /opt/forge-api/forge-api --config /etc/forge-api/config.json serve`. `Restart=on-failure`,
  `UMask=0027`.
- **Library mount:** `RequiresMountsFor=<library_root>`, plus `AssertPathExists=<sentinel>` and
  `AssertPathIsDirectory=<library>/35-forge`. Install the unit always; enable it only with a
  `forge_api_enabled` switch, as for the gate.
- **Filesystem:** `ProtectSystem=strict` and `ReadWritePaths=<library>/35-forge` **only** (the
  difference from the gate). The service writes nothing else: new requests, proposals, ticks and
  the forge lock all live in `35-forge/`. Also `ReadOnlyPaths=/run`, `ProtectHome=yes` and
  `PrivateTmp=yes` (nothing uses it).
- **Network:** `RestrictAddressFamilies=AF_INET AF_UNIX` (add `AF_INET6` only for an IPv6 bind),
  and `SocketBindDeny=any`: the service never binds; it only uses the socket systemd passes. Add
  the IP filter above too.
- **Privileges:** `NoNewPrivileges=yes`, `CapabilityBoundingSet=` and `AmbientCapabilities=`.
  The service needs no capability, because the socket unit binds port 879.
- **Ordering:** `Requires=forge-api.socket` and `After=forge-api.socket docker.service`.
- **Sandboxing:** `PrivateDevices`, `PrivateIPC`, the `ProtectKernel*` set, `ProtectControlGroups`,
  `ProtectClock`, `ProtectHostname`, `ProtectProc=invisible`, `ProcSubset=pid` (unlike the gate,
  this service reads nothing in `/proc/net`), `RestrictNamespaces`, `RestrictRealtime`,
  `RestrictSUIDSGID`, `LockPersonality`, `MemoryDenyWriteExecute`, `RemoveIPC`, and
  `SystemCallFilter=@system-service ~@privileged` with `SystemCallArchitectures=native`.
- **Resources:** `MemoryMax=256M`. `TasksMax=64` covers 16 handler threads, 16 deadline timers and
  the main thread. `LimitNOFILE=256`.

**Verify step (H8):**
1. `forge-api check-config --require-library` as rgate → 0.
2. `ss -ltnp` shows `172.20.0.1:879` only, held by systemd (and the service once started).
3. From the host, `curl -m 5 http://172.20.0.1:879/requests` gets no answer (IP filter) or 403
   (filter relaxed); never 200 or 401.
4. From a source of 172.20.0.4 without the token: 401 (needs a client in open-webui's network
   namespace; see open questions).
5. `journalctl -u forge-api | grep -cF "$token"` is 0, and so is the same grep over the Ansible log.
6. LAN and tailnet → 879 are refused.

## Trust boundary: Open WebUI (Reviewer Q, Q-1)

The service trusts exactly what open-webui (172.20.0.4) sends with the token. So **Open WebUI is
the trust boundary**: anything that can run code or make requests as Open WebUI, with the Valve
holding the token, can tick searches and sources **without Noah's dialog**. That includes:

- another Open WebUI **admin** (admins can read and edit every Tool, its Valves, and Functions);
- any user given **workspace Tools or Functions permission** (they can install or edit code that
  runs inside Open WebUI's process);
- any **installed Function** (Filter, Pipe, Action) or Tool: they run in the same process and can
  read the token Valve or call this API directly;
- a chat **embed or artifact** rendered with **`iframeSandboxAllowSameOrigin`** on: same-origin
  script can act as the logged-in admin and call the tool.

The token is stored in Open WebUI's database, so **open-webui volume backups hold the token** and
must be protected like it. The Integrator asserts, and keeps asserting:

1. Noah is the only admin.
2. No user has workspace Tools/Functions permission.
3. `iframeSandboxAllowSameOrigin` is off, both as the admin default and in Noah's own settings.
4. Backups of the open-webui volume are treated as holding the token.

What this service guarantees regardless: only open-webui's address, only with the token, ticks
only by the configured approver id, only items the forge already lists, and every tick recorded by
the forge with `by=`.

### Noah copies the token into the Valve himself (Q-F4)

The token is never shown through the `claude` operator account, so it never appears in its
session logs or sudo I/O logs. On ai-box, logged in as **his own admin user** (not `claude`), Noah
runs:

```
sudo cat /etc/forge-api/token
```

He copies the one line into Open WebUI → Workspace → Tools → "Web research (approval required)" →
Valves → the token field, and saves. Then he clears the terminal (`clear`; with tmux,
`tmux clear-history`). To rotate it: `sudo` regenerate the file (H8's documented command),
`sudo systemctl restart forge-api.service`, then paste again.

## Limits and residuals

- **Same user as the gate:** the service runs as `rgate`, like the research gate (the contract's
  choice), so the gate's user can also read the token. They share one trust zone; a separate user
  would need the library group (see open questions).
- **Defence in depth that tests can't separate:** reading an item checks the path three ways: the
  record pattern, `abs_path` (no `..`, no symlinked component), and `O_NOFOLLOW` plus a regular-file
  check. A mutant that drops just one of the last two survives, because the others still refuse.
  Only a symlink swapped in between the header check and the open (a hostile local writer, already
  out of scope in §12.12) would need all three.
- **PDF items:** no text extraction yet (the gate stores the original).
- **Over-long lines:** a request line over 8 KiB gets 414. Header limits are `http.server`'s (100
  headers, 64 KiB per line) inside the 30 s connection deadline.
