# Handoff: Dashboard v2 (homelab health + fix-it PWA)

## Overview
One dark-only web app at `/` that shows the health of a self-hosted media server and lets the owner fix common problems from an iPhone (installed PWA) or a desktop browser. It replaces the old dashboard, the ntfy alert app and the bare push-subscription page (push-core). The original product brief is included as `dashboard-v2-brief.md`; this README is the design spec that answers it.

Jobs, in priority order: (1) is everything OK, (2) fix it from the phone, (3) understand the media drive / boot gate, (4) media activity and health, (5) manage alerts.

## About the design files
The files in `prototype/` are **design references built in HTML**: interactive prototypes showing the intended look and behavior. They are **not production code to copy**. Rebuild the UI in the app's real stack under the team rules in `apps/` (or, if none is set yet, a small SPA framework that suits an offline-capable PWA). Use the brief's data contracts (§7) for the backend; all values in the prototype are sample data.

To view: open `prototype/Dashboard v2.dc.html` in a browser (keep `support.js` and `_ds/` next to it). The **Tweaks** panel (or the `data-props` defaults in the file) switches:
- `device`: `mobile` (390×844) | `desktop` (1440×900)
- `scenario`: `normal` (normal day) | `bad` (bad night: gate failed after a power-cycle)
- `access`: `tailnet` | `remote`
- `state`: `live` | `loading` | `backend` (backend unreachable) | `offline` (PWA offline screen) | `sab` (one app API unreachable) | `clean` (clean start / empty)
- `rehearsal`: boolean

`prototype/Dashboard v2 Components.dc.html` is the component + token + notes sheet.

## Fidelity
**High fidelity.** Colors, type, spacing, radii, copy and interactions are final-intent. Recreate pixel-close. Sample data and names (`<host>`, Indexer A/B/C, Guest 1) are placeholders; never ship real hostnames, IPs or domains.

---

## Design tokens

### Color (Nocturne, from the brief; dark only)
| Token | Value | Use |
|---|---|---|
| `--ground` | `#161826` | Page/app background |
| `--deep` | `#0f1119` | Sidebar, tab bar, log viewer, inset panels |
| `--surface` | `#232532` | Cards, tiles |
| `--surface-hover` | `#2b2d3c` | Card hover, toast background |
| `--text` | `#e9e9ed` | Primary text |
| `--text-2` | `#cfd3e5` | Secondary text, values |
| `--muted` | `#9397ab` | Labels, captions (5.2:1 on surface) |
| `--faint` | `#75798c` | Non-essential only (log timestamps) |
| `--disabled` | `#595d6c` | Disabled button text |
| `--border` | `#3f424d` | Button/input borders |
| `--accent-border` | `#5d5294` | Primary button border |
| `--divider` | `rgba(233,233,237,0.16)` | Row rules, header/sidebar edges |
| `--accent` | `#9184d9` | Progress fills, chart projection |
| `--accent-text` | `#d2cefd` | Primary button text, links |
| `--accent-bright` | `#b5abfc` | Icons in accent, chart line |
| `--accent-pale` | `#e7e5fe` | Active nav text, link hover |
| `--tint-hover` | `rgba(145,132,217,0.10)` | Hover on buttons/rows |
| `--tint-active` | `rgba(145,132,217,0.16)` | Active nav item, pressed, chart area |
| `--tint-btn` | `rgba(145,132,217,0.12)` | Primary button fill |
| `--track` | `#2b2741` | Progress/bar tracks |

### Semantic status tokens (new, proposed)
Text values are ≥ 4.5:1 on `--surface`. Tints are the same color at 13–14% alpha.
| Token | Value | Tint (`-bg`) | Contrast on surface | Use |
|---|---|---|---|---|
| `--ok` | `#6cc79a` | `rgba(108,199,154,0.13)` | ~7.1:1 | healthy, pass, resolved, direct play |
| `--warn` | `#e4b363` | `rgba(228,179,99,0.13)` | ~7.3:1 | degraded, near limit, power-cycle, reboot needed, stale, Remote badge |
| `--fail` | `#f08a8a` | `rgba(240,138,138,0.14)` | ~5.6:1 | down, failed, media-lost, active alert, destructive confirm |
| `--info` | `#86b7e8` | `rgba(134,183,232,0.13)` | ~6.6:1 | starting/in progress, rehearsal mode, hints |
| idle | text `--text-2` | `--idle-bg: rgba(147,151,171,0.14)` | ~10:1 | stopped on purpose / held by gate (not an error) |

