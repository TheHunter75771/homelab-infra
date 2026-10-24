# homelab-infra

Infrastructure as code for a self-hosted home media server: one Ansible run takes a plain
Ubuntu LTS host to a working, hardened media stack that survives reboots and a flaky USB
media drive, and pushes alerts to a phone.

**What it builds**
- **Boot gate:** nothing starts until the media drive is verified (UUID, sentinel, write test).
  A repair ladder and a single power-cycle come first, then a push alert, with no boot loops.
  A drop-out watcher stops the media apps if the drive disappears while running.
- **Media stack** (Docker Compose, pinned by digest): Jellyfin (QuickSync), Sonarr, Radarr,
  Prowlarr, SABnzbd, Jellyseerr, plus Cloudflare Tunnel for public streaming only.
- **Private access via Tailscale only:** admin UIs behind `tailscale serve` (HTTPS, tailnet
  identity), with no reverse-proxy IP trust rules. The backup Wi-Fi uplink is outbound-only.
- **Push core:** a tiny PWA that phones subscribe to. The host notifier, the alert floor and an
  off-site heartbeat all deliver Web Push through it.
- **Backups:** integrity-checked nightly archives, pulled read-only to a workstation.
- **Hardening:** key-only SSH, UFW plus a DOCKER-USER guard, unattended security patches
  (manual reboots), hardware watchdog.

**Layout:** `playbooks/` · `roles/` · `apps/push-core/` · `inventory/group_vars/` (public defaults,
pinned versions, vault) · `docs/CONTRACTS.md` (the spec) · `docs/adr/` · `docs/runbooks/`.

**Usage**
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-tools.txt
.venv/bin/ansible-galaxy collection install -r requirements.yml -p ./collections
cp inventory/hosts.example.yml inventory/hosts.yml   # then fill inventory/host_vars/
.venv/bin/ansible-playbook playbooks/bootstrap.yml -K  # once, as the admin user
.venv/bin/ansible-playbook playbooks/site.yml
```
Identifiers (hostnames, addresses, domains) live only in the git-ignored `inventory/host_vars/`.
Secrets live only in the ansible-vault file.

## License
MIT. See [LICENSE](LICENSE).
