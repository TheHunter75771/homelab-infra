# voice_profile

The "write in Noah's voice" profile builder (module V, `apps/voice-profile/`) as a hardened, **on-demand**
oneshot on ai-box (docs/CONTRACTS.md §12.21, deploy module H7). Hosts: `ai_servers`, after the AI Library share
is mounted (`playbooks/ai-library.yml`), the research gate (it owns `rgate`) and the AI stack (the broker).
Modelled on role skill_drafter (account, guards, install, IP filter, the sandboxed `status` check), minus the
long-running service: nothing here starts a build.

| | |
|---|---|
| Account | `vprof`: system, primary group `vprof`, **supplementary group `rgate` only** (`groups: [rgate]`, `append: false`), `/usr/sbin/nologin`, home `/nonexistent` (not created). A read-only check fails the run unless `id -Gn vprof` is exactly `vprof rgate` (never docker). This role never creates `rgate`: preflight refuses if it is missing |
| Code | `/opt/voice-profile/`, root-owned: V's launcher `voice-profile` (0755), `voice_profile/`, and `library/` from `apps/skill-library` (0755 dirs, 0644 `*.py`) |
| Config | `/etc/voice-profile/config.json` (root:vprof 0640, no secrets), dir root:vprof 0750 |
| Unit | `voice-profile.service`: `Type=oneshot`, `TimeoutStartSec=40min`, **no `[Install]`** (static) and **no timer**: `python3 -I -B /opt/voice-profile/voice-profile --config /etc/voice-profile/config.json build` |

## Running a build (Noah or Claude)
```sh
sudo systemctl start voice-profile              # blocks until the build ends (up to 40 min with the CPU model)
sudo systemctl start --no-block voice-profile   # or detach
journalctl -u voice-profile -f                  # follow it
sudo -u vprof python3 -I -B /opt/voice-profile/voice-profile --config /etc/voice-profile/config.json status
```
A dry run or a stats-only run (`build --dry-run`, `build --no-ai`) uses the same sandbox through `systemd-run`:
copy the `--property=` list from the unit (or from `tasks/verify.yml`) and add `-- <argv> build --no-ai`.
The unit has no `[Install]` section, so `systemctl enable` refuses it ("static") and it never starts at boot.

## What a converge does
1. **Preflight** (before anything is written):
   - `tailnet_ip` is a dotted IPv4 inside `100.64.0.0/10`; the broker port; the cap, `MemoryMax`, `TasksMax`, `TimeoutStartSec` (they go into the unit unescaped); a plain account name;
   - `voice_profile_settings` is a dict with a numeric `timeout` (an override that drops it is refused with a
     clear message, M-7), and `TimeoutStartSec` is at least that `timeout` + 300 s (otherwise systemd would kill
     a build mid-call);
   - **V's own loader** (`voice_profile.config.validate`) on the role's config, and the library's
     `naming.parse_size` on the cap, run on the controller from the sources;
   - the IP filter is sound: deny exactly `any`, no allow entry a `/0` or malformed, at least one broker range, and
     no broker range overlaps SearXNG's subnet (finance_digest's H5-1 assert);
   - V's and the library's sources exist on the controller; the group `rgate` exists on the host;
   - the library is really mounted (§12.11): the root is a real directory on exactly `voice_profile_library_mount`
     (not `/`, not the disk under an empty mount point), `.ai-library-sentinel` is a regular file and
     `40-writing/` and `90-archive/` real folders;
   - **no build is running** (`ActiveState` inactive or failed; a oneshot is `activating` while it runs): a
     converge never replaces code or config under a running build.
2. **Base:** Python 3.12+; the account above.
3. **Archive folder:** `90-archive/40-writing` (guarded, then created `rgate:rgate 0770`; see below).
4. **Code:** every directory checked to be absent or a real directory before the first write and again before
   each find (never write or prune through a symlink); only `*.py`, skipping `__pycache__`, `test`, `tests`,
   `node_modules`; the launcher. Everything else under `/opt/voice-profile` is pruned (V's tests, README and
   example config are never deployed).
