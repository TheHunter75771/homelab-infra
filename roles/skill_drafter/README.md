# skill_drafter

The Skill Forge drafter (module K2, `apps/skill-drafter/`) as a hardened, long-running host service on ai-box
(docs/CONTRACTS.md §12.19.1, deploy module H6; the Lead's K2 decisions, 2026-10-27). Hosts: `ai_servers`, after
the AI Library share is mounted (`playbooks/ai-library.yml`), the research gate (it owns `rgate`) and the AI
stack (the broker). Modelled on role finance_digest (account, guards, IP filter) and research_gate (the switch,
the long-running unit, the restart handler).

| | |
|---|---|
| Account | `sdraft`: system, primary group `sdraft`, **supplementary group `rgate` only** (`groups: [rgate]`, `append: false`), `/usr/sbin/nologin`, home `/nonexistent` (not created). A read-only check fails the run unless `id -Gn sdraft` is exactly `sdraft rgate` (never docker). This role never creates `rgate`: preflight refuses if it is missing |
| Code | `/opt/skill-drafter/`, root-owned: K2's launcher `skill-drafter` (0755), `skill_drafter/`, and `forge/` + `library/` from `apps/skill-library` (0755 dirs, 0644 `*.py`) |
| Config | `/etc/skill-drafter/config.json` (root:sdraft 0640, no secrets), dir root:sdraft 0750 |
| Unit | `skill-drafter.service` (`Type=exec`, `Restart=on-failure`, `RestartSec=30s`, `WantedBy=multi-user.target`): `python3 -I -B /opt/skill-drafter/skill-drafter --config /etc/skill-drafter/config.json serve` |
| Switch | `skill_drafter_enabled` (default **true**): enabled and running. `false`: installed only; a running drafter is stopped and disabled |

## What a run does
1. **Preflight** (before anything is written):
   - the switch is a boolean; `tailnet_ip` is a dotted IPv4 inside `100.64.0.0/10`; the broker port; the resolver
     stub (a 127.x address); the cap, `MemoryMax`, `TasksMax`, `RestartSec` (they go into the unit unescaped);
   - **K2's own loader** (`skill_drafter.config.validate`) on the role's config, and the library's
     `naming.parse_size` on the cap, run on the controller from the sources;
   - the IP filter is sound: deny exactly `any`, no allow entry a `/0` or malformed, and no broker range overlaps
     SearXNG's subnet (finance_digest's H5-1 assert);
   - K2's, forge's and library's sources exist on the controller; the group `rgate` exists on the host;
   - **enabled only:** the library is really mounted (§12.11): the root is a real directory on exactly
     `skill_drafter_library_mount` (not `/`, not the disk under an empty mount point), `.ai-library-sentinel` is
     a regular file and `35-forge/` a real folder.
2. **Base:** Python 3.12+; the account above.
3. **Code:** every directory checked to be absent or a real directory before the first write and again before
   each find (never write or prune through a symlink); only `*.py`, skipping `__pycache__`, `test`, `tests`,
   `node_modules`; the launcher. Everything else under `/opt/skill-drafter` is pruned (K2's tests, README and
   example config are never deployed). Any change restarts the drafter (when enabled).
4. **Config:** exactly K2's keys: `library_root`, `broker_url` (`http://<tailnet_ip>:<ai_stack_broker_port|11434>`),
   `model` (`assistant`), `poll_interval` 120, `max_source_chars` 16000, `num_ctx` 16384, `timeout` 1200,
   `max_attempts` 3. Validated before install with **K2's own loader from the installed code**
   (`skill_drafter.config.load`). A change restarts the drafter.
5. **Unit**, daemon-reload, restart if anything changed, then the switch (enabled+started or stopped+disabled).
6. **Verify** (enabled, not in check mode; `skill_drafter_verify: false` skips it): the unit is active and stays
   so through a start-up and its first pass (no restart in between), and `systemd-run --wait --pipe` runs
   `skill-drafter status` as sdraft in a transient service with **exactly the unit's sandbox and IP filter**
   (one list, `skill_drafter_sandbox`, renders the unit and builds the `--property=` arguments). It must exit 0:
   K2 opened the mounted library and read `35-forge/` through the real sandbox. `status` never calls the broker.

