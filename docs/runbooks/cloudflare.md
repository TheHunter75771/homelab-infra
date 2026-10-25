# Runbook: Cloudflare tunnel (public Jellyfin only)

The tunnel is **remotely managed**: Ansible runs `cloudflared` (stack `edge`, role `stacks`) with
the token from the vault (`vault_cloudflared_tunnel_token`), and `cloudflared` fetches its routes
(the tunnel's ingress) from Cloudflare. Ansible never calls the Cloudflare API.

**The zone's configuration is code.** The tunnel's ingress, all DNS records, the HTTPS/TLS settings
(HSTS included), the `watch.` cache rule, the apex's Pages custom domain and Email Routing live in
OpenTofu in [`terraform/cloudflare/`](../../terraform/cloudflare/README.md), the source of truth
(its README lists exactly what it manages and what it leaves out). Change them only there, with a
plan that passes `plan-guard.sh` and is applied as that README says. For anything the module
manages, the dashboard is **read-only**: a hand edit shows up as drift in the next plan, and the
next apply reverts it. Not in the module, among others (see its README): the tunnel itself and its
token (section 5), the homepage's status Worker and its Pages project, and the dashboard settings in
section 3 (Bot Fight Mode, Managed Transforms).

Sections 1–3 are how the zone was **first** set up, by hand in the dashboard, before the module
adopted it. They stay as the from-scratch path (a brand-new zone or tunnel, before it is imported);
on the adopted zone, read them as what the code does, not as steps to repeat.

Only one thing is public through the tunnel: Jellyfin, on its own hostname `<public_watch_host>`
(the `watch.` subdomain, ADR 0016). Every admin UI (Sonarr, Radarr, Prowlarr, SABnzbd, Jellyseerr,
the dashboard) is tailnet-only through `tailscale serve`, and **none of them gets a Cloudflare route
on any hostname**. The `cloudflared` container sits on the `hl-public` network next to Jellyfin only,
so even a wrong route cannot reach anything else.

Values below in `<angle brackets>` are your own and live in `inventory/host_vars/` (never in this
repo): `<public_watch_host>` is the `public_watch_host` variable (`watch.<domain>`), and `<domain>`
is the zone's apex.

## 1. The one route the tunnel needs

> First setup only. Today this route is in `tunnel.tf` and its DNS record in `dns.tf`
> ([module README](../../terraform/cloudflare/README.md)); change them there, not in the dashboard.

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
  The Jellyfin admin account can't sign in through the tunnel (its remote access is off,
  [first-run-apps.md](first-run-apps.md) section 1 step 7); use the tailnet or the home LAN for
  admin work. Jellyfin API keys (the ones Sonarr, Radarr and the dashboard use) are not users and
  are not network-checked, so a key works through the tunnel too: guard keys like passwords. The
  admin block is about the admin's password sign-in and its sessions.
- Saving the route creates the `CNAME` for `<public_watch_host>`; nothing else gets one.

## 2. Remove the old routes and DNS records

> First setup only: this cleanup was done once, during the rebuild, before the module adopted the
> zone. Today the tunnel's routes are in `tunnel.tf` and the zone's DNS records in `dns.tf`; change
> them there. Access applications and other tunnels are not in the module (steps 3 and 4).

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

> The cache rule below is first-setup history: today it is in `cache.tf` (and the zone's
> HTTPS/TLS/HSTS settings in `zone.tf`); change them there. Bot Fight Mode and Managed Transforms
> are not in the module: they stay dashboard settings, set as below.

- **Caching → Cache Rules** (do this): add a rule "Hostname equals `<public_watch_host>`" →
  **Bypass cache**. The whole hostname is Jellyfin: per-user API responses, images and video
  streams. Cloudflare caches by file extension by default, and that list includes MKV, MP4 and WEBM
  ([default cache behavior](https://developers.cloudflare.com/cache/concepts/default-cache-behavior/)),
  which matches Jellyfin's direct-play URLs (`…/Videos/<id>/stream.mkv`). HLS (`.m3u8`, `.ts`) is
  not on the list, but bypassing the whole hostname covers everything.
- **Security → WAF / Bots**: leave Bot Fight Mode **off** for this hostname. It breaks the Jellyfin
  mobile and TV apps, which cannot solve challenges.
- **Rules → Managed Transforms** (the menu path depends on the dashboard version): leave **Remove
  visitor IP headers** **off**. Jellyfin needs `X-Forwarded-For` to see the visitor's real address.
  If it is ever on, every visitor reaches Jellyfin as the tunnel itself and is treated as remote
  (the `!172.30.10.0/24` LAN exclusion, [first-run-apps.md](first-run-apps.md) section 1 step 6):
  safe, but the real addresses are gone from Jellyfin's activity log.
- **Terms, for Noah to weigh:** Cloudflare's CDN terms say its paid services (such as Stream) "must"
  be used "to serve video and other large files via the CDN"
  ([service-specific terms](https://www.cloudflare.com/service-specific-terms-application-services/)).
  Bypassing the cache doesn't change that: the traffic still crosses Cloudflare's network.

## 4. Verify

On a clean start the tunnel isn't running yet: role `stacks` holds it back while Jellyfin's startup
wizard is unfinished, and the `site.yml` run after the wizard starts it
([first-run-apps.md](first-run-apps.md) section 1 step 8).

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

The tunnel and its token are not in the OpenTofu module, so this stays a dashboard step. Rotate the
token of the existing tunnel rather than creating a new one: the module's ingress (`tunnel.tf`) is
tied to the tunnel's id.

1. Dashboard → **Networks → Tunnels** → the tunnel → **Configure** → refresh/rotate the token, then copy it.
2. `ansible-vault edit inventory/group_vars/media_servers/vault.yml` → set `vault_cloudflared_tunnel_token`.
3. Run `playbooks/site.yml`. `/srv/homelab/stacks/edge/.env` changes, so compose recreates `cloudflared`.

## Rehearsal

With `rehearsal: true` there is no tunnel: role `stacks` does not deploy `edge` and removes a
leftover edge stack (and its `.env`) if it finds one. Nothing in Cloudflare needs to change.