5. **Config:** V's keys: `library_root` (`/srv/ai-library`), `broker_url`
   (`http://<tailnet_ip>:<ai_stack_broker_port|11434>`), and `voice_profile_settings` (V's defaults: `model`
   `assistant`, `num_ctx` 16384, `timeout` 1800, the zip/size caps, `min_words`, `dedupe_jaccard`, `excerpts`,
   `excerpt_chars`). Validated before install with **V's own loader from the installed code**
   (`voice_profile.config.load`). It applies at the next build.
6. **Unit**, then a daemon-reload if it changed. Nothing is started, restarted, stopped or enabled.
7. **Verify** (not in check mode; `voice_profile_verify: false` skips it), read-only, never a build:
   - `systemctl show` says `LoadState=loaded`, `UnitFileState=static` and `TriggeredBy=` empty (no timer or path
     unit can start it);
   - `systemd-run --wait --pipe` runs `voice-profile status` as vprof in a transient service with **exactly the
     unit's sandbox and IP filter** (one list, `voice_profile_sandbox`, renders the unit and builds the
     `--property=` arguments). It must exit 0: V loaded its config and opened the mounted library and
     `40-writing/` through the real sandbox. Its lines are printed, including `broker: reachable|unreachable`
     (a TCP connect through the real IP filter; reported, not asserted, so a broker that is down doesn't fail
     the converge).

A second converge is `changed=0`.

