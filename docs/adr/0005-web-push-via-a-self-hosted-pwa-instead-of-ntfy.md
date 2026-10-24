# ADR 0005: Web Push via a self-hosted PWA instead of ntfy

- Status: accepted (2026-10-23/24)

## Context
Alerts must reach a phone without a third-party app, including while Docker is held down by the gate.

## Decision
A tiny push-core PWA stores subscriptions. A host-level notifier (Python + pywebpush) sends with the same VAPID keys, so gate alerts work with Docker down. An off-site heartbeat on a VPS covers 'box offline'.

## Consequences
No extra app, and alerts work even with Docker down. iOS requires installing the PWA to the Home Screen over HTTPS.
