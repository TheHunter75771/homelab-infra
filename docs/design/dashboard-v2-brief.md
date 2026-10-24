# Dashboard v2: design brief

> **For:** Claude Design (UI design). Then a Dashboard v2 spec gets written (a new `docs/CONTRACTS.md` section), and Claude builds it under the team rules in `apps/`.
> **Owner:** the homelab's single admin. Placeholders like `<host>` stand in for private names; never put real hostnames, IPs or domains in designs.
> **Status:** brief only. Nothing here is built yet, except the push-subscription page (push-core), which Dashboard v2 absorbs.

## 1. What this is
One web app that shows the health of a self-hosted media server and lets the owner fix common problems from a phone: restart an app, retry the media-drive check, see why something broke. It replaces three things: the old dashboard, the ntfy alert app, and the current bare push-subscription page.

**The jobs it must do, in priority order:**
1. **At a glance: is everything OK?** One status for the whole box, and what's wrong if it isn't.
2. **Fix it from the phone:** start/stop/restart an app, read its logs, re-run the media-drive check, update an app to the pinned version.
3. **Understand the media drive:** it's an old USB drive that has failed before. Show the boot-gate history, its free space, and when it will be full.
4. **See what's happening with media:** now playing, downloads, requests, and whether the indexers and download client are healthy.
5. **Manage alerts:** which devices get pushes, recent alerts, a test button.

## 2. Users, devices, access
- **Primary:** the owner on an **iPhone, installed to the Home Screen** (PWA, standalone, dark). It's opened often, for seconds at a time.
- **Secondary:** a desktop browser for longer sessions (logs, history, charts).
- **Two access levels.** The UI must make the difference obvious:
  - **Tailnet (full):** everything, including **Update** and **Re-run gate**.
  - **Remote (via Cloudflare Access): proposal, not yet allowed.** The owner chose this on 2026-10-23, but the current design (ADR 0003, CONTRACTS) exposes only `/watch` publicly. It needs an ADR and a contract change (a tunnel route plus an Access app for the dashboard) before it's built. **Design both modes anyway:** remote shows view plus start/stop/restart, and **Update and Re-run gate are hidden or disabled with an explanation** ("Only available on the tailnet").
- It shares an origin with the admin apps (`/sonarr`, `/radarr`, `/prowlarr`, `/sabnzbd`, `/watch`). The dashboard lives at `/`.

## 3. Design language: "Nocturne" (match the owner's finance app)
It's dark only. Use these exact tokens, and add semantic status colors in the same style that hit WCAG AA on `--surface`.

```css
--ground: #161826;  --deep: #0f1119;  --surface: #232532;  --surface-hover: #2b2d3c;
--text: #e9e9ed;    --text-2: #cfd3e5; --muted: #9397ab;   --faint: #75798c;  --disabled: #595d6c;
--border: #3f424d;  --accent-border: #5d5294;  --divider: rgba(233,233,237,0.16);
--accent: #9184d9;  --accent-text: #d2cefd;   --accent-bright: #b5abfc;  --accent-pale: #e7e5fe;
--tint-hover: rgba(145,132,217,0.10); --tint-active: rgba(145,132,217,0.16); --tint-btn: rgba(145,132,217,0.12);
--track: #2b2741;
/* propose: --ok, --warn, --fail, --info (+ tinted backgrounds) */
```

- **Type:** Inter (self-hosted, no external font requests; the app must work offline from cache), with `ui-monospace` for logs, versions and timestamps.
- **Feel:** calm and dense but readable, like a cockpit, not a marketing page. Status is shown with color **and** an icon or word, never color alone.
- **Icons:** simple line icons, self-hosted SVG.

## 4. Screens

### 4.1 Overview (home)
- **Health banner:** "All systems normal", or "2 issues" with a one-line summary each and a tap target to the cause.
- **Boot gate chip:** `ok` / `waiting` / `failed` / `media-lost`, with a short plain-English line (e.g. "Media drive missing since 02:41. Apps stopped. Retrying every 5 min").
- **Tiles:**
  - Media drive: free space and the forecast "full in ~N weeks"
  - Host: load, RAM, root disk, uptime, **"Reboot needed"** badge
  - Backups: last nightly OK, last copy pulled to the PC
  - Uptime: last 30 days
  - Apps: 9 / 9 healthy
- **Now playing:** a compact strip, hidden when empty.

### 4.2 Apps
- A list or cards for each container: name, what it does in one line, status (healthy / unhealthy / starting / **stopped on purpose** / down), uptime, restart count, pinned version, and "update available" (later, when Renovate is on).
- **Actions:**
  - **Open** the app's page
  - **Restart**
  - **Stop/Start** (with confirmation)
  - **Logs**
  - **Update** (tailnet only; confirm; shows progress, then the result)
- **App detail:** the same info at full size plus a **live log viewer** (monospace, follow/pause, search/filter, copy).

