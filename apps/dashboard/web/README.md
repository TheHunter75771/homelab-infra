# Dashboard v2 web

The PWA at `/`: health of the box and fix-it actions from a phone or a desktop browser
(`docs/CONTRACTS.md` §11.4, design in `docs/design/handoff/`, corrected by `CORRECTIONS.md`).
It talks only to `apps/dashboard/server` on the same origin.

## What the server serves

**`apps/dashboard/web/public/`** is the whole deployable tree, as committed. There's no build step at
deploy time: role `dashboard` copies `public/` into `web_dir` (`/opt/homelab-dashboard/web`) and the
server serves it as static files.

- `/` → `index.html`. Routes are in the hash (`/#/apps/sonarr`), so every dashboard window stays on path `/`,
  where the service worker's exact-path window reuse for push clicks finds it.
- The apps open on their own `tailscale serve` ports on the same host (ADR 0015): the path apps on 8444,
  Jellyseerr on 8443. Links come from the server's `apps[].open`, and only a plain path (one leading `/`,
  no `\`) is accepted.
- `/sw.js` must stay at the root (scope `/`): installed phones keep their push subscriptions.
- File types used: `.html .js .mjs .css .svg .png .woff2 .webmanifest` (plus `LICENSE-*.txt`).
- Compatible with the server's CSP: no inline scripts, styles or handlers. Dynamic sizes are set through
  the CSSOM. There are no requests to any other origin.

## Stack

Preact 11 + hooks + htm, vendored as ES modules in `public/vendor/` and loaded by the browser as-is. No
bundler, no transpiler, about 17 KB of library code.
- Vanilla DOM code would have to hand-roll diffing to keep the countdown, live log stream and SSE updates
  from fighting focused inputs.
- Preact does that in a small, dependency-free package.
- htm gives JSX-like templates without a compile step.

Code layout (`public/js/`):
- `format.js`: formatters. Sizes, durations, countdowns and dates, in the host's time zone when the
  summary names it (`server.timeZone`).
- `model.js`: selectors from `/api/summary` to what the screens show. They use the server's decisions
  (statuses, lock reasons, the issue list, alert states, the forecast) and fall back to the same rules only
  when a field is missing.
- `store.js` (pure reducer), `live.js` (the connection), `actions.js`.
- `api.js` (fetch + an SSE reader built on fetch streaming), `push.js`, `snapshot.js`, `logs.js`, `route.js`.
- `ui.js` (primitives) and `screens/*.js`.

## API used (§11.3, as implemented in `apps/dashboard/server/README.md`)

| Call | Use |
|---|---|
| `GET /api/summary` (ETag) | first load, and the 10 s poll whenever the stream is down |
| `GET /api/events` (SSE) | `summary` and `tick` events plus `: ping` comments; reconnects send `Last-Event-ID`; a stream silent for 45 s is dropped |
| `GET /api/apps/:name/logs?follow=1&tail=500` (SSE) | `lines` batches, then `end` (`ended`, `max-duration` or `error`); a 409 shows its reason |
| `POST /api/apps/:name/{restart,stop,start,update}` → `GET /api/actions/:id` (SSE) | `step` events, then `end` with `done`/`failed` and the server's text |
| `POST /api/gate/rerun` → `GET /api/actions/:id` | steps `requested` → `running`, then the result |
| `GET /api/push/devices`, `DELETE /api/push/devices/:id` | device list and removal |
| `GET`, `PUT /api/settings` | `{thresholds}`, `{subscriptions}` and `{integrations: {jellyfinApiKey, jellyseerrApiKey}}` patches |
| `GET /api/vapid-public-key`, `POST /api/subscribe`, `POST /api/test` | push-core's API, unchanged |

Every non-GET sends `X-Homelab: 1` and `Content-Type: application/json` with a JSON body (`{}` when empty).

## States

- **Loading:** skeletons.
- **Live:** "Live · N s ago" (pings and ticks count as contact).
- **Backend unreachable:** the stale banner, with the last data kept and actions paused with the reason.
- **Offline:** the full-screen snapshot, on a cold start when the server can't be reached or `tailscale
  serve` answers 502/503/504. The app saves summaries from the stream at most every 15 s, and the worker saves
  every 200 from `/api/summary`. A 5xx never replaces that copy.
- **Live but stale:** when the server's own refreshes stop (`generatedAt` hasn't advanced for 35 s) the
  indicator says "Stale · updated N min ago", even while SSE pings still arrive.
- **Rehearsal:** the rehearsal banner.
- **Remote:** the badge, with tailnet-only actions disabled and their reasons shown.

## Service worker and updates

- **Versioned shell.** `sw.js` carries the shell list and a content hash, written by `npm run stamp`; a test
  fails if they are out of date. Any changed file means a new worker version.
- **Atomic install.** The new worker precaches the whole shell into `homelab-shell-<version>` with
  `cache.addAll`, bypassing the HTTP cache. If one file fails, the install fails and the old version keeps
  serving.
- **Cache-first.** Shell requests are answered only from the active version's cache, so a page is always
  served from one version, and a proxy 5xx can never replace a file. The network is used only for a file
  that isn't cached, and only a 2xx is kept.
- **Updates.** A new version waits. The page asks it to take over once the page is loaded and idle (no
  dialog open, no field being edited), then reloads once. `activate` deletes the old version's cache. The
  page checks for an update whenever it comes back to the foreground.
- Proven in a real browser by `npm run test:browser`: a simulated deploy, and 502/503/504 from the proxy.

## Development

```bash
npm ci                 # dev dependencies only (preact, htm, @fontsource/inter, @phosphor-icons/core, linkedom)
npm test               # the node:test suite under TZ=UTC, America/New_York and Pacific/Kiritimati:
                       # formatters, selectors, reducer/live/actions, service worker, static checks, and a
                       # DOM smoke test of every route (linkedom + fake API over HTTP)
npm run test:browser   # optional, headless Chromium (skips without it):
                       # - every route under the server's CSP
                       # - 44 px targets at 390
                       # - dot shapes
                       # - offline from the cache with the server gone or answering 502/503/504
                       # - dialog focus, including the progress phase
                       # - a simulated deploy
npm run dev -- --port 8791 --scenario bad   # preview with the fake API (test/fake-api.mjs)
npm run vendor         # re-copy vendored files from node_modules (after a version bump), then stamp
npm run stamp          # re-stamp sw.js after changing anything in public/
npm run dump-summaries # refresh test/server-summaries/ from the real server code (needs npm ci in ../server)
```

**Updating a vendored dependency:**
1. Bump the exact version in `package.json`.
2. Run `npm install`, then `npm run vendor`.
3. Commit `public/` and `package-lock.json`.

**Fixtures:** `test/fixtures.mjs` follows the server's shape. `test/server-summaries/*.json` are real
`/api/summary` documents from the server's own test scenarios (normal, bad-night, clean, one-app-down,
docker-inactive, media-lost). The tests render both.
- `npm run dump-summaries` is deterministic: a fixed clock and fixed device endpoints make a re-dump
  byte-identical.
- `test/dumps.test.mjs` regenerates the dumps in memory from `../server` and fails with "run npm run
  dump-summaries" when they drift.
- It skips, with that reason, when `../server/node_modules` isn't installed.

`npm test` fails if `public/` and the lockfile disagree. Icons come from `scripts/icons.txt` (one Phosphor
name per line) and are built into `public/icons/phosphor.svg`.

## Third-party files in `public/`

| What | Version | License | File |
|---|---|---|---|
| Preact + hooks | 11.0.2 | MIT | `vendor/LICENSE-preact.txt` |
| htm | 3.1.1 | Apache-2.0 | `vendor/LICENSE-htm.txt` |
| Inter woff2 (400/500/600, latin + latin-ext) | @fontsource/inter 5.3.0 | SIL OFL 1.1 | `fonts/LICENSE-inter.txt` |
| Phosphor icons (regular; `play-circle-fill`) | @phosphor-icons/core 2.1.1 | MIT | `icons/LICENSE-phosphor.txt` |

The app icons are copied from push-core's (this repo, MIT).

## Where it departs from the handoff, and why

- **Box uptime:** the tile reads "Box uptime" and the Uptime screen "from the host's boot records"
  (CORRECTIONS 6).
- **Re-run check:**
  - The confirm text follows CORRECTIONS 3.
  - The progress list is the server's two steps (CORRECTIONS 3, amended): "Requested", then "Checking the
    drive (detect, verify, mount /media)".
  - The button is disabled with the server's reason while the gate is `ok` (the retry only acts on a
    failure).
- **Offline screen note:** the dashboard now runs outside Docker (ADR 0013), so the note no longer says
  "the dashboard runs in Docker too".
- **Drive facts when `ok`:** "Last check" (the last pass) replaces "Last fsck". The gate only runs `fsck -n`
  inside the repair ladder.
- **Settings:**
  - An **API keys** card for Jellyfin and Jellyseerr, which §11.3 says are entered in Settings. Keys are
    write-only; the card shows "set · ends abcd".
  - Renewals can be added and removed as well as re-dated.
- **Alerts:**
  - A device-name field next to Subscribe. A device named `test` gets rehearsal and drill pushes.
  - The test-push line never says "test devices only". The test button is exempt from rehearsal (§9c).
- **Apps:**
  - Open links go to the apps' own ports (ADR 0015); Jellyseerr gets one too (8443).
  - Apps stopped because Docker isn't running show "Stopped", not "Stopped on purpose".
- **Docker not running, gate not holding it:** the apps show "Unknown" (warn), with the reason "Docker isn't
  running (apps may still be serving)" on every action. They are never "Stopped", and never counted as down
  or stopped on purpose (CONTRACTS §11.3, amended).
- **"<app> crashed and restarted":** shown as "Resolved · recovered on its own", never active. It uses the
  server's `note`, with the server's own title rule as the fallback.
- **Status dots:** the Apps tab and sidebar item get a dot when an app is down (fail) or unhealthy (warn),
  as the other areas do. A failure dot is a circle and a warning dot a diamond, each with a text label.
- **Uptime day bars:** a day with downtime is shorter, a day mostly down is short and striped. Each bar has
  a date-and-% label, and the Uptime screen shows a key.
- **Runbooks:** listed by title. They become links when the summary carries `runbooks: [{title, url}]`.
