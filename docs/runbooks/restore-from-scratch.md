# Runbook: rebuild the server from scratch

Use this for a dead SSD, a reinstall, or a new box. Prerequisites:
- Ubuntu Server LTS installed with OpenSSH
- this repo and its venv on the workstation
- the **ansible-vault password** (it's in the password manager)
- the private `inventory/host_vars/` (it's in the workstation's backups)

Commands on the box run as the admin user, with `sudo` where shown.

## 1. Network identity
1. Tailscale admin console: delete the old node. Join the new box **with the same machine name** (the `tailscale_serve` role checks it equals `tailnet_fqdn`) and **as `tag:server`** (`sudo tailscale up --advertise-tags=tag:server`, or use a tagged auth key). Untagged, the edge heartbeat's grant doesn't match, which means false "offline" alerts. Then set the node's IP back to the old one so the inventory still matches.
2. Put the workstation's admin key on the admin user.
3. **The new box has new SSH host keys.** On the workstation, drop the old ones and re-accept the new ones only after comparing fingerprints (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the box's console):
   ```bash
   ssh-keygen -R <tailnet_fqdn>; ssh-keygen -R <lan_ip>
   ssh <admin>@<tailnet_fqdn> true; ssh <admin>@<lan_ip> true
   ```
   Both entries matter: Ansible uses the tailnet name, and the nightly backup pull uses the LAN IP with `BatchMode`.

## 2. Bootstrap once
You type the sudo password: `.venv/bin/ansible-playbook playbooks/bootstrap.yml -K`

## 3. Converge
`.venv/bin/ansible-playbook playbooks/site.yml`, then run it again and expect `changed=0`.

**A new or blank media drive.** The `storage` role never formats unless told to, for one run, and even then it refuses any disk that has a filesystem, partition table or readable backup superblock. Retail drives ship formatted (exFAT/NTFS), so wipe the **right** disk first:
1. Identify it by serial, not by `sdX`: `lsblk -o NAME,SIZE,MODEL,SERIAL,TRAN`, then `ls -l /dev/disk/by-id/ | grep -i <serial>`.
2. Dry run: `sudo wipefs -n /dev/disk/by-id/<id>`. It lists what would be erased. Stop if it shows anything you didn't expect.
3. Wipe: `sudo wipefs -a /dev/disk/by-id/<id>`.
4. Format and converge, for this run only:
   ```bash
   .venv/bin/ansible-playbook playbooks/site.yml -e storage_format_media=true -e storage_format_device=/dev/disk/by-id/<id>
   ```
   The role formats with the **existing** `media_uuid` (`mkfs -U`), so `host_vars` needs no change. **Unplug the old drive first**, because both would carry the same UUID.

## 4. Restore app data (optional; the default is a clean start)
There's no restore playbook yet, so do it by hand:
1. Copy the newest archive from the workstation's pull folder (`appdata-<stamp>.tar`) to the box. Then, in an **empty scratch dir**:
   ```bash
   mkdir ~/restore && cd ~/restore && sudo tar -xpf ../appdata-*.tar && sudo sha256sum -c MANIFEST.sha256
   ```
   **Stop if any line isn't OK.** (sudo keeps the owners stored in the archive; extracted without sudo, the databases would end up owned by the admin and the apps couldn't read them.)
2. Stop what you're restoring: `sudo docker compose -f /srv/homelab/stacks/media/compose.yaml stop`, and `sudo systemctl stop homelab-dashboard` if you're restoring push devices or dashboard data.
3. App files, keeping the owners stored in the archive (no blanket chown):
   ```bash
   sudo tar -xpzf files.tar.gz -C /srv/homelab/data
   ```
4. SQLite databases: for each `sqlite/<path>`, `sudo cp -p` it over the matching file under `/srv/homelab/data/<path>`, and `sudo rm -f` any `<db>-wal`/`<db>-shm` next to it.
5. Push devices (optional; otherwise phones subscribe again): `sudo tar -xpzf push.tar.gz -C /srv/homelab/push`, then `sudo chown -R dashboard:dashboard /srv/homelab/push && sudo chown root:dashboard /srv/homelab/push/vapid.json` (archives from before Dashboard v2 carry the old `pushcore` uid, which is the same uid 10101, so this is a no-op for them).
6. Dashboard data and alert history (optional; only archives that contain `dashboard.tar.gz`/`notify.tar.gz`, since older ones don't). With `homelab-dashboard` still stopped:
   ```bash
   sudo tar -xpzf dashboard.tar.gz -C /var/lib/homelab-dashboard
   sudo tar -xpzf notify.tar.gz -C /var/lib/homelab-notify
   sudo stat -c '%a %U %n' /var/lib/homelab-dashboard /var/lib/homelab-dashboard/settings.json
   ```
   Expect `750 dashboard` and `600 dashboard`. The archive stores numeric owners and modes, and `tar -p` under sudo restores them, so don't chown. `dashboard.tar.gz` brings back the settings (Jellyfin/Jellyseerr API keys, renewal dates, thresholds), the drive samples and the uptime rollups. `notify.tar.gz` holds only `history.jsonl`, never the notifier's dedupe state. Then `sudo systemctl start homelab-dashboard`.
7. **Don't restore `bootgate.tar.gz`.** Its state is stale and the gate rebuilds it.
8. **Use the same app versions the backup came from** (`inventory/group_vars/all/versions.yml` at that commit), start the stacks (`sudo docker compose -f ... up -d`) and, if you stopped it and skipped step 6, `sudo systemctl start homelab-dashboard`. Check them, then upgrade.

## 5. Off-box pieces
Re-run `playbooks/gaming-pc.yml` and `playbooks/edge.yml`. Check the Cloudflare tunnel route (`cloudflare.md`).

## 6. Prove it
- Push test: `sudo homelab-notify --title test --body test` (all devices), or `sudo homelab-notify --test --title test --body test` (devices labelled `test` only).
- A reboot test.
