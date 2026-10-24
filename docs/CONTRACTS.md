# Build contracts: homelab-infra MVP

This is the single spec for the MVP build. Builders own exactly the paths listed for their module and conform to every interface here. **Changing an interface goes through the Lead** (team rules). A module is done only when the **Reviewer** marks it APPROVED. The **Integrator** proves that the whole thing deploys to the real host and that a second run reports `changed=0`.

## 0. Target and ground rules
- Host `mediaserver` (inventory group `media_servers`): Ubuntu 26.04, kernel 7.0, Docker 29.5 + compose v5, Tailscale 1.102 (Tailscale SSH **off**), Python 3.14. The app layer was wiped clean 2026-10-23. Ansible connects as `{{ automation_user }}` (passwordless sudo, created by `playbooks/bootstrap.yml`).
- **Public repo.** Never commit identifiers (hostnames, IPs, tailnet names, domains, emails, MACs, UUIDs) or secrets. Use the variables below. Identifiers live in `inventory/host_vars/*.yml` (git-ignored). Secrets live in `inventory/group_vars/media_servers/vault.yml` (ansible-vault).
- Every role: idempotent (second run `changed=0`), `ansible-lint` production profile clean, `yamllint` clean, FQCN modules, handlers for restarts, no `shell`/`command` without `changed_when`/`creates`.
- Image versions come only from `images.*` / `local_images.*` in `inventory/group_vars/all/versions.yml`. Never hard-code a tag.
- Run tools with redirected stdin (`</dev/null 2>&1 | cat`). Ansible refuses non-blocking stdio in this environment.
- Builders **do not run playbooks against the host.** Only the Integrator does. Builders may run `--syntax-check`, `ansible-lint`, `yamllint` and unit tests locally.

## 1. Variables (already defined; read-only for builders)
| Variable | Meaning |
|---|---|
| `homelab_root` `/srv/homelab`, `homelab_stacks_dir`, `homelab_data_dir`, `homelab_backups_dir`, `homelab_push_dir`, `homelab_incomplete_dir` | Layout (§2) |
| `media_mount` `/media` | Media drive mount point |
| `admin_user` (mediaadmin), `automation_user` (ansible), `backup_pull_user` (backuppull) | Accounts |
| `mediasvc_user`/`mediasvc_uid` 1001/`mediasvc_gid` 1001, `media_group`/`media_gid` 1002 | Media file ownership (must match existing files exactly) |
| `timezone` | Host timezone |
| host_vars: `lan_iface`, `lan_ip`, `lan_cidr`, `wifi_iface`, `tailnet_ip`, `tailnet_fqdn`, `media_uuid`, `gaming_pc_lan_ip`, `gaming_pc_tailnet_ip`, `public_watch_host`, `admin_pubkeys` (list), `backup_pull_pubkey` | Identifiers |
| `rehearsal` (bool), `bootgate_wait_seconds` 720, `bootgate_powercycle_method` reboot\|rtcwake, `bootgate_retry_minutes` 5 | Behavior |
| `cloudflared_tunnel_token`, `wol_relay_token`, `vapid_public_key`, `vapid_private_key` (raw base64url, P-256) | Secrets (vault) |

## 2. Filesystem layout on the host
```
/srv/homelab/                      root:root 0755
  stacks/<stack>/compose.yaml      root:root 0644  (rendered by role stacks)
  stacks/<stack>/.env              root:root 0600  (secrets for that stack only)
  data/<app>/                      app config; owner per app (linuxserver apps: 1001:1002, because the images chown /config to PUID:PGID on every start)
  downloads-incomplete/            1001:1002 2775  (SABnzbd scratch, on the NVMe)
  backups/appdata/                 root:backuppull 0750, archives 0640 (role backups)
  push/                            push-core data (§5), 0750
/media                             media drive (UUID media_uuid), mounted by the boot gate
  .homelab-media-sentinel          sentinel file created by role storage (root 0644)
  tv/ movies/ downloads/complete/  1001:1002 2775
/var/lib/bootgate/                 boot gate state (§3)
/run/bootgate/                     runtime trigger dir (§3)
/opt/homelab-notify/               notifier venv (§4)
```

