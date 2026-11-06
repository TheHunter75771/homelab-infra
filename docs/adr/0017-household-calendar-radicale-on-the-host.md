# ADR 0017: The household calendar is Radicale from the distro, on the host, tailnet only

- Status: accepted (2026-11-06)

## Context
Noah wants one self-hosted calendar for the household. It has to sync with both iPhones and the desktop, fire reminders, and let a second household member add their own schedule. The media server is the host (P8 #10, pulled forward). The home connection is behind CGNAT, and nothing private on this box is public (ADR 0003, ADR 0014). The boot gate holds all of Docker when the media drive is missing (ADR 0004), and the calendar has nothing to do with the media drive.

## Decision
- **CalDAV with Radicale.** Phones and desktops use their own calendar apps (two-way sync, offline cache). Reminders are VALARMs stored on each event and fired by the phone, so v1 has no push code of its own.
- **From the Ubuntu archive, run as a hardened host service** (`homelab-calendar.service`, user `calendar`). Like the dashboard (ADR 0013), it has no ordering on Docker or the gate, and its data lives on the system disk under `homelab_data_dir`, which the nightly backup already covers. The archive package is patched by unattended-upgrades (ADR 0008). The role asserts the major.minor series instead of pinning an exact version.
- **Tailnet only, on its own origin:** `tailscale serve` on 8445 (ADR 0015). The household member's phone joins the tailnet with its own login, and the policy lets household reach only 8443 and 8445.
- **Radicale's own logins** (htpasswd, bcrypt), with `owner_only` rights. CalDAV clients need a username and password anyway. `tailscale serve`'s identity headers aren't used: Tailscale sends none for tagged source devices, and Radicale ignores them under htpasswd auth.
- **Shared calendars live under a third login, `household`.** Each phone adds it as a second account. Radicale 3.6 has no sharing that iOS discovers; native sharing arrived in 3.7. Revisit when the archive ships 3.7 or later.
- **Undo history:** the storage hook commits every change to a git repo inside the collections folder. Phones can't delete a whole calendar (`permit_delete_collection = False`).

## Consequences
- **One small, extra host service.** A media-drive failure doesn't take the calendar down, and phones keep working from their cache while the box is off.
- **Each phone has two CalDAV accounts** (own + `household`). Each login can see only its own calendars, and anything both people should see goes under `household`.
- **Recovery:**
  - A failed service pushes "Calendar is down" through `homelab-notify` (after 5 failed starts in 10 minutes), and "Calendar back online" once it's running again.
  - Restores come from git (one event) or the nightly backup (everything).
- **Later items:** a FastAPI debrief service, a PWA, and push for the household member's phone. Each is its own item and reads the same CalDAV server.
