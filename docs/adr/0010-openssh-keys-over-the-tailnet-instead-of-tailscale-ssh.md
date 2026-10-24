# ADR 0010: OpenSSH keys over the tailnet instead of Tailscale SSH

- Status: accepted (2026-10-23/24)

## Context
Tailscale SSH check mode needs browser re-approval that expires, which blocked unattended automation.

## Decision
Tailscale SSH off. Key-only OpenSSH, reachable on tailscale0 and from one LAN workstation. A dedicated automation user with source-restricted keys and passwordless sudo.

## Consequences
Unattended runs, timers and drills work. Access control rests on keys, UFW and the tailnet ACL.