A second run is `changed=0` (and restarts nothing).

## Writes: 35-forge only (the INDEX.md question)
`forge.draft_skill` (apps/skill-library/forge/core.py) does **not** rebuild INDEX.md: it stages the draft in
`35-forge/<id>/.forge-staging-*`, moves it to `skill/` (an older draft to `previous/<n>/`), writes `review.md`
and `state.md`, all under the forge lock `35-forge/.forge-lock`; the library's temp files go in the destination
folder. Only the forge **CLI** reindexes, on Noah's side. K2's own `drafter-log.md` is in the request folder. 35-forge
isn't indexed anyway (`library/core.py`: "not indexed or checked here"). So `ReadWritePaths` stays
`35-forge`; the rest of the library (`10-sources/`, the texts it drafts from, and the whole root, walked for
the cap) is read-only through `ProtectSystem=strict`.

## The unit
- `User=sdraft` (its supplementary groups come from `/etc/group` via initgroups, so `rgate` applies),
  `NoNewPrivileges`, `ProtectSystem=strict`, `ReadWritePaths=/srv/ai-library/35-forge` (the only writable path),
  `ReadOnlyPaths=/run`, `ProtectHome`, **`PrivateTmp`** (K2 writes the validated draft to a temp dir before
  `draft_skill` copies it in), no capabilities, `RestrictAddressFamilies=AF_INET AF_UNIX`, `@system-service`
  minus `@privileged`, `PrivateDevices`, `PrivateIPC`, the Protect* set, `ProtectProc=invisible` and **no
  `ProcSubset=pid`** (§12.17 Reviewer E), `RestrictNamespaces`, `MemoryDenyWriteExecute`, `MemoryMax=256M`,
  `TasksMax=32`, `UMask=0027`.
- **`Environment=AI_LIBRARY_CAP=500GB`** (`skill_drafter_library_cap`): the library's cap as its CLI reads it;
  `draft_skill` files nothing past it. The only environment line.
- **Mount safety:** `RequiresMountsFor=/srv/ai-library`, `AssertPathExists=<root>/.ai-library-sentinel`,
  `AssertPathIsDirectory=<root>/35-forge` (research_gate's pattern). A failed assert at boot leaves the unit
  failed (`Restart=` doesn't retry a failed condition); the next converge, or `systemctl start skill-drafter`,
  starts it once the share is up.
- **Restarts and attempts:** a restart (converge, or `Restart=on-failure`) during a broker call loses that call;
  its `start` line in `drafter-log.md` counts as one of the round's 3 attempts. SIGTERM lets K2 finish the current
  pass, up to systemd's default 90 s stop timeout, then SIGKILL.

### IP filter (the broker only)
`IPAddressDeny=any`. `IPAddressAllow=`:
- `<tailnet_ip>/32` **and** the broker's DNAT target range: the broker is published on `tailnet_ip` with the
  userland proxy off, so the request is DNATed in nat OUTPUT to the broker container's bridge address (systemd's
  egress filter sees that), and the reply is un-NATed before delivery (the ingress filter sees `tailnet_ip`).
  Both are needed (§12.15 Reviewer D, §12.16.4, §12.18.3). The broker's container address isn't pinned (queued
  for ai_stack P2), so the range is role docker's `docker_address_pools_container` (172.16.0.0/12) **minus
  SearXNG's pinned subnet** (`searxng_subnet`), read from the inventory, else from roles docker's and searxng's
  defaults (`include_vars`, never copied): SearXNG forwards query strings to public search engines, so reaching
  it would be a way out for source texts and drafts. Set `skill_drafter_broker_ranges` (a list of CIDRs) to
  narrow it to the broker network's subnet or a `/32` (finance_digest's value can be reused);
- `127.0.0.53/32`, systemd-resolved's stub (nothing resolves a name: the broker URL is an IPv4 literal).

