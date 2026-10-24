# ADR 0014: Dashboard v2.0 is tailnet-only

- Status: accepted (2026-10-24)

## Context
The design covers a "remote via Cloudflare Access" mode. Only `/watch` is public (ADR 0003), and on a failed-boot night the tunnel (a container) is held anyway.

## Decision
v2.0 serves the dashboard only through `tailscale serve`. The remote mode's UI and server path are built but disabled by a server-side flag.

## Consequences
No new public surface. Turning remote on later needs its own ADR, a tunnel route, an Access app and Access-JWT verification (the code path already exists).