### 4.3 Media drive & boot gate
- **Current state** card, with what the gate does next ("Retrying in 3:42").
- A **Re-run check now** button (tailnet only, with confirmation).
- **History timeline:** one row per event: `pass`, `repair`, `powercycle`, `fail`, `retry-pass`, `media-lost`. Each row shows time, boot, detail and wait seconds. Group by boot, and make power-cycles visually distinct.
- **Space forecast chart:** daily used-space samples, a projected line to 100%, thresholds at 90% and 97%, and the estimated full date.
- **Usage split:** TV / Movies / Downloads.

### 4.4 Media
- **Now playing** (Jellyfin): user, title, device, progress, and whether it's transcoding.
- **Downloads** (SABnzbd): speed, queue items with ETA and progress, paused state.
- **Requests** (Jellyseerr): pending requests.
- **Media health:**
  - Indexers (each: ok / failing since / near its API or grab limit)
  - Download client reachable
  - Library folders present
  - Usenet login OK
- **Subscription expiry:** the owner enters each indexer's and the usenet provider's renewal date. The screen shows a countdown, and a reminder push fires before each one.

### 4.5 Alerts
- **Recent alerts:** title, body, time, tag, and resolved/active (e.g. "Media drive lost" followed later by "Media drive back").
- **Devices:** subscribed devices (label, added date) with **Remove**, plus **Subscribe this device**. Show the iOS install hint when the app isn't running standalone.
- **Send test push.**
- A **Rehearsal mode** indicator when it's on (drill pushes go to test devices only).

### 4.6 Uptime & drills
- Monthly uptime per app (bars or a heatmap) and box-level uptime from the off-site heartbeat (up/down timeline).
- Media-drive failures per month.
- Last restore-drill result (pass/fail, duration), for later.

### 4.7 Settings / About
- Thresholds: disk warning %, backup max age.
- Subscription expiry dates.
- Pinned versions table.
- Links to the runbooks.

## 5. Navigation & global UI
- **Mobile:** a bottom tab bar (Overview, Apps, Media, Alerts, More). "More" holds Drive & Gate, Uptime and Settings, and leaves room for future tabs (Home Assistant, calendar).
- **Desktop:** a left sidebar with a 2–3 column layout.
- **Header:**
  - the box's name placeholder
  - an **access badge** ("Tailnet" / "Remote")
  - a **live/stale indicator** and "updated 12 s ago"
- **iOS:** respect safe-area insets, standalone mode, pull to refresh.

## 6. States to design (all of them)
- **Loading:** skeletons.
- **Empty / clean start:**
  - no media yet
  - no downloads
  - no subscribed devices ("Subscribe this phone to get alerts")
  - no alert history
- **Errors:**
  - one app's API unreachable (that tile only)
  - the dashboard can't reach its backend
- **The dashboard itself is down.** Important: it runs in Docker, and **when the media drive fails, the gate holds Docker down**, so the dashboard is unreachable at exactly the moment things are worst. The PWA needs an **offline screen from cache**: "Can't reach the server. Last known state at 02:41: media drive failed." Alerts still arrive by push in this state, because they come from the host and an off-site heartbeat, not from the dashboard.
- **Action feedback:** confirmation dialogs (stop, update, re-run gate), progress, success and failure toasts, and disabled-with-reason for remote access.
- **Rehearsal mode on:** a persistent banner.

## 7. Real data available (the design should fit these)
These are the data sources the backend will read. **The backend doesn't exist yet.** Some sources exist on the host today (gate files, Docker, backups, heartbeat log); others have to be built (alert history storage, forecast samples, and reading the workstation's `.last_pull`). Use realistic sample values and no real names.

| Panel | Data |
|---|---|
| Gate | `status` ∈ {ok, waiting, failed, media-lost}; history lines `{ts, boot_id, event, detail, wait_seconds}` |
| Apps | Docker: name, state, health, started_at, restart_count, image tag; label `homelab.watch=true` |
| Host | load, RAM used/total, root disk %, uptime, `reboot-required` flag + packages |
| Drive | mounted, used/total, daily samples; folder sizes tv/movies/downloads |
| Backups | `LAST_OK` timestamp, archive size; PC pull `.last_pull` timestamp |
| Heartbeat | `{ts, state: up|down, rtt_ms}` every 2 min, about 30 days |
| Push | devices `{id, label, created}`; recent alerts `{title, body, tag, ts}` |
| Media | Jellyfin sessions; SABnzbd queue (speed, items, ETA, paused); Jellyseerr pending; Prowlarr indexer status and limits; *arr health checks |

**Sample scenario for the mockups:** a normal day (9/9 healthy, drive 34% full, a backup 3 h ago, one stream playing), **plus** a bad night (the gate shows `failed` after a power-cycle, and the alert history shows "Media drive lost", "Media drive failed", "Media drive back").

## 8. Deliverables wanted from design
1. Mobile (390 px) and desktop (1440 px) designs for every screen in §4.
2. The states from §6.
3. A small component set: status chip, health banner, app card/row, action button with confirm, gate timeline row, forecast chart, log viewer, tile, bottom tab bar/sidebar, toast, access badge.
4. Semantic status tokens added to the Nocturne palette.
5. Short handoff notes: spacing scale, radii, motion (subtle, respects reduced-motion).

## 9. Out of scope
Home Assistant, the calendar, multi-user accounts, editing app settings that belong in the apps themselves, and any public (unauthenticated) access.
