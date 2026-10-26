# ai_stack

The ai-box compose stack (docs/CONTRACTS.md §12.10, module H2). Hosts: `ai_servers`, after role `docker`
in `playbooks/ai-server.yml`.

| Service | Image | Published | Memory |
|---|---|---|---|
| `ollama-cpu` | `images.ollama` | nothing (compose network only) | 5g |
| `gpu-broker` | `local_images.gpu_broker`, built on the host | `{{ tailnet_ip }}:11434` | 256m |
| `open-webui` | `images.open_webui` | `{{ tailnet_ip }}:8080` | 1536m |

All three: `restart: unless-stopped`, `memswap_limit` = `mem_limit` (no swap: OOM-killed rather than
thrashing the HDD), `no-new-privileges`, label `homelab.watch=true` (the label the
notifier's alert floor and the dashboard's `homelab-dockerctl` select on; neither runs on ai-box yet, so
today it only marks the containers as homelab-managed), healthchecks (`ollama list`; the broker image's own
`--healthcheck`; Open WebUI's `/health`). The project network `ai` is a plain compose bridge (its subnet
comes from Docker's pinned address pools). Volumes: `ollama-models`, `open-webui-data`.

## What a run does
1. **Preflight:** `tailnet_ip` and `gaming_pc_tailnet_ip` are IPv4, the model variables are unset or plain
   names, and `tailnet_ip` is on the host (tailscale is up).
2. **Layout:** `{{ homelab_stacks_dir }}/ai/` and `/etc/homelab-ai/` (root 0755).
3. **Open WebUI secret:** `{{ homelab_stacks_dir }}/ai/.env` (root 0600) holds only `WEBUI_SECRET_KEY`,
   64 hex chars generated **once on the host** (Python `secrets`, `O_EXCL`, `creates:`). It is never in the
   repo, the vault or the output. Open WebUI reads it through `env_file`. Without it, Open WebUI makes its
   own key inside the container (not on the data volume), so every container recreate would sign everyone
   out. Losing the file costs the same (a re-login), nothing more. No vault variable is needed.
4. **Broker image:** `apps/gpu-broker` is synced to `/opt/homelab-ai/gpu-broker-src` (only `Dockerfile`,
   `.dockerignore`, `requirements.lock` and `gpu_broker/`; `--delete-excluded`). A source hash (every
   file's path + sha256, plus `images.python_base`) is compared with the image's `homelab.source-hash`
   label. It's rebuilt (`docker_image_build`, `PYTHON_IMAGE={{ images.python_base }}`) only when they
   differ: first run, source change, base bump, or a lost/overwritten image. Same gate as role `stacks`.
5. **Broker config** `/etc/homelab-ai/gpu-broker.json` (root 0644), the broker's real keys
   (`gpu_broker/config.py`): `gate_url` `http://{{ gaming_pc_tailnet_ip }}:11500`, `gpu_url`
   `http://{{ gaming_pc_tailnet_ip }}:11434`, `cpu_url` `http://ollama-cpu:11434`, and `aliases`:
   `{assistant: {gpu: ai_gpu_model, cpu: ai_cpu_model}}` only when **both** are set, else `{}`. Everything
   else keeps the broker's defaults. The file is validated before it's installed by the built image itself
   (`docker run --rm --network none … --check`), so a bad config never reaches the running broker. It's
   bind-mounted read-only at `/etc/gpu-broker/config.json` (`GPU_BROKER_CONFIG`), with
   `create_host_path: false`. Its sha256 is a label on the broker container, so a config change recreates
   the broker in the same `compose up` (it reads the config only at start, and an existing single-file bind
   mount would keep the old inode).
6. **Compose:** `compose.yaml` (validated with `docker compose config`), the boot helper's script and unit,
   then, **under the stack lock** (below), `docker_compose_v2` `state: present`, `pull: missing`,
   `build: never`, `wait: true` (600 s: Open WebUI's first start is slow on the HDD).
7. **Heal:** the boot helper's own check in `--heal` mode (no address wait, same lock): any tailnet-published
   service that is up without exactly `tailnet_ip:<port>` is recreated (`compose up --detach --pull never
   --force-recreate --no-deps <services>`). The task is `changed` only when the helper printed a `RECREATE`
   line, so a healthy host stays `changed=0`. In check mode it runs with `--dry-run` and reports what it
   would recreate. Then every binding is read back with `docker port` and asserted. Finally the boot helper
   unit is enabled and started.
8. **Model:** with `ai_cpu_model` set, `ollama show` inside `ollama-cpu`; `ollama pull` only if it's missing.
   Unset: nothing. `ai_gpu_model` is the gaming PC's (role `ollama_rocm`).

A second run is `changed=0`.

## Tailnet publishing at boot
Both ports are published on `tailnet_ip`, which tailscaled adds on its own schedule, possibly after
docker.service has started the containers. **Seen on the real box (docs/CONTRACTS.md §12.9, Integrator
finding 2):** Docker 29 then runs the containers healthy but **without** those port bindings (no error, an
empty `docker port`, no DNAT rule), and `docker restart` doesn't bring them back. Only recreating the
container does. (The repo's other tailnet-only service, wol-relay, binds inside the app and retries, which a
Docker publish can't do.)

So `homelab-ai-stack-start.service` (oneshot, `After=docker.service tailscaled.service`,
`PartOf=`/`Requisite=`/`WantedBy=docker.service`, the same shape as the firewall's DOCKER-USER guard) runs
`/usr/local/sbin/homelab-ai-stack-start` whenever Docker starts:
1. It waits up to 300 s for `tailnet_ip` (bounded; the unit fails if the address never appears, and Docker is
   never called).
2. For each tailnet-published service (`ai_stack_tailnet_publishes` in `vars/main.yml`; the test checks
   it against the compose file's `ports:`), it marks the service for recreation when it is `running` or
   `restarting` but `docker port` doesn't list `tailnet_ip:<port>`, or when it is down with a Docker start
   error (`State.Error`).
3. It runs one `docker compose -f …/ai/compose.yaml up --detach --pull never --force-recreate --no-deps
   <those services>`, then checks the bindings again.

**Stopped on purpose is left alone.** With `restart: unless-stopped`, Docker brings every container back at
boot (and restarts any that exit) except one stopped by hand (`docker stop`, `docker compose stop`, the
dashboard's Stop). So a container that is down (`exited`/`created`) **with no start error** was stopped on
purpose and isn't touched. A container that doesn't exist (`compose down`, `docker rm`) isn't touched either;
the next playbook run recreates it.

**Stack lock.** The helper's check-and-recreate and the playbook's `compose up` and heal never run at
once: both use `flock` on `/run/homelab-ai-stack.lock` (`ai_stack_lock_file`; root-only `/run`, not the
world-writable `/run/lock`) and give up after 300 s
(`ai_stack_lock_wait`) with a message naming who may hold it. Ansible tasks are separate processes, so the
playbook holds the lock through a transient unit, `homelab-ai-stack-lock-<random>` (`systemd-run …
flock --wait 300 <lock> sleep infinity`). The play waits for the unit's "held" marker (its
RuntimeDirectory), runs `compose up`, and stops the unit in an `always:` step. `RuntimeMaxSec`
(`ai_stack_lock_hold_max`: lock wait + compose wait + 120 s = 1020 s) frees the lock even if the play dies mid-run. The heal step then takes the lock
itself, through the helper. Check mode takes no lock (nothing changes); a `--dry-run` doesn't either.

No `set -e`: a failure is reported, the other services are still checked, and the unit fails (visible in
`systemctl --failed`) if anything failed: a lookup, the recreate, or a binding still missing afterwards.
docker.service, its drop-ins and sysctls are untouched.

## Exposure
- **What listens:** only `tailnet_ip:8080` (Open WebUI, login required) and `tailnet_ip:11434` (the broker,
  **no auth**). `ollama-cpu` isn't published. Nothing is on the LAN address, `0.0.0.0` or IPv6.
- **UFW doesn't decide this.** Published ports are DNAT rules, forwarded before UFW's INPUT chain sees them
  (docs/CONTRACTS.md §9b). The `firewall_allow_rules` entries for 8080 and 11434 document the intended
  policy and would apply if a service ever listened on the host directly; the `from` limits on 11434 do
  **not** restrict the DNATed broker today.
- **What restricts it:** (1) the publish address: only packets addressed to `tailnet_ip` are DNATed, and
  tailnet peers reach that address over `tailscale0`; (2) the tailnet policy (who may reach ai-box at all);
  (3) the firewall role's DOCKER-USER guard, which drops tailnet-range sources arriving on any interface but
  `tailscale0`, so a LAN host can't spoof a tailnet peer.
- **Known gap (for module A):** a LAN host that adds a route to `tailnet_ip` via ai-box's LAN address, with
  its own LAN source address, is DNATed too: the guard only checks sources. Closing it takes a DOCKER-USER
  rule on the original destination, for example
  `! -i tailscale0 -m conntrack --ctstate NEW --ctorigdst <tailnet_ip> -j DROP`. Limiting 11434 to the
  mediaserver and gaming PC for real also belongs in DOCKER-USER (`--ctorigdstport 11434`) or in the
  tailnet policy.

## First login (Noah)
1. Open `http://<ai-box tailnet IP>:8080` from a tailnet device and create the first account. Open WebUI
   makes the first account the admin. The role never creates accounts.
2. Close sign-ups: Admin Panel → Settings → General → *Enable New Sign Ups* off, and save. Then set
   `ai_webui_enable_signup: false` (group_vars `ai_servers`) and re-run the playbook, so a fresh data volume
   also starts closed.
   - Open WebUI keeps this setting in its database once the admin saves it, so the admin panel is what
     counts from then on. The variable decides the value until then and on a new volume.
   - Until it's off, anyone on the tailnet who can reach 8080 can sign up, but they get the `pending` role
     (`DEFAULT_USER_ROLE`), so they can't use anything until the admin approves them.

## Variables
| Variable | Default | Meaning |
|---|---|---|
| `ai_cpu_model` | **unset** | CPU model, pulled into ollama-cpu when set (Noah's choice) |
| `ai_gpu_model` | **unset** | GPU model on the gaming PC; used here only for the alias |
| `ai_stack_alias_name` | `assistant` | The broker alias, rendered only when both models are set |
| `ai_webui_enable_signup` | `true` | Open WebUI `ENABLE_SIGNUP` (see First login) |
| `ai_stack_mem_limits` | 5g / 256m / 1536m | Per-container memory limits |
| `ai_stack_ollama_env` | keep-alive 10m, 1 model, 1 request | ollama-cpu environment |
| `ai_stack_compose_wait_timeout` | 600 | `compose up --wait` timeout (s) |
| `ai_stack_tailnet_wait_seconds` | 300 | Boot helper's wait for the tailnet address (s) |
| `ai_stack_lock_file` | `/run/homelab-ai-stack.lock` | Stack lock (boot helper and playbook) |
| `ai_stack_lock_wait` | 300 | Longest wait for the stack lock (s) |
| `ai_stack_lock_hold_max` | lock wait + compose wait + 120 (1020) | Longest a playbook run's lock holder lives (s) |

From host_vars: `tailnet_ip`, `gaming_pc_tailnet_ip`. From versions.yml: `images.ollama`,
`images.open_webui`, `images.python_base`, `local_images.gpu_broker`.

## Tests
```sh
.venv/bin/ansible-playbook -i localhost, -c local roles/ai_stack/tests/test_render.yml </dev/null 2>&1 | cat
```
Renders `compose.yaml` and checks its shape (services, tailnet-only ports, labels, limits, Open WebUI
settings, the config-hash label); renders the broker config for every model combination and validates each
with the broker's own `python -m gpu_broker --check` (from `apps/gpu-broker/.venv`, see that README), and
checks that `--check` rejects a bad file; runs the boot helper (`tests/boot_case.yml`, one case each)
against fake `ip` and `docker` commands: up without a binding, restarting without one, the wrong address
bound, a refused start (all recreated, in one compose call); everything bound, stopped on purpose, removed
(nothing happens); the compose call failing, and a recreate that still doesn't bind (exit 1); and no
address (bounded wait, exit 1, Docker never called). `--heal` cases: recreate, a healthy host (no
`RECREATE` line, so `changed=0`), compose failing, `--dry-run` (reports, calls no compose), and no address
(fails at once, no wait). Every recreate must run with the stack lock held (the fake `docker compose`
checks it with `flock --nonblock`); with the lock held elsewhere the helper times out (1 s in the test) and
calls no Docker command, while a dry run still works. It also checks that the helper's service list matches
the compose file.
No Docker or root needed.
