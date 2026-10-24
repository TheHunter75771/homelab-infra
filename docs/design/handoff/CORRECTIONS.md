# Corrections to the design handoff (Lead, 2026-10-24)

The handoff is high-fidelity and approved for the look and behavior. Where it disagrees with the real system, **this file and `docs/CONTRACTS.md` §11 win**.

1. **Apps.** The real watched containers are cloudflared, jellyfin, jellyseerr, prowlarr, radarr, sabnzbd, sonarr and wol-relay. There's no Caddy (dropped, ADR 0003), and the dashboard isn't a container: it runs as a host service (ADR 0013), so it isn't in the Apps list. push-core is retired into the dashboard.
2. **What the gate holds.**
   - Drive **lost while running** (`media-lost`): the watcher stops the whole media stack, so jellyfin, jellyseerr, prowlarr, radarr, sabnzbd and sonarr are "Held by gate" (6, Prowlarr included). cloudflared and wol-relay keep running.
   - Drive **missing at boot** (`failed`): the gate holds **all of Docker**, so all 8 containers show "Held by gate". The dashboard is live because it runs outside Docker, which is why "bad night" works.
3. **Re-run check copy.** The retry **never power-cycles and never repairs the filesystem**. It re-detects the drive (udev, USB re-bind), verifies (UUID, sentinel, write test) and mounts at **`/media`** (not `/mnt/media`). Steps: *Detecting drive → Verifying → Mounting /media*. **Amended after review:** the server can only see *requested* (trigger file created) and *running* (the retry service picked it up), then the result. So the dialog shows two live steps, *Requested → Checking the drive (detect, verify, mount /media)*, then passed or failed. Don't show substeps the server can't observe. Confirm body: "Re-checks the media drive now: detect, verify and mount at /media. Never reboots. Apps stay held until it passes."
4. **Remote access** is off in v2.0 (tailnet only). The remote UI path is built but disabled by a server-side flag. Enabling it needs an ADR, a tunnel route and an Access app.
5. **Fonts and icons** are self-hosted (Inter OFL, Phosphor MIT), with their licenses vendored. The prototype's CDN links are for preview only.
6. **Box uptime** in v2.0 comes from the host's own boot records. The off-site heartbeat log is a later integration, so label the tile "Box uptime" until then.
7. **Pinned versions** list only the real images from `inventory/group_vars/all/versions.yml`.