## 3. Boot gate (module B: `storage`, `bootgate`)
- **The fstab line for the media drive is removed** (`storage` role). Only the gate mounts `/media`, via a systemd mount unit `media.mount` (What=/dev/disk/by-uuid/{{ media_uuid }}, Type=ext4, Options=defaults,noatime) with **no `WantedBy`**. The gate starts it explicitly.
- `bootgate.service`: `Type=oneshot`, `RemainAfterExit=yes`, runs `/usr/local/sbin/bootgate check --boot`. It runs early (`After=systemd-udevd.service local-fs.target`), and **must not** require the network.
- **docker drop-in** `/etc/systemd/system/docker.service.d/10-bootgate.conf`: `Requires=bootgate.service` + `After=bootgate.service`. Docker (and so every container) waits for the gate, and doesn't start if it fails. Also covers `docker.socket` activation.
- `bootgate check --boot` algorithm:
  1. Wait up to `bootgate_wait_seconds` for `/dev/disk/by-uuid/{{ media_uuid }}`.
  2. Start `media.mount`.
  3. Verify: `findmnt -n -o UUID /media` == uuid **and** the sentinel exists **and** a write+delete test in `/media` succeeds.
  4. On failure, run the repair ladder, logging each step: `udevadm trigger --action=add --subsystem-match=block` + settle; USB re-bind of the drive's port (sysfs `authorized` 0/1, or unbind/bind); retry the mount; `fsck -n` (**read-only, never -y**).
  5. Still failing: if `/var/lib/bootgate/attempts` < 1 → write attempts=1, append history, power-cycle (`reboot`, or `rtcwake -m off -s 60` when `bootgate_powercycle_method=rtcwake`). If attempts ≥ 1 → **no reboot**; write status `failed`, exit 1 (docker stays down), and `bootgate-alert.service` fires.
  6. On success: attempts=0, status `ok`, exit 0.
- `bootgate check --retry` (from `bootgate-retry.timer`, every `bootgate_retry_minutes`, only acts when status is `failed` or `media-lost`): steps 1–3 with a short wait (30 s), **never reboots**. On success it sets status `ok`, then `systemctl start docker.service` (and `docker compose up -d` of the media stack if status was `media-lost`), and pushes "media drive back".
- **Drop-out watcher** `bootgate-watch.timer` (every 60 s, only when status `ok`): if the verify fails → `docker compose -f {{ homelab_stacks_dir }}/media/compose.yaml stop`, status `media-lost`, history entry, push "media drive lost; media apps stopped".
- `bootgate-alert.service` (OnFailure of bootgate.service; `After=network-online.target`, `Wants=network-online.target`): `homelab-notify --tag bootgate --title "Media drive failed" --body "..."`.
- **Contract 1 (history file):** `/var/lib/bootgate/history.jsonl`, one JSON object per event:
  `{"ts": ISO8601, "boot_id": str, "event": "pass|repair|powercycle|fail|retry-pass|media-lost", "detail": str, "wait_seconds": float}`. `/var/lib/bootgate/status` holds one word: `ok|waiting|failed|media-lost`.
- **Contract 2 (re-run trigger):** creating `/run/bootgate/rerun` (dir mode 0770, group `bootgate`) triggers `bootgate-rerun.path` → `bootgate-retry.service`. The service deletes the file.

## 4. Notifier (module B: role `notifier`)
- CLI `/usr/local/bin/homelab-notify --title T --body B [--tag TAG] [--test]`. It sends a Web Push to every subscription in `{{ homelab_push_dir }}/subscriptions.json` using `{{ homelab_push_dir }}/vapid.json`.
- It's a Python venv at `/opt/homelab-notify` with `pywebpush` (pinned). Runs as root. Exit 0 if at least one push was delivered, 2 if there are no subscriptions, 1 on error. It removes subscriptions that return 404/410. Logs to journald (`SyslogIdentifier=homelab-notify`).
- **Dedupe:** with `--tag`, it doesn't resend the same tag+title within 30 min (state in `/var/lib/homelab-notify/`).
- **Rehearsal:** when `/etc/homelab/rehearsal` exists, it prefixes the title with `[REHEARSAL]` and sends only to subscriptions whose `label` is `test`.
- **Alert floor** (role `notifier`): `homelab-alertfloor.timer` every 2 min. For each container labeled `homelab.watch=true` it pushes once per incident on: health `unhealthy` for 2 consecutive checks, state `exited`/`dead` while not intentionally stopped, or restart count rising by 3+ between checks. It pushes "recovered" when the condition clears. Skipped when bootgate status ≠ `ok`.

