# Role `unit_hardening`

Hides tailscaled's local API (`/run/tailscale/tailscaled.sock`) from a Lead-declared, per-host list of sandboxed
systemd services. Contract: `docs/CONTRACTS.md` §12.30 (H15); the finding is Reviewer Q's Q2-1.

**Why.** The socket is mode 0666, so any local process can ask tailscaled to dial a tailnet peer. tailscaled
makes that connection as root, which gets around a unit's `IPAddressDeny=`/`IPAddressAllow=` (the filter sees
tailscaled's traffic, not the unit's). AI Control and Mission Control already hide `/run` (`TemporaryFileSystem=/run:ro`).
This role covers the other sandboxed units on ai-box and the gaming PC.

## What it installs

| File | Content | Why |
|---|---|---|
| `/etc/systemd/system/<unit>.d/50-no-tailscaled.conf` (each declared unit) | `[Service]` `InaccessiblePaths=-/run/tailscale -/var/run/tailscale` | Hides the directory (and so the socket) in the unit's mount namespace. `-`: no error if the path is missing. |
| the same drop-in, **root units only** | `PrivatePIDs=yes`, `TemporaryFileSystem=/run:ro`, `BindReadOnlyPaths=<run_allow>` (if any), and `Environment=SYSTEMCTL_FORCE_BUS=1` for units in `unit_hardening_systemctl_via_bus` | See "Root units". |
| `/etc/systemd/system/tailscaled.service.d/50-unit-hardening-keep-rundir.conf` | `[Service]` `RuntimeDirectoryPreserve=yes` | See "The detach problem". |
| `/etc/tmpfiles.d/unit-hardening-tailscale.conf` | `d /run/tailscale 0755 root root -` | `/run/tailscale` exists from early boot, so a unit that starts before tailscaled still has something to hide. |
| `/run/tailscale` (directory, for this boot) | 0755 root:root | Normally tailscaled made it already. |

### The detach problem (why the tailscaled drop-in is needed)

`InaccessiblePaths=` hides `/run/tailscale` by mounting over that directory inside the unit's namespace. If the host
**removes** a directory that is a mount point in another namespace, the kernel detaches those mounts. Proven
twice on 2026-10-29:

- with `unshare`;
- with real systemd: a transient user unit with `InaccessiblePaths=-<dir>` saw the socket again after
  `rm -rf <dir>; mkdir <dir>; touch <dir>/tailscaled.sock`. It kept it hidden when only the socket file was
  unlinked and recreated.

`tailscaled.service` (Ubuntu and Fedora packages) has `RuntimeDirectory=tailscale` with the default
`RuntimeDirectoryPreserve=no`. So every tailscaled stop or restart (a package upgrade, for example) deletes
`/run/tailscale`, and the hiding in every running hardened unit would silently end.

With `RuntimeDirectoryPreserve=yes`, systemd keeps the directory. tailscaled then only removes and recreates the
socket file inside it, and the hiding mount stays in place.

The drop-in only takes effect after a `daemon-reload`; tailscaled itself is never restarted.

## Inputs

| Variable | Default | Meaning |
|---|---|---|
| `unit_hardening_units` | `[]` | `[{unit: <name>.service, restart: true\|false}]`, plus `run_allow: [<path under /run>, ...]` for a root unit that needs something from `/run`. Empty: only the inputs are checked; the host is not touched. |
| `unit_hardening_remove` | `[]` | Units whose drop-in is removed (retiring). No restart. |
| `unit_hardening_verify_settle` | `5` | Seconds between the verify's two reads of each unit (0 to 60). |
| `unit_hardening_systemctl_via_bus` | `[]` | Root units whose `systemctl` calls go over the D-Bus system bus (`SYSTEMCTL_FORCE_BUS=1`), so they can take `PrivatePIDs=yes`. A Lead decision per unit. |
| `unit_hardening_busy_urls` | `{ollama.service: http://<gaming_pc_tailnet_ip>:11434/api/ps}` | Units asked "are you busy?" before a restart (Q6-4). |
| `unit_hardening_allow_busy_restart` | `false` | `-e unit_hardening_allow_busy_restart=true` restarts a busy unit anyway. |

The inputs check refuses:

- anything but a list of exactly `{unit, restart}`, with `restart` a YAML boolean;
- a name that isn't a plain `*.service`, so no `.socket`, `.timer`, template or instance;
- duplicates, or a unit that is both listed and in the remove list;
- the units in `vars/main.yml` `unit_hardening_excluded` (see the needs table);
- `ollama.service` with `restart: true` but no usable `/api/ps` URL (for example `gaming_pc_tailnet_ip` unset);
- a `run_allow` entry that isn't a plain absolute path under `/run`. Anchored with `\Z`, so a trailing newline
  is refused too (Q9-1);
