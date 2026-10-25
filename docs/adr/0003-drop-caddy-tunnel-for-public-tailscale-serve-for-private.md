# ADR 0003: Drop Caddy; tunnel for public, tailscale serve for private

- Status: accepted (2026-10-23/24)

## Context
The old reverse proxy decided 'admin' by source IP (100.64/10). The host also has a backup Wi-Fi uplink whose DHCP hands out 100.100/16, so that rule could be spoofed.

## Decision
Public streaming goes Cloudflare Tunnel → Jellyfin directly (`/watch`). Everything private goes through `tailscale serve` (HTTPS, tailnet identity, path-mapped to loopback-only ports). No reverse proxy.

## Consequences
One less service. No IP-based trust rule to spoof. Automatic HTTPS for the PWA. App URL bases must be kept in sync with serve mounts, because serve strips the mount prefix.

**Amended by ADR 0016 (2026-10-24):** public Jellyfin has its own `watch.` subdomain (the whole hostname goes to the tunnel, `/watch` base URL kept). The apex is a static homepage that doesn't go through the tunnel.