**Rule:** status is never color alone. Every chip, dot or row pairs color with an icon and/or a word.

### Typography
- Family: **Inter** (self-host it; no external font requests). Mono: `ui-monospace, "SF Mono", Menlo, Consolas, monospace` for logs, versions, image tags, timestamps, boot IDs, gate event names, wait seconds.
- Weights: 400 body, 500 headings/labels (never above 500 for headings; 600 only for inline emphasis inside banners).

| Role | Size / weight | Notes |
|---|---|---|
| Mobile screen title | 26 / 500 | letter-spacing −0.015em, line-height 1.15 |
| Desktop screen title | 22 / 500 | |
| Tile figure | 22 / 500 | e.g. "2.64 TB free", "99.82%" |
| Health banner title | 19 / 500 | |
| Card headline | 17 / 500 | gate line, forecast headline |
| App name | 16 / 500 | |
| Body | 13–15 / 400 | 14 default; list rows 13 |
| Section label | 13 / 500, `--muted` | card headers ("Current state", "Downloads") |
| Chip / caption | 12 / 500 | |
| Mono data | 12 (13 in fact grids, 15 for countdown) | |
| Tab bar label | 11 / 500 | |

### Spacing, radius, elevation
- Page padding: **14 px mobile / 24 px desktop**. Grid gap: **10 px** (tiles) / **12 px** (cards, sections).
- Card padding: 14 px (tiles) / 16 px (cards). Gap inside cards: 8–12 px.
- Button gap: 6 px. Icon-to-label gap: 4–6 px.
- Radius: `4px` buttons/inputs/inset rows · `8px` cards/tiles/toast · `14px` dialogs/sheets · `999px` chips/badges · `44px` (mobile frame only, mock device).
- Elevation: cards are flat surface fills (no shadow). Toast: `0 0 0 1px #595d6c, 0 6px 18px rgba(0,0,0,.55)`. Dialog: `0 0 0 1px #9397ab, 0 16px 40px rgba(0,0,0,.65)`. Dialog backdrop `rgba(15,17,25,0.74)`.
- Hit targets: **44 px** on mobile, **32 px** on desktop for every button.

### Motion
- Hover/press tint: 120 ms ease-out.
- Toast and bottom-sheet enter: 200 ms ease-out (fade + 8 px rise).
- Spinner (busy chip icon, progress step): 1 s linear infinite rotation.
- Toast dwell: 3.8 s.
- `prefers-reduced-motion: reduce` → all animation/transition off; spinners become static icons.

### Icons
Phosphor (regular weight; `fill` only for the now-playing play glyph). Self-host the SVGs. Names used: gauge, squares-four, hard-drive(s), film-strip, bell, bell-ringing, bell-slash, pulse, gear-six, dots-three-outline, caret-left/right, check, check-circle, warning, warning-octagon, x, x-circle, info, pause-circle, circle-notch, lightning, wrench, plugs, power, arrow-clockwise, arrows-clockwise, arrow-circle-up, arrow-square-out, arrow-up-right, arrow-down, stop, stop-circle, play, play-circle, pause, scroll, copy, lock-simple, shield-check, globe-simple, cpu, archive, desktop, timer, download-simple, hand-waving, first-aid-kit, calendar-check, device-mobile, laptop, device-tablet, export, paper-plane-tilt, flask, cloud-slash, clock-counter-clockwise, chart-line-up, book-open.

---

## Global layout