- a `run_allow` entry that hands out root, or other units' secrets (matched ignoring case):
  - tailscaled's socket;
  - docker, containerd, podman, libvirt, crio, lxd or incus sockets;
  - snapd's sockets (root on snapd runs unconfined hooks);
  - `/run/systemd/private`, systemd's varlink sockets (`/run/systemd/io.systemd.*`) or `/run/systemd/userdb`;
  - `/run/user/...` and `/run/credentials/...`;
  - udev's control socket (`/run/udev/control`);
- a `run_allow` entry that is a **directory above** one of those (`unit_hardening_run_deny_paths`), because a bind
  brings everything below it. So `/run/systemd`, `/run/udev`, `/run/containerd` and `/run` itself are refused,
  while `/run/systemd/system` and `/run/dbus/system_bus_socket` are fine (Q9-1);
- `/run/dbus`, or any other directory above the bus socket: the bus may only be allowed as
  `/run/dbus/system_bus_socket` itself (Q10-1).

### Root units (Q6-1, Q8-1)

A root unit gets two layers on top of `InaccessiblePaths`: its own PID namespace (Q6-1) and a `/run` allowlist
(Q8-1).

#### Its own PID namespace (Q6-1)

Reviewer Q proved that a root unit with only `CAP_CHOWN`/`CAP_DAC_READ_SEARCH` (like ai-openwebui-backup) can reach the socket through `/proc/<pid>/root` of another root process that still sees it. The kernel allows that walk when the target's capabilities are a subset of the caller's. A full-capability root unit can do the same through `/proc/1/root`. `InaccessiblePaths` alone can't stop that.

So a unit whose `User=` is empty, `root` or `0` also gets **`PrivatePIDs=yes`** (systemd 257+; both hosts run 259). It then gets its own PID namespace and a `/proc` that shows only its own processes, so there is no other root process to walk through.

The preflight refuses a root unit that can't take it. The Lead decides what to do with it. A unit can't take it when:

- it is **`Type=forking`** (systemd doesn't support PrivatePIDs there);
- its directives, or the code the scan reads, use something that needs host PIDs or systemd's private socket.
  `unit_hardening_pidns_re` covers `systemctl`, `systemd-run`, `pgrep`, `pkill`, `pidof`, `killall`, `nsenter`,
  `/proc/<n>/`, `os.kill(` and `psutil`.

Why `systemctl` breaks:

- As root, systemctl talks to PID 1 over `/run/systemd/private`, and its peer check (`bus_check_peercred()`) fails
  when the peer's PID reads as 0. That is what happens in a child PID namespace: the peer (PID 1) has no PID there,
  so systemctl fails with ENODATA.
- Proven 2026-10-29 with transient user units (systemd 259.9). `SO_PEERCRED` showed pid 0, and
  `systemctl --user is-active` failed with "No data available".
- With `SYSTEMCTL_FORCE_BUS=1`, the same call went over the D-Bus broker and worked. As root on the system bus,
  that needs no polkit.

That is the only remedy the role offers: list the unit in `unit_hardening_systemctl_via_bus`, and its drop-in adds
`Environment=SYSTEMCTL_FORCE_BUS=1`. Everything else in the list above needs a code change, or the unit stays out.

The code of the four root candidates (read 2026-10-29):

| Unit | What it runs | PID-namespace check |
|---|---|---|
| ai-openwebui-backup (ai-box) | `ai_backup.py stage`: sqlite3 backup API, tarfile, `os.chown`; no subprocess | Clean. |
| ai-openwebui-pull (PC) | `ai_backup.py pull`: rsync, which runs ssh as its child; `findmnt` reads `/proc/self/mountinfo` (it worked under PrivatePIDs in the test) | Clean. |
| ai-openwebui-restore-check (PC) | `ai_backup.py restore-check`: tarfile, sqlite3 `PRAGMA integrity_check`, `findmnt` | Clean. |
| homelab-ai-skills-reconcile (ai-box) | `skills_reconcile.py`: a TCP connect plus `systemctl is-active` / `start --no-block` / `stop --no-block` on mount units | **Refused unless on the bus list.** It only talks to systemd, never to other processes, so `SYSTEMCTL_FORCE_BUS=1` is enough. Lead's call. |

The live read-only preflight on ai-box gave exactly that refusal, and passed with the unit on the bus list.

What running as PID 1 changes, for these Python oneshots:

- **Signals.** The kernel only delivers a signal to a namespace's PID 1 if it installed a handler for it (SIGKILL is
  the exception). Python has none for SIGTERM, so `systemctl stop` or a `TimeoutStartSec` expiry waits until systemd
  sends SIGKILL after `TimeoutStopSec`. The rest of the unit's processes (rsync, ssh) still get SIGTERM at once, so
  a pull stopped mid-transfer fails right away. A stop during a sqlite copy ends at the SIGKILL, the same outcome as
  SIGTERM without a handler, only later.
