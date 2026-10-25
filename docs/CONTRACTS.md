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
  push/                            push store (§5/§11), owned by `dashboard` 10101, 0750
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
  5. Still failing: if `/var/lib/bootgate/attempts` < 1 → write attempts=1, append history, power-cycle (`reboot`, or `rtcwake -m off -s 60` when `bootgate_powercycle_method=rtcwake`). **Live box (2026-10-24): `rtcwake` with `bootgate_rtcwake_seconds: 600`.** rtcwake starts its timer before the shutdown, and a gate power-cycle happens while the drive misbehaves, so a slow shutdown (stuck I/O; systemd waits up to 90 s per stage) must not outlast the alarm. It was verified on the hardware: S5 cuts USB power (the drive spun down) and the RTC alarm woke the box by itself after 2 min. Risk: a BIOS reset can disable RTC wake, leaving the box off until the power button. The BIOS settings are listed in the rebuild outline. If attempts ≥ 1 → **no reboot**; write status `failed`, exit 1 (docker stays down), and `bootgate-alert.service` fires.
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
- **Dedupe:** with `--tag`, it doesn't resend the same tag+title within 30 min (state in `/var/lib/homelab-notify/`). **Amended 2026-10-24:** sending a different title on a tag clears that tag's dedupe entries, so lost → back → lost pushes (and records) the second loss. The clear happens even when that send reached no device (it's still in the history). A suppressed duplicate exits 0, checked before subscriptions (callers treat 0 and 2 alike).
- **Rehearsal:** when `/etc/homelab/rehearsal` exists, it prefixes the title with `[REHEARSAL]` and sends only to subscriptions whose `label` is `test`.
- **Alert floor** (role `notifier`): `homelab-alertfloor.timer` every 2 min. For each container labeled `homelab.watch=true` it pushes once per incident on: health `unhealthy` for 2 consecutive checks, state `exited`/`dead` while not intentionally stopped, or restart count rising by 3+ between checks. It pushes "recovered" when the condition clears. Skipped when bootgate status ≠ `ok`.
- **Alert floor amendments (follow-ups, 2026-10-24):**
  - A single unexpected crash that the restart policy recovers (a `die` with no intent **and** a non-zero exit code or a preceding OOM; not part of a 3+ restart loop; not staying down) pushes **once**: `<app> crashed and restarted`, tag `alertfloor:<app>:crash` (body: the exit code, plus "(out of memory)"). It's informational: it opens no incident and sends no "recovered". A die and its restart that land in different runs still push once (the pending crash is kept between runs). If the push fails, it's retried on later runs for up to 1 h. The usual 30 min tag+title dedupe applies.
  - Only kills with a fatal signal (`SIGKILL`, `SIGTERM`, `SIGINT`, `SIGQUIT`, or the container's configured stop signal) count as intent. A `kill -s HUP` or `USR1` reload doesn't.

## 5. Push core (module D). **Superseded by §11:** push-core was absorbed into the Dashboard v2 server (`apps/dashboard/server/`) on 2026-10-24, with the same API, store and port
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
  - **edge:** `cloudflared` (`tunnel --no-autoupdate run`, token from `.env`), on `hl-public`. In rehearsal: not deployed. (**Amended 2026-10-25: gated on Jellyfin's startup wizard.**
    - **Why:** until the wizard is finished, anyone who reaches Jellyfin can create its admin, and a clean start brings Jellyfin and the tunnel up in the same run.
    - **The check:** before the stack is brought up, `stacks` reads `StartupWizardCompleted` from `http://127.0.0.1:8096/watch/System/Info/Public` (`stacks_jellyfin_public_info_url`; anonymous, read-only, 6 tries 5 s apart).
    - **`true`:** brought up as before. A box whose wizard is done sees no change, and a re-run is `changed=0`.
    - **`false`:** `.env` and `compose.yaml` are written, but the stack stays down (taken down if it is running, never just stopped). The play goes on, so utils still deploys, and it prints the next step: finish the wizard over the tailnet (`first-run-apps.md` §1), then re-run `site.yml`, which starts the tunnel.
    - **No answer, or no boolean in it:** the play fails before the edge stack is touched.
    - **After a restore:** `site.yml` runs before the data restore, so the tunnel stays held until the `site.yml` run after it (`restore-from-scratch.md` §4 step 9).)
  - **media:** `jellyfin` (published `{{ lan_ip }}:8096` and `127.0.0.1:8096`; `/dev/dri`; group_add render gid; `/media` → `/data:ro`; BaseUrl `/watch` preset), `sonarr` 127.0.0.1:8989, `radarr` 127.0.0.1:7878, `prowlarr` 127.0.0.1:9696, `sabnzbd` 127.0.0.1:8080 (incomplete → `{{ homelab_incomplete_dir }}`), `jellyseerr` 127.0.0.1:5055. linuxserver apps `PUID={{ mediasvc_uid }} PGID={{ media_gid }} TZ`. `/media` → `/data` (rw for sonarr/radarr/sabnzbd). **Bind-mount propagation `rslave`** on `/media`, so a remount by the gate is visible.
  - **Clean start:** no data restored. Presets are written **only if absent** (`force: false`): *arr `config.xml` with `UrlBase` `/sonarr` `/radarr` `/prowlarr` and `AuthenticationRequired=Enabled` (**amended 2026-10-24:** no `AuthenticationMethod`. Forms with no user locked the owner out; without a method the apps force their login-setup box, and the owner completes it right after deploy, per `first-run-apps.md`); sabnzbd `url_base=/sabnzbd` and `host_whitelist` incl. `{{ tailnet_fqdn }}`; jellyfin `network.xml` BaseUrl `/watch` and KnownProxies = `hl-public` subnet (**amended 2026-10-25:** plus `LocalNetworkSubnets` = RFC1918, IPv6 ULA and link-local, **the tailnet `100.64.0.0/10`**, and **`!<hl-public subnet>`** (an exclusion). Jellyfin's empty-list fallback is loopback plus RFC1918 (the IPv6 ranges only with IPv6 on), without the tailnet. Tailnet clients reach Jellyfin through `tailscale serve`, whose `X-Forwarded-For` is honored because that hop arrives from the `hl-public` gateway, so without the range they count as remote. The exclusion covers a tunnel request whose `X-Forwarded-For` is missing or unparsable: Jellyfin then keeps cloudflared's own address, which is inside 172.16.0.0/12, and without the exclusion it would count as LAN. With both, the admin account runs with *Allow remote connections* **off**: home LAN and tailnet only, refused through the tunnel. Accepted side effects:
    - Tailnet clients count as LAN, so they get no remote bitrate cap, Jellyfin's LAN-only anonymous endpoints (e.g. `/System/Restart`) and the local password-reset PIN. The tailnet policy lets only admin devices reach 8444.
    - The host's own loopback calls (the dashboard, the edge stack's wizard check) now count as remote. They use an API key or no auth, and API keys aren't users, so the admin block doesn't touch them (and doesn't apply to API keys anywhere). **The server-wide *Allow remote connections* must therefore stay on:** with it off, Jellyfin answers every remote address with 503, including these. To take the public site offline, take the edge stack down instead.)
  - **utils:** `wol-relay` built from `roles/stacks/files/wol-relay/` (source from the old box), `network_mode: host`, token from `.env`.
  - **push:** retired 2026-10-24. Push-core was absorbed into the Dashboard v2 host service (§11); `stacks` removes the old stack (`tasks/retire_push.yml`).
  - Media dirs `/media/{tv,movies,downloads/complete,downloads/complete/tv,downloads/complete/movies}` are created by `stacks` with 1001:1002 2775 (after the gate has mounted). The SABnzbd category folders are pre-created so Radarr and Sonarr's path checks pass before the first download. Rehearsal: SABnzbd starts paused (`pause_on_start`-equivalent preset) and no tunnel.
- `tailscale_serve` role (**amended 2026-10-23**): `tailscale serve set-config` only handles `svc:` services in 1.102, not the node. So the role renders the desired config in the shape `tailscale serve status --json` returns, compares it as sorted JSON, and **only on drift** runs `tailscale serve reset` plus one `tailscale serve --bg --yes --https=<port> --set-path=<mount> <backend>` per handler, then re-reads and asserts an exact match. Serve **strips the mount prefix** (`http.StripPrefix`), so backends carry their base path:
  - **Amended 2026-10-24 (ADR 0015): the dashboard has its own origin.**
  - HTTPS 443: `/` → `http://127.0.0.1:8085` (the dashboard) **only**.
  - HTTPS 8443: `/` → `http://127.0.0.1:5055` (jellyseerr).
  - HTTPS 8444: `/watch` → `http://127.0.0.1:8096/watch`, `/sonarr` → `…:8989/sonarr`, `/radarr` → `…:7878/radarr`, `/prowlarr` → `…:9696/prowlarr`, `/sabnzbd` → `…:8080/sabnzbd`.
  - **Never funnel.**
- Public Jellyfin (**amended 2026-10-24, ADR 0016**): the Cloudflare tunnel's public hostname `{{ public_watch_host }}` (now the `watch.` subdomain), **whole hostname** → `http://jellyfin:8096`. Jellyfin keeps its `/watch` base URL and redirects `/` to it. The apex is a static homepage on Cloudflare Pages, not on the tunnel. That's configured in Cloudflare, not by Ansible (**amended 2026-10-25:** the route, the zone's DNS records and its HTTPS settings are OpenTofu code in `terraform/cloudflare/`, module T, applied only through a plan that passes `plan-guard.sh`; `docs/runbooks/cloudflare.md`, module C, keeps the first-setup steps).

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
  - **Amended 2026-10-24 (live test):** on this HP the iTCO timer **never resets the box**. After a kernel panic with kdump unloaded, it stayed hung 10 min until the power button; the driver reports "initialized, heartbeat=30 sec", so the BIOS likely handles the TCO timeout itself. The systemd settings stay (harmless, and they may work on other hardware), but recovery no longer relies on them:
    - sysctl `kernel.panic=10` (reboot 10 s after any panic), `kernel.hardlockup_panic=1` (a hard-locked CPU becomes a panic; the NMI watchdog is already on), keeping the existing `kernel.panic_on_oops=1`. Persisted in `/etc/sysctl.d/`.
    - **Not** `softlockup_panic` or `hung_task_panic`: a stalling USB media drive can trigger those, and a panic-reboot loop would bypass the boot gate's one-power-cycle rule.
    - **kdump disabled** (Noah, 2026-10-24): purge `kdump-tools` and any crashdump metapackage, remove the `crashkernel=` reservation (its `/etc/default/grub.d/kdump-tools.cfg`, then `update-grub`). That frees 512 MB of RAM after the next reboot. `kexec-tools` may stay if nothing else needs it removed.
    - Known gap: a hard freeze that never panics (and isn't a hard lockup) still needs the power button. Recorded in P7.

## 8. Backups and ops (module E: roles `backups`, playbooks `gaming-pc.yml`, `edge.yml`)
- `backups` (host): `homelab-backup.timer` 03:30 nightly (`Persistent=true`). It writes `{{ homelab_backups_dir }}/appdata/appdata-YYYYmmddTHHMMSSZ.tar` (members `sqlite/<path>`, `files.tar.gz`, `push.tar.gz`, `bootgate.tar.gz`, then `MANIFEST.sha256` last; the pull verifies the manifest, because `tar -tf` passes on a truncated archive), containing an integrity-checked `sqlite3 .backup` copy of every `*.db` under `{{ homelab_data_dir }}` plus a tar.gz of the rest (excluding caches, metadata, transcodes, logs), plus `{{ homelab_push_dir }}` and `/var/lib/bootgate`. Keep 14. Group `backuppull` read. On failure → `homelab-notify --tag backup`. Writes `{{ homelab_backups_dir }}/LAST_OK` (ISO timestamp) on success.
- `playbooks/gaming-pc.yml` (hosts `workstations`, no become): installs a **user** systemd timer `homelab-pull.timer` (daily 06:00, `Persistent=true`). It runs `rsync -a --delete-after -e "ssh -i {{ pull_ssh_key }} -o BatchMode=yes" {{ backup_pull_user }}@{{ pull_source_host }}: {{ pull_dest }}/` (rrsync on the box limits the path), then verifies the newest archive with `tar -tf` and writes `{{ pull_dest }}/.last_pull`. `~/backups` is already covered by the existing restic job.
- `playbooks/edge.yml` (hosts `edge`): heartbeat watcher `homelab-heartbeat.timer` every 2 min on the VPS. It's `tailscale ping -c 1 --timeout 5s --until-direct=false {{ heartbeat_target }}` (amended: the box is reached via DERP relay from the VPS, and without the flag ping exits 1 even on a pong) plus TCP 22. After 3 consecutive failures → Web Push "mediaserver offline" (once), and again "back online" on recovery. Uses its own copy of `vapid.json` + `subscriptions.json`, synced from the box by the playbook (`fetch` from mediaserver → copy to the VPS) into `/opt/homelab-heartbeat/`, with the same Python `pywebpush` approach (its own venv). **Contract 8:** an up/down log at `/var/lib/homelab-heartbeat/log.jsonl` `{"ts","state":"up|down","rtt_ms"}`: one line per check, `state` = that check's observation, `rtt_ms` null when down, trimmed to about 30 days.
- **Don't touch the VPS's other services** (Money Hub, ERAU dashboard, the nftables game relay). Never run `nft -f` / flush on the VPS.

## 9. Lead-owned files
`ansible.cfg`, `requirements.yml`, `inventory/**`, `playbooks/bootstrap.yml`, `playbooks/site.yml`, `docs/CONTRACTS.md`, `README.md`, `CLAUDE.md`, `.github/workflows/ci.yml`, `.gitignore`, lint configs.

`playbooks/site.yml` order (hosts `media_servers`, become): base → users → ssh_hardening → tailscale → dns → watchdog → storage → firewall → docker → bootgate → notifier → stacks → dashboard → tailscale_serve → backups (firewall moved before docker, review L1; dashboard added 2026-10-24, §11.5).

## 9b. Ownership of shared paths (amended)
- `{{ homelab_push_dir }}`: owner and mode set **only** by `dashboard` (user `dashboard` 10101, §11.1; was `stacks`/`pushcore` before 2026-10-24). The notifier, backups and heartbeat read it and never chown or chmod it.
- Docker publishes with `userland-proxy: false`, so `lan_ip:8096` is a DNAT rule, not a bind. Jellyfin doesn't fail to start if the LAN address is late.

- Handler names are unique across the whole play (roles must not reuse another role's handler name).
- Jellyfin's `lan_ip:8096` is a DNAT (it bypasses UFW INPUT). Reachable from the whole LAN by design; the Wi-Fi and tailnet-spoof guards in DOCKER-USER cover the rest.

- Python packages are pinned in `versions.yml` → `python_packages` (pywebpush). The notifier and heartbeat both use it.

## 9c. Review decisions (2026-10-24)
- **Single writer for `subscriptions.json`: the dashboard** (push-core until 2026-10-24). The notifier no longer rewrites the file. It prunes dead subscriptions best-effort via `POST http://127.0.0.1:8085/api/unsubscribe` (`X-Homelab: 1`, JSON), and skips pruning if the dashboard is down. The heartbeat's copy on the VPS is never written back.
- `tune2fs -m 1` on the media drive is approved (it's in the rebuild plan).
- The format guard also refuses when a backup ext4 superblock is readable.
- The push-core `/api/test` is human-triggered and exempt from rehearsal.
- Logs never contain push endpoints (notifier, heartbeat, push-core).
- Collections and control-node tools are pinned (`requirements.yml`, `requirements-tools.txt`).

## 9d. Encrypted-only DNS (decided 2026-10-24)
- **dns (A):**
  - **Strict** DNS-over-TLS: `DNSOverTLS=yes`, with cert names `DNS=1.1.1.1#cloudflare-dns.com 9.9.9.9#dns.quad9.net` and `FallbackDNS=1.0.0.1#cloudflare-dns.com 149.112.112.112#dns.quad9.net`.
  - The LAN link ignores DHCP/RA DNS (`UseDNS=no`, as on the Wi-Fi link). Apply it without bouncing the LAN.
  - `DNSStubListenerExtra={{ docker_dns_ip }}`.
- **docker (C):** `daemon.json` gets `"dns": ["{{ docker_dns_ip }}"]` and `"bip": "{{ docker_dns_bip }}"`. Containers' embedded DNS forwards to the host's resolved.
- **firewall (A):**
  - UFW allows 53/udp+tcp **in** from 172.16.0.0/12 to `{{ docker_dns_ip }}`.
  - UFW **denies 53/udp+tcp out** to everything (loopback is unaffected).
  - DOCKER-USER drops forwarded `--dport 53` (udp+tcp), v4 and v6.
  - Result: no plaintext DNS leaves the host.
- **Verify (Integrator):**
  - `resolvectl` shows +DNSOverTLS, with only the declared upstreams.
  - A container lookup works.
  - A direct plaintext query to 1.1.1.1:53 fails from both the host and a container.
  - Established upstream connections are on :853 only.

## 10. Module ownership
| Module | Builder owns |
|---|---|
| A host | `roles/{base,users,ssh_hardening,firewall,tailscale,dns,watchdog}/`, `tailscale/policy.example.hujson` |
| B gate | `roles/{storage,bootgate,notifier}/` |
| C stacks | `roles/{docker,stacks,tailscale_serve}/`, `docs/runbooks/cloudflare.md`, `docs/runbooks/first-run-apps.md` |
| D dashboard server | `apps/dashboard/server/` (was `apps/push-core/`, retired 2026-10-24) |
| G dashboard web | `apps/dashboard/web/` |
| H dashboard host | `roles/dashboard/` |
| T cloudflare-as-code | `terraform/cloudflare/` (OpenTofu; P8 #2, encrypted local state, never applied without a reviewed plan) |
| E ops | `roles/backups/`, `playbooks/gaming-pc.yml`, `playbooks/edge.yml`, `roles/heartbeat/`, `roles/backup_pull/` |

## 11. Dashboard v2 (build started 2026-10-24)
Design: `docs/design/handoff/README.md` (high fidelity), corrected by `docs/design/handoff/CORRECTIONS.md`. Decisions: **host service outside Docker** (ADR 0013), **tailnet-only in v2.0** (ADR 0014). The dashboard **absorbs push-core**: the same port, the push store and every push-core endpoint the notifier and phones use.

### 11.1 Runtime (module H, role `dashboard`)
- **Node 22 from the Ubuntu repo** (`nodejs` package; security updates via unattended-upgrades). App code at `/opt/homelab-dashboard/`. npm deps are pinned by the lockfile and installed with `npm ci --omit=dev`; `web-push` stays at 3.6.7.
- **systemd `homelab-dashboard.service`:**
  - Runs as user and group **`dashboard`, uid/gid 10101**. This replaces `pushcore`: rename it and keep the id, so the existing push store keeps its owner.
  - Supplementary groups: `media`, to read the *arr and SABnzbd `config.xml`/`sabnzbd.ini` for their API keys, and `bootgate`, to touch `/run/bootgate/rerun`. **Not** in `docker`.
  - `After=network-online.target`. **No ordering on docker.service or bootgate.service**: it must run while the gate holds Docker.
  - `Restart=always`.
  - Hardened: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `ReadWritePaths=/var/lib/homelab-dashboard {{ homelab_push_dir }} -/run/bootgate`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, `CapabilityBoundingSet=` (empty), `SystemCallFilter=@system-service`.
- Listens on **127.0.0.1:8085**, the same port push-core used, so 443 `/` keeps pointing there. Since ADR 0015 it is the **only** handler on 443; the admin apps moved to 8444.
- Data: `/var/lib/homelab-dashboard/` (0750 dashboard): `settings.json` (0600), `samples.jsonl` (drive samples), `uptime/` (per-app daily rollups).
- Push store: `{{ homelab_push_dir }}` stays, owned by **`dashboard`** (10101). The contract 4 file formats are unchanged. The **dashboard is the single writer** of `subscriptions.json`; the notifier's prune call (`POST 127.0.0.1:8085/api/unsubscribe`) keeps working.

### 11.2 Privileged helper `homelab-dockerctl` (module H)
- Root, **socket-activated**, at `/run/homelab-dockerctl.sock` (0660 root:dashboard).
- It accepts only peers whose `SO_PEERCRED` uid is `dashboard`, and logs every request to journald (`SyslogIdentifier=homelab-dockerctl`).
- Written in Python stdlib (small, auditable), with timeouts on everything.
- Protocol: one JSON request per connection → a JSON response, or JSON-lines for streams.
- **The only operations allowed:**
  - `ps`: for containers labelled `homelab.watch=true` only: `name, state, health, started_at, restart_count, image` (running ref), `pinned` (the image ref in the deployed compose file), `stack`. No env, no mounts, no raw inspect.
  - `logs {name, tail≤2000, follow, since}`: a stream for an allowed container; follow ends on client close or after 30 min.
  - `start|stop|restart {name}`: allowed containers only.
  - `update {name}`: `docker compose -f {{ homelab_stacks_dir }}/<stack>/compose.yaml up -d --no-deps <service>`, so the pinned image from the deployed compose file is pulled and recreated. Streams the steps `pulling → recreating → waiting-health → done|failed`.
  - `docker-state`: `active|inactive|failed` of docker.service. **Never calls the docker CLI or socket while docker.service isn't active**: touching the socket would start Docker, and so re-run the gate.
  - `last-pull`: the time of the last successful `backuppull` SSH session, from the journal.
  - `disk-split`: sizes of `{{ media_mount }}/{tv,movies,downloads}`, cached for 24 h and computed with `du` under nice/ionice.
  - `boots`: `journalctl --list-boots` (first/last entry times) for box uptime.
- **Wire protocol (as built, Builder H):**
  - **Request:** one JSON line, at most 4 KiB, per connection (`Accept=yes`: one short-lived root process per connection).
  - **Unary reply:** `{"ok":true,"op","data"}` or `{"ok":false,"error":{"code","message"}}`.
  - **Streams:** a header `{"ok":true,"op","stream":true}`, then `{"type":"line|step|output",...}` events, always ending with `{"type":"end","ok","reason","message"?}`.
  - The client never half-closes. Refused peers are dropped before the request is read.
- **Extra rules (as built):**
  - start/restart/update of `media`-stack apps are refused unless the gate status is `ok`; stop is always allowed.
  - `update` refuses locally built images (wol-relay).
  - `ps` works while Docker is inactive, returning apps declared in the deployed compose files without touching Docker.
  - **Concurrency (review, 2026-10-24):** the dashboard runs at most **one `update` at a time** (a second request gets 409) and at most 8 log follows; the socket's `MaxConnections` covers that worst case with headroom (32).
- **The npm footprint:** the host gets only `nodejs`. `npm ci --omit=dev --ignore-scripts` runs on the controller (`.build/`, git-ignored) and `node_modules` is synced; there are no native modules.
- **Never:** exec, build, arbitrary paths, other containers, image or volume management.

### 11.3 HTTP API (module D, server ↔ module G, web)
- **Push-core API, kept as-is:** `GET /api/vapid-public-key`, `POST /api/subscribe`, `POST /api/unsubscribe`, `POST /api/test`, `GET /healthz`.
- **New endpoints:**
  - `GET /api/summary`, with ETag
  - `GET /api/events` (SSE pushing summary changes; the client falls back to a 10 s poll)
  - `GET /api/apps/:name/logs?follow=1&tail=500` (SSE)
  - `POST /api/apps/:name/{restart|stop|start|update}` (update streams its progress via SSE `GET /api/actions/:id`)
  - `POST /api/gate/rerun` (touches `/run/bootgate/rerun`; the dashboard then reports status changes)
  - `GET /api/push/devices` (`{id,label,created}`), `DELETE /api/push/devices/:id`
  - `GET|PUT /api/settings`
- **Guards (as in push-core):** every non-GET needs `X-Homelab: 1` and a JSON body ≤ 8 KB. Allowed `Sec-Fetch-Site` values are `same-origin` or `none`; a missing header is allowed. **Amended (review, 2026-10-24):** `/api/*` **GETs** also refuse `Sec-Fetch-Site: same-site|cross-site` (403), so another site can't make the admin's browser drive helper calls. The shell and static files are exempt.
- **Access:** `access` is decided server-side. In v2.0 it's always `tailnet`. A `remote_enabled=false` flag and the Access-JWT check path exist but are off. Tailnet-only actions (update, gate rerun) return **403** for `remote`.
- **Summary schema:** as in the handoff's "State management" section. Sources:
  - **gate:** `/var/lib/bootgate/{status,attempts,history.jsonl}` (world-readable), plus `systemctl show bootgate-retry.timer` for the next retry time.
  - **apps:** the helper's `ps` and `docker-state`. When Docker isn't active, every app is "Held by gate" (`failed`) or "Stopped" (other).
  - **host:** `/proc/loadavg`, `/proc/meminfo`, `statfs /`, `/proc/uptime`, `/run/reboot-required` and `/run/reboot-required.pkgs`.
  - **drive:** `statfs {{ media_mount }}` (only if the gate status is `ok`, else last-known plus a flag), plus a daily sample appended to `samples.jsonl`. The forecast is a least-squares fit over the last 30 days; fewer than 7 samples gives none. Split from the helper's `disk-split`.
  - **backups:** `/var/lib/homelab-backup/status.json` (module E) and the helper's `last-pull`.
  - **uptime:** per-app daily rollups from the dashboard's own 60 s sampling; box uptime from the helper's `boots` (labelled "Box uptime"; the heartbeat log comes later).
  - **media:** Jellyfin sessions, SABnzbd queue, Jellyseerr pending, Prowlarr indexer status/limits, Sonarr/Radarr health. API keys: *arr/SABnzbd read read-only from their config files; Jellyfin and Jellyseerr entered in Settings (`settings.json`, never returned to the client in full). One app timing out marks only that tile.
  - **push:** the store, plus `/var/lib/homelab-notify/history.jsonl` (module B) for recent alerts.
  - **subscriptions:** renewal dates from settings. A reminder push fires 7 days before each, sent by the dashboard itself.
- **Summary additions (Lead, 2026-10-24):**
  - `server.timeZone`: the host's IANA zone (config `timezone`, then `/etc/timezone`, then `timedatectl`, then the process zone). The web shows server times in it and falls back to the browser's zone.
  - `apps[].stoppedReason`: `gate | docker-down | intentional | null`, non-null only for `held`/`stopped`. Only `intentional` counts as "stopped on purpose". A crashed app is `down` with `null`.
  - **Amended (follow-ups, 2026-10-24):** when docker.service isn't active **and the gate isn't holding it** (gate `ok`), `live-restore` means the containers may still be running. Those apps get `status: unknown` with `stoppedReason: docker-down`. The web shows "Unknown: Docker isn't running (apps may still be serving)", never "Stopped". When the gate holds Docker, the apps stay `held`/`gate`.
  - `links.runbooks`: `[{title, url}]`, built from the config keys `runbook_base_url` (plain `https://`, ending in `/`) and `runbooks` (`[{title, path}]`, repo-relative `.md`, no `..`). Role H renders both and checks that each file exists; the server drops a bad entry with a warning instead of failing.
- Secrets never reach the browser or the logs: API keys, VAPID private key, push endpoints.
  - That includes text copied from the apps (SABnzbd warnings, *arr health messages, queue names, container logs): it's redacted (`apikey=`, `API Key:`, tokens, the configured keys) **before** any truncation, and keys are registered from config files whether or not the app is up.
- **Resource limits (review, 2026-10-24):** a malformed request URL is a 400, never a crash; every async path is caught, plus a process-level `unhandledRejection` handler. SSE streams (`/api/events`, logs, actions) honour backpressure: a client that stops reading is dropped once about 1 MB is buffered. An `/api/events` slot is reserved before any await and always released on close.

### 11.4 Web (module G)
- A PWA per the handoff, at `/`. No CDN at runtime: Inter woff2 (OFL) and a Phosphor SVG sprite (MIT) are vendored with their licenses.
- Small stack: vanilla JS modules or Preact + htm as vendored ESM, with no bundler required at deploy time. If a build step is used, its output is committed and reproducible.
- **Service worker:** caches the shell and the last `/api/summary` with its timestamp, giving the **offline screen**. It keeps push-core's push and notificationclick behaviour (exact-path window reuse).
- Accessibility: status is never shown by color alone, focus-visible, reduced-motion respected, 44 px targets on mobile.
- Tests: unit tests for formatters and state reducers (`node:test`), plus a smoke test that serves the static build and checks that every route renders without errors using fake summaries (normal, bad night, clean, offline).

### 11.5 Changes in other modules
- **C (stacks):** remove the `push` stack (container, compose, built image). Ownership and mode of `{{ homelab_push_dir }}` and `vapid.json` move to the `dashboard` role. The `pushcore` user is renamed to `dashboard` by role H, not removed.
- **B (notifier):** append every send to `/var/lib/homelab-notify/history.jsonl` (0644) as `{ts, title, body, tag, delivered, failed}`, trimmed to 90 days. No endpoints in it. Accepted: the state dir is 0755 (the dedupe state stays 0600), and sends that reached no device are recorded too (0 delivered), so the dashboard always shows the alert.
- **E (backups):** write `/var/lib/homelab-backup/status.json` (dir 0755, file 0644, atomic) on every run, success or failure, as `{ok, last_ok, last_run, archive, size_bytes, error}`. `last_ok`, `archive` and `size_bytes` carry forward on failure. `error` is a short fixed summary with no paths. The failure push uses the same summary.
- **Lead:** in `site.yml`, `dashboard` goes after `stacks` (push-core gone first, so 8085 is free) and before `tailscale_serve`. Plus versions and ADRs.

### 11.6 Ownership
| Module | Owns |
|---|---|
| D server | `apps/dashboard/server/` (Node; migrates push-core's store, sender, VAPID and guards with their tests) |
| G web | `apps/dashboard/web/` |
| H host | `roles/dashboard/` (incl. `files/homelab-dockerctl/` helper + tests) |
| B, C, E | the changes in §11.5 within their existing roles |
| Lead | contract, versions, site.yml, ADRs, CI wiring, retiring `apps/push-core/` once D has migrated it |