### Mobile (390 wide, PWA standalone)
Column, top to bottom:
1. **Safe-area top** (`env(safe-area-inset-top)`; mock uses 47 px status bar).
2. **Header** (padding 8/16/12): left = screen title (26/500) above a meta line (12 px): `<host>` (mono) · live dot (7 px circle) + state word + "N s ago". Right = **access badge**. On sub-screens a back button (44×44, `caret-left`, `--accent-text`) sits before the title. Bottom border `--divider`.
3. Optional **Rehearsal banner** and **Backend-down banner** (full width, see States).
4. **Scrollable content** (padding 14, gap 12). Pull to refresh on every tab.
5. **Bottom tab bar**: `--deep`, top border divider, padding `6px 6px` + safe-area bottom (mock 28 px). Five equal tabs: Overview (gauge), Apps (squares-four), Media (film-strip), Alerts (bell), More (dots-three-outline). Icon 23 px over 11/500 label; active = `--accent-pale`, inactive `--text-2`. A status dot (8 px, 2 px `--deep` ring) at icon top-right when that area has a problem (Media: warn; Alerts: fail; More: fail when Drive & Gate has a problem).
6. **More** screen: a list card of 52 px rows → Drive & Gate, Uptime & drills, Settings & about (each with icon, label, optional status dot, chevron). Leave room for future rows (Home Assistant, calendar).

### Desktop (1440 wide)
- **Left sidebar** 232 px, `--deep`, right border divider, padding 18/12. Brand block: 30 px square (8 px radius, `--accent-border` 1 px border, hard-drives icon in `--accent-bright`), "<host>" 14/500 and "Homelab dashboard" 12 muted. Nav items 36 px tall, radius 4, icon 18 + label 14/500, gap 10; active bg `--tint-active`, text `--accent-pale`; hover `--tint-hover`; trailing status dot 7 px. Items: Overview, Apps, Drive & Gate, Media, Alerts, Uptime, Settings. Footer: `dashboard 2.0.0` (mono 12) + "Served at / on this origin".
- **Header** padding 16/24: title 22/500 + meta line; access badge right.
- **Content** padding 24, gap 12. Card grids use `repeat(auto-fit, minmax(N, 1fr))` so the same markup reflows to 1 column on mobile and 2–3 on desktop.

### Access badge
26 px pill, 1 px border, 12/500, icon 14.
- Tailnet: `shield-check`, text `--accent-text`, bg `--tint-btn`, border `--accent-border`.
- Remote: `globe-simple`, text/border `--warn` (border at 35% alpha), bg `--warn-bg`. Tooltip: "Through Cloudflare Access. Update and Re-run gate are tailnet only."

### Live indicator
7 px dot + word + age. Live = `--ok` "Live · 12 s ago" (ticks). Stale = `--warn` "Stale · updated 14:08". Loading = `--info` "Connecting".

---

## Screens

### 1. Overview
1. **Health banner** (radius 8, padding 14, bg = status tint). 38 px circle (1 px border in status color) with icon (22 px), title 19/500, sub 13 `--text-2`.
   - OK: `check`, "All systems normal", sub "9 apps healthy · drive 34% · backup 3 h ago".
   - Issues: `warning-octagon` (fail) or `warning` (warn only), title "2 issues", sub "Tap one to see the cause", then one button row per issue (min-height 44/32, `--surface` bg, radius 4, status icon 18 + one-line text 14 + chevron) linking to the cause screen. Bad night: "Media drive failed after a power-cycle. 5 apps held." → Drive & Gate; "Indexer C failing since 11:02 (HTTP 503)" → Media.
2. **Boot gate row** (surface card, tappable → Drive & Gate): label "Boot gate" (13/500 muted), mono chip `ok | waiting | failed | media-lost`, plain-English line 13 `--text-2`, chevron. Ex: "Media drive missing since 02:41. Apps stopped. Retrying every 5 min."
3. **Tiles** — grid `minmax(165px,1fr)` (2 cols mobile, 5 cols desktop), each padding 14, label row (icon 16 + 13/500 muted):
   - **Media drive** (→ Drive): "2.64 TB free" (22/500), 6 px bar (track `--track`, fill `--accent`; `--warn` at ≥ disk-warning %), "Full in ~63 weeks". Drive offline: value suffixed `*`, caption "Last known · drive offline".
   - **Host**: key/value rows (Load, RAM, Root disk, Uptime; values mono 12 `--text-2`), then "Reboot needed" warn chip (`arrows-clockwise`) when `reboot-required`.
   - **Backups**: `check-circle` ok + "Nightly OK · 3 h ago" (15/500), "03:12 today · 18.4 GB", `desktop` icon + "Pulled to PC · yesterday 21:40".
   - **Uptime · 30 days** (→ Uptime): "99.95%", 30 mini day bars (16 px tall, 2 px gap; ok/warn/fail colors), "Box heartbeat, off-site".
   - **Apps** (→ Apps): "9 / 9 healthy", 9 segment bar colored by each app's status, sub "All containers up" / "5 held by the gate" / "N stopped on purpose".