## Writes: 40-writing and 90-archive/40-writing
V writes `40-writing/samples-text/<name>.md` + `MANIFEST.md`, `40-writing/style-stats.md` and
`40-writing/voice-profile.md` (or `voice-profile.next.md` when the profile isn't a draft), through the library's
`atomic_write_bytes` (temp file + rename in the same folder). When a sample is removed or newly excluded, V moves
its stale cleaned text to `90-archive/40-writing/samples-text/` with `Library.archive` (the Lead's H7 decision,
§12.21), so W3 never serves it. So `ReadWritePaths=/srv/ai-library/40-writing` and
`ReadWritePaths=/srv/ai-library/90-archive/40-writing`; inside the first, `ReadOnlyPaths=-/srv/ai-library/40-writing/samples`
makes Noah's originals read-only again (V only reads them; `-`: no error while the folder is missing). The rest
of the library is read-only (`ProtectSystem=strict`).

The role creates `90-archive/40-writing` (a fresh library has only `90-archive/`) as `rgate:rgate 0770`, which is
what the CIFS client shows for everything on the share (`uid=rgate,gid=rgate,dir_mode=0770`), so later converges
change nothing. Both `90-archive` and `90-archive/40-writing` must be absent or real directories (never a symlink:
systemd follows a symlink in `ReadWritePaths`, which would make its target writable). The unit asserts the folder
(`AssertPathIsDirectory`), so a deleted folder stops a build with a clear assert until the next converge.

**The archive move crosses two mounts (EXDEV), and V handles it.** systemd implements each `ReadWritePaths=`
entry under `ProtectSystem=strict` as its own bind mount, and rename(2) never crosses mount points, even of the
same filesystem: inside the unit, `os.replace` from `40-writing/` to `90-archive/40-writing/` fails with `EXDEV`
(reproduced with `systemd-run --user -p ProtectSystem=strict` and these two paths on 2026-10-27; copy + unlink
works). The Lead's decision: V falls back to copy, fsync, then unlink on `EXDEV`, and the unit keeps exactly these
two writable paths (no writable library root with read-only binds over the rest, which would be a deny-list).

## The unit
- `User=vprof` (its supplementary groups come from `/etc/group` via initgroups, so `rgate` applies),
  `NoNewPrivileges`, `ProtectSystem=strict`, `ReadWritePaths=/srv/ai-library/40-writing` and
  `/srv/ai-library/90-archive/40-writing`, `ReadOnlyPaths=/run`, `-/srv/ai-library/40-writing/samples` and
  `-/srv/ai-library/40-writing/include.txt` (Noah's override list: a build can't edit it, M-6),
  `InaccessiblePaths=/dev/shm` (`PrivateDevices` keeps the host's writable, shared /dev/shm; M-3), `ProtectHome`, `PrivateTmp`, no capabilities,
  `RestrictAddressFamilies=AF_INET AF_UNIX`, `@system-service` minus `@privileged`, `PrivateDevices`,
  `PrivateIPC`, the Protect* set, `ProtectProc=invisible` and **no `ProcSubset=pid`** (§12.17 Reviewer E),
  `RestrictNamespaces`, `MemoryDenyWriteExecute`, `MemoryMax=256M`, `TasksMax=32`, `UMask=0027`.
- **`Environment=AI_LIBRARY_CAP=500GB`** (`voice_profile_library_cap`): V checks the library cap before writing.
  The only environment line.
- **Mount safety:** `RequiresMountsFor=/srv/ai-library`, `AssertPathExists=<root>/.ai-library-sentinel`,
  `AssertPathIsDirectory=<root>/40-writing` and `<root>/90-archive/40-writing`, and
  `AssertPathIsSymbolicLink=!` for `40-writing`, `40-writing/samples`, `90-archive` and `90-archive/40-writing`
  (M-4: systemd follows a symlink in `Read*Paths` when it builds the sandbox, so a link planted after the
  converge's guard would redirect the writable or read-only path; the build refuses to start instead).
- `TimeoutStartSec=40min` (`voice_profile_timeout_start`): one whole build, longer than V's broker `timeout`
  (1800 s) plus a margin. No `Restart=`: a failed build stays failed until someone starts it again.

### IP filter (the broker only)
`IPAddressDeny=any`. `IPAddressAllow=`:
- `<tailnet_ip>/32` **and** the `ai` network's `/24`: the broker is published on `tailnet_ip` with the userland
  proxy off, so the request is DNATed in nat OUTPUT to the broker container's address (systemd's egress filter
  sees that), and the reply is un-NATed before delivery (the ingress filter sees `tailnet_ip`). Both are needed
  (§12.15 Reviewer D, §12.18.3, §12.19). The `/24` is `ai_stack_network_subnet` (pinned and owned by role
  ai_stack, §12.18b), from the inventory, else role ai_stack's default (`include_vars`, never copied);
  `voice_profile_broker_ranges` (a list of CIDRs) overrides it. Preflight proves no range overlaps SearXNG's
  subnet: SearXNG forwards query strings to public search engines, a way out for Noah's samples.

No resolver allow (Reviewer M, M-5): the broker URL is an IPv4 literal, so V never resolves a name.

## Inputs
- **Required (host_vars, git-ignored):** `tailnet_ip`.
- Optional: `ai_stack_broker_port` (default 11434), `ai_stack_network_subnet` / `voice_profile_broker_ranges`,
  `voice_profile_settings` (V's ranges; override the whole dict or `combine()` into it),
  `voice_profile_timeout_start`, `voice_profile_library_cap`, `voice_profile_memory_max`,
  `voice_profile_library_root` / `_mount`, `voice_profile_verify`.

## Host checks for the Integrator
- `id vprof` → groups `vprof rgate` only; `ls -la /opt/voice-profile` shows `voice-profile`, `voice_profile`,
  `library`, all root-owned; `/etc/voice-profile` root:vprof 0750, `config.json` 0640.
- `systemd-analyze verify /etc/systemd/system/voice-profile.service` clean; `systemctl is-enabled voice-profile`
  → `static`; `systemctl list-timers | grep voice` → nothing; `systemd-analyze security voice-profile` for the
  record; `systemctl show -p IPAddressDeny -p IPAddressAllow voice-profile` lists the ranges above.
- The converge's verify line ends `broker: reachable` (the TCP connect went through the real filter).
- **A build:** `systemctl start voice-profile` with Noah's 20 samples → exit 0 (or 1 with a logged reason),
  `40-writing/samples-text/` + `MANIFEST.md` (the peer-review sheet excluded), `style-stats.md`,
  `voice-profile.md` with `status: draft`; no seccomp/`EPERM`/AVC errors; `samples/` unchanged (mtimes); no
  new file outside `40-writing/` and `90-archive/40-writing/` (INDEX.md unchanged).
- **The archive path:** remove one sample from `samples/` and build again: its old cleaned text must move to
  `90-archive/40-writing/samples-text/` (V's EXDEV fallback; no "cannot archive" in the journal).
- A second build after Noah sets `status: approved` writes `voice-profile.next.md` and leaves the approved file
  alone.

## Tests
```sh
roles/voice_profile/tests/run.sh   # from anywhere; the repo's .venv if present, else PATH; no root; the scratch dir must not be on /
# CI: TMPDIR=/dev/shm ANSIBLE_VAULT_PASSWORD_FILE=/tmp/vault-pass roles/voice_profile/tests/run.sh
```
`test_voice_profile.yml` at `-vvv --diff`, with a stub handler that counts daemon-reloads and a fake
`systemd-run` and `systemctl` (tests/bin; the fake systemctl answers only the two read-only `show` queries and
fails on anything else):
- the defaults (the config equals V's `config.example.json` apart from the broker URL, with the same JSON types);
  the account task's exact attributes and the `id -Gn` check; **no task or handler can start, restart, stop,
  enable or mask anything** (the only systemd action is one daemon-reload; no timer anywhere);
- preflight passes against a scratch library (initialized with the library CLI) on the scratch dir's real mount,
  and with a failed last build; the exact allow/deny lists, the `/24` following `ai_stack_network_subnet`, and the pinned
  gpu-broker `/32` (§12.23) accepted with `tailnet_ip/32` still added by the role;
  48 refusals (tailnet address empty, off-tailnet, short form, a name; broker port 0 and 65536;
  cap with a space; MemoryMax; start timeout with a space, `infinity`, too short for V's broker timeout, a
  broker timeout raised past it; settings with a string or boolean timeout, without timeout, or not a dict; V's loader on model, num_ctx, excerpts, an unknown key;
  an unparseable cap; a `/0`, malformed or empty broker range; a deny list that isn't `any`; 3 broker ranges
  reaching SearXNG and its subnet resolving to empty; library paths; the mount, sentinel and 40-writing checks;
  missing sources; a missing `rgate` group; the account named `rgate`; a running build; no `90-archive`, a
  symlinked `90-archive`);
- the archive folder: created 0770 owned like the library, untouched by a second run; a symlinked `90-archive`
  or `90-archive/40-writing` refused by the guard with the victim untouched;
- install twice with junk and strays (including a symlink), a third run touching nothing (inodes and mtimes);
  the installed launcher imports only the installed copies; 4 symlink cases refused by the guard with the victim
  untouched;
- config.json exactly V's keys, 0640 in 0750, unchanged by a second run; V's loader refuses 10 bad configs and
  accepts the rendered one; a symlinked config dir refused;
- the unit (default and a narrowed range): `systemd-analyze verify` with any warning failing, only `[Unit]` and
  `[Service]` (no `[Install]`), and **every `[Unit]` and `[Service]` line spelled out literally in
  `tests/units_case.yml`, in order** (M-1: not derived from the role's vars, so deleting or changing any sandbox
  entry fails), the IP lines exact;
  service.yml twice: one daemon-reload, then none;
- verify.yml: the real installed voice-profile runs `status` against the scratch library (broker on a closed
  local port, so the test never reaches out) through the fake systemd-run, which gets `--wait --pipe --collect`,
  every sandbox property and `status` (never `build`); a static, untriggered unit passes; an enabled unit, a
  timer-triggered unit, an unloaded unit and a status that exits 2 (not an initialized library) are refused;
  40-writing and the archive folder are untouched afterwards. (The EXDEV behaviour needs a real systemd sandbox, so it
  is documented above, not in this offline test.)
