# ADR 0016: Public Jellyfin moves to a subdomain; the apex is a static homepage

- Status: accepted (2026-10-24)

## Context
Public Jellyfin was planned at the apex domain under `/watch`, through the Cloudflare tunnel (ADR 0003). The owner now wants a homepage at the apex. If the homepage shared that hostname, it would have to come through the tunnel too, or sit behind a Worker that splits paths. Through the tunnel, the homepage would go down whenever the box is off or the boot gate holds Docker (cloudflared is a container), which is exactly when people check it. The tunnel route had not been created yet, so moving it cost nothing.

## Decision
- **Jellyfin** gets its own public hostname, `public_watch_host` = the `watch.` subdomain. The tunnel routes the whole hostname to `http://jellyfin:8096`. Jellyfin keeps its `/watch` base URL, so nothing inside the app changes, and it redirects `/` to `/watch`.
- **The apex** serves a static homepage from Cloudflare Pages, independent of the box.
- **The admin apps** stay tailnet-only (ADR 0003, ADR 0015). The homepage links only to public things: Jellyfin and the game-server addresses.

## Consequences
- **Jellyfin URL:** the public URL is `https://watch.<domain>/watch`. Typing `https://watch.<domain>` redirects there.
- **VAPID subject:** `vapid_subject` follows `public_watch_host`. It's only a contact URL in the push JWT, so existing phone subscriptions keep working.
- **Unchanged:** TVs on the LAN keep using `http://<lan_ip>:8096/watch`.
- **Homepage status:** the homepage's status chips need a small public status source that can't reach anything private, such as a Worker. That gets designed when the homepage is built.