4. **Desktop only — 3-column row** (`1.1fr 1.3fr 1fr`): Apps list (rows: status icon, name, status word, uptime mono; → app detail), Recent alerts (latest 4), Media summary (now playing with progress, Downloads summary, Requests pending, Media health summary). Each card header is a link ("All apps ›", "All alerts ›", "Details ›").
5. **Mobile only — Now playing strip** (hidden when nothing plays): filled play icon 24 `--accent-bright`, title 13/500, user right, 3 px progress bar. → Media.

### 2. Apps
Grid of cards `minmax(340px,1fr)` (1 col mobile, 3 col desktop). Card padding 14, gap 10:
- Name (16/500) + one-line purpose (13 muted) — tappable → App detail. Status chip on the right.
- Meta row mono 12 `--text-2`: `up 3d 8h` · `0 restarts` · `image:tag`.
- If pinned version newer than running: `arrow-circle-up` + "Pinned version 5.17.2 is newer than the running image" (12, `--accent-text`).
- Action row (wrap, gap 6): **Open** (only when the app has a path: /watch, /sonarr, /radarr, /prowlarr, /sabnzbd), **Restart**, **Stop** or **Start**, **Logs**, **Update to X** (primary, only when an update exists).
- Reason line under actions when anything is locked: `lock-simple` + 12 muted text.

App statuses → chip: Healthy (ok, check-circle) · Unhealthy (warn, warning) · Starting (info, spinning circle-notch) · Updating (info, spinner) · Held by gate (idle, pause-circle) · Stopped on purpose (idle, pause-circle) · Down (fail, x-circle).

Sample apps (9): Jellyfin, Jellyseerr, Sonarr, Radarr, Prowlarr, SABnzbd, Caddy, cloudflared, Dashboard. Gated (stopped by the boot gate when the drive fails): Jellyfin, Jellyseerr, Sonarr, Radarr, SABnzbd.

### 3. App detail
Back → Apps. Two-column on desktop (`minmax(380px,1fr)`), stacked on mobile.
- **Info card**: purpose + status chip; 2-col fact grid (label 12 muted, value 13 mono): Status, Uptime, Restarts, Health check ("passing · 30 s"), Image, Started. Same action row + reason line as the card.
- **Log viewer**: toolbar = "Logs" label, **Pause/Follow** toggle (following = primary style), **Copy** (toast "Copied N lines"), full-width filter input (mono 13, placeholder "Filter lines (e.g. ERR, health)"). Body: `--deep`, mono 12/1.5, height 360 mobile / 520 desktop, scrolls; each line = time (`--faint`), level (28 px column; INF `--text-2`, DBG `--muted`, WRN `--warn`, ERR `--fail`), message (`--text-2`, wraps). Following appends live lines and keeps the view at the bottom; Pause freezes. Empty filter result: "No lines match “q”." Footer 12 muted: "N lines (match)" left, "Following live" / "Paused" right.

