# Runbook: rotate a secret

All secrets live in `inventory/group_vars/media_servers/vault.yml` (ansible-vault). Edit it with:
```bash
.venv/bin/ansible-vault edit inventory/group_vars/media_servers/vault.yml
```
After any change: `.venv/bin/ansible-playbook playbooks/site.yml` (it re-renders the affected `.env` file or key file and restarts only that stack), then run it again and expect `changed=0`.

| Secret | Where to get a new one | After updating the vault |
|---|---|---|
| `vault_cloudflared_tunnel_token` | Cloudflare Zero Trust → Networks → Tunnels → the tunnel → refresh the token. Keep the same tunnel: its route is OpenTofu code tied to the tunnel's id (`terraform/cloudflare/tunnel.tf`). A brand-new tunnel can't just get a new `tunnel_id`: that forces a replace, which `prevent_destroy` refuses. Re-adopt instead, in one reviewed change: `tofu state rm` the old tunnel's config, set the new `tunnel_id`, import the new tunnel's config, and the plan must show no create and no destroy (that module's README, *Deletion protection*). While `imports.tf` still has the tunnel config's `import` block (built from `tunnel_id`), the plan does the import by itself; expect in-place updates to the `watch.` record and the ingress | site.yml; check that the cloudflared logs show 4 registered connections |
| `vault_wol_relay_token` | `python3 -c 'import secrets;print(secrets.token_hex(32))'` | site.yml; update any client that calls the relay |
| `vault_vapid_public_key` / `vault_vapid_private_key` | On the **server** (the workstation has no Docker): `sudo docker run --rm <images.node_base> npx -y web-push@3.6.7 generate-vapid-keys --json` (raw base64url P-256 pair) | site.yml, **then every device must subscribe again**, then `playbooks/edge.yml` so the heartbeat gets the new keys and subscriptions |
| App API keys / logins (Sonarr, Radarr, Prowlarr, SABnzbd, Jellyfin, Jellyseerr) | Each app's own settings page | Not in the vault (clean start). Update wherever another app uses it (e.g. Prowlarr → Sonarr) |
| Usenet / indexer credentials | Provider's site | Enter in SABnzbd / Prowlarr |
| **Vault password itself** | `.venv/bin/ansible-vault rekey inventory/group_vars/media_servers/vault.yml` | Update `~/.config/homelab-infra/vault-pass` **and** the password-manager item |

Never commit a decrypted vault, and never paste a secret into an issue, a commit or a chat. gitleaks runs in pre-commit and CI.