## 5. Push core (module D: `apps/push-core/`, deployed by module C)
- **Contract 4 (push store):** `{{ homelab_push_dir }}/vapid.json` = `{"publicKey": b64url, "privateKey": b64url, "subject": "{{ vapid_subject }}"}` (amended: an https URL, since Apple may reject a localhost mailto) (written by role `stacks` from vault, 0640 root:pushcore). `{{ homelab_push_dir }}/subscriptions.json` = `[{"endpoint": str, "keys": {"p256dh": str, "auth": str}, "label": str, "created": ISO8601}]`, **owned by push-core** (container uid 10101), written atomically (tmp + rename). The notifier only reads it, and only removes dead entries.
- App: Node 22, **no runtime dependencies except `web-push`** (pinned). It listens on `0.0.0.0:8085` in the container, published to `127.0.0.1:8085` only.
  - `GET /` → PWA page "Homelab alerts": Subscribe button, label input (default device name), a test button, and the list of subscribed devices (label + created).
  - `GET /manifest.webmanifest`, `GET /sw.js` (handles `push` → showNotification, `notificationclick` → open `/`).
  - `GET /api/vapid-public-key`.
  - `POST /api/subscribe` `{subscription, label}`.
  - `POST /api/unsubscribe` `{endpoint}`.
  - `POST /api/test` → push "Test from homelab" to all.
  - `GET /healthz`.
  - POSTs require header `X-Homelab: 1` (CSRF guard) and `Content-Type: application/json`, body ≤ 8 KB.
- It's reachable **only via `tailscale serve`** at `https://{{ tailnet_fqdn }}/` (§7). No auth beyond tailnet membership (Tailscale ACL limits who that is).
- **Push payload contract** (every sender: notifier, alert floor, backups, heartbeat, push-core test): the Web Push message body is JSON `{"title": str, "body": str, "tag": str|null, "url": "/"}`. Senders use pywebpush/web-push with the VAPID keys from `vapid.json`. `homelab-notify --test` sends only to subscriptions labelled `test`, with no title prefix. **Rehearsal** (`/etc/homelab/rehearsal`, owned by the notifier role; no other role writes `/etc/homelab/`): `[REHEARSAL]` title prefix, `test`-labelled subscriptions only.
- Tests: `npm test` (node:test) covers the store (atomic write, dedupe by endpoint, removal) and the API guards. Dockerfile: multi-stage from `images.node_base`, non-root uid 10101, read-only rootfs-friendly.

