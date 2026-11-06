# searxng

SearXNG on ai-box for the Skill Forge research gate (docs/CONTRACTS.md §12.16.2, module H4). Hosts:
`ai_servers`, after role `docker`, before role `research_gate` (`playbooks/research.yml`).

| | |
|---|---|
| Image | `images.searxng` (versions.yml, tag + digest; the official `searxng/searxng`, granian, runs as uid 977) |
| Project | `/srv/searxng` (`compose.yaml`, `.env`, `config/settings.yml`), compose project `searxng` |
| Network | own bridge `research` (kernel name `br-research`), subnet `172.29.16.0/28`, gateway `.1`, container `172.29.16.2` |
| Published | `127.0.0.1:8888` → container 8080, nothing else (never the tailnet, the LAN or 0.0.0.0) |
| Used by | role `research_gate`, which connects to `172.29.16.2:8080` directly (see "Why the gate skips the publish") |

## What a run does
1. **Preflight:** `images.searxng` is `name:tag@sha256:<64 hex>`; the publish is 127.0.0.1; the merged settings
   still hold the contract (below); the pinned subnet fits role docker's plan: a /24-/29 inside
   `docker_address_pools_container`, overlapping no Docker pool (`172.20.0.0/14`, `172.24.0.0/14`: ai_stack's `ai`
   network and every other compose network are allocated there), not docker0 (`docker_dns_bip`) and not
   `docker_address_pools_avoid`; gateway and container are distinct usable hosts in it. Role docker's values are read
   with `include_vars` (inventory wins), never copied. Then, read-only on the host, no existing Docker network other
   than `research` overlaps the subnet.
2. **Layout:** `/srv/searxng` and `/srv/searxng/config`, root 0755, each checked first to be absent or a real
   directory (never written through a symlink).
3. **Secret:** `/srv/searxng/.env` (root 0600) holds only `SEARXNG_SECRET=<64 hex>`, generated **once on the host**
   (Python `secrets`, `creates:`), `no_log`. It is never in the repo, the vault, a template or the output, and a
   converge never rotates it. An existing `.env` must be a regular file, checked **before** `creates` (which would
   accept a dangling symlink); the creation itself opens with `O_CREAT|O_EXCL|O_NOFOLLOW` (`searxng_secret_script`),
   so it refuses any existing path, symlink or not. SearXNG reads `server.secret_key` from `SEARXNG_SECRET`; docker compose
   reads `env_file` as root, so the container never needs to read a root-only file. Losing the file only costs SearXNG's cookie signing (a new key on the next run).
4. **settings.yml** (root 0644, no secret in it), `use_default_settings` over the image's defaults:
   - `engines.keep_only`: duckduckgo, brave, wikipedia, wikidata, mdn, github, stackoverflow, arxiv (none needs a
     key). **startpage is dropped** (Reviewer F): upstream marks it `inactive: true` (a proof-of-work captcha);
   - `engines:` overrides (merged by name into the default entries) add `general` to mdn `[general, it]`, github
     `[general, it, repos]`, stackoverflow `[general, it, q&a]` and arxiv `[general, science, scientific
     publications]`: the gate searches with no category, so only `general` engines answer it
     (`searxng_engine_categories`; preflight requires every name to be kept and every list to contain `general`);
   - `search.formats: [html, json]`, autocomplete and favicon resolver off, `safe_search: 0`;
   - `server.limiter: false`, `public_instance: false`, `image_proxy: false`; no valkey; metrics off.
   `searxng_settings_extra` merges over these, but preflight refuses an extra that changes any of the above or sets
   `engines:`. Validated before install **by the pinned image itself** (`docker run --rm --network none
   --read-only --cap-drop ALL ...` imports `searx` with the candidate file: SearXNG's own loader and schema, then the
   formats, the off switches, the engine names and the four engines in `general` are asserted). Its sha256 is the container label
   `homelab.config-hash`, so a change recreates the container in the same `compose up`.
5. **compose.yaml**, validated with `docker compose config`, then `docker_compose_v2` `state: present`,
   `pull: missing`, `wait: true` (the healthcheck must pass).