- **Child reaping.** Each script waits for its own children (`subprocess.run`). An orphaned grandchild, such as ssh
  if rsync died, stays a zombie until the oneshot ends and the namespace goes away. That's harmless for oneshots.
- **Host PIDs.** None of the four reads another process's `/proc`, signals one, or calls docker.

The verify asserts `PrivatePIDs=yes` (and `SYSTEMCTL_FORCE_BUS=1` where declared) on every root unit. For a root
service that is running, it also checks that the process is in a different PID namespace from the probe's.

#### A /run allowlist (Q8-1)

Reviewer Q proved on ai-box that a root unit with PrivatePIDs could still reach tailscaled without its socket,
through anything under `/run` that trusts root:

- **D-Bus `StartTransientUnit`:** polkit authorises uid 0, so it can start a new, unsandboxed unit that calls the
  local API;
- **container runtimes:** `docker.sock` and `containerd.sock` (a container with a host mount is a full escape). On
  the PC, libvirt and podman are the same;
- **systemd's varlink sockets:** destructive, though not code execution.

So for root units, the per-path hiding becomes an allowlist, the same pattern as ai_control and mission_control:

- `TemporaryFileSystem=/run:ro`: an empty, read-only `/run`;
- `BindReadOnlyPaths=` for exactly the entries in the unit's `run_allow`. Connecting to a read-only bound socket
  isn't a write, so it still works.

`InaccessiblePaths` stays in the drop-in; on the empty tmpfs it finds nothing to hide.

The preflight refuses:

- `run_allow` on a non-root unit (non-root units keep `InaccessiblePaths`; Q found no bypass there);
- a unit in `unit_hardening_systemctl_via_bus` without the bus in its `run_allow`:
  - `/run/dbus/system_bus_socket`;
  - `/run/systemd/system`: systemctl's system scope checks `sd_booted()`, which looks for that directory. AI
    Control's helper binds it for the same reason;
- a root unit whose own unit file already binds more of `/run` than its `run_allow`.

The verify asserts:

- `TemporaryFileSystem` holds `/run:ro`;
- the unit's `/run` binds (`BindPaths` plus `BindReadOnlyPaths`) are exactly its `run_allow`;
- for a running root unit, `/proc/<pid>/root/run` holds nothing but the allowlist and the directories leading to
  it. On ai-box, AI Control's `/run` under this same pattern held only its one bound socket; systemd adds nothing.

What the root units need from `/run` (read 2026-10-29 from their code and units):