### 4. Drive & Gate
Two columns on desktop: left = Current state + Usage split stacked; right = Space forecast; full-width Gate history below.
- **Current state**: label + large mono chip; gate line 17/500; inset `--deep` row with icon + "Next automatic retry" + live countdown mono 15/500 (e.g. "3:42"; ok state shows "Next check · next boot"); 3-col facts (Mounted, Attempts, Apps held / Mounted path, Last fsck, Boot); **Re-run check now** (primary, tailnet only, confirm). Remote: disabled with lock + "Only available on the tailnet".
- **Space forecast**: headline "Full around Jan 6, 2028 (~63 weeks)", sub "1.36 TB used of 4.0 TB (34%) · +6 GB a day". Chart 180 px tall: x = last 60 days + projection to 100%; y 0–100% with 50/100 labels; solid daily-used line `--accent-bright` 2 px with `--tint-active` area; dashed projection `--accent` 1.5 px (5/5); dashed threshold lines at **90%** (`--warn`) and **97%** (`--fail`) with right-side labels "90% Nov 27" / "97% Dec 27"; vertical "today" rule with label above; x labels start date and "full Jan 28". Legend below. Fewer than 7 days of samples → empty panel "The forecast starts after 7 days of samples."
- **Usage split**: 10 px stacked bar (TV `--accent-bright`, Movies `--accent`, Downloads `--accent-border`) + legend rows with sizes (mono).
- **Gate history**: grouped by boot, newest first. Group header: power icon, `boot 4e1c` (mono), "started Oct 24 02:35", "· current". Rows: grid `92px 130px 1fr 48px` desktop (time mono · event chip mono · detail 13 · wait seconds mono right); on mobile time + chip + wait on line one, detail wraps to line two. Event chips: pass (ok, check), repair (info, wrench), **powercycle (warn, lightning; whole row tinted `--warn-bg` with 1 px dashed warn border at 45%)**, fail (fail, x), retry-pass (ok, arrow-clockwise), media-lost (fail, plugs).

### 5. Media
Three columns on desktop (1 on mobile): [Now playing, Downloads] · [Media health] · [Requests, Subscription renewals].
- **Now playing**: title 15/500 + mode chip (Direct play ok/lightning; Transcoding should use info), "user · device", 4 px progress, mono time "1:13:20 / 1:56:00". Empty: "Nothing playing right now." / "Jellyfin is held by the gate."
- **Downloads**: speed (mono 13/500) in header; items = mono 12 name + ETA, 4 px progress. Paused state should show a "Paused" idle chip in the header. Empty: "Queue is empty." Error (API unreachable — this tile only): fail-tinted panel, "SABnzbd isn't answering", "Timed out after 5 s at 14:19. The container is running, so this may clear on its own.", Retry button.
- **Requests · N pending**: title + "by · when". Empty: "Nothing waiting for approval."
- **Media health**: rows (icon 17 + label + status text right in status color): Indexer A/B/C (ok / "912 / 1000 API calls today" warn / "Failing since 11:02 · HTTP 503" fail), Download client reachable, Library folders present, Usenet login.
- **Subscription renewals**: name, date (mono), countdown ("23 days"; warn ≤ 30, fail ≤ 7, "expired"). Caption "A reminder push fires 7 days before each date. Edit dates in Settings."

### 6. Alerts
Two columns on desktop: Recent alerts | (Devices, Test).
- **Recent alerts**: rows separated by divider: status icon, title 14/500, time mono right, body 13, tag pill (mono 11, bordered) + state word (Active fail `bell-ringing` · Resolved ok · Info info). Pairs like "Media drive lost" → later "Media drive back" mark the earlier one Resolved. Empty: "No alerts yet" + explanation.
- **Devices**: header with "N subscribed". Rows: device icon 20, label 14 with "this device" (accent) / "test" (info) pills, "added Sep 30 2026" mono, **Remove** (fail text, confirm). **Subscribe this device** (primary) when the current device isn't subscribed. iOS install hint (info tint, export icon) when not running standalone: "On iPhone, pushes need the app on your Home Screen. Tap Share, then Add to Home Screen, and open it from there." Empty: "Subscribe this phone to get alerts".
- **Test**: "Sends a push to N devices (test devices only)" + **Send test push**. Rehearsal line when on.