6. **Checks** (not in check mode): a field-limited `docker inspect --format` (only the checked fields, never
   `Config.Env`, which holds `SEARXNG_SECRET`; `no_log` too) shows the container's only network is `research` at `172.29.16.2`; user `977:977`,
   read-only root, `CapDrop [ALL]`, no `CapAdd`, `no-new-privileges`, not privileged, the pids limit;
   `docker port` is exactly `8080/tcp -> 127.0.0.1:8888`; `/healthz` answers 200 on `127.0.0.1:8888` **and** on
   `172.29.16.2:8080`; `/search?q=test&format=json` answers 200 with SearXNG's JSON (`query`, `results`). The result
   count is only printed (engines need the internet; an unreachable one is listed in `unresponsive_engines`).

A second run is `changed=0`.

## Container hardening
- `user: "977:977"` from the first process. The image has no `USER`, so by default its entrypoint runs as root,
  `chown -R`s the config and data dirs and runs `update-ca-certificates`, and granian would stay root. As 977 the
  entrypoint skips all three (they are guarded by `id -u = 0`), so **no capability is needed: `cap_drop: [ALL]`, no
  `cap_add`**. `FORCE_OWNERSHIP=false` as well.
- `read_only: true`. The only writable paths are tmpfs: `/tmp` (SearXNG's sqlite caches) and `/var/cache/searxng`
  (its data dir; the image declares it a `VOLUME`, so the tmpfs also stops an anonymous volume per recreate), each
  64m, uid/gid 977, 0700. `/etc/searxng` (the other `VOLUME`) is the read-only bind of `config/`.
- The entrypoint prints a harmless `"/etc/searxng" directory is not owned by "searxng:searxng"` warning: the host
  dir is root-owned on purpose (uid 977 must not be able to write it). It only reads the file.
- `no-new-privileges`, `mem_limit` = `memswap_limit` = 384m (ai-box has 8 GB; ai_stack budgets about 6.8 GB),
  `pids_limit: 128`, `restart: unless-stopped`, `GRANIAN_HOST=0.0.0.0` (IPv4 only), healthcheck
  `wget -q -O /dev/null http://127.0.0.1:8080/healthz` (busybox/wget are in the image).
- 127.0.0.1 is always up at boot, so the late-address problem of §9b (a publish silently dropped) can't happen here;
  no boot helper is needed.

## Why the gate skips the publish
With `userland-proxy: false`, `127.0.0.1:8888` is a DNAT in nat OUTPUT. systemd's IP filter checks the destination
of outgoing packets **and the source of incoming ones**. Going out, the gate's packet already carries the container
address (DNAT happened in OUTPUT); coming back, conntrack undoes the DNAT before the packet reaches the socket, so the
reply's source is 127.0.0.1 again, which the gate's `IPAddressDeny` (loopback) drops. Connecting straight to
`172.29.16.2:8080` needs no NAT at all, so the gate's allow list is exactly that /32 in both directions. Host to
container traffic doesn't pass DOCKER-USER or Docker's raw-table drop for non-bridge ingress (both are on the
forwarding/PREROUTING path; locally generated packets take OUTPUT).

## Inputs
- `images.searxng` in `inventory/group_vars/all/versions.yml` (Lead): `docker.io/searxng/searxng:<tag>@sha256:<digest>`.
- Optional: `searxng_subnet` / `searxng_gateway` / `searxng_ipv4` (if the host already has a network there),
  `searxng_settings_extra`, `searxng_mem_limit`. Role `research_gate` reads `searxng_ipv4` and
  `searxng_container_port` (inventory first, else these defaults through `include_vars`).

## Host checks for the Integrator
- `curl -s 'http://127.0.0.1:8888/search?q=test&format=json' | python3 -m json.tool | head` on ai-box shows results.
- A plain search (no category, as the gate sends it) uses the `general` overrides:
  `curl -s 'http://127.0.0.1:8888/search?q=python+subprocess+timeout&format=json' | python3 -c 'import json,sys; print(sorted({e for r in json.load(sys.stdin)["results"] for e in r.get("engines", [])}))'`
  lists stackoverflow and/or github/mdn beside the web engines, and never startpage. The settings `validate` (the
  image's own loader) runs only on the host: a failure there names the bad key.
- **Not reachable** from the LAN or the tailnet: from the gaming PC, `curl -m3 http://<ai-box tailnet ip>:8888/` and
  `curl -m3 http://<ai-box lan ip>:8888/` both fail; `ss -ltn` on ai-box shows nothing on 8888 (it is a DNAT, not a
  bind); `iptables -t nat -S DOCKER | grep 8888` shows only `-d 127.0.0.1/32`. From a LAN host with a static route
  to `172.29.16.0/28` via ai-box's LAN address, `172.29.16.2:8080` must not answer (Docker 28+'s raw PREROUTING drop
  for container addresses from non-bridge interfaces): check `iptables -t raw -S PREROUTING | grep 172.29.16.2`.
- `docker logs searxng` shows granian listening on `0.0.0.0:8080`, no `ultrasecretkey` error, no permission errors
  (if a write fails on the read-only root, add a tmpfs for that path and record it here).
- `docker inspect searxng --format '{{.HostConfig.CapAdd}} {{.HostConfig.CapDrop}} {{.Config.User}}'` is
  `[] [ALL] 977:977`.
- Container DNS works: the search returns results (the embedded resolver forwards to the host stub on
  `docker_dns_ip`, which UFW allows from `172.16.0.0/12`; DOCKER-USER drops direct port-53 traffic).
- The proof for the gate's path (as root): `systemd-run -P -p IPAddressDeny=127.0.0.0/8 -p IPAddressAllow=172.29.16.2/32
  curl -m5 -s http://172.29.16.2:8080/healthz` prints `OK`, while the same with `http://127.0.0.1:8888/healthz`
  times out. That pins the design above.
- Second run `changed=0`.

## Tests
```sh
roles/searxng/tests/run.sh       # from anywhere; uses the repo's .venv; no root, no Docker
```
`run.sh` first runs `check_no_log.py` (every task in `tasks/*.yml` that generates or names the secret is
`no_log: true`), then `test_searxng.yml` with `-vvv --diff`, a fake `docker` on PATH (`tests/bin/docker`, answers only
the network listing) and the role's filter plugins. It covers:
- **preflight:** passes on a converged-ai-box network list and on a re-run (its own `research` network present), and 21
  refusals: an image without a digest, without a tag, missing; a 0.0.0.0 publish; a subnet in a Docker pool, in
  docker0, in hl-public, outside the container range; a /30; the container on the gateway, the broadcast, outside its
  subnet; settings extras that turn the limiter on, put a `secret_key` in the file, drop json, add an engine list,
  turn autocomplete on; a category override for an engine that isn't kept, or without `general`; startpage back in
  the list; an existing host network that overlaps;
- **layout + secret:** generated once (0600, `SEARXNG_SECRET=<64 hex>`, checked by a hidden-output script), the same
  inode and content on a re-run; a symlinked project dir, config dir and `.env`, and a **dangling** `.env` symlink are
  each refused with the victim untouched (the dangling link's target is never created); and the creation script on
  its own refuses a dangling link and an existing file (`FileExistsError`, nothing created or rewritten);
- **settings.yml and compose.yaml** rendered and checked key by key (in place of `docker compose config`): only the
  reviewed service keys (no `cap_add`, `privileged`, `network_mode`, devices), user, read-only, tmpfs, limits, env,
  the single read-only bind with `create_host_path: false`, the loopback-only publish, the pinned network and IPAM, the
  healthcheck and the config-hash label; a settings change changes the hash; the four `general` overrides, no startpage;
- **verify.yml** against the fake docker (its inspect fixture's `Config.Env` holds a canary `SEARXNG_SECRET`; `--format`
  is evaluated for `{{json .Path}}`) and a fake SearXNG on loopback (`fake_searxng.py`): passes on the good container,
  and refuses one with `CapAdd: [NET_ADMIN]` at the hardening assert.

After the run, `run.sh` reads the secret the test generated and fails if it appears in the output as text, as base64
of the value (with and without LF), or as base64 of the whole `.env` line (slurp's form); and it fails if the inspect
canary appears as text or base64 (checked even when the play failed). A full `docker inspect` in verify.yml (the
Reviewer F finding) is caught by it.
