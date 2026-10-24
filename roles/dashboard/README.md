# Role: dashboard

Dashboard v2 as a hardened host service, outside Docker, plus its privileged Docker helper.
Module H, docs/CONTRACTS.md §11.1/§11.2, ADR 0013 (host service) and ADR 0014 (tailnet-only).
The dashboard absorbs push-core: same port (127.0.0.1:8085, so `tailscale_serve` is unchanged),
same push store, same push API.

Nothing in this role touches Docker. The dashboard deploys and runs while the boot gate holds
Docker, which is the "bad night" it exists for.

Run order (site.yml): after `users`, `bootgate` (groups `media`, `bootgate`) and `stacks` (which
retires push-core, freeing port 8085 and every process running as uid 10101), and before
`tailscale_serve`.

**Controller requirements:** Node.js 22.x and npm. The server's dependencies are built on the
controller (no npm on the host, Lead decision 2026-10-24).

## Installed
| Path | What |
|---|---|
| `nodejs` | Ubuntu archive (Node 22; security updates via unattended-upgrades). No `npm` on the host; one left by an earlier run is not removed automatically |
| user/group `dashboard` 10101 | system account, no login; supplementary groups `media`, `bootgate`, never `docker` |
| `/opt/homelab-dashboard/server/` | `apps/dashboard/server/` (root-owned, 0644/0755), plus `node_modules` built on the controller |
| `/opt/homelab-dashboard/web/` | `apps/dashboard/web/public/` (the PWA's static files) |
| controller: `.build/dashboard-server/` | the dependency build (package.json, package-lock.json, node_modules, `.deps-stamp`); git-ignored |
| `/etc/homelab-dashboard/config.json` | module D's settings (root:dashboard 0640), passed as `DASHBOARD_CONFIG` |
| `/etc/homelab-dashboard/dockerctl.json` | the helper's settings (root 0644) |
| `/var/lib/homelab-dashboard/` | dashboard:dashboard 0750 (settings.json, samples.jsonl, uptime/) |
| `{{ homelab_push_dir }}` | dashboard:dashboard 0750; `vapid.json` root:dashboard 0640 from the vault; `subscriptions.json` seeded `[]` once |
| `homelab-dashboard.service` | `node /opt/homelab-dashboard/server/<package.json "main">` |
| `/usr/local/sbin/homelab-dockerctl` | the helper (`files/homelab-dockerctl/homelab_dockerctl.py`, Python 3 stdlib) |
| `homelab-dockerctl.socket` + `homelab-dockerctl@.service` | `/run/homelab-dockerctl.sock` 0660 root:dashboard, `Accept=yes` |
| `/var/cache/homelab-dockerctl/` | helper cache (disk-split, 24 h) and per-container action locks (root 0700) |

## The account: pushcore becomes dashboard
The id is fixed at 10101 because the existing push store belongs to it. The role reads the
account databases and refuses to continue (it never changes an id) unless:
- `dashboard` and `pushcore` are each absent or exactly uid/gid 10101;
- at most one of the two names exists; and
- no other account uses 10101.

When `pushcore` exists, it checks that nothing runs as 10101 (`pgrep`; push-core must be gone),
then runs `usermod --login dashboard pushcore` and `groupmod --new-name dashboard pushcore`. Each
step runs only while the old name still exists, so a re-run, or a run after a half-done rename,
is safe. On a fresh host the account is created with 10101. The supplementary groups are set
exactly (`append: false`), so a stray `docker` membership is removed.

## Push store (contract 4)
Ownership moved here from role `stacks` (§11.5). The preflight checks the VAPID public key (87
base64url characters), the private key (43) and the subject (an https URL), with `no_log`, so
no value can reach the output. `vapid.json` is rendered exactly as `stacks` rendered it, so the
existing file stays byte-for-byte unchanged. `subscriptions.json` is seeded only if absent; the
dashboard is its single writer (§9c), and the role never touches its owner or mode afterwards.

## Deploy and restarts
- Code: two `synchronize` runs (`--checksum`, `--delete`, `--chmod=D0755,F0644`). `node_modules/`
  and `.git/` are excluded everywhere, and `/test/`, `/tests/` and `/coverage/` at the top of each
  tree. Excluded paths are also safe from `--delete`, so the host's `node_modules` stays.
- Dependencies (`tasks/build.yml`, on the controller, `run_once`):
  - It asserts that the controller runs Node.js 22.x and copies package.json and
    package-lock.json into `.build/dashboard-server/` (in the repo, git-ignored).
  - `npm ci --omit=dev --ignore-scripts --no-bin-links` runs there only when the sha256 of the
    two manifests differs from `.deps-stamp`, or when `node_modules` is missing.
  - It then refuses native modules (any `*.node` or `binding.gyp`), because a controller build
    is only portable as plain JavaScript.
  - The stamp is dropped before a rebuild and written only after the check passes, so a failed
    or refused build is redone on the next run.
  - `node_modules` is then synced as its own tree to the host (`--checksum --delete
    --chmod=D0755,F0644`), root-owned and read-only.
- Restarts happen through handlers only, in this order: `Dashboard daemon-reload`,
  `Restart homelab-dockerctl socket`, `Restart homelab-dashboard`. The dashboard restarts on code,
  dependency, config, unit, VAPID or account changes.
- The helper never needs a restart: each connection starts the current script with the current
  config. Only a change to the socket unit restarts the socket, and that doesn't stop requests
  already in flight.
- The run ends by checking `GET http://127.0.0.1:8085/healthz` (200).

## homelab-dashboard.service hardening
Per §11.1:
- `User=dashboard`, `SupplementaryGroups=media bootgate`, `After=network-online.target`;
- no ordering on docker.service or bootgate.service;
- `Restart=always`, `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`;
- `ReadWritePaths=/var/lib/homelab-dashboard {{ homelab_push_dir }}`;
- `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, an empty `CapabilityBoundingSet`, `SystemCallFilter=@system-service`.

It adds:
- `ReadOnlyPaths=/run` and `-/run/bootgate` in `ReadWritePaths` (see below);
- `PrivateDevices`, `PrivateIPC`, `ProtectKernel{Tunables,Modules,Logs}`, `ProtectControlGroups`,
  `ProtectClock`, `ProtectHostname`, `ProtectProc=invisible`;
- `RestrictNamespaces`, `RestrictRealtime`, `RestrictSUIDSGID`, `LockPersonality`, `RemoveIPC`;
- `SystemCallFilter=~@privileged` (EPERM), `SystemCallArchitectures=native`;
- `UMask=0077`, `MemoryMax=512M`, `TasksMax=128`.

`systemd-analyze security` (systemd 259, offline, on the rendered unit): **1.4 OK**. What remains, and why:
- **IP sockets**: it listens on loopback and sends Web Push to the internet (no IP allow list is possible).
- **W+X memory**: V8's JIT needs it (no `MemoryDenyWriteExecute`).
- **Supplementary groups**: the contract (media, bootgate).
- **No `ProcSubset=pid`**: the host tile reads `/proc/loadavg`, `/proc/meminfo` and `/proc/uptime`.
- **No `PrivateUsers=`**: it would turn the `media`/`bootgate` memberships into unmapped groups.
  The kernel should still honor them, but that is unproven on the host, so it stays out.

`/run` is made read-only explicitly with `ReadOnlyPaths=/run`. On the box it was writable in the
unit's namespace despite `ProtectSystem=strict`, which left world-writable directories such as
`/run/lock` reachable. `/run/bootgate` stays writable, because a `ReadWritePaths=` entry nested
inside a `ReadOnlyPaths=` one wins (systemd.exec); the dashboard must create `/run/bootgate/rerun`
there (contract 2). The `-` means a missing directory never blocks the start. Connecting to Unix
sockets under `/run` (the helper, D-Bus, journald) is not a write, so it keeps working. The helper
unit gets `ReadOnlyPaths=/run` as well.

## homelab-dockerctl (§11.2)
**Activation:** `Accept=yes`. systemd accepts each connection and starts one short-lived
`homelab-dockerctl@.service` instance, with the connection on stdin. This was chosen over
`Accept=no` with a loop because:
- there's no long-running root daemon;
- each request gets its own process, so a crash or hang affects one request only;
- systemd enforces concurrency (`MaxConnections=32`: up to 8 log streams, one update and the 2–3 calls of a refresh, with headroom) and a hard lifetime (`RuntimeMaxSec=40min`)
  per request;
- the code has no threads or accept loop to audit.

The cost, about one Python start per request, is irrelevant at the dashboard's polling rate.
Shared state (the disk-split cache, the action locks) lives in `CacheDirectory=` under `flock`.

**Rules:**
- **Peer check:** the peer's `SO_PEERCRED` uid must be 10101; everyone else, root included, gets
  `forbidden` before the request is even read.
- **Requests:** one JSON line of at most 4 KiB. Unknown fields, wrong types or a bad name are
  `bad-request`; an unknown op is `unknown-op`. Names must match `^[a-z0-9][a-z0-9_.-]{0,62}\Z`.
- **Containers:** names resolve to full container IDs among containers labelled
  `homelab.watch=true` only, so a name can never match an ID prefix or an unlabelled container.
- **The Docker guard:** `systemctl is-active docker.service` must print `active` before **every**
  docker command (one is never reused for the next). Otherwise the error is `docker-inactive` and
  Docker is never touched, because socket activation would start Docker and re-run the gate.
- **Pins:** read from `{{ homelab_stacks_dir }}/*/compose.yaml` by a small, strict reader of the
  YAML block subset the stacks templates render (anchors and merge keys included), checked
  against PyYAML in the tests. A compose file, its directory and the stacks dir must be root-owned
  and not group/world-writable, or they're ignored: the helper runs compose as root.
- **No shell, ever:** every command is an argv list with a timeout. Spawned commands get their own
  process group, which is ended as a whole.
- **Gate hold (an extra safety rule):** while the boot gate status isn't `ok`, `start`, `restart`
  and `update` of apps in a gate-held stack (`media`) are refused with `held-by-gate`. `stop` is
  always allowed.
- **Locks:** at most one action per container at a time (otherwise `busy`).
- **Logging:** every request is logged to journald (`homelab-dockerctl`): the peer uid and pid,
  the op and its fields, then the result and duration. Container output and log lines never go to
  the journal.

**Protocol** (`apps/dashboard/server/src/helper/client.js` speaks it):
```
request  {"op": "...", ...}\n                      (write it; never half-close the socket)
unary    {"ok": true, "op": OP, "data": DATA}  |  {"ok": false, "error": {"code": CODE, "message": TEXT}}
stream   {"ok": true, "op": OP, "stream": true, ...}   (or the error line above)
         {"type": "line"|"step"|"output", ...}  ...
         {"type": "end", "ok": BOOL, "reason": TEXT, "message"?: TEXT}
```
| op | request fields | data / events |
|---|---|---|
| `ps` | none | `{docker: active\|inactive\|failed, containers: [{name, state, health, started_at, restart_count, image, pinned, stack}], declared: [{name, stack, service, pinned, local_build}]}`. `containers` is empty and Docker untouched unless docker.service is active; `declared` comes from the compose files and always works |
| `logs` | `name`, `tail` 0..2000 (200), `follow` bool, `since` (unix s, `10m`/`2h`, RFC 3339) | header `{name, follow}`, then `{type: line, ts, text, stream: stdout\|stderr}`. Ends with `eof`, or `max-duration` after 30 min of follow, or on client close |
| `start` `stop` `restart` | `name` | `{name, state}` |
| `update` | `name` | header `{name, pinned}`, then `{type: step, step, at, ...}`: `pulling` (`image`), `recreating`, `waiting-health`, `done` (`image`, `health`) or `failed` (`failed_step`, `code`, `message`); `{type: output, text}` is compose progress. Runs `docker compose -f <stack>/compose.yaml pull <svc>` then `up --detach --no-deps --no-build --pull never <svc>`, then waits for `healthy` (or 5 s running without a healthcheck). Local-build images (wol-relay) are refused with `local-image`. It always runs to its end, even if the client leaves |
| `docker-state` | none | `{state: active\|inactive\|failed, unit_state}` |
| `last-pull` | none | `{last_pull, started_at, duration_seconds, last_attempt}`: last sshd `Accepted … for backuppull … port P` paired with `Disconnected from user backuppull … port P` (45 days of journal) |
| `disk-split` | none | `{sizes: {tv, movies, downloads}, computed_at, cached, stale, reason?}`. `du -s -x -B1` under `ionice -c3 nice -n19`, cached 24 h. It never measures an unverified mount (gate `ok`, mounted, sentinel present); otherwise it serves the stale cache or `media-unavailable` |
| `boots` | none | `{boots: [{index, boot_id, first_entry, last_entry}]}` from `journalctl --list-boots -o json` |

Error codes: `forbidden`, `bad-request`, `unknown-op`, `docker-inactive`, `not-allowed`,
`held-by-gate`, `local-image`, `busy`, `media-unavailable`, `timeout`, `failed`, `internal`.

**Unit hardening** (root, because of the Docker socket, the journal and the media folders):
- `CapabilityBoundingSet=CAP_DAC_READ_SEARCH`, only so `du` can read folders an app made 0700;
- `PrivateNetwork=yes` with `IPAddressDeny=any` and `RestrictAddressFamilies=AF_UNIX`: the Docker,
  systemd and journal sockets are filesystem sockets, and the daemon (not the CLI) pulls images;
- `NoNewPrivileges`, `ProtectSystem=strict` plus `ReadOnlyPaths=/run` (writable: its `CacheDirectory`
  only), `ProtectHome`,
  `PrivateTmp` (the docker CLI's `DOCKER_CONFIG` lives there), `PrivateDevices`, `PrivateIPC`;
- `ProtectKernel*`, `ProtectControlGroups`, `ProtectClock`, `ProtectHostname`, `ProtectProc=invisible`;
- `RestrictNamespaces`, `RestrictRealtime`, `RestrictSUIDSGID`, `LockPersonality`,
  `MemoryDenyWriteExecute`, `SystemCallFilter=@system-service ~@privileged`;
- `StandardOutput=journal`, so replies only ever go through the socket.

`systemd-analyze security`: **1.0 OK**. What remains is root itself, the one capability, AF_UNIX
and `@resources` (needed by nice/ionice).

## Tests
```bash
python3 -m unittest discover -s roles/dashboard/files/homelab-dockerctl/tests -v
```
No root, no Docker. A fake host scripts systemctl, docker, journalctl and du; replies travel over
real socketpairs, streamed output over real pipes.
- `test_protocol.py`: the `SO_PEERCRED` rejection (the real kernel value, root included), the
  exact op allowlist, field and type validation, the 4 KiB limit, and request timeouts.
- `test_docker_guard.py`: no docker argv for any op in any non-active state, and the guard
  re-checked before every docker command (Docker stopping mid-update stops it before `up`).
- `test_compose.py`: pins from the rendered stacks templates (also against PyYAML when installed),
  anchors and merge keys, label forms, and untrusted files ignored.
- `test_journal.py`: last-pull session pairing (other users, ports, IPv6, unfinished sessions) and boots.
- `test_actions.py`: the update step stream and its failure modes, local images, the gate hold,
  locks, an update finishing after the client left, and start/stop/restart by full ID.
- `test_logs_and_disk.py`: the log stream (stdout/stderr, ANSI stripped, long lines, follow
  limit, client close), and the disk-split cache, nice/ionice and the unverified-mount refusal.
- `test_config_drift.py`: `templates/dockerctl.json.j2` against the helper's real `Config`.
  - Statically (PyYAML): every rendered key and timeout is a `Config` field, and the required ones
    are present.
  - Rendered (Jinja2): the template is rendered with the role defaults, the committed group_vars
    and placeholder host vars, then loaded with `Config.from_mapping`; the socket and service
    units are checked to point at the same config file, cache and group. In CI a missing Jinja2
    fails this test instead of skipping it.
- `test_e2e.py`: the real script started the way `Accept=yes` starts it, with fake executables.
  It covers `main()`, the config file, and the process-group kill on client close.