### 7. Uptime & drills
- **Box heartbeat** (full width): 99.95% (22/500); 30 day bars 34 px tall (ok / warn partial / fail); date labels; list of down episodes (time mono, reason, duration).
- **Monthly uptime per app** heatmap: grid `88px repeat(6,1fr)`, 24 px cells radius 3 with value mono 11; bg ≥99.9 `rgba(108,199,154,.30)`, ≥99 `--ok-bg`, ≥97 `--warn-bg`, else `--fail-bg`; text ok/warn/fail. Caption: time held by the gate counts as down.
- **Media-drive failures per month**: bars (34 px per failure, max 36 px wide, fail color; zero = 2 px border-color stub), count above, month below.
- **Last restore drill** ("later" pill): result + "Sep 28 · 14 m 02 s".

### 8. Settings / About
Cards: **Thresholds** (number inputs: Disk warning at __ % used (90); Backup max age __ hours (36)) · **Subscription renewal dates** (date inputs, live countdown) · **Pinned versions** (name + `image:tag` mono) · **Runbooks** (link rows with book-open icon + external arrow).

---

## Interactions & behavior

### Actions & confirmation
- **Restart**: no confirm. Chip → Starting (spinner) → Healthy; toasts "Restarting X…" then "X is healthy again".
- **Stop / Start**: confirm dialog. Stop → status "Stopped on purpose" (does not alert, does not count as an issue). Copy: "Stop X?" / "X stays stopped until you start it again. It shows as “stopped on purpose” and won't raise alerts."
- **Update** (tailnet only): confirm "Update X to 5.17.2?" / "Pulls image:5.17.2, recreates the container and waits for its health check. X is down for about a minute." → progress steps *Pulling image → Recreating container → Waiting for health check* → closes with success toast (or failure toast with reason).
- **Re-run check** (tailnet only): confirm "Re-run the media-drive check?" / "Runs the boot gate now: detect the drive, power-cycle if needed, fsck, mount. Apps stay held until it passes." → steps *Detecting drive → Checking filesystem → Mounting /mnt/media* → toast "Check passed. Drive mounted at /mnt/media." or "Check failed: drive not detected. Next automatic retry in 5 min."
- **Remove device**: confirm, fail-colored confirm button.
- Dialog: **bottom sheet on mobile** (full width, radius 14 top, safe-area bottom padding), **centered 420 px on desktop**. Icon 24 + title 18/500, body 14/1.5 `--text-2`, actions right-aligned: Cancel (secondary) + confirm (outlined in the dialog's status color with its tint fill). During progress the action row is replaced by the step list (done = ok check, current = spinner, pending = muted circle).
- **Toast**: bottom-center (above tab bar on mobile: 104 px; 24 px on desktop), max 440 px, `--surface-hover`, radius 8, shadow-md, status icon 19 + text 14. `role="status"`.

### Disabled-with-reason
Every disabled action shows `lock-simple` and a visible reason line (hover titles don't exist on phones):
- Remote access: Update, Re-run gate → "Only available on the tailnet".
- Held by gate: Restart/Stop → "Held by the boot gate until the media drive is back."
- Backend down: all actions → "Backend unreachable. Actions are paused".
- Dashboard container: Stop → "The dashboard can't stop itself".
Disabled style: text `--disabled`, border `--border`, transparent, `not-allowed` cursor.

### Buttons
- Secondary: 1 px `--border`, transparent, `--text-2`, 13/500 (14 for standalone CTAs), radius 4, padding 0 11–16, height 44 mobile / 32 desktop, icon 15. Hover `--tint-hover`.
- Primary: 1 px `--accent-border`, bg `--tint-btn`, text `--accent-text`; hover `--tint-active`. Never a solid fill (Nocturne).
- Focus: `outline: 2px solid var(--accent); outline-offset: 2px` on `:focus-visible`.

### Live data
- Overview "updated N s ago" ticks; poll or SSE (suggest SSE/WebSocket with 10 s fallback poll).
- Gate countdown ticks every second.
- Log viewer streams while following.
- Pull to refresh on mobile.

---

## States (all designed in the prototype)
- **Loading**: skeletons in place of content: banner block (76 px), gate row (52 px), five tile skeletons with bars in `--surface-hover`. Live indicator "Connecting".
- **Clean start / empty**: no media yet (drive "3.98 TB free", "No media yet", forecast empty), no downloads ("Queue is empty."), no now-playing (strip hidden), no requests, no devices ("Subscribe this phone to get alerts" + iOS hint + Subscribe CTA), no alert history, backups "No backup yet / First nightly run at 03:00".
- **One app's API unreachable** (`sab`): only the Downloads tile shows the error panel; Media health "Download client reachable → Not answering"; overview banner lists it as a warn issue.
- **Backend unreachable** (`backend`): full-width fail banner under the header: "**Can't reach the dashboard backend.** Showing data from 14:08. Actions are paused. Retrying every 10 s." Live indicator → Stale. Last-known data stays visible; actions disabled with reason.
- **Dashboard down / offline** (`offline`): full-screen cached view from the service worker: cloud-slash icon (52 px circle), "Can't reach the server" (26/500), "Last known state at 02:41: media drive failed.", snapshot card "Saved on this device · 02:41" with Boot gate / Apps / Last backup rows, info panel "Alerts still arrive by push. They come from the host and an off-site heartbeat, not from this dashboard.", note explaining the gate holds Docker down, **Try again** button. The SW must cache the shell and the last `/api/summary` response with its timestamp.
- **Rehearsal mode on**: persistent info banner under the header: flask icon, "Rehearsal mode is on. Drill pushes go to test devices only." Test push copy changes to test devices only.
- **Remote access**: Remote badge; tailnet-only actions disabled with reason (not hidden).
- **Bad night** (`bad`): gate `failed`, 5 gated apps "Held by gate", banner "2 issues", host "Reboot needed", alerts "Media drive failed" / "Media drive lost" active plus an older "lost → back" pair resolved, tab/sidebar status dots.

> **Architecture note:** the dashboard runs in Docker and the gate holds Docker down on drive failure, so in reality the "bad night" live view is only reachable if the dashboard container doesn't depend on the media mount and is exempt from the gate. Recommend exempting it (it only needs read access to gate files, the Docker socket and the host metrics). Otherwise the offline screen is the bad-night experience.

---

## State management (client)
- `route`: overview | apps | app/:id | drive | media | alerts | uptime | settings | more
- `access`: tailnet | remote (from the server, based on the request origin/Access headers; never trust the client)
- `connection`: connecting | live | stale(lastOkAt) | offline
- `summary`: gate {status, line, nextRetryAt, history[]}, apps[] {name, state, health, startedAt, restartCount, image, tag, pinnedTag, path}, host, drive {mounted, used, total, samples[], split}, backups, heartbeat[], media {sessions, queue, requests, indexers, checks}, push {devices, alerts}, rehearsal, subscriptions[]
- `pendingAction`: {kind, appId, phase: confirm|progress|done, step} — drives dialog + optimistic chip (Starting/Updating)
- `toast`: {kind, text}
- `logs`: per-app stream buffer, `follow` bool, `filter` string
- Settings: thresholds {diskWarnPct, backupMaxAgeH}, subscription dates (persisted server-side; reminder push 7 days before).

Endpoints (suggested, to be defined in `docs/CONTRACTS.md`): `GET /api/summary`, `GET /api/apps/:id/logs?follow=1` (SSE), `POST /api/apps/:id/{restart,stop,start,update}`, `POST /api/gate/rerun` (tailnet only, 403 otherwise), `GET/POST/DELETE /api/push/devices`, `POST /api/push/test`, `GET/PUT /api/settings`.

## Assets
- Fonts: Inter (self-host, woff2, 400/500/600).
- Icons: Phosphor regular (self-host SVG sprite). The prototype loads Inter and Phosphor from CDNs **for preview only**.
- No images/photography.

## Files
- `prototype/Dashboard v2.dc.html` — the full interactive prototype (all screens, both devices, all states via props). Markup is the template; logic + sample data are in the `class Component` script at the bottom.
- `prototype/Dashboard v2 Components.dc.html` — component sheet: status tokens, chips, badges, buttons (incl. disabled-with-reason), banners, tile, gate row, toasts, spacing/type/motion tables.
- `prototype/support.js`, `prototype/_ds/…` — runtime + Nocturne stylesheet needed to open the prototypes locally.
- `dashboard-v2-brief.md` — the original brief.