| Unit | run_allow | Why |
|---|---|---|
| ai-openwebui-backup (ai-box) | none | `PrivateNetwork=yes`, no subprocess. Logs go to stdout (journald's stream fd, not `/run`). Its one group lookup (`grp.getgrnam`) is answered by NSS `files` (`/etc/group`) before `systemd`'s varlink. |
| ai-openwebui-pull (PC) | none | rsync runs ssh with `-F none`, an explicit key (no `SSH_AUTH_SOCK`, no ControlMaster) and an IP literal for ai-box, so no DNS and no resolver stub. `findmnt` reads `/proc/self/mountinfo` (libmount's optional `/run/mount/utab` may be missing). |
| ai-openwebui-restore-check (PC) | none | tarfile, sqlite3 and `findmnt`; `PrivateNetwork=yes`. |
| homelab-ai-skills-reconcile (ai-box) | `/run/dbus/system_bus_socket`, `/run/systemd/system` | systemctl over the bus. Proven with a transient user unit: under `TemporaryFileSystem=/run:ro` plus PrivatePIDs, `systemctl is-active` failed with nothing bound, and worked with the bus bound and `SYSTEMCTL_FORCE_BUS=1`. |

**ADVISORY.** A root unit's hiding is advisory, never "protected", when it keeps either of these:

- **the system bus:** it can still ask systemd, through StartTransientUnit, to start an unsandboxed unit;
- **CAP_SYS_ADMIN** in its capability bounding set: it can change its own mounts. This reason is kept as a
  precaution. Reviewer Q tested the obvious use in round 9: `umount -l /run` inside such a unit exposed the root
  filesystem's own, empty `/run` directory, not the host's `/run`, and did not reach the socket. skills-reconcile
  stays advisory because of the bus.

The preflight records the reasons from `run_allow` and `CapabilityBoundingSet`. The bus reason fires for any
`run_allow` entry that is the bus socket or a directory above it (Q10-1), so one that slipped past the inputs check
still reads advisory. The verify labels every unit, both in
its summary and in a dedicated "ADVISORY units" line:

- `protected`;
- `advisory (root + system bus)`;
- `advisory (root + CAP_SYS_ADMIN)`;
- `advisory (root + system bus, root + CAP_SYS_ADMIN)`: today that is homelab-ai-skills-reconcile (38
  capabilities, no syscall filter). It is the only root candidate with CAP_SYS_ADMIN: ai-openwebui-backup has two
  capabilities, and the pull and the restore check have none.

It stays hardened anyway: PrivatePIDs, and nothing in `/run` but the bus. A queued ai_stack item will remove its root
and bus need.

The Integrator runs each ai_backup root oneshot once after deploy (they're timer or on-demand jobs, so a converge
never starts them).

### `restart`

- **`true`**: the role runs `systemctl try-restart` (restart only if the unit is running) when the unit's drop-in
  changed. It also restarts a unit whose running process still sees the socket, for example after a run that
  stopped between the write and the restart. Namespace settings only apply at a (re)start; a reload does nothing
  for them. Use this for long-running services.
- **`false`**: the role never restarts the unit; the drop-in applies from its next start. This is **required for
  every `Type=oneshot` unit** (the preflight refuses `true` on one), so the role never forces a backup, digest or
  build run.

### Socket activation (`.socket` vs `.service`)

The drop-in goes on the **service**, never the socket. A `.socket` unit's sandbox options only cover its own
`Exec*` lines; the service it starts gets its namespace from the service unit. `forge-api.socket` keeps the
listening socket bound while `forge-api.service` restarts (the service `Requires=` the socket and isn't
`PartOf=` it), so a restart doesn't close the port. If the service is idle, the next connection starts it
with the drop-in.

## Order (tasks/main.yml)

1. **inputs.yml**: no host access.
2. **preflight.yml** (read-only). It refuses:
   - a unit `systemctl cat` can't find;
   - a unit whose `LoadState` isn't `loaded` (masked or broken);
   - tailscaled not loaded;
   - `restart: true` on a oneshot;
   - a `DynamicUser=yes` unit (the same-uid sweep can't name its uid);
   - a root unit that can't take `PrivatePIDs=yes` (see "Root units");
   - **any use of tailscaled's socket**, found two ways:
     - in the unit's own directives (all files `systemctl cat` prints, except this role's drop-in; no comments;
       no `InaccessiblePaths=`/`TemporaryFileSystem=` lines). For example `BindPaths=/run/tailscale/...`, or
       `ExecStartPre=/usr/bin/tailscale ...`;
     - in the code the unit runs: `files/scan_exec.py` reads every file named on an `Exec*=` or
       `EnvironmentFile=` line, and walks that file's directory and its real directory. That covers the app's own
       tree, for example `/opt/forge-api` for `/opt/forge-api/forge-api`, and `/etc/forge-api` for its config.
       Shared directories (`/usr/bin`, `/usr/local/lib`, `/opt`, `/etc`...) aren't walked. Directory arguments
       (`--dest`, `--volume`) are data and are never walked.

   The scan also refuses when it can't read a file or goes over 4000 files / 256 MiB: nothing would be proven.

   "Use" means (ignoring case):
   - `tailscaled.sock` or `/run/tailscale`;
   - `localapi` or `local-api`;
   - `tailscale.com/client`;
   - `/usr/bin/tailscale`;
   - a quoted `"tailscale"` argv[0];
   - `tailscale <subcommand>`.

   `After=tailscaled.service` and words like "tailscaled hasn't assigned" don't match: ordering on tailscaled, or
   binding the tailnet IP with `IP_FREEBIND`, needs no socket. A false positive (for example a comment saying
   "tailscale ip") refuses the unit; fix the comment in the owning role.

   **Busy check (Q6-4).** Before any restart, the preflight works out exactly what apply.yml will do: a check-mode
   render of the drop-in, plus a look at the running process. If a `restart: true` unit in `unit_hardening_busy_urls`
   would be restarted, it GETs that URL. It refuses while Ollama's `/api/ps` lists a loaded model, or while the
   URL doesn't answer with JSON (it can't tell), unless `-e unit_hardening_allow_busy_restart=true`.

   A converge with nothing to restart never asks. So once Ollama is hardened, later converges pass with models
   loaded.

   Live, read-only, 2026-10-29: the gaming PC was refused with `nomic-embed-text:v1.5, qwen3.5:35b loaded`. The
   Integrator converges the PC when `/api/ps` is empty.
3. **install.yml**: writes the drop-ins, tailscaled's drop-in, the tmpfiles entry and `/run/tailscale`; removes the
   `unit_hardening_remove` drop-ins.
4. **apply.yml**:
   - `daemon-reload` only when a file changed;
   - then `try-restart` for each `restart: true` unit whose drop-in changed, or whose running process
     (`/proc/<MainPID>/root`) still sees the socket.
5. **verify.yml** + **verify_assert.yml** (live). Each unit is read twice, `settle` seconds apart. The checks:
   - **loaded:** our drop-in is in `DropInPaths`, and `InaccessiblePaths` has `-/run/tailscale -/var/run/tailscale`;
   - **still active:** a unit that was active before the change is active at both reads, with the same MainPID
     and NRestarts (no crash loop);
   - **every running non-oneshot service, seen through `/proc/<MainPID>/root` as root:**
     - the namespace root is readable (the positive control);
     - `/run/tailscale` is present and is a mount point in its `mountinfo`;
     - `tailscaled.sock` is **absent**. "Unknown", such as EACCES, never counts as absent;
   - **root units:** `PrivatePIDs=yes`; `TemporaryFileSystem=/run:ro` with exactly its `run_allow` bound; for a
     running root service, its own PID namespace and a `/run` holding only the allowlist; `SYSTEMCTL_FORCE_BUS=1`
     where declared; each unit labelled `protected` or `advisory (root + system bus)`;
   - **a oneshot that is mid-run** and still sees the socket is only noted ("takes effect at its next run");
   - **same-uid sweep (Q6-3):** for every non-root user of a hardened unit, every process on the host running as that
     uid (real, effective, saved or filesystem uid) must show `tailscaled.sock` absent through its
     `/proc/<pid>/root`. That includes processes outside any hardened unit, such as a `sudo -u rgate` shell or an
     owning role's own `systemd-run` check. A mid-run oneshot's processes are tolerated only when every process of
     that uid belongs to such a oneshot, so no other process of the uid exists to walk through them;
   - **the host side:** `/run/tailscale/tailscaled.sock` really is a socket, and `/run/tailscale` is a directory. This
     is the positive control: without the socket, "the unit can't see it" proves nothing;
   - **tailscaled:** `RuntimeDirectoryPreserve=yes`, coming from this role's drop-in;
   - **tmpfiles:** the tmpfiles entry is present;
   - **declared units only:** this role's drop-in is on exactly the declared units. A unit dropped from the list
     keeps its drop-in until it is put in `unit_hardening_remove`.

   No `nobody` dial probe is needed: the check is about the namespace, and it uses no network.

**Check mode** (`--check --diff`): the reads and the whole preflight run for real (`check_mode: false` on the
read-only commands), the drop-ins show their diff, nothing is reloaded or restarted, and the verify is skipped.

To back out a unit, move it to `unit_hardening_remove`, converge, then restart it by hand.

## Needs table

Researched 2026-10-29 from the owning roles' templates and code, and confirmed read-only on the hosts:

- `systemctl cat`/`show`;
- the live preflight scan of the installed code on ai-box and the gaming PC (clean);
- the probe of today's namespaces: every running candidate still sees the socket.

| Unit | Host | Owning role | Needs tailscaled.sock? | Why |
|---|---|---|---|---|
| forge-api.service (+ forge-api.socket) | ai-box | forge_api | **No** | Python serving the socket systemd passes it. IP filter: open-webui's /32 only. No tailscale use in /opt/forge-api or /etc/forge-api. Drop-in on the .service only; the .socket has no Exec. |
| research-gate.service | ai-box | research_gate | **No** | Fetches the public internet; denies 100.64.0.0/10. The local API is the exact bypass of that deny. Only `After=network-online.target`. |
| skill-drafter.service | ai-box | skill_drafter | **No** | Talks to the broker on ai-box's tailnet IP: an ordinary TCP connect through the kernel TUN, no socket needed. |
| finance-digest.service (timer finance-digest.timer) | ai-box | finance_digest | **No** | Oneshot. Money Hub over the tailnet by IP (TCP), the broker, and Notion (public internet). The drop-in goes on the .service; the timer needs nothing. |
| voice-profile.service | ai-box | voice_profile | **No** | Oneshot, on demand. Broker by IPv4 literal. |
| ai-openwebui-backup.service (timer) | ai-box | ai_backup | **No** | Oneshot, `PrivateNetwork=yes`, but `AF_UNIX` is allowed, so the socket is reachable today. Root, with `CapabilityBoundingSet=CAP_DAC_READ_SEARCH CAP_CHOWN`: gets **PrivatePIDs** and an empty read-only `/run` (Q6-1, Q8-1). |
| homelab-ai-skills-reconcile.service (timer homelab-ai-skills-reconcile.timer) | ai-box | ai_stack | **No** | Oneshot, every few minutes. A TCP connect to the PC's SMB port, plus `systemctl`. Root with full capabilities: gets **PrivatePIDs** and a `/run` holding only the bus and `/run/systemd/system` (systemctl over the bus: `unit_hardening_systemctl_via_bus`, Lead's call). **ADVISORY (root + system bus):** the bus can still start an unsandboxed unit. A queued ai_stack item removes the root/bus need. |
| ollama.service | gaming PC | ollama_rocm | **No** | Binds the tailnet IPv4 by retrying after `After=tailscaled.service`: ordering only. The binary (40 MB) has no tailscale reference. Outbound IP is open (`ollama pull`), so this is defence in depth. Restarting it unloads models: converge when the GPU is idle. |
| gpu-gate.service | gaming PC | gpu_gate | **No** | `IP_FREEBIND` on the tailnet address. The only "tailscale" text in its code is a comment. Same uid as ollama (see below). |
| ai-host-agent.service | gaming PC | ai_host_agent | **No** | `IP_FREEBIND`, plus `systemctl show` and `journalctl` over D-Bus / `/run/systemd`, which aren't hidden. Comment-only mention. |
| ai-openwebui-pull.service (timer) | gaming PC | ai_backup | **No** | Oneshot, root, no capabilities. rsync over SSH to ai-box's tailnet IP (TCP). `After=tailscaled.service` is ordering only. Gets **PrivatePIDs** and an empty read-only `/run`. |
| ai-openwebui-restore-check.service | gaming PC | ai_backup | **No** | Oneshot, by hand, `PrivateNetwork=yes` (AF_UNIX allowed). Root: gets **PrivatePIDs** and an empty read-only `/run`. |
| tailscaled.service | both | tailscale | **Yes** (excluded) | Owns the socket. This role only adds `RuntimeDirectoryPreserve=yes` to it. |
| homelab-heartbeat.service | edge-vps | heartbeat | **Yes** (excluded) | `homelab_heartbeat.py` runs `tailscale ping`. |
| ai-control.service, mission-control.service | ai-box | ai_control, mission_control | Hidden already (excluded) | `TemporaryFileSystem=/run:ro` plus their own guards. aictl-helper@.service is a template (refused by name) and also has `/run` as tmpfs. |

