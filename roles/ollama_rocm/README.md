# Role `ollama_rocm`

Native Ollama (ROCm) on the gaming PC for ai-box's gpu-broker (`docs/CONTRACTS.md` §12.10). Applied by
`playbooks/gaming-pc-ai.yml` (`become: true`), before role `gpu_gate`.

| What | Where |
|---|---|
| Binary | `/usr/local/bin/ollama`, a real file (`bin_t`) |
| Libraries | `/usr/local/lib/ollama`, a symlink to `.ollama-<version>-<random>/lib/ollama` (the live release tree under `/usr/local/lib`, `lib_t`) |
| Install marker | `/usr/local/lib/ollama/.homelab-release` (inside the live tree) |
| Account | system user/group `ollama`, home `/var/lib/ollama`, also in `render` and `video` |
| Models | `/var/lib/ollama/models` (`OLLAMA_MODELS`) |
| Unit | `/etc/systemd/system/ollama.service` |
| Firewall | firewalld policy `homelab-ai-only` (ANY → HOST, priority -100). tailscale0 stays in zone `trusted`. |

Inputs (host_vars, git-ignored): `gaming_pc_tailnet_ip`, `ai_box_tailnet_ip`, `ai_box_tailnet_ip6`. The
ai-box addresses must be single addresses (asserted: no prefix length, no range). `ai_gpu_model` has **no
default**; unset, null or empty means nothing is pulled.

## Install (staged, then atomic swaps)
1. Both tarballs (`ollama-linux-amd64.tar.zst`, then `-rocm`) are downloaded with `get_url` and their pinned
   sha256 into `/var/cache/homelab-ollama`. A mismatch fails here, before anything is touched.
2. A new tree `/usr/local/lib/.ollama-<version>-<random>` is created (`tempfile`). It sits under
   `/usr/local/lib`, so it is `lib_t` from the start, never the cache's `var_t`. Both tarballs are
   extracted into it with `--no-same-owner`, so files are root-owned whatever the archive says. Only
   the members `bin/ollama` and `lib/ollama` are extracted. The unused `lib/ollama/cuda_*` backends are
   removed and the marker is written.
3. The tree's `bin/ollama` is copied over `/usr/local/bin/ollama`. `copy` writes a temp file beside it
   and renames it over the old one, so the swap is atomic and the file gets its default context, `bin_t`.
   Then the `/usr/local/lib/ollama` symlink is replaced. `file` writes a temp link and renames it over
   the old one, so that swap is atomic too.
4. The handler restarts ollama after the swap. Every run removes any `.ollama-*` tree that isn't live: the
   previous release, or a tree left by an interrupted run. The tarballs (about 2.5 GB) are deleted.

"Installed" means `lib/ollama` is a symlink, its marker names the pin, and `/usr/local/bin/ollama` is
byte-identical to the live tree's `bin/ollama`. So a half-finished swap or an edited binary is redone.
Migration: a plain `lib/ollama` directory, such as the upstream installer leaves, is removed once
before the first symlink. A directory can't be swapped atomically. Ollama finds its libraries at
`<dir of the executable>/../lib/ollama`, which is the symlink.

## Backend: `ollama_backend` (default `vulkan`)
Decided by Noah after the live check in CONTRACTS §12.9, "§12.7 risk CONFIRMED live, then resolved":
- **ROCm:** compute runs on `/dev/kfd` user queues, which DRM fdinfo never accounts, so gpu-gate's
  `ollama_share` stayed 0.
- **Vulkan (Mesa RADV):** shows `drm-engine-compute` advancing, at about 9% fewer tok/s.

| `ollama_backend` | Extra `Environment=` in the unit | Host packages (x86_64, asserted) |
|---|---|---|
| `vulkan` | `OLLAMA_VULKAN=1`, `HIP_VISIBLE_DEVICES=-1`, `ROCR_VISIBLE_DEVICES=-1` | `mesa-vulkan-drivers`, `vulkan-loader` |
| `rocm` | none | none |

