# Runbook: Cloudflare tunnel (public Jellyfin only)

The tunnel is **remotely managed**: Ansible runs `cloudflared` (stack `edge`, role `stacks`) with
the token from the vault (`vault_cloudflared_tunnel_token`). The **routes** live in the Cloudflare
dashboard, and Noah sets them by hand. Ansible never calls the Cloudflare API.

Only one thing is public through the tunnel: Jellyfin, on its own hostname `<public_watch_host>`
(the `watch.` subdomain, ADR 0016). Every admin UI (Sonarr, Radarr, Prowlarr, SABnzbd, Jellyseerr,
the dashboard) is tailnet-only through `tailscale serve`, and **none of them gets a Cloudflare route
on any hostname**. The `cloudflared` container sits on the `hl-public` network next to Jellyfin only,
so even a wrong route cannot reach anything else.

Values below in `<angle brackets>` are your own and live in `inventory/host_vars/` (never in this
repo): `<public_watch_host>` is the `public_watch_host` variable (`watch.<domain>`), and `<domain>`
is the zone's apex.

## 1. The one route the tunnel needs

Cloudflare dashboard → **Zero Trust** → **Networks** → **Tunnels** → select the media server's
tunnel → **Public hostnames** / **Routes** tab (the name depends on the dashboard version) →
**Add a public hostname**:

| Field | Value |
|---|---|
| Subdomain + Domain | `watch` + `<domain>`, together `<public_watch_host>` |
| Path | *(empty: the whole hostname)* |
| Service type | `HTTP` |
| URL | `jellyfin:8096` |

- The whole hostname goes to Jellyfin. Jellyfin runs with base URL `/watch` (preset by role
  `stacks`) and answers every path outside it with a redirect to `/watch/web/`, so a visitor who
  types the bare hostname lands on the login page.
- Cloudflare does **not** strip or rewrite the path, which is what Jellyfin needs.
- Leave **TLS / No TLS Verify** and the other origin options at their defaults (the hop is plain
  HTTP inside Docker).
- Jellyfin trusts `cloudflared` as a proxy (Known proxies `172.30.10.0/24`, the `hl-public` subnet,
  preset by role `stacks`), so it logs the real client address and treats tunnel visitors as
  remote (its per-user login lockout still applies; there is no per-address rate limit).
- Saving the route creates the `CNAME` for `<public_watch_host>`; nothing else gets one.

## 2. Remove the old routes and DNS records

The old box published more through this domain (password vaults, the admin dashboard, the Jellyfin
invite page, DNS admin, and a whole-site route on the apex to the old reverse proxy). None of that
exists any more.

1. In the same **Public hostnames / Routes** tab, delete every route except the one in section 1.
   That includes any route whose service is `caddy:80`, `vaultwarden…`, `dashboard…`, `jfa-go…`
   or `adguard…`, and **any route on the apex `<domain>`**.
2. **DNS** → **Records** for the zone: deleting a tunnel route does not always delete its DNS
   record. Delete every `CNAME` that points at `<tunnel-id>.cfargotunnel.com` except the one for
   `<public_watch_host>`, including one on the apex.
3. **Zero Trust** → **Access** → **Applications**: delete Access applications that protected
   hostnames you just removed (the old admin dashboard and vaults). Leave none pointing at dead names.
4. If the old box used a **different tunnel**, and this rebuild got a new token: delete the old
   tunnel under **Networks → Tunnels** once its connector shows *Inactive*.

**The apex is not the tunnel's.** `<domain>` is reserved for the static homepage on Cloudflare
Pages (ADR 0016), so it stays up when the box or the boot gate is down. Never add a tunnel route
on the apex; the homepage only links to public things.

## 3. Edge settings

- **Caching → Cache Rules** (do this): add a rule "Hostname equals `<public_watch_host>`" →
  **Bypass cache**. The whole hostname is Jellyfin: per-user API responses, images and video
  streams. Cloudflare caches by file extension by default, and that list includes MKV, MP4 and WEBM
  ([default cache behavior](https://developers.cloudflare.com/cache/concepts/default-cache-behavior/)),
  which matches Jellyfin's direct-play URLs (`…/Videos/<id>/stream.mkv`). HLS (`.m3u8`, `.ts`) is
  not on the list, but bypassing the whole hostname covers everything.
- **Security → WAF / Bots**: leave Bot Fight Mode **off** for this hostname. It breaks the Jellyfin
  mobile and TV apps, which cannot solve challenges.
- **Terms, for Noah to weigh:** Cloudflare's CDN terms say its paid services (such as Stream) "must"
  be used "to serve video and other large files via the CDN"
  ([service-specific terms](https://www.cloudflare.com/service-specific-terms-application-services/)).
  Bypassing the cache doesn't change that: the traffic still crosses Cloudflare's network.

## 4. Verify

From a device **off** the tailnet (phone on mobile data):

- `https://<public_watch_host>/` redirects to `…/watch/web/` and shows the Jellyfin login page.
  From a shell: `curl -sI https://<public_watch_host>/ | grep -i '^location'` shows `watch/web/`.
- `https://<public_watch_host>/sonarr` (or any path outside `/watch`) also redirects to Jellyfin's
  `/watch/web/`. It is **never** an admin app: the tunnel can reach nothing but Jellyfin.
- Jellyfin apps (phone, TV) take the server address **with** the base URL:
  `https://<public_watch_host>/watch`.
- `https://<domain>/` is the Pages homepage (or nothing yet), never Jellyfin or the tunnel.

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
