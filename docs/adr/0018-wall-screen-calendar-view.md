# ADR 0018: The wall-screen calendar view is a read-only page fed by a read-only CalDAV login

- Status: accepted (2026-11-06)

## Context
Noah wants the household calendar on the pantry station's wall screen: a 15.6" 1080p screen run by a full-screen browser, with no keyboard and no login prompt. The calendar is Radicale with per-user logins and `owner_only` rights (ADR 0017). A wall page can't type a password, and it shows calendars that belong to two different logins (Noah's Routines, and the shared household calendars).

## Decision
- **A small, read-only host service** (`homelab-calendar-view`, Python stdlib server plus the archive's `icalendar` and `recurring-ical-events`). It renders today plus the next 6 days, and has no write paths.
- **It reads Radicale over CalDAV as a dedicated viewer login.** Radicale's rights move from `owner_only` to an equivalent `from_file`, plus one read-only (`r`) rule per viewer, limited to exactly the calendars it lists.
  - We considered reading Radicale's files from disk. It would need no credentials, but it would mean group read on every collection, including private ones.
- **Tailnet only, on its own origin:** `tailscale serve` 8446 (ADR 0015). There's no login on the page; the tailnet policy is the gate. Household devices and a future `tag:kiosk` station device may reach 8446, and nothing else on the box.
- **When Radicale is down, it serves its last good agenda** with a visible "not updated" banner, never an empty week. No push alerts: a dead wall page is visible on the wall.

## Consequences
- **Least privilege:** the viewer password grants read access to three calendars and nothing else, and Radicale enforces that.
- **The rights change touches the live phones' access path.** Tests reproduce `owner_only` exactly, and rollback is a redeploy of the previous commit.
- **Anyone with tailnet access to 8446 can read those three calendars' event titles.** That matches who can already see them, plus the wall screen in the room.
- **The 8446 handler lands after the `serve-safe` change,** so adding it is purely additive and the other serve ports never drop.