- **Validation:** any other value fails the run (`tasks/validate.yml`).
- **Library check:** after the install, `tasks/backend_installed.yml` asserts that
  `lib/ollama/vulkan/libggml-vulkan.so` exists for `vulkan`. Without it Ollama silently runs on the CPU.
- **`ollama_rocm_extra_env`:** merged over the backend's variables; a key there wins (on purpose).
  Asserted:
  - it's a mapping;
  - names match `^[A-Za-z_][A-Za-z0-9_]*$` and aren't the role's own (`HOME`, `OLLAMA_HOST`,
    `OLLAMA_KEEP_ALIVE`, `OLLAMA_MAX_LOADED_MODELS`, `OLLAMA_MODELS`), so it can't widen the
    tailnet-only bind;
  - values are strings or numbers with no whitespace, control character (NUL included: systemd ends
    the line there, so it could inject a directive), quote, backslash or `%`, so nothing can add a
    directive or change how systemd parses the line.

  All input regexes live in `vars/main.yml` and are anchored with `\Z`, not `$`, because `$` also
  matches before a trailing newline.
- **Changes:** a change to either restarts ollama (the unit template's handler).
- **Devices:** both backends keep `DeviceAllow=char-drm` (Vulkan uses `/dev/dri/renderD*`) and
  `/dev/kfd`, so switching back is just a variable change.
- **Packages are asserted, not installed:** Fedora's Mesa subpackages must all be the same version, so
  a `dnf install mesa-vulkan-drivers` in the middle of a converge could upgrade the desktop's whole
  graphics stack. Fedora Workstation ships both packages anyway (installed on this PC:
  mesa-vulkan-drivers 26.2.4, vulkan-loader 1.4.341). If one is missing, the failure message gives the
  `dnf` command.

## Model pull
Only when `ai_gpu_model` is set (not null or empty) and the model isn't listed yet.
- **Name form:** `ai_gpu_model` must be a plain name, `[host/][namespace/]model[:tag]`, with no digest
  and no registry port (asserted).
- **"Already pulled":** the name is compared, in lower case, in the short form that `/api/tags` lists:
  without `registry.ollama.ai/` and `library/`, and with `:latest` added when there is no tag. So a
  fully qualified or untagged name doesn't re-pull on every converge.
- **Wait for the server:** up to 3 min for the server to answer on the tailnet.
- **Bound the pull:** one `ollama pull` under `timeout --kill-after=30 {{ ollama_rocm_pull_timeout }}`
  (default 7200 s). Both bounds must be positive whole seconds (asserted; 0 is refused).
- **On timeout:** the run fails with "was still running after N s and was stopped". That covers TERM
  (rc 124) and KILL (rc -9/137). The next run resumes the partial download.
- **Other errors:** report the rc and stderr.

## Binding at boot
`OLLAMA_HOST` is the tailnet IPv4. Ollama can't bind it until tailscaled has configured the address,
and Ollama has no `IP_FREEBIND`. `After=tailscaled.service` only orders the start. The unit retries a
failed start with a growing delay instead: `Restart=on-failure`, `RestartSec=2s`, `RestartSteps=8`,
`RestartMaxDelaySec=60s`, and `StartLimitIntervalSec=0`, so it never gives up. Expect one or two
"cannot assign requested address" starts in the journal at boot. This was chosen over an
`ExecStartPre` wait loop: that needs `AF_NETLINK` plus a shell in the sandbox (or a `+` root prefix),
and it still needs the restart policy for a tailscaled that comes up late.

## SELinux (Fedora, enforcing)
No custom policy, port label or boolean.
- **Labels:** `/usr/local/bin/ollama` is `bin_t` and everything under `/usr/local/lib` is `lib_t`, by
  default file contexts (checked with `matchpathcon`).
- **How the install keeps them:** the tree is created under `/usr/local/lib`, and the binary is written
  by an atomic rename that applies its default context. A `restorecon -R -v` on the binary, the symlink
  and the live tree (by its real path, because restorecon doesn't follow the link) only repairs drift.
- **Domain:** systemd (`init_t`) runs a `bin_t` executable as `unconfined_service_t`. Checked on this
  PC: `tailscaled`, also a `bin_t` binary started by systemd, runs as `unconfined_service_t`. That domain
  may bind any port, use `/dev/kfd` (`hsa_device_t`) and `/dev/dri/*` (`dri_device_t`), and write
  `/var/lib/ollama` (`var_lib_t`).
- **To verify on the host (Integrator):** `ps -eZ | grep ollama` shows `unconfined_service_t`, and
  `ausearch -m avc -ts recent` is empty after a model load.

## Firewall: policy `homelab-ai-only` (CONTRACTS §12.9, Module G review M1)
Zone rules only see their own zone's interfaces. A LAN host can add `ip route add <tailnet-ip>/32 via
<gaming-pc-lan-ip>`; its packets then arrive on the LAN interface (zone `public`), never on tailscale0.
So the ports are guarded by a **policy** instead:

| Setting | Value |
|---|---|
| ingress-zones | `ANY` (every zone, so tailscale0, the LAN and Wi-Fi) |
| egress-zones | `HOST` (traffic to this machine) |
| priority | `-100` (negative: before the zones) |
| target | `CONTINUE` (anything not rejected goes on to the zone rules as before) |

```
rule family="ipv4" source NOT address="<ai_box_tailnet_ip>" port port="11434" protocol="tcp" reject
rule family="ipv4" source NOT address="<ai_box_tailnet_ip>" port port="11500" protocol="tcp" reject
rule family="ipv6" source NOT address="<ai_box_tailnet_ip6>" port port="11434" protocol="tcp" reject
rule family="ipv6" source NOT address="<ai_box_tailnet_ip6>" port port="11500" protocol="tcp" reject
```
Each rule names its tcp port, so no other port is affected.

**Why it runs before `trusted`'s ACCEPT.** Source: firewalld **2.4.4**, the version installed here
(`python3-firewall`), file paths under `firewall/core/`:
- **Zones are policies too.** Every zone is applied as derived policies with priority
  `ZONE_POLICY_PRIORITY = 0` (`fw_zone.py:34`). 0 is reserved for them (`io/policy.py:770`,
  `priority_reserved = [0]`), and new policies default to -1 (`base.py:12`, `DEFAULT_POLICY_PRIORITY`).
- **One dispatch chain.** In the nftables backend all filter-INPUT dispatch goes through one chain,
  `filter_INPUT_POLICIES` (`nftables.py:855`, `:867`). Its rules are ordered by
  `_policy_dispatch_sort_key` (`nftables.py:1172`).
- **The sort key.** It starts with the ingress zone, interface and source, then the egress zone, and
  ends with `suffix = (last_sort_order, priority)` (`nftables.py:1241`).
- **The zone's ACCEPT comes last.** The zone's own target (ACCEPT for `trusted`) is the zone policy's
  "last" rule, `last_sort_order = 2` (`nftables.py:1225`, `:1314`). An ordinary policy jump has 0.
- **ANY covers every zone.** `ingress-zones ANY` is expanded into each active zone
  (`fw_policy.py:1872`). Our jump therefore gets the same ingress/egress key as that zone's own rules,
  and it sorts first: `(0, -100)` < `(0, 0)` < `(2, 0)`.
- **Before the dispatch chain.** The base chain only accepts established/related traffic and `lo`, and
  drops invalid packets (`nftables.py`, filter_INPUT, before the jump at `:867`). A new connection to
  11434/11500 always reaches our rules first.
- **Upstream docs.** Upstream documents policy priority in firewalld.policies(5). That page isn't
  installed here, so the ordering above is cited from the code, not from the man page.

Tailscale's own `ts-input` accept lives in a different nftables table. It doesn't override this reject,
because every base chain on the input hook must accept a packet.

**How it's applied.** ansible.posix 2.2.2's `firewalld` module has no policy support, so
`tasks/firewall.yml` drives `firewall-cmd`:
1. **Permanent config.** Create the policy if it's missing. Then apply the computed differences
   (`ollama_rocm_policy_ops`): priority, target, zones (other zones out before ANY/HOST go in; firewalld
   refuses to mix them), and rules, with missing ones added before stale ones are removed. Then remove
   every zone rich rule on 11434/tcp or 11500/tcp in any zone. That covers the first build's
   zone-`trusted` rules and hand edits.
2. **One `firewall-cmd --reload`.** It runs when step 1 changed anything, or when the runtime differs
   (the policy is missing at runtime, its zones or rules differ, or a runtime zone rule on our ports
   exists). Reload rebuilds the runtime from the permanent config, so runtime-only stale rules go too,
   and established connections are kept.
3. **Read-only asserts.** The policy is in `--get-active-policies`, and its runtime zones and rules are
   exactly the declared ones.

By design a converged run makes no change and no reload (the Integrator proves `changed=0` on the host). In check mode nothing is applied; a debug task reports what
would change (marked changed).

Loopback is accepted before the policy dispatch, so gpu-gate still reaches Ollama on the tailnet IP
locally.

## Sandbox
`systemd-analyze security --offline=yes` reports **1.6 OK**. GPU access uses `DevicePolicy=closed`,
`DeviceAllow=/dev/kfd rw` and `char-drm rw` (not `PrivateDevices=`), plus `SupplementaryGroups=render
video`. `MemoryDenyWriteExecute=` is left out until it's proven with the ROCm runtime on the host.

## Tests
```sh
.venv/bin/ansible-playbook -i localhost, -c local roles/ollama_rocm/tests/test_install.yml </dev/null 2>&1 | cat
.venv/bin/ansible-playbook -i localhost, -c local roles/ollama_rocm/tests/test_render.yml </dev/null 2>&1 | cat
```
- **`test_install`** runs `tasks/install.yml` unprivileged against fixture tarballs over `file://`. It
  covers:
  - migration from a plain upstream `lib/ollama`;
  - the member restriction and the CUDA prune;
  - the exact tree, symlink and binary layout;
  - a no-op re-run, and repair of an edited binary;
  - an upgrade that leaves exactly one tree;
  - removal of an interrupted staging tree;
  - a bad checksum that leaves the previous install intact.
- **`test_render`** renders the unit for `vulkan` (with its exact `Environment=` list) and `rocm`, and
  runs `systemd-analyze verify` on both (any warning fails). It also checks:
  - that `ollama_rocm_extra_env` overrides a backend variable;
  - `tasks/validate.yml`, 48 cases, each must pass or fail with the right message:
    - backends: `cuda`, `VULKAN` and `""` fail, as do an i686-only `vulkan-loader` and a missing
      `mesa-vulkan-drivers`;
    - `ollama_rocm_extra_env`: non-mappings, the five reserved names, bad names (including one with a
      newline), and values with a newline-injected `ExecStartPre=`, a space, a tab, quotes, a backslash,
      `%`, a bool or a list;
    - pull bounds: 0, negative, 1.5, `"2h"` and `true`;
    - model names: a digest, a registry port, a space, `../`;
  - `tasks/backend_installed.yml`: the library is present, missing, or a directory, and rocm needs none;
  - the short-name normalisation;
  - `tasks/pull.yml` against a stub `/api/tags` server with a recording client: fully qualified,
    untagged, `library/` and mixed-case names that are listed don't pull, and unlisted ones do;
  - `tasks/pull_model.yml` with stand-in clients: success, failure (rc and stderr), a hang (rc 124) and
    a TERM-ignoring hang (KILL);
  - the exact rich-rule strings;
  - `ollama_rocm_policy_ops` for a new, a converged, a drifted and a runtime policy (order included);
  - the legacy-rule finder across zones (ports and protocols that only look alike are ignored);
  - the single-address assert.

  Where `python3-firewall` is installed, it runs firewalld's own parser (every rule prints back
  unchanged) and its policy validator. That validator accepts the declared policy, and the test proves
  it really rejects a reserved priority and ANY mixed with a zone.