## 6. Stacks (module C: roles `docker`, `stacks`, `tailscale_serve`)
- `docker` role: `/etc/docker/daemon.json` = `live-restore: true`, `log-driver: local` with max-size 10m / 3 files, `userland-proxy: false`. Restarts docker only via handler (the gate drop-in is owned by `bootgate`; don't touch it).
- Networks (compose-defined, not external): each stack is its own compose project. `edge` and `media` share the external network `hl-public` (created by the `stacks` role, subnet 172.30.10.0/24) so cloudflared can reach jellyfin.
- **Stacks** at `{{ homelab_stacks_dir }}/<name>/compose.yaml`, all containers `restart: unless-stopped`, label `homelab.watch=true` (except one-shots), `mem_limit` set, healthchecks where the image supports them:
  - **edge:** `cloudflared` (`tunnel --no-autoupdate run`, token from `.env`), on `hl-public`. In rehearsal: not deployed.
  - **media:** `jellyfin` (published `{{ lan_ip }}:8096` and `127.0.0.1:8096`; `/dev/dri`; group_add render gid; `/media` → `/data:ro`; BaseUrl `/watch` preset), `sonarr` 127.0.0.1:8989, `radarr` 127.0.0.1:7878, `prowlarr` 127.0.0.1:9696, `sabnzbd` 127.0.0.1:8080 (incomplete → `{{ homelab_incomplete_dir }}`), `jellyseerr` 127.0.0.1:5055. linuxserver apps `PUID={{ mediasvc_uid }} PGID={{ media_gid }} TZ`. `/media` → `/data` (rw for sonarr/radarr/sabnzbd). **Bind-mount propagation `rslave`** on `/media`, so a remount by the gate is visible.
  - **Clean start:** no data restored. Presets are written **only if absent** (`force: false`): *arr `config.xml` with `UrlBase` `/sonarr` `/radarr` `/prowlarr`, `AuthenticationMethod=Forms`, `AuthenticationRequired=Enabled`; sabnzbd `url_base=/sabnzbd` and `host_whitelist` incl. `{{ tailnet_fqdn }}`; jellyfin `network.xml` BaseUrl `/watch` and KnownProxies = `hl-public` subnet.
  - **utils:** `wol-relay` built from `roles/stacks/files/wol-relay/` (source from the old box), `network_mode: host`, token from `.env`.
  - **push:** `push-core` built from `apps/push-core/` on the host (`docker compose build`, image `local_images.push_core`), `127.0.0.1:8085`, volume `{{ homelab_push_dir }}`.
  - Media dirs `/media/{tv,movies,downloads/complete}` are created by `stacks` with 1001:1002 2775 (after the gate has mounted). Rehearsal: SABnzbd starts paused (`pause_on_start`-equivalent preset) and no tunnel.
- `tailscale_serve` role (**amended 2026-10-23**): `tailscale serve set-config` only handles `svc:` services in 1.102, not the node. So the role renders the desired config in the shape `tailscale serve status --json` returns, compares it as sorted JSON, and **only on drift** runs `tailscale serve reset` plus one `tailscale serve --bg --yes --https=<port> --set-path=<mount> <backend>` per handler, then re-reads and asserts an exact match. Serve **strips the mount prefix** (`http.StripPrefix`), so backends carry their base path:
  - HTTPS 443: `/` → `http://127.0.0.1:8085` (push core), `/watch` → `http://127.0.0.1:8096/watch`, `/sonarr` → `…:8989/sonarr`, `/radarr` → `…:7878/radarr`, `/prowlarr` → `…:9696/prowlarr`, `/sabnzbd` → `…:8080/sabnzbd`.
  - HTTPS 8443: `/` → `http://127.0.0.1:5055` (jellyseerr).
  - **Never funnel.**
- Public Jellyfin: the Cloudflare tunnel's public hostname `{{ public_watch_host }}` path `/watch*` → `http://jellyfin:8096`. That's configured in Cloudflare by Noah (documented in `docs/runbooks/cloudflare.md`, written by module C), not by Ansible.

## 7. Host security (module A: roles `base`, `users`, `ssh_hardening`, `firewall`, `tailscale`, `dns`, `watchdog`)
- `base`: timezone, packages (`sqlite3 rsync jq smartmontools unattended-upgrades needrestart curl`), persistent journald (SystemMaxUse=500M), unattended-upgrades **security only, Automatic-Reboot "false"**.
- `users`: ensure `mediasvc` 1001/1001 and group `media` 1002 exist; `mediasvc` in `media` and `render`; `{{ admin_user }}` in `media`, `docker`; `{{ backup_pull_user }}` system user, home `/var/lib/backuppull`, authorized_keys with `{{ backup_pull_pubkey }}`, options `restrict,from="{{ gaming_pc_lan_ip }}",command="/usr/bin/rrsync -ro {{ homelab_backups_dir }}"` (role `backups` owns the dir and its group). `{{ automation_user }}` is managed by bootstrap; this role only asserts it.
- `ssh_hardening`: `/etc/ssh/sshd_config.d/10-homelab.conf` (key-only, no root, no X11, `AllowUsers {{ admin_user }} {{ automation_user }} {{ backup_pull_user }}`, `MaxAuthTries 3`). Remove the stale `99-hardening.conf` only after confirming the new file is valid (`sshd -t`). The handler reloads sshd.
- `firewall`: UFW **converged** to a declared state (never `ufw reset`; declared rules are added first, then unmanaged rules are pruned; SSH on tailscale0 is never removed). Default deny in (v4+v6), allow out.
  - Allow `22/tcp` in on `tailscale0`.
  - Allow `22/tcp` from `{{ gaming_pc_lan_ip }}` on `{{ lan_iface }}`.
  - Allow `8096/tcp` on `{{ lan_iface }}` from `{{ lan_cidr }}` (TVs). **No 7359/udp** (amended): discovery broadcasts can't reach a bridge-network container, so TVs add `http://<lan_ip>:8096/watch` by hand.
  - Allow `9999/tcp` on `tailscale0` (wol-relay).
  - **No rules on `{{ wifi_iface }}`.** Note (accepted, L2): UFW's built-in `before.rules` still accept ICMP echo, DHCP replies, mDNS 5353 and SSDP 1900 on every interface. Nothing on the host listens on 5353 or 1900. Documented, not overridden.
  - Plus `homelab-docker-user.service` (oneshot, `After=docker.service`, `PartOf=docker.service`): inserts into DOCKER-USER `-i {{ wifi_iface }} -m conntrack --ctstate NEW,INVALID -j DROP` (amended: replies to containers' own outbound traffic still flow, so Wi-Fi failover works) and `-s 100.64.0.0/10 ! -i tailscale0 -j DROP`, plus IPv6 equivalents when Docker has an IPv6 DOCKER-USER chain (idempotent: `-C` before `-I`).
  - The Wi-Fi uplink stays configured as backup (route metric 200, already in netplan) and is **outbound-only**.
- `tailscale`: `tailscale set --ssh=false --accept-dns=false --accept-routes=false` (idempotent via `tailscale debug prefs`). Also writes `tailscale/policy.example.hujson` (the repo file, owned by module A) with tags/groups and no emails.
- `dns`: systemd-resolved stub on, `/etc/resolv.conf` → `../run/systemd/resolve/stub-resolv.conf`, no dependency on MagicDNS. **Owns the upstream resolvers** (amended): `dns_upstreams` (default 1.1.1.1, 9.9.9.9) with opportunistic DNS-over-TLS, replacing the unmanaged leftover `resolved.conf.d/custom-dns.conf`.
- `watchdog` (amended): Ubuntu's blacklist also blocks systemd-modules-load, so a `homelab-watchdog-load.service` runs `modprobe iTCO_wdt` early in boot. systemd drop-in `RuntimeWatchdogSec=30s`, `RebootWatchdogSec=10min`. Only enable the systemd watchdog if `/dev/watchdog0` exists after the module loads.

## 8. Backups and ops (module E: roles `backups`, playbooks `gaming-pc.yml`, `edge.yml`)
- `backups` (host): `homelab-backup.timer` 03:30 nightly (`Persistent=true`). It writes `{{ homelab_backups_dir }}/appdata/appdata-YYYYmmddTHHMMSSZ.tar` (members `sqlite/<path>`, `files.tar.gz`, `push.tar.gz`, `bootgate.tar.gz`, then `MANIFEST.sha256` last; the pull verifies the manifest, because `tar -tf` passes on a truncated archive), containing an integrity-checked `sqlite3 .backup` copy of every `*.db` under `{{ homelab_data_dir }}` plus a tar.gz of the rest (excluding caches, metadata, transcodes, logs), plus `{{ homelab_push_dir }}` and `/var/lib/bootgate`. Keep 14. Group `backuppull` read. On failure → `homelab-notify --tag backup`. Writes `{{ homelab_backups_dir }}/LAST_OK` (ISO timestamp) on success.
- `playbooks/gaming-pc.yml` (hosts `workstations`, no become): installs a **user** systemd timer `homelab-pull.timer` (daily 06:00, `Persistent=true`). It runs `rsync -a --delete-after -e "ssh -i {{ pull_ssh_key }} -o BatchMode=yes" {{ backup_pull_user }}@{{ pull_source_host }}: {{ pull_dest }}/` (rrsync on the box limits the path), then verifies the newest archive with `tar -tf` and writes `{{ pull_dest }}/.last_pull`. `~/backups` is already covered by the existing restic job.
- `playbooks/edge.yml` (hosts `edge`): heartbeat watcher `homelab-heartbeat.timer` every 2 min on the VPS. It's `tailscale ping -c 1 --timeout 5s --until-direct=false {{ heartbeat_target }}` (amended: the box is reached via DERP relay from the VPS, and without the flag ping exits 1 even on a pong) plus TCP 22. After 3 consecutive failures → Web Push "mediaserver offline" (once), and again "back online" on recovery. Uses its own copy of `vapid.json` + `subscriptions.json`, synced from the box by the playbook (`fetch` from mediaserver → copy to the VPS) into `/opt/homelab-heartbeat/`, with the same Python `pywebpush` approach (its own venv). **Contract 8:** an up/down log at `/var/lib/homelab-heartbeat/log.jsonl` `{"ts","state":"up|down","rtt_ms"}`: one line per check, `state` = that check's observation, `rtt_ms` null when down, trimmed to about 30 days.
- **Don't touch the VPS's other services** (Money Hub, ERAU dashboard, the nftables game relay). Never run `nft -f` / flush on the VPS.

## 9. Lead-owned files
`ansible.cfg`, `requirements.yml`, `inventory/**`, `playbooks/bootstrap.yml`, `playbooks/site.yml`, `docs/CONTRACTS.md`, `README.md`, `CLAUDE.md`, `.github/workflows/ci.yml`, `.gitignore`, lint configs.

`playbooks/site.yml` order (hosts `media_servers`, become): base → users → ssh_hardening → tailscale → dns → watchdog → storage → docker → bootgate → firewall → notifier → stacks → tailscale_serve → backups.

## 9b. Ownership of shared paths (amended)
- `{{ homelab_push_dir }}`: owner and mode set **only** by `stacks` (`pushcore` 10101). The notifier, backups and heartbeat read it and never chown or chmod it.
- Docker publishes with `userland-proxy: false`, so `lan_ip:8096` is a DNAT rule, not a bind. Jellyfin doesn't fail to start if the LAN address is late.

- Handler names are unique across the whole play (roles must not reuse another role's handler name).
- Jellyfin's `lan_ip:8096` is a DNAT (it bypasses UFW INPUT). Reachable from the whole LAN by design; the Wi-Fi and tailnet-spoof guards in DOCKER-USER cover the rest.

- Python packages are pinned in `versions.yml` → `python_packages` (pywebpush). The notifier and heartbeat both use it.

## 9c. Review decisions (2026-10-24)
- **Single writer for `subscriptions.json`: push-core.** The notifier no longer rewrites the file. It prunes dead subscriptions best-effort via `POST http://127.0.0.1:8085/api/unsubscribe` (`X-Homelab: 1`, JSON), and skips pruning if push-core is down. The heartbeat's copy on the VPS is never written back.
- `tune2fs -m 1` on the media drive is approved (it's in the rebuild plan).
- The format guard also refuses when a backup ext4 superblock is readable.
- The push-core `/api/test` is human-triggered and exempt from rehearsal.
- Logs never contain push endpoints (notifier, heartbeat, push-core).
- Collections and control-node tools are pinned (`requirements.yml`, `requirements-tools.txt`).

## 10. Module ownership
| Module | Builder owns |
|---|---|
| A host | `roles/{base,users,ssh_hardening,firewall,tailscale,dns,watchdog}/`, `tailscale/policy.example.hujson` |
| B gate | `roles/{storage,bootgate,notifier}/` |
| C stacks | `roles/{docker,stacks,tailscale_serve}/`, `docs/runbooks/cloudflare.md`, `docs/runbooks/first-run-apps.md` |
| D push core | `apps/push-core/` |
| E ops | `roles/backups/`, `playbooks/gaming-pc.yml`, `playbooks/edge.yml`, `roles/heartbeat/`, `roles/backup_pull/` |