## Inputs
- **Required (host_vars, git-ignored):** `tailnet_ip`.
- Optional: `ai_stack_broker_port` (default 11434), `skill_drafter_enabled`, `skill_drafter_broker_ranges`,
  `skill_drafter_library_cap`, `skill_drafter_model` / `_poll_interval` / `_max_source_chars` / `_num_ctx` /
  `_timeout` / `_max_attempts` (K2's ranges), `skill_drafter_library_root` / `_mount`, `skill_drafter_verify`.

## Host checks for the Integrator
- `id sdraft` → groups `sdraft rgate` only; `ls -la /opt/skill-drafter` shows `skill-drafter`, `skill_drafter`,
  `forge`, `library`, all root-owned; `/etc/skill-drafter` root:sdraft 0750, `config.json` 0640.
- `systemd-analyze verify /etc/systemd/system/skill-drafter.service` clean; `systemctl is-active skill-drafter`;
  `systemd-analyze security skill-drafter` for the record; `systemctl show -p IPAddressDeny -p IPAddressAllow
  skill-drafter` lists the ranges above.
- **The broker through the filter** (the verify's `status` doesn't call it): `systemd-run -P -p IPAddressDeny=any
  -p IPAddressAllow="<the unit's allow list>" curl -m5 -s -o /dev/null -w '%{http_code}\n'
  http://<tailnet_ip>:11434/api/version` prints 200, and the same against
  `http://<searxng_ipv4>:8080/healthz` times out.
- §12.19.3: a request "learn APA 7 reference formatting" with one ticked official source → within a poll or two
  after the gate settles it, `journalctl -u skill-drafter` shows "drafted ..." and `35-forge/<id>/` has `skill/SKILL.md`,
  `review.md` and `drafter-log.md` (`ok: filed ...`); no seccomp/`EPERM`/AVC errors; no new file outside
  `35-forge/` (INDEX.md unchanged until Noah's next forge command).
- A hostile source page produces a normal draft (the injection appears only as quoted data).

## Tests
```sh
roles/skill_drafter/tests/run.sh   # from anywhere; uses the repo's .venv; no root; the scratch dir must not be on /
```
`test_skill_drafter.yml` at `-vvv --diff`, with stub handlers that count restarts and a fake `systemd-run` and
`systemctl` (tests/bin):
- the defaults (the config equals K2's `config.example.json` apart from the broker URL); the account task's exact
  attributes and the `id -Gn` check;
- preflight passes enabled against a scratch library (initialized with the library CLI, one `forge new`
  request) on the scratch dir's real mount, and disabled with no library at all; the exact allow/deny lists, the
  broker ranges proven to tile 172.16.0.0/12 minus SearXNG's /28 with no overlap; 39 refusals (the switch;
  tailnet address empty, off-tailnet, short form, a name; broker port 0 and 65536; resolver stub; cap with a
  space; MemoryMax; RestartSec; K2's loader on model, poll interval, num_ctx, attempts; an unparseable cap; a
  `/0`, malformed or empty broker range; a deny list that isn't `any`; 3 broker overrides reaching SearXNG and its
  subnet resolving to empty; library paths; the mount, sentinel and 35-forge checks; missing sources; a missing
  `rgate` group);
- install twice with junk and strays, a third run touching nothing (inodes and mtimes) and requesting no restart;
  the installed launcher imports only the installed copies; 5 symlink cases refused by the guard with the victim
  untouched;
- config.json exactly K2's keys with integer values, 0640 in 0750, a restart on the first write and none on the
  second; K2's loader refuses 9 bad configs and accepts the rendered one; a symlinked config dir refused;
- the unit (default and a narrowed range): `systemd-analyze verify` with any warning failing (except one about
  the machine's own unit files), `[Service]` exactly the sandbox list plus five fixed keys, the IP lines exact;
- verify.yml: the real installed drafter runs `status` against the scratch library through the fake
  systemd-run, which gets `--wait --pipe --collect`, every sandbox property and the status command; a steady
  unit passes; a never-active unit, a restarting unit and a status that exits 2 (not an initialized library)
  are refused.