Seen, not in the brief, not recommended now:

- ai-box: `ai-library-tailnet-wait.service` (oneshot, polls `ip addr`, no IP filter);
- gaming PC: `ai-library-backup.service` and `pc-control.service`.

### Same-uid rule (Q6-3: now a live check)

A non-root process can open `/proc/<pid>/root/...` of any other process with the **same uid** (ptrace read
access; this is the reason gpu-gate's unit refuses CAP_SYS_PTRACE). That process's namespace still shows the
socket. Reviewer Q proved it on ai-box: as rgate, it reached the socket through research-gate's `/proc/1648/root`
before hardening.

So every service that shares a `User=` must be hardened together:

- `rgate`: forge-api and research-gate;
- `ollama`: ollama and gpu-gate.

The verify's same-uid sweep checks this on every converge: every process of each hardened non-root user must hide
the socket, or the converge fails and names the process. On 2026-10-29 every process of rgate, ollama, sdraft,
aictl-agent, fdigest and vprof belonged to a unit on the recommended lists.

Root units are covered by PrivatePIDs instead: root has too many processes for a sweep to mean anything.

## Recommended inventory

`inventory/host_vars/ai-box.yml`:

```yaml
unit_hardening_units:
  - {unit: forge-api.service, restart: true}
  - {unit: research-gate.service, restart: true}
  - {unit: skill-drafter.service, restart: true}
  - {unit: finance-digest.service, restart: false}
  - {unit: voice-profile.service, restart: false}
  - {unit: ai-openwebui-backup.service, restart: false}
  # Root + system bus: ADVISORY (Q8-1). Its systemctl goes over the bus (needs both binds), so it can take
  # PrivatePIDs. Lead's call: drop this entry and the via_bus line to leave the unit as it is today.
  - unit: homelab-ai-skills-reconcile.service
    restart: false
    run_allow: [/run/dbus/system_bus_socket, /run/systemd/system]
unit_hardening_systemctl_via_bus: [homelab-ai-skills-reconcile.service]
```

`inventory/host_vars/gaming-pc-root.yml`:

```yaml
unit_hardening_units:
  - {unit: ollama.service, restart: true}
  - {unit: gpu-gate.service, restart: true}
  - {unit: ai-host-agent.service, restart: true}
  - {unit: ai-openwebui-pull.service, restart: false}
  - {unit: ai-openwebui-restore-check.service, restart: false}
```

The read-only preflight was run on both hosts on 2026-10-29:

- **ai-box** passes with these lines. Without the `systemctl_via_bus` line it refuses skills-reconcile, and without
  its `run_allow` it refuses the bus line.
- **The gaming PC** passes everything except the busy check: two models were loaded. Converge it when `/api/ps` is
  empty. `unit_hardening_busy_urls` defaults to `gaming_pc_tailnet_ip`, which gaming-pc-root.yml already sets.

## Playbook

Use a new `playbooks/unit-hardening.yml` with two plays: `ai_servers` and `workstations_root`, `become: true`,
`gather_facts: false`, `roles: [unit_hardening]`. Run it **after** the playbooks that own the units:

- research
- finance
- skills
- voice
- ai-backup
- gaming-pc-ai
- ai-control

The preflight refuses a unit that isn't installed yet. That is also why this role shouldn't be appended to each
owning playbook: research.yml would fail on a host where voice.yml hasn't run.

The owning roles re-render their unit files but never touch `<unit>.d/`, so the drop-ins persist across their
converges. An owning role restart applies the drop-in again automatically.

## Limits

- The same-uid sweep is a snapshot taken at converge time. A process of a hardened user that starts later outside
  its unit (`sudo -u`, a cron job, an owning role's `systemd-run` check) isn't seen until the next converge. Threads
  are not enumerated either: the sweep reads `/proc/<pid>/status` of each thread-group leader, and a thread that
  changed its own credentials (`setresuid` on one thread) wouldn't show there.
- A bound socket is bound by inode: if dbus-broker restarted and recreated `/run/dbus/system_bus_socket`, a unit
  already running would keep the old one. That's harmless for skills-reconcile, a fresh oneshot every run.
- Abstract unix sockets aren't files, so `/run` hiding doesn't cover them. They live per network namespace:
  - ai-openwebui-backup and ai-openwebui-restore-check have `PrivateNetwork=yes`, so they see none;
  - ai-openwebui-pull keeps the host network (it needs ai-box) and can see the PC's abstract sockets: X11 and ICE
    (both cookie-authenticated, and the cookies are in a home it can't read), and iSCSI admin;
  - homelab-ai-skills-reconcile keeps ai-box's network and sees iSCSI admin and multipathd's control socket.

  None of these is a route to tailscaled. Listed 2026-10-29, read-only (`ss -xl`).
- The owning roles' own `systemd-run` checks (finance-digest's `--dry-run`, skill-drafter's and voice-profile's
  `status`, forge-api's `check-config`) build transient units from their sandbox lists, which don't include
  this drop-in. They prove the app works, not the hiding. If the owners ever want `InaccessiblePaths` in their own
  sandbox lists, that change is theirs.
- The scan reads the unit's code and config, not the interpreter's libraries (`/usr/lib/python3`). A
  dependency that talks to the local API by itself would be missed. None of these apps import anything outside
  the standard library and their own packages.
- A root unit with CAP_SYS_ADMIN and no syscall filter (homelab-ai-skills-reconcile) can change its own mounts.
  Reviewer Q's round-9 test: `umount -l /run` only exposed the root filesystem's empty `/run`, not the host's, and
  didn't reach the socket. The CAP_SYS_ADMIN label reason stays as a precaution, and the unit stays advisory because
  of the bus. The queued ai_stack item removes both: no root and no bus, or at least `CapabilityBoundingSet=` /
  `SystemCallFilter=~@mount`.

## Tests

`roles/unit_hardening/tests/run.sh` runs offline: no root, no systemd, nothing applied to this machine. It serves
`<tmp>/www/api/ps` on a free loopback port for the busy check, and stops only that server, by its PID.

- `test_filters.py`:
  - the `systemctl cat` parsing (file headers, skipping our own drop-in, line continuations, Exec prefixes,
    `--opt=/path`);
  - the "uses" regex against 12 real-world positives and 9 negatives;
  - the PID-namespace regex against 9 blockers and 7 clean lines (taken from the ai_backup and skills_reconcile
    code);
  - the `Bind*Paths` property parser;
  - the denied-ancestor check: 10 refused (the denied paths and the directories above them), 7 allowed (siblings
    and near-misses such as `/run/systemd/system`, `/run/systemd-x`, `/run/credentialsx`).
- `test_scripts.py`: `ns_probe.py` and `scan_exec.py`, run exactly as the role runs them (`python3 -I -B -c`,
  JSON on stdin), against scratch trees:
  - namespaces: hidden, stale, unreadable (EACCES reads as unknown), no mount, wrong mount, no process;
  - the host socket must be a real socket;
  - its own PID namespace (PrivatePIDs);
  - the `/run` allowlist walk: allowlisted entries and the directories leading to them pass, anything else is
    listed, and bound directories aren't descended into;
  - the same-uid sweep: real/effective/saved uids, names and numeric uids, an unknown user, a process that exits;
  - the scan: helpers deep in the app tree, a symlinked launcher, a match across the 1 MiB read boundary, broad
    directories, limits, unreadable files, every distinct match per regex.
- `test_unit_hardening.yml`, run against a synthetic host built by `make_fixture.py`: users in `etc/passwd`, three
  root units, Ollama, and refusal units. The fake `tests/bin/systemctl` models:
  - `cat` from disk;
  - `show` from the drop-ins loaded at the last daemon-reload: `InaccessiblePaths`, `PrivatePIDs`, `Environment`;
  - `try-restart`: it replaces the process with one whose fake `/proc/<pid>` (root view, mountinfo, status uids,
    cgroup, PID namespace) follows the loaded drop-in.

  The playbook covers:
  - 46 input refusals, and an empty list doing nothing. 20 are about `run_allow`:
    - Q8-1: a string, tailscaled's socket, docker.sock, systemd's private socket, a varlink socket, a user's
      runtime dir, `/run` itself, `..`, a path outside `/run`;
    - Q9-1: `/run/systemd`, both snapd sockets and `/run/snapd/ns`, `/run/credentials` and an entry inside it,
      udev's control socket, `/run/udev`, a deny word in another case, a trailing newline;
    - Q10-1: `/run/dbus`, the directory above the bus socket;
  - 17 preflight refusals:
    - unknown, masked;
    - a oneshot with restart;
    - a `BindReadOnlyPaths=` of the socket;
    - a tailscale `ExecStartPre`;
    - `tailscale ip` in a helper module two levels into the app;
    - a shell script curling the local API;
    - over the scan limit;
    - an unreadable file;
    - DynamicUser;
    - Q6-1: a root unit calling systemctl without the bus, `Type=forking`, a `pgrep` ExecStartPre (the bus doesn't
      excuse it), and the bus list naming a non-root unit;
    - Q8-1: `run_allow` on a non-root unit, the bus without both bus entries in `run_allow`, and a root unit binding
      docker.sock itself;
  - Q10-1: a root unit with `run_allow: [/run/dbus]`, run through the preflight alone (past the inputs), is
    labelled advisory (root + system bus);
  - a full converge:
    - literal renders against `tests/expected/` (plain, root, root + bus);
    - one daemon-reload, and `try-restart` for exactly the restart-true units;
    - the live verify: the root service in its own PID namespace with an empty `/run`, the bus unit's two binds
      and its ADVISORY label, the sweep covering every non-root user;
  - idempotence: no write, no reload, no restart;
  - self-heal: a stale process with an unchanged drop-in is restarted, nothing else;
  - Q6-3: a stray process of a hardened user that still sees the socket fails the verify;
  - Q6-4:
    - a busy Ollama, or one that doesn't answer `/api/ps`, is refused, and nothing is written;
    - the override restarts it;
    - a converge with Ollama busy but already hardened neither asks nor restarts;
    - an idle Ollama is asked, then restarted;
  - retiring a unit;
  - a stray drop-in failing the verify;
  - a unit that dies once hardened failing the still-active check;
  - 46 verify cases on canned readings (41 failure modes, 5 that must pass, the last one checking the
    protected/advisory labels).
- `test_check_mode.yml` with `--check` on a fresh fixture: the preflight runs; nothing is written, reloaded or
  restarted; the verify is skipped.
