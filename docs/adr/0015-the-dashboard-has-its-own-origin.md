# ADR 0015: The dashboard has its own origin

- Status: accepted (2026-10-24)

## Context
`tailscale serve` put the dashboard at `/` and Jellyfin, Sonarr, Radarr, Prowlarr and SABnzbd under paths on the same HTTPS origin (port 443). Browsers isolate by origin (scheme, host, port), not by path. So a script running in any of those apps would count as same-origin to the dashboard. That includes a stored XSS in Jellyfin, which is also public through the tunnel. Such a script would pass the `X-Homelab` and `Sec-Fetch-Site` guards. With push-core that only exposed subscriptions. Dashboard v2 can stop and update containers and re-run the boot gate.

## Decision
- **Port 443** serves only the dashboard: `/` → `127.0.0.1:8085`.
- **Port 8444** serves the path-based admin apps: `/watch`, `/sonarr`, `/radarr`, `/prowlarr`, `/sabnzbd`, with the same backends and URL bases as before.
- **Port 8443** stays Jellyseerr at `/`; it has no URL base support.

## Consequences
- **The guards now hold.** A script on 8443 or 8444 is cross-origin but same-site. The dashboard's guards refuse same-site writes, and the custom `X-Homelab` header forces a CORS preflight the server never approves.
- **Nothing to redo on the phone.** The dashboard keeps its origin, so the phone's push subscription and its Home Screen install are unchanged.
- **App addresses gain `:8444`.** Nothing has been bookmarked yet: first-run setup hasn't happened. The apps' URL bases, the public `/watch` route and the LAN address of Jellyfin are unchanged.
- **The apps still share an origin with each other.** That's acceptable: each one is an admin UI with its own login or API key.
- **The firewall is unaffected.** `tailscale serve` listeners are handled by tailscaled, so no UFW rule is involved.
- Cookies are not isolated by port. The dashboard must never use a cookie to decide who is allowed (it uses none today). Its protection rests on the origin checks and CORS.
- Role `tailscale_serve` refuses any handler on 443 other than the dashboard's own `/`.
