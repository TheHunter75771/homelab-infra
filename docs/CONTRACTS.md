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
- Docker publishes with `userland-proxy: false`, so `lan_ip:8096` is a DNAT rule, not a bind. Jellyfin doesn't fail to start if the LAN address is late. **Amended 2026-10-26 (live on ai-box, Docker 29):** if the host address is missing when the container starts, the publish is silently dropped (no error, empty `docker port`), and `docker restart` doesn't restore it; only a recreate does. Stacks that publish on a late address need a post-boot recreate check (see ai_stack's helper).

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

## 12. ai-box: the AI host (build started 2026-10-26)
Plan: the 2nd HP desktop (EliteDesk 800 G2 DM: i5-6500 4C/4T, 8 GB, 500 GB HDD, **no GPU slot**) runs the
LLM front door, Open WebUI and (later) the Household Calendar. It **borrows the gaming PC's GPU** under Noah's
rule (§12.4). Inventory group `ai_servers`, host `ai-box`; Ubuntu 26.04.1, kernel 7.0, Tailscale 1.104 joined by
hand, **no Docker yet**, no Wi-Fi interface. Admin `{{ admin_user }}` (key-only, NOPASSWD, password locked).

### 12.1 Variables added (Lead)
host_vars (ai-box): `mediaserver_lan_ip`, `mediaserver_tailnet_ip` (plus the standard `lan_iface`, `lan_ip`,
`lan_cidr`, `tailnet_ip`, `tailnet_fqdn`, `gaming_pc_*`, `admin_pubkeys`). **No `wifi_iface`.**
group_vars/ai_servers: `users_media_accounts: false`, `ssh_hardening_allow_users`, `firewall_allow_rules`.

### 12.2 Module A amendments (reuse the reviewed host roles on ai-box)
Hard rule: **the mediaserver's converged state must not change.** `site.yml --check --diff` against
`mediaserver` reports `changed=0` after the refactor.
- `users`: new boolean `users_media_accounts` (role default `true`). When `false`: skip the media groups,
  `mediasvc`, `{{ backup_pull_user }}` and the rrsync checks; keep the automation-user assert and the stale-sudoers
  cleanup. `{{ admin_user }}` joins `docker` only if that group exists (docker installs later in the play).
- `ssh_hardening`: unchanged (`ssh_hardening_allow_users` is already a variable).
- `firewall`: the hard-coded allow rules move to a declared list **`firewall_allow_rules`**
  (`[{port, proto, iface, from?, comment}]`). The current mediaserver rules move verbatim into
  `group_vars/media_servers` (Lead applies the group_vars edit the builder specifies). Everything that references
  `wifi_iface` (rules and the DOCKER-USER Wi-Fi drop) runs only when `wifi_iface` is defined. The tailnet-spoof
  guard and the convergence/pruning logic are unchanged. The container-DNS rules (`docker_dns_ip`) stay
  wherever docker is converged.
- `base`, `tailscale`, `dns`, `docker`: reused unchanged. If any hard media dependency turns up, the builder
  reports it to the Lead instead of editing around it. `watchdog` is not applied to ai-box yet.
- ai-box declared rules: 22/tcp on tailscale0; 22/tcp on `lan_iface` from `gaming_pc_lan_ip` and from
  `mediaserver_lan_ip` (the recovery key, see memory). Later phases add service ports **on tailscale0 only**.

### 12.3 Playbooks (Lead)
`bootstrap.yml` targets `media_servers:ai_servers`. `ai-server.yml`: base → users → ssh_hardening → tailscale →
dns → firewall → docker. **Acceptance (Integrator):** bootstrap once; ai-server.yml twice with the second run
`changed=0`; `site.yml --check --diff` on mediaserver `changed=0`; from the gaming PC's LAN address only 22/tcp
answers on ai-box's LAN IP; lint clean.

### 12.4 gpu-gate (module J: `apps/gpu-gate/`; role `gpu_gate` comes later)
Runs on the **gaming PC** (Fedora 44, RX 7600 XT, amdgpu). Python 3 **stdlib only**, a systemd system service.
- **Sampling** every `interval` (5 s):
  - `gpu_busy`: `gpu_busy_percent` from the first `/sys/class/drm/card*/device` that has it (overridable).
  - `ollama_share`: the percent of the interval that Ollama processes spent on the gfx engine, from
    `/proc/<pid>/fdinfo/*` `drm-engine-gfx: N ns` deltas, de-duplicated by `drm-client-id`. Ollama processes:
    comm/exe matches `ollama` (configurable).
  - `game_util = clamp(gpu_busy - ollama_share, 0, 100)`.
  - `avg30`: the mean of `game_util` over the trailing `window` (1800 s); computed over the samples present
    while the window fills.
- **Game detection** (each sample, first match wins, report the name):
  - environ `SteamAppId=<n>` with n not 0.
  - Proton/wine processes (`wine*`, `*.exe`).
  - `gamescope`.
  - java whose cmdline mentions `minecraft` or a Prism instance.
  - Config `games` (allow, by comm/exe substring) and `ignore` (deny) lists.
  - Reading other users' `/proc/*/environ|fdinfo` needs ptrace-read: run with the least privilege that works
    and document it.
- **Rule (Noah):** `available = avg30 < threshold(50) OR game is None`. `reason` says which clause decided it.
- **On available→unavailable:** for each model in Ollama `GET /api/ps`, `POST /api/generate
  {"model": m, "keep_alive": 0}` against `ollama_url` (default `http://127.0.0.1:11434`). Errors are logged,
  never fatal.
- **API:** `GET /status` → `{"available", "reason", "avg30", "samples", "game", "gpu_busy", "ollama_share",
  "ts"}`; `GET /healthz`. Binds the configured addresses (the gaming PC's tailnet IP), port **11500**. No auth;
  exposure is limited by firewalld to ai-box's tailnet IP (role `gpu_gate`, later).
- **Config:** a JSON file (path by flag); every value above has a default. Logs to stderr (journald); `--once` prints only the status JSON on stdout (amended 2026-10-26).
- **Tests:** `python3 -m unittest` against fake `/sys` and `/proc` trees and a stub Ollama. Window math, the
  fdinfo exclusion, both clauses of the rule, game detection on fixtures, the unload call on the transition.

### 12.5 gpu-broker (module K: `apps/gpu-broker/`; role `ai_stack` comes later)
Runs on **ai-box** in a container. Python 3.12+. Minimal pinned deps (an ASGI server + an async HTTP client),
pinned in the app's lockfile; the image tag goes in `versions.yml` → `local_images`.
- An **Ollama-compatible proxy** on container port 11434: `/api/chat`, `/api/generate`, `/api/embed`,
  `/api/embeddings`, `/api/show`, `/api/tags`, `/api/ps`, `/api/version`, `/v1/*` (OpenAI-compatible) pass
  through with **streaming preserved** (NDJSON and SSE, chunk by chunk).
- **Routing** per request: ask gpu-gate `GET /status` (cache `status_ttl` 5 s, timeout 1 s; any failure =
  unavailable) → `gpu_url` (gaming PC Ollama) if available, else `cpu_url` (ai-box Ollama).
- **Aliases:** config `aliases: {name: {gpu: model, cpu: model}}`. A request for an alias has its `model` field
  rewritten for the chosen backend; non-alias models pass through unchanged to the chosen backend.
  `/api/tags` returns the aliases (as if they were models) plus the CPU backend's models.
- **Failover:** if the GPU backend fails before the first response byte, retry once on CPU.
- Model management (`/api/pull`, `/api/delete`, `/api/create`, `/api/copy`, `/api/push`) goes to the **CPU
  backend only**.
- Every response carries header `X-Backend: gpu|cpu`. One log line per request: path, alias, backend, status,
  latency.
- `GET /broker/healthz` and `GET /broker/status` (the last gate answer + cache age).
- **Tests:** stub gate + stub GPU/CPU backends. Routing both ways, alias rewrite, streaming chunk boundaries,
  failover, management-to-CPU, gate timeout ⇒ CPU.

### 12.6 Module ownership (additions to §10)
| Module | Builder owns |
|---|---|
| A host (amended §12.2) | as §10, plus the `users_media_accounts` and `firewall_allow_rules` refactors |
| J gpu-gate | `apps/gpu-gate/` |
| K gpu-broker | `apps/gpu-broker/` |

### 12.7 Lead decisions on builder questions (2026-10-26)
- **A:** `firewall_allow_rules` moves into `group_vars/media_servers` (Lead applied it). On ai-box the `docker`
  group is created by the role that installs Docker (§12.8), so `users` only adds the admin when the group exists.
  The second converge run must be `changed=0`. A one-time change on the first run that installs Docker is accepted.
- **K:** aiohttp (one dependency, its own HTTP server) is accepted in place of "ASGI server + async client".
  The extra paths (`/api/blobs/*` CPU-only, `GET|HEAD /`, the `/v1/models` merge, 404 otherwise) are accepted.
  - **Failover:** 500/502/503/504 fail over, 404 does not.
  - **`/api/tags` with the CPU down:** 200 + aliases + `X-Broker-Warning`.
  - **Management calls:** not alias-rewritten.
  - **Logging:** `/broker/*` logs at DEBUG.
  - **GPU first-byte timeout:** off by default.
  - **Base image:** pinned by digest in `versions.yml` → `images.python_base` and passed as a build arg by
    `ai_stack`. Image `local_images.gpu_broker: homelab/gpu-broker:0.1.0`.
- **J:** the `/status` details are part of the interface:
  - `ts` is epoch float.
  - `reason` ∈ `no_game|below_threshold|game_busy|game_no_samples`.
  - `/status` returns 503 only before the first sample.
  - `IP_FREEBIND` binds and `--once` are accepted.
  - **Privileges:** DynamicUser + `CAP_SYS_PTRACE` only, with the ptrace and memory-read syscalls filtered. The
    Steam rule reads the world-readable `reaper SteamLaunch AppId=` cmdline.
  - **Open risk:** ROCm work through `/dev/kfd` may not appear in `drm-engine-*` fdinfo, which would make
    `ollama_share` 0. **Verify with `--once` during inference before relying on the 30-min average.**
  - **PCI slot address in fixtures:** acceptable (it isn't an identifier on the §0 list).

### 12.8 Docker on ai-box (module C amendment, pending a builder)
The `docker` role assumes Docker is preinstalled. For `ai_servers` it gains `docker_install: true`, which
installs Docker Engine + the compose plugin from Docker's official apt repo, with versions from `versions.yml`.
This matches the mediaserver's source. The default stays `false`, so the mediaserver is unchanged.

### 12.9 Lead decisions after review round 1 (2026-10-26)
- **gpu-gate privileges (replaces §12.7):** the gate runs as **`User=ollama`, with no capabilities**.
  - **Why:** CAP_SYS_PTRACE also opens `/proc/<pid>/root|cwd`, which defeats the sandbox.
  - **What still works:** it reads the fdinfo of the Ollama processes it shares a uid with.
  - **Game detection:** uses only world-readable `comm`, `cmdline` and `stat` (Steam `reaper SteamLaunch
    AppId=`, wine/`.exe` names, gamescope, java+minecraft cmdline). It never reads `exe` links or `environ`
    of other users. The Steam environ rule becomes cmdline-based.
- **gpu-gate engines:** default `ollama_engines: ["gfx", "compute"]`. It applies only to Ollama's own
  processes, so counting both is strictly more accurate.
- **gpu-gate staleness:** `/status` returns **503** when the last successful sample is older than 3 intervals,
  the same rule as `/healthz`. The broker already treats any non-200 as unavailable.
- **gpu-broker:** responses the broker generates itself (404/405/413/400 and `/broker/*`) carry
  **`X-Backend: broker`**. A request path that changes when normalized (`..`, `%2e`, `//`) gets **400**.
  Buffered (alias-rewritten) bodies are capped by a concurrency limit.
- **gpu-broker buffering (round 2, replaces the count limit above):**
  - **Byte budget:** buffering is limited by a byte budget, `max_buffered_bytes`, default 256 MiB.
  - **Small bodies:** a body of 1 MiB or less never waits for budget.
  - **Body reads:** time out after `body_read_timeout` (30 s) → 408 with `X-Backend: broker`.
  - **Waiting for budget:** times out after `buffer_wait_timeout` (10 s) → 503 with `X-Backend: broker`.
  - **Encoded separators:** paths containing `%2F` or `%5C` (any case) → 400.
- **gpu-gate request deadline:** each connection has an overall request deadline (10 s) on top of the per-read
  timeout, so slow-trickling clients can't hold every handler slot.
- **Integrator finding (2026-10-26), UFW resets `log_martians`:** UFW applies `/etc/ufw/sysctl.conf` on every
  start (boot, enable, reload, via `IPT_SYSCTL`), after systemd-sysctl. That file ships `log_martians=0`, so the
  base role's live-value assert fails after any boot or UFW start. This was latent on the mediaserver: it passes
  today only because base last ran after UFW's last start.
  - **Fix (module A, role `firewall`):** manage both `log_martians` lines in `/etc/ufw/sysctl.conf` as `=1`, so
    UFW agrees with base. No other key conflicts.
  - **Exception:** this deliberately changes the mediaserver (two lines in that file). It's an approved
    exception to §12.2's "unchanged".
- **Known minors, accepted for now (Reviewer round 3):**
  - gpu-broker: a corrupt gzip body is answered 400, but aiohttp's drain then logs one ERROR traceback per
    request. This is log noise a tailnet client could repeat. Fix in a follow-up: drain quietly, or close the
    connection.
  - gpu-broker: a client that declares a large `Content-Length` and stalls holds that budget for up to
    `body_read_timeout`.
  - gpu-broker: waiting for budget isn't first-come.
  - gpu-gate: reconnecting slow clients can still starve some requests. It fails safe to CPU.
- **Base self-heal (amends §12.2 "base reused unchanged"):** base reads the live values of every key it
  asserts. On drift it re-applies `99-zz-homelab-hardening.conf` once (reported changed), re-reads, and keeps the
  assert as the final guard. In check mode, drift is reported as a would-be change and doesn't fail the run (the
  sysrq-not-zero rule still applies). A converged host stays `changed=0`. This is the base half of the UFW
  `log_martians` fix; without it a drifted host deadlocks at base.
  - base test guard (`tests/live_setup.yml`): it can be bypassed by deliberately overriding the internal `live_test_tmp` with `-e`. Harden it: realpath not under `/proc`, tmp under the system temp dir and created by this run.

### 12.10 Phase 3b/3c roles (build started 2026-10-26, code + review only; gaming PC changes wait for Noah)
**Models are Noah's decision.** Every model name is a variable with **no default** (`ai_gpu_model`,
`ai_cpu_model`, `ai_embed_model`). Roles must converge with them unset, which means no pulls, and the broker
alias config is rendered only when they are set. Versions come from `versions.yml`: Ollama `v0.40.11` (asset
`ollama-linux-amd64-rocm.tar.zst` + `ollama-linux-amd64.tar.zst`, sha256 from the release's `sha256sum.txt`
pinned in versions.yml); Open WebUI `v0.12.4` (`ghcr.io/open-webui/open-webui:v0.12.4` pinned by digest);
the Ollama container image for ai-box pinned by digest.

**Module G (new): roles `ollama_rocm`, `gpu_gate`, playbook `playbooks/gaming-pc-ai.yml`** (new, Lead-owned:
hosts `workstations`, `become: true`, separate from the user-level `gaming-pc.yml`).
- `ollama_rocm`:
  - **Install:** native Ollama from the pinned release tarballs (sha256 verified) under
    `/usr/local/lib/ollama` + `/usr/local/bin/ollama`. System user/group `ollama`; the user is in `render`
    and `video`.
  - **Service:** systemd `ollama.service` with `OLLAMA_HOST={{ gaming_pc_tailnet_ip }}:11434`,
    `OLLAMA_KEEP_ALIVE=5m`, `OLLAMA_MAX_LOADED_MODELS=1`, and hardening that still allows `/dev/kfd` and
    `/dev/dri`. Models live in `/var/lib/ollama`.
  - **SELinux (Fedora):** must not need permissive mode.
  - **Firewall:** firewalld. **tailscale0 stays in zone `trusted`** (other things rely on it). Add rich
    rules that reject 11434/tcp and 11500/tcp from any source except `{{ ai_box_tailnet_ip }}` (IPv4 and
    IPv6 tailnet addresses).
  - **Pull:** `ai_gpu_model`, only when set.
- `gpu_gate`:
  - **Install:** deploys `apps/gpu-gate` to `/opt/gpu-gate` (owned by root, read-only to the service) and
    its reviewed unit (`User=ollama`, no capabilities).
  - **Config:** `/etc/gpu-gate/config.json`, with `binds: [gaming_pc_tailnet_ip]`, port 11500, and
    `ollama_url` set to the tailnet address. Enabled and started.
- **Acceptance (Integrator, later, with Noah present):**
  - `ollama --version` reports the pin.
  - `/status` is reachable from ai-box and refused from the LAN and other tailnet nodes.
  - The ROCm accounting check (§12.7).

**Module H2 (new): role `ai_stack`** (ai-box, appended to `ai-server.yml` by the Lead).
- **Compose stack** `{{ homelab_stacks_dir }}/ai/compose.yaml`:
  - `ollama-cpu`: official image, digest-pinned, CPU only, `OLLAMA_KEEP_ALIVE=10m`, models on a named volume.
  - `gpu-broker`: built from `apps/gpu-broker` with `--build-arg PYTHON_IMAGE={{ images.python_base }}`,
    tagged `local_images.gpu_broker`, config at `/etc/gpu-broker/config.json` via `GPU_BROKER_CONFIG`.
  - `open-webui`: digest-pinned. `OLLAMA_BASE_URL=http://gpu-broker:11434`. `WEBUI_AUTH=true` (Noah creates
    the first admin account in the browser; the role never creates accounts). Telemetry off. Data on a named
    volume.
- **Broker config:** `gpu_url` is the gaming PC's tailnet Ollama, `gate_url` the gaming PC's tailnet gate,
  `cpu_url` is `http://ollama-cpu:11434`. Aliases come from the model vars when set.
- **Publishing (tailnet only):** Open WebUI on `{{ tailnet_ip }}:8080`, the broker on `{{ tailnet_ip }}:11434`
  (for HA on the mediaserver). Nothing listens on the LAN IP, and the firewall gets matching
  `firewall_allow_rules` on tailscale0. The broker port is restricted to the mediaserver's and the gaming PC's
  tailnet IPs if the firewall schema allows; otherwise tailnet-wide (decide and report).
- **Memory budget (8 GB box):** `mem_limit` on each container. Ollama-cpu gets most of it; the broker 256m;
  Open WebUI about 1.5g.
- **Acceptance (Integrator):**
  - Converge twice, second run `changed=0`.
  - `curl {{ tailnet_ip }}:11434/broker/healthz` returns 200 with `X-Backend: broker`.
  - With the gate unreachable (the gaming PC isn't set up yet) the broker routes to CPU.
  - Open WebUI's login page loads over the tailnet.
  - A LAN scan shows nothing new.
- **ai_stack Lead decisions (2026-10-26):**
  - **Accepted additions:** a host-generated `WEBUI_SECRET_KEY` (no vault), H2's extra hardening
    (`ENABLE_OPENAI_API=false`, `DEFAULT_USER_ROLE=pending`, one model loaded and one request at a time on
    the CPU, a read-only root and no capabilities for the broker), and the boot helper that starts the stack
    once the tailnet address exists.
  - **Exposure fix (module A, required before deploy):** DOCKER-USER drops any NEW connection whose original
    destination is `{{ tailnet_ip }}` unless it arrived on tailscale0. This closes LAN hosts routing to the
    tailnet IP through the LAN interface and reaching the unauthenticated broker.
  - **Deferred to Phase 4:** the tailnet policy grants (mediaserver → ai-box:11434; ai-box → gaming-pc
    11434/11500), the embeddings model, and backups of `open-webui-data`.
- **Module G Lead decisions (2026-10-26):**
  - The gate binds the gaming PC's tailnet IPv4 **and** IPv6. Ollama binds IPv4 only, so the broker's `gpu_url`
    uses IPv4.
  - `ollama_rocm` owns every firewalld rich rule on 11434/tcp and 11500/tcp in zone `trusted`, and prunes
    stale ones.
  - Prune the unused `lib/ollama/cuda_*` after extraction (AMD-only host): a small follow-up for Builder G.
  - `--check` on a fresh gaming PC failing at the user and directory steps is accepted and documented.
  - `IPAddressAllow` on the gate and `MemoryDenyWriteExecute` on Ollama are deferred to host testing with Noah.
- **ai_stack exposure decisions (2026-10-26, after review):**
  - **Broker reach:** the broker (11434, unauthenticated) is reachable from the **whole tailnet** until the
    Phase 4 tailnet policy grants land. The UFW `from:` rules only document intent (DNAT bypasses INPUT). This
    is accepted: the tailnet is Noah's own devices plus his servers.
  - **Open WebUI first sign-up:** the first account becomes admin. Noah creates his account right after deploy,
    then sets `ai_webui_enable_signup: false`. The window is tailnet-only.
  - **Swap:** containers get `memswap_limit` = `mem_limit`, so they're OOM-killed rather than thrashing the HDD.

### 12.11 AI Library (Phase 4.5, module L: `apps/skill-library/`, the `library` package; build started 2026-10-26)
The plan: storage on the gaming PC at `/mnt/share/ai-library` (the NTFS "share" drive), soft cap **500 GB**,
exported later by Samba to ai-box's tailnet IP only. The share and mount roles come later, with Noah present.
This module is the **pure-Python core** that files, indexes and caps the library. It is used by the Phase 5
features and by Noah's tooling.
- **Layout** (created on `init`): `README.md` (rules), `INDEX.md` (generated), `00-inbox/`,
  `10-sources/<topic>/`, `20-notes/<topic>/`, `30-skills/<skill>/`, `40-writing/{samples,drafts}/`,
  `50-finance/log/`, `90-archive/`.
- **Naming:** `YYYY-MM-DD_kebab-title.ext`. **Topic and skill names:** kebab-case `[a-z0-9-]{1,48}`.
- **Front-matter:** every `.md` starts with YAML: `title, source, added (ISO date), tags (list), summary,
  related (list)`. A non-Markdown original gets a sidecar `<name>.<ext>.md` with the same header.
- **Operations** (library API + CLI `python3 -m library`):
  - `init`
  - `file <path> --topic T [--title --source --tags --summary]`: copies into `10-sources/T/`, writes the
    sidecar, never overwrites (adds a `-2` suffix).
  - `inbox --topic-map` / `inbox --default-topic unsorted`: moves items out of `00-inbox` and records each move
    in `90-archive/inbox-log.md`.
  - `index`: rebuilds `INDEX.md`, one line per item, grouped by folder, sorted; deterministic, so an unchanged
    library gives identical bytes.
  - `cap`: usage vs the cap; warns at 90 %, and `file`/`inbox` refuse at 100 %.
  - `archive <path>`
  - `check`: validates the naming and front-matter of every item; reports, never fixes.
- **Never deletes;** archives instead. Every write stays inside the library root: no symlink escapes, no path
  traversal. Writes are atomic (tmp + rename). It must work on NTFS (ntfs3: no POSIX permissions; case-insensitive
  collisions possible, so handle `Foo.md` vs `foo.md`).
- **Stdlib only,** Python 3.12+. The front-matter is a small YAML subset (written and parsed by this module),
  with no PyYAML.
- **Tests:** `python3 -m unittest` on temp dirs: every operation, idempotent `index`, cap thresholds, a
  collision suffix, a case-insensitive collision, symlink/traversal refusal, and malformed front-matter in `check`.
- **Module G review decisions (2026-10-26):**
  - **M1 (LAN route to the tailnet IP):** replace the zone-`trusted` rich rules with a **firewalld policy**,
    `homelab-ai-only`: `ingress-zones: ANY`, `egress-zones: HOST`, negative priority (it runs before the zones).
    Its rich rules reject 11434/tcp and 11500/tcp from any source except ai-box's tailnet IPv4/IPv6 (single host
    addresses, asserted). That covers every interface (tailscale0, the LAN, Wi-Fi). Prune any legacy zone rules
    on those ports. Add a read-only assert that the policy is active.
    - **Integrator acceptance:** a LAN host with a static route to the gaming PC's tailnet IP via its LAN IP must
      be refused.
  - **Minors to fix:**
    - atomic upgrade (stage under `/usr/local/lib`, then rename-swap, then restart);
    - `--no-same-owner`;
    - add-then-prune;
    - prune both runtime and permanent rules;
    - host-address-only asserts;
    - assert that the gpu_gate install dir basename is `gpu-gate`;
    - handle a null `ai_gpu_model`;
    - a pull wait of ≥ 120 s;
    - the gpu_gate test asserts no change on its third install;
    - also prune the unused `lib/ollama/cuda_*` (already decided).
  - **Module J:** correct the comment in `gpu-gate.service` about which role owns the 11500 rules.
- **Module L Lead decisions (2026-10-26):**
  - **Accepted as built:** the topic-map format, the stricter topic names, the exemption list, `added` = the
    filing date, `cap` exit 3, auto-reindex, folder archive, the cap passed by flag/env, the extra README rules,
    and single-writer.
  - **Mount safety (for the later Samba/mount role):** `init` and every command run only when the share is
    really mounted. Use a sentinel file `.ai-library-sentinel` on the share plus `RequiresMountsFor`. Never run
    on an empty mount point.
  - **NTFS:** the Integrator runs the suite once with `TMPDIR` on the share.

### 12.12 Skill Forge (Phase 5; Noah, 2026-10-26: approve via BOTH files and Open WebUI; sandboxed code skills allowed)
The AI may extend itself only through a gated, human-approved pipeline. **The files are the source of truth;**
Open WebUI buttons only edit those files.
- **Layout additions to the library:**
  - `35-forge/<request-id>/`: one folder per feature request (the id is `YYYY-MM-DD_kebab-title`).
  - `95-shelf/<skill>/`: denied skills, kept with their draft **and** their approved sources, so they can be
    revived without re-research.
- **Request lifecycle**, with a state file `state.md` holding front-matter `status:` plus an append-only log.
  Every step waits for Noah:
  1. **`request.md`:** Noah's words, verbatim.
  2. **`searches.md`:** proposed queries, each a checkbox line `- [ ] <query> — why: <plain-English justification>`.
     Noah ticks `[x]` to approve, or edits the query. **No network access before this approval.**
  3. **`sources.md`:** after only the approved queries have run, one checkbox per candidate: URL, title, why,
     and what will be taken. Only the result snippets from the search API are shown at this stage; **no page is
     fetched** before approval.
  4. **Ingest:** only the ticked URLs are fetched, through the research gate, and filed into
     `10-sources/<topic>/` with front-matter `source:` = the URL, plus `forge: <request-id>`.
  5. **`skill/`:** the draft (`SKILL.md`, rules/checklist/examples; optional `code/` + `manifest.md` declaring
     inputs, outputs and the files or folders it may read). Noah sets `decision: approve | deny | revise` in
     `review.md`, and may edit any file first.
  6. **On approve:** the skill moves to `30-skills/<skill>/` with `CHANGELOG.md`. **On deny:** the request
     folder moves to `95-shelf/<skill>/`. **On revive:** it moves back to `35-forge/`, starting at step 5 with
     no new searches.
- **Research gate** (a separate service; the only thing with internet egress):
  - It accepts a search only if the exact query string is ticked in that request's `searches.md`.
  - It fetches a URL only if it is ticked in `sources.md`.
  - It logs every call to `35-forge/<id>/gate-log.md`, with size and time limits.
  - The search backend is a **pending decision** (recommendation: self-hosted SearXNG on ai-box).
- **The model has no internet.** The AI containers sit on a network with no egress except the research gate.
  Fetched content is **data, never instructions**; injection is mitigated by the approval gates and by never
  auto-acting on source text.
- **Code skills:** they run only after Noah approves the exact code (the content hash is recorded in
  `review.md`; any change needs re-approval).
  - **Sandbox:** a container with no network, a read-only root, and no capabilities.
  - **Access:** only the paths declared in `manifest.md`, mounted read-only unless the manifest says otherwise
    and Noah approved that.
  - **Limits:** CPU, memory and time.
- **Module F (forge core, offline, stdlib):** request scaffolding, checkbox parsing, state transitions,
  shelve/revive, hash-pinned approval, and gate authorization checks (`is_query_approved`, `is_url_approved`).
  Built on `library`.
  - **Later:** the research gate service, the sandbox runner, and Open WebUI actions.
- **Module G APPROVED (round 2, 2026-10-26).** Residuals, accepted. Each gets a README note or an Integrator step:
  - Between the binary swap and the library swap, a crash leaves the new binary with the old libraries until the
    next run repairs it.
  - `firewall-cmd --reload` (only on the first run or on drift) drops runtime-only firewalld changes made by hand.
    Tell Noah.
  - **Integrator:** `firewall-cmd --list-all-policies` on the host, to rule out an earlier-priority accept policy.
  - The CUDA prune pattern misses `mlx_cuda_*`: disk space only.
- **Phase 3b live run (with Noah present), Integrator checklist:**
  1. `ansible-playbook playbooks/gaming-pc-ai.yml` twice; the second run is `changed=0` with no reload.
  2. `ollama --version` = v0.40.11; `ps -eZ | grep ollama` → `unconfined_service_t`; no AVCs (`ausearch -m avc -ts recent`).
  3. GPU detected: the Ollama logs show ROCm with gfx1102. If it's not supported, use `ollama_rocm_extra_env`
     `HSA_OVERRIDE_GFX_VERSION`.
  4. Firewall: from ai-box, `curl <pc-tailnet>:11434/api/version` and `:11500/status` succeed. From the mediaserver
     (tailnet) and from a LAN host with a static route to the PC's tailnet IP, both are refused.
     `firewall-cmd --list-all-policies` is reviewed.
  5. §12.7: with a model loaded and generating, `sudo -u ollama /opt/gpu-gate/gpu-gate --once` shows
     `ollama_share` > 0. If it's 0, ROCm work is invisible in fdinfo: stop and bring the options to Noah.
  6. End-to-end: PC idle → broker `X-Backend: gpu`; start a game → it flips to `cpu` and VRAM is freed; quit → `gpu`.
- **Skill Forge trust model (Lead, 2026-10-26; this is binding for the gate, the sandbox and the Open WebUI phases):**
  - **No direct write access for the AI:** the AI processes (models, agents, Open WebUI tools) have NO write access
    to the library share.
  - **Narrow forge service:** they reach the forge only through a service that exposes `new`, `propose-searches`,
    `propose-sources`, `record-ingest` (called by the research gate), `draft-skill`, `status` and `authorize`, and
    **refuses** `decide`, `revive` and any edit of ticks or `review.md`.
  - **Noah's channels:** his ticks and decisions arrive only through the share mounted as Noah's own account, or
    through Open WebUI actions authenticated as Noah's admin account (they call a separate approver endpoint).
  - **Code skills:** approving content that changed since the draft requires Noah to confirm the first 8 hex
    characters of the new hash.
- **Module F Lead decisions:**
  - **Accepted:** the extras (`verify`, `authorize`); a draft with zero ticked sources (searches must still be
    done); approving over an existing skill archives the old one; `record-ingest` requires the header to have
    `source:` = the URL and `forge: <id>` (the gate writes them).
  - **Crash windows:** documented, with manual repair (nothing is deleted, everything is logged).
  - **Still to add later:** a no-research request path (a request may go straight to `draft-skill` if Noah ticks
    "no research needed" in `searches.md`).
  - **Library follow-ups for Builder L:**
    - add `35-forge/` and `95-shelf/` to the layout;
    - `check` exempts `30-skills/<skill>/code/**`, `manifest.md` and `CHANGELOG.md` naming;
    - `draft-skill` requires library headers on `SKILL.md` and `manifest.md`;
    - `INDEX.md` excludes `35-forge/` and `95-shelf/` (`forge status` covers them);
    - make `abs_path`, `ensure_dir`, `write_new`, `cap_gate` and `move_dir` public.
  - **Research gate (later):** follow no redirects unless the target is also ticked; refuse private and loopback
    IPs after DNS; fetch the canonical URL exactly.
- **Skill Forge, "what Noah sees is what goes out" (Reviewer BLOCKER B1, 2026-10-26):**
  - **Allowed characters:** search queries and source fields are printable-only. Unicode categories
    Cf/Co/Cn/Cs, the variation selectors, the bidi controls, `<`, `>` and backticks are refused, and so is any
    lookalike dash before `why:`.
  - **Checked at parse time too:** a ticked line containing any of these counts as NOT approved (fail closed).
  - **Code skills:** approving changed code requires `confirm_hash:` (the first 8 hex of the new hash) in
    `review.md`.
- **AI Library share (for the later Samba role):** the library assumes one writer and no symlinks. The Samba share
  must stop clients creating symlinks: no Unix extensions, `follow symlinks = no`, `wide links = no`. The library
  must also be mounted only through the sentinel check. **Accepted limit:** a symlink swapped in mid-operation by a
  hostile local user is out of scope (documented in the README "Limits" section).
- **Skill Forge, round 2 decisions (2026-10-26):**
  - **ASCII only:** search queries and `why` justifications are printable ASCII (0x20–0x7E), with no `<`, `>`,
    backticks or `" - why:"`-like forms. This applies at propose time and at parse time, and closes every
    lookalike-separator and blank-glyph spoof. Source titles may stay Unicode, because they can't change what is
    approved; the URL rules already apply.
  - **Code:** any file in `skill/` other than `SKILL.md`, `manifest.md` and other `*.md` counts as code
    (case-insensitive), so it triggers the manifest requirement and `confirm_hash`.
  - **Restore on failure:** if the move fails, approval restores the archived previous version.
- **Module L APPROVED (round 3).** The three minors are accepted as follow-ups:
  - an inode mismatch → stay pending (not a false "undone");
  - flag an orphan empty item placeholder directly;
  - README note: run `inbox` on the host with the ntfs3 mount, not over CIFS without `serverino`.
- **Skill Forge, round 3 decisions (2026-10-26, Reviewer BLOCKER: a rendered Markdown image exfiltrates through
  Noah's own viewer):**
  - **Queries and justifications:** allowlist `A-Z a-z 0-9`, space, and `. , : ; ? ' " / + = % @ - ( )`. The
    substring `why:` (any case) is refused inside the text.
  - **Source title, why and take:** may be Unicode, but refuse `[ ] ! & \ * _ ~ |`.
  - **Viewing:** `forge status <id>` prints every query and URL raw (repr) with its tick state; that is the
    authoritative view. The future Open WebUI approver shows forge files as **plain text, never rendered
    Markdown**, and no view of forge files ever loads remote resources.
- **Module F APPROVED (round 4) and module L APPROVED (round 3 + follow-ups), 2026-10-26.** Follow-ups:
  - write URLs in `sources.md` inside a code span (backticks are already refused in URLs, so nothing can break
    out), so URL-path Markdown (`_ * ~ &` entities) can't change the rendered view; until then, Noah views
    `35-forge/` in Obsidian source mode;
  - typographer rendering is cosmetic and needs no fix.
  - **Closed (2026-10-26):** URLs in `sources.md` are now code spans, so the source-mode caveat no longer applies (Reviewer-confirmed, 191 tests).
- **Integrator findings after the room move (2026-10-26, Noah power-cycled everything):**
  1. **ai-box clock deadlock.** The RTC lost time while the box was unplugged (likely a dead CMOS battery), and
     systemd fell back to 2026-07-27. With a wrong clock, Tailscale's TLS fails, and so does resolved's
     DNS-over-TLS. Plaintext DNS out is blocked by UFW, so chrony can't resolve its NTP servers, and nothing ever
     recovers by itself. **Fixed by hand** (date set from the gaming PC, `hwclock --systohc`).
     - **Required fix (module A, `base`), amended after review:** the real cause is chrony 4.8's default
       `authselectmode mix`. With NTS sources configured, every NTS source is *required*, so the IP sources can
       never be selected while NTS fails (confirmed on ai-box: `R` in EOpts). Fix: `authselectmode ignore`
       (NTS keeps `prefer`, so the IP sources are used only when no NTS source is selectable), plus
       `makestep 1 -1`, plus **2** IP sources (one Cloudflare, one Google) so NTS keeps the falseticker
       majority. The assert checks `chronyc -n selectdata`. No `nocerttimecheck`.
     - **Hardware:** replace ai-box's CR2032.
     - The mediaserver has the same design and is safe only while its RTC holds.
  2. **ai_stack boot helper doesn't restore published ports.** When containers start before the tailnet IP
     exists, Docker 29 runs them **without** their tailnet port bindings: no error, an empty `docker port`.
     `docker restart` doesn't fix it; only a recreate does.
     - **Required fix (module H2):** the helper waits for the tailnet IP, then runs
       `docker compose up -d --force-recreate --no-deps` for the tailnet-published services whose `docker port`
       is empty (not `docker start`).
     - Also the §9b note: published ports are DNAT-only, and with a missing host IP they are silently dropped.
  3. **ai-box LAN address changed** (.149 → .147, no DHCP reservation). This breaks the mediaserver recovery
     path (`Host ai-box` → .149) and ai-box's `lan_ip` host_var. **Needed:** a DHCP reservation on the gateway
     (Noah), or a static address, then update host_vars and the mediaserver's ssh config.
- **Clock deadlock CLOSED (Integrator, 2026-10-26):** base was deployed to ai-box only (run 2 `changed=0`); then
  the live test. `chronyc -n selectdata -a` showed 0 sources with `R`. `date -s 2026-07-27` plus a chrony restart
  stepped back to the correct time in **≈15 s**, with the Google IP source selected and DNS working again. The
  mediaserver gets the same change on its next real converge (approved).
- **ai_stack boot helper + heal + lock DEPLOYED (Integrator, 2026-10-26):** run 2 `changed=0`, no leftover
  lock units, lock file `/run/homelab-ai-stack.lock` root:root. Cold reboot: Open WebUI and the broker answered
  over the tailnet about 2 min after boot. The helper logged both bindings present and needed no recreate (the
  tailnet was up in time). The late-tailnet recreate path is covered by the 12 render-test cases. Live proof is
  still pending a boot where the tailnet comes up late.
- **Models chosen (Noah, 2026-10-26):** `ai_gpu_model: qwen3.5:9b` (gaming PC) and `ai_cpu_model: qwen3.5:4b`
  (ai-box), both in `group_vars/all`. The broker alias is `assistant`. Measured on ai-box: 4B ≈ 7.8 tok/s.

### 12.13 AI operator account `claude` (Noah, 2026-10-26: full sudo on every machine, fully logged; separate from `ansible`)
Purpose: everything the AI (Claude) does runs under its own account, so it is attributable in logs, and the AI can
run playbooks unattended. `ansible` stays the account Noah's own playbook runs use. `mediaadmin` and `hunter` stay
Noah's.
- **Account:** `claude` (system user, `/bin/bash`, home `/home/claude`), password locked, SSH key only. Its public
  key is `operator_pubkey` (host_vars/group_vars; the private key lives only in the gaming PC's
  `~hunter/.ssh/claude_ops_ed25519`).
  - On the **servers**, the key is restricted with `from="<gaming_pc tailnet>,<gaming_pc lan>"`.
  - On the **gaming PC**, `from="127.0.0.1,::1"`. The AI reaches it as `ssh claude@localhost`, and sshd gets a
    `Match User claude` block that refuses anything except loopback.
- **sudo:** `/etc/sudoers.d/20-claude-operator` (validated by visudo), containing `claude ALL=(ALL) NOPASSWD: ALL`,
  plus:
  - **Full I/O logging:** `Defaults:claude log_input, log_output, iolog_dir=/var/log/sudo-io/claude,
    iolog_file=%{seq}`, with `Defaults:claude !requiretty`.
  - **Retention:** a systemd-tmpfiles rule deletes I/O logs older than 90 days.
  - **Journal:** every command is also in the journal, via sudo's syslog line with `USER=root`, `COMMAND=…` and
    `claude`.
  - **Replay:** a README note on `sudoreplay -d /var/log/sudo-io/claude -l` to list sessions.
- **Groups:** none beyond what sudo needs. Not in `docker` (docker access goes through sudo, so it's logged).
- **SSH:** `ssh_hardening_allow_users` must include `claude` on the servers. On the gaming PC (sshd not managed by
  the repo until now), use a minimal drop-in `/etc/ssh/sshd_config.d/20-claude-operator.conf` with just the Match
  block. It's validated with `sshd -t` before reload, and nothing else in Noah's sshd changes.
- **Bootstrap:**
  - **Servers:** applied with the existing `ansible` account (the Integrator can do this now).
  - **Gaming PC:** needs Noah's sudo once (`-K`) through a new playbook; after that the AI uses `claude@localhost`.
- **Inventory (Lead):** an `operator_user: claude` variable. When the AI runs playbooks it passes
  `-u claude --private-key ~/.ssh/claude_ops_ed25519`, or a later inventory switch. The gaming PC gets a second
  inventory alias, `gaming-pc-root` (`ansible_connection: ssh`, `ansible_host: 127.0.0.1`, user `claude`), for the
  root-level gaming-pc-ai play.
- **Module O (new):** role `operator_account` and `playbooks/operator.yml` (hosts: `media_servers:ai_servers` and
  `workstations`, become), plus tests.
- **Amendment (Noah, 2026-10-26): two separate accounts, not one.**
  - **`claude`:** Claude, the operator. Exactly as above: full sudo, I/O-logged, SSH key from the gaming PC only.
  - **`localai`:** Noah's local AI (Ollama-side agents, the future forge service, Skill Forge tools).
    - It's a **system user** with **no sudo** (no sudoers entry; the role asserts that `sudo -l -U localai`
      grants nothing), **no SSH login** (no authorized_keys, shell `/usr/sbin/nologin`) and **no password**.
    - It owns nothing by default. Later phases grant it specific paths and services explicitly, per the Skill
      Forge trust model: it can propose, but never approve or write approvals.
    - Its actions are attributable by UID in the journal and in file ownership.
  - The role takes a list (`operator_accounts`) with per-account sudo, logging, SSH and shell settings, so the
    two can't drift into sharing.
- **Module O Lead decisions (2026-10-26):**
  - **sudo-rs finding (Builder O):** Ubuntu 26.04's default `sudo` is sudo-rs, which has **no I/O logging**. It
    reads the same sudoers, silently drops the `log_*` Defaults, and would have given claude unlogged root.
    - **Accepted fix:** on sudo-rs hosts, every `full_logged` account joins `operator-no-sudo-rs`, and every sudo-rs
      binary gets `dpkg-statoverride root:operator-no-sudo-rs 4705`, so members can't execute it. That leaves
      `sudo.ws` (classic, logged) as their only path to root, and a verify step proves claude can't run sudo-rs.
    - This supersedes "no groups" for claude. `localai` stays group-less.
  - **Paths are derived from the account name:** `/etc/sudoers.d/20-operator-<name>`, `/var/log/sudo-io/<name>`,
    `/etc/tmpfiles.d/operator-<name>.conf`, `/etc/ssh/sshd_config.d/20-operator-<name>.conf`.
  - **Audit commands:** `journalctl _COMM=sudo _COMM=sudo.ws`; `sudoreplay[.ws] -d /var/log/sudo-io/claude -l`.
  - **sshd Match** (a list of only negations never matches): `Match User claude Address *,!127.0.0.1,!::1` →
    `DenyUsers claude`.
  - **Pending Noah:** whether to switch the servers' `sudo` alternative to `sudo.ws`. That silences the per-run
    "unknown setting" warnings sudo-rs prints for the logging Defaults, and makes Noah's own sudo logged too.
- **Noah, 2026-10-26: switch the servers' `sudo` to classic sudo.** Role `base` sets the Debian alternative
  `sudo` → `/usr/bin/sudo.ws` (`community.general.alternatives`, idempotent; only when that alternative exists).
  This makes everyone's `sudo` the logged classic sudo and silences sudo-rs's "unknown setting" warnings.
  `operator_account`'s sudo-rs guard stays as defence in depth (`/usr/bin/sudo-rs` remains callable).
- **Module O review round 1 (2026-10-26):** CHANGES REQUIRED. M1: derive the sudo-rs guard set from
  `dpkg -L sudo-rs`. M2: a revocable `operator_sudo`/`operator_ssh_login`. Plus minors. Lead fixes:
  - The `gaming-pc-root` alias exists (group `workstations_root`, `claude@127.0.0.1` with the operator key).
    `playbooks/gaming-pc-ai.yml` now targets it, so the AI runs it as claude: logged, no Noah needed.
    `playbooks/operator.yml` still targets `workstations` (local, Noah runs it once with `-K`).
  - **Honest limit (Lead):** the AI's own process runs as `hunter` on the gaming PC, and `hunter` is in `libvirt`,
    whose polkit rule grants root-equivalent libvirt management without sudo. Root work the AI does is logged only
    when it goes through `claude@localhost`, and from here on the AI does all root work that way. Removing hunter
    from `libvirt` is Noah's call (he may use it for VMs).
- **Integrator, accounts and sudo live round (2026-10-26):**
  - **ai-box:**
    - sudo switch applied: classic `Sudo version 1.9.17p2`, manual mode, run 2 `changed=0`.
    - `operator.yml` applied: run 3 `changed=12`, run 4 `changed=0`. Live test passed: claude logs in with its own
      key; `sudo` gives root via classic sudo and is I/O-logged; `/usr/bin/sudo-rs` and `/usr/lib/cargo/bin/sudo`
      give *Permission denied*; `localai` SSH is refused.
    - Verify reported a false negative (`'\b'` inside a Jinja literal is a backspace); being fixed (module O).
  - **Classic visudo prints its own invocation name** (`visudo.ws version …`). Detection is fixed and approved.
  - **mediaserver:**
    - The base sudo probe blocked the switch (fail-safe). mediaadmin there has a *usable* password, so a
      password-required rule is correct; the probe's "passwords are locked" premise is wrong. Being fixed (module A):
      locked passwords need NOPASSWD, usable passwords need an ALL rule.
    - Ansible to the mediaserver failed after the reboot because the ssh-agent was empty. Fixed with
      `ansible_ssh_private_key_file` in the mediaserver host_vars.
- **Integrator finding: Ubuntu 26.04 Rust coreutils (uutils) `test -x` ignores supplementary groups (2026-10-26).**
  - On ai-box, `runuser -u claude -- test -x /usr/lib/cargo/bin/su` reported "executable", although the file is
    `-rws---r-x root:operator-no-sudo-rs` and claude is in that group.
  - A real claude login gets *Permission denied* from the kernel, and the bash builtin `test` agrees. **The block
    works; the check was wrong.**
  - **Rule for every role:** never use `/usr/bin/test` (or other userspace permission re-implementations) to prove
    what another user can access. Attempt the actual operation as that user and check the kernel's result.
  - Fixed in `operator_account` verify (module O).
  - Integrator also checks the other shell helpers on ai-box (the ai_stack boot helper, the dashboard scripts)
    for this pattern.
- **mediaserver, base + ssh_hardening applied (Integrator, 2026-10-26; Noah approved the sudo switch and the
  accounts):**
  - Run 1 `changed=8`: sudo → classic (manual), chrony IP sources + `authselectmode`, **the latent `log_martians`
    drift healed** (live 1), and the AllowUsers drop-in gains claude. Run 2 `changed=0`. All 8 containers up.
  - The passwd -S probe passed (mediaadmin usable password with an ALL rule; ansible NOPASSWD). `passwd` is
    shadow-utils on both servers, with no diversions.
  - Not applied, pending Noah: the firewall role's UFW `log_martians` file fix and the DOCKER-USER tailnet-dest
    rule (the full `site.yml` converge).
  - **MINOR, module A:** `ssh_hardening`'s effective-config assert fails in `--check` when AllowUsers changes,
    because the drop-in isn't written in check mode. Skip or soften that assert in check mode.
  - Module O optional follow-up: in `verify_exec.yml`, require `rc == 126` plus empty stdout for blocked files (drop the "Permission denied" text fallback).
- **§12.7 risk CONFIRMED live, then resolved (2026-10-26):**
  - **ROCm:** Ollama 0.40.11's worker (`llama-server`) shows `drm-memory-vram` (6.4 GiB) but **no `drm-engine-*`
    lines**, because ROCm compute goes through `/dev/kfd` user queues. The gate's `ollama_share` stays 0 at 96%
    GPU busy.
  - **Vulkan** (`OLLAMA_VULKAN=1`, `HIP_VISIBLE_DEVICES=-1`, `ROCR_VISIBLE_DEVICES=-1`, Mesa RADV): the worker
    shows `drm-engine-compute` advancing in step with `gpu_busy`. Measured 42.1 tok/s against ROCm's 46.2 (−9%).
  - **A second bug:** the gate matched Ollama processes by comm/argv0 `ollama`, but the GPU worker is
    `llama-server`.
  - **Noah's decision: Vulkan.**
    - **Module G:** `ollama_backend: vulkan` (default `vulkan`, alternative `rocm`) sets that env; the pull task
      gets a timeout.
    - **Module J:** the gate counts every process owned by the gate's own uid (`ollama`) as Ollama, plus the name
      list (`llama-server` added). Engines `["gfx","compute"]`.
  - **Acceptance:** the corrected verify shows `ollama_share > 0` during generation.
- **Gate `/status` interface amended (Lead, 2026-10-26).** Two keys are added: `uid_match` (bool) and
  `uid_match_reason` (string or null).
  - **Uid guard:** when the gate runs as uid 0, or as a uid ≥ `UID_MIN`, uid matching is turned off for that run,
    with one stderr warning. Only the `ollama_match` names count then.
  - **Consumers:** gpu-broker reads only `available` (and logs `reason`), so it needs no change. Consumers must
    ignore unknown keys.

### §12.14 Finance (Phase 5, pulled forward by Noah 2026-10-26: "I need finances tonight")
- **Module F, `apps/finance-tools/` (new builder).**
  - **What it is:** an Open WebUI Tool, "Money Hub (read-only)": one self-contained Python file, stdlib only.
  - **Where it runs:** inside the `open-webui` container on ai-box. That container can reach Money Hub over the
    tailnet; verified.
  - **What it exposes:** fixed GET functions only, with an **allowlist of paths**, so the model can never form a URL,
    a method or a body. No POST/PUT/PATCH/DELETE anywhere, and no `/api/sync`.
- **Why read-only lives in this code:** Money Hub has **no auth** (any tailnet device can write). A Money Hub read
  token, or a network-level guard, is a later Money Hub item for its `docs/GAPS.md`.
- **Staleness is mandatory output:**
  - Every answer carries Money Hub's `/api/state` sync status and each account's `as_of`/`state`.
  - If `sync_failing`, it is stated first.
  - Reason: on 2026-10-26 SimpleFIN had been returning 403 since about 2026-10-05, so balances were 3 weeks old.
- **Integrity:**
  - Money is integer cents in, formatted dollars out, computed in code.
  - Totals are computed by the tool, never left to the model.
  - Debts' notes are shown as plain text.
- **Install:** Noah pastes the file in Open WebUI → Workspace → Tools (admin), with valve `base_url`. It is enabled
  for the `assistant` model only.
- **Later (not tonight):** the daily digest timer and log (the `assistant_jobs` role); the Notion log.
- **Mediaserver full converge (Noah: "All yes", 2026-10-26).**
  - `site.yml -l mediaserver` was run as `claude`: check mode first, then the apply (changed=5), then a rerun
    (changed=0).
  - **What changed:**
    - UFW's sysctl file keeps martian logging on;
    - the DOCKER-USER guard gains the tailnet-destination rule (5 tagged rules live);
    - dashboard config refreshed with the image pins.
  - **Result:** all 8 containers healthy, and no Docker daemon restart.
- **Lead decisions (§12.14):**
  - Module F's `payday_plan(check="", period="")` is accepted as surface: six tool functions.
  - A `timezone` valve (default America/New_York, UTC fallback with a note) sets "current month".
  - Public methods are `async` and use `asyncio.to_thread`, because Open WebUI calls sync tools on its event loop.
  - CI runs `apps/finance-tools/tests`.
- **Module J (gpu-gate uid/llama-server fix, --once 2 samples): Reviewer APPROVED 2026-10-26 (round 2).**
- **Module G (ollama_rocm: Vulkan backend, extra_env validation, bounded pull): Reviewer APPROVED 2026-10-26 (round 3).**
- **Integrator, 2026-10-26 (gaming PC, as `claude` via gaming-pc-root):**
  - `gaming-pc-ai.yml` run 1: changed=4 (Ollama unit, gate modules and unit). Run 2: **changed=0**.
  - `verify-gaming-pc.sh`, all PASS:
    - Ollama `library=Vulkan` (RADV NAVI33, 16 GiB) under the hardened unit;
    - SELinux `unconfined_service_t` with 0 AVC denials;
    - **the gate sees Ollama's GPU work (`ollama_share` 68.6)**, so the §12.7 risk is closed;
    - `assistant` answered from the GPU at 48.9 tok/s.
  - The live game test is pending (watcher running).
- **Module F (finance-tools v0.2.1): Reviewer APPROVED 2026-10-26 (round 3).**
