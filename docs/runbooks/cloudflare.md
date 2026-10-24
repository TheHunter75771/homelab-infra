# Runbook: Cloudflare tunnel (public Jellyfin only)

The tunnel is **remotely managed**: Ansible runs `cloudflared` (stack `edge`, role `stacks`) with
the token from the vault (`vault_cloudflared_tunnel_token`). The **routes** live in the Cloudflare
dashboard, and Noah sets them by hand. Ansible never calls the Cloudflare API.

Only one thing is public: Jellyfin under `/watch` on `public_watch_host`. Every admin UI (Sonarr,
Radarr, Prowlarr, SABnzbd, Jellyseerr, the dashboard) is tailnet-only through `tailscale serve`. None of
them gets a Cloudflare route. The `cloudflared` container sits on the `hl-public` network next to
Jellyfin only, so even a wrong route cannot reach anything else.

Values below in `<angle brackets>` are your own and live in `inventory/host_vars/` (never in this repo):
`<public_watch_host>` is the `public_watch_host` variable.

## 1. The one route the tunnel needs

Cloudflare dashboard → **Zero Trust** → **Networks** → **Tunnels** → select the media server's
tunnel → **Public hostnames** / **Routes** tab (the name depends on the dashboard version) →
**Add a public hostname** (or edit the existing one):

| Field | Value |
|---|---|
| Subdomain + Domain | together they make `<public_watch_host>` |
| Path | `^/watch(/.*)?$` |
| Service type | `HTTP` |
| URL | `jellyfin:8096` |

- The path is a Go regular expression matched against the request path, and it is **not** anchored
  unless you anchor it. `^/watch(/.*)?$` matches `/watch` and everything below it and nothing else.
  The spec writes this as `/watch*`. If the dashboard refuses the anchors, plain `^/watch` also works.
- Cloudflare does **not** strip or rewrite the path. That is what we want: Jellyfin runs with base
  URL `/watch` (preset by role `stacks`), so it expects to see `/watch/...`.
- Leave **TLS / No TLS Verify** and the other origin options at their defaults (the hop is plain
  HTTP inside Docker).
- Anything that matches no route gets the tunnel's built-in catch-all (HTTP 404).

## 2. Remove the old routes and DNS records

The old box published more through this domain (password vaults, the admin dashboard, the Jellyfin
invite page, DNS admin, and a whole-site route to the old reverse proxy). None of that exists any more.

1. In the same **Public hostnames / Routes** tab, delete every route except the one in section 1.
   That includes any route whose service is `caddy:80`, `vaultwarden…`, `dashboard…`, `jfa-go…`,
   `adguard…`, or any route with an empty path on `<public_watch_host>`. A route with an empty path
   would send the whole hostname (and so every path) to the tunnel.
2. **DNS** → **Records** for the zone: deleting a tunnel route does not always delete its DNS
   record. Delete every `CNAME` that points at `<tunnel-id>.cfargotunnel.com` except the one for
   `<public_watch_host>`.
3. **Zero Trust** → **Access** → **Applications**: delete Access applications that protected
   hostnames you just removed (the old admin dashboard and vaults). Leave none pointing at dead names.
4. If the old box used a **different tunnel**, and this rebuild got a new token: delete the old
   tunnel under **Networks → Tunnels** once its connector shows *Inactive*.

## 3. Recommended edge settings (optional, cheap)

- **Caching → Cache Rules**: add a rule "hostname equals `<public_watch_host>` **and** URI path
  starts with `/watch`" → **Bypass cache**. Jellyfin responses are per-user and should not sit in
  Cloudflare's cache.
- **Security → WAF / Bots**: leave Bot Fight Mode **off** for this hostname. It breaks the Jellyfin
  mobile and TV apps, which cannot solve challenges.

## 4. Verify

From a device **off** the tailnet (phone on mobile data):

- `https://<public_watch_host>/watch` shows the Jellyfin login page.
- `https://<public_watch_host>/sonarr` and `https://<public_watch_host>/` return 404 (not routed).

On the server:

```bash
sudo docker compose -f /srv/homelab/stacks/edge/compose.yaml ps    # cloudflared "healthy"
sudo docker logs --tail 20 cloudflared                              # "Registered tunnel connection" x4
```

`healthy` means `cloudflared tunnel ready` passed, i.e. at least one connection to Cloudflare is up.

## 5. Rotate the tunnel token

1. Dashboard → **Networks → Tunnels** → the tunnel → **Configure** → refresh/rotate the token, then copy it.
2. `ansible-vault edit inventory/group_vars/media_servers/vault.yml` → set `vault_cloudflared_tunnel_token`.
3. Run `playbooks/site.yml`. `/srv/homelab/stacks/edge/.env` changes, so compose recreates `cloudflared`.

## Rehearsal

With `rehearsal: true` there is no tunnel: role `stacks` does not deploy `edge` and removes a
leftover edge stack (and its `.env`) if it finds one. Nothing in Cloudflare needs to change.
