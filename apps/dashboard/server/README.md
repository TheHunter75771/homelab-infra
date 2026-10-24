# Dashboard v2 server

The backend of Dashboard v2 (`docs/CONTRACTS.md` §11). It also absorbs push-core (§5): the same port, the
same push store and the same push endpoints.

- **Runtime:** Node 22 (the distro `nodejs`) as `homelab-dashboard.service`, user `dashboard` (10101),
  listening on `127.0.0.1:8085` and reached only through `tailscale serve`. It runs outside Docker, so it
  stays live while the boot gate holds Docker (ADR 0013).
- **Dependencies:** `node:http` only. The single runtime dependency is `web-push` 3.6.7 (pinned exactly,
  lockfile committed).
- **Docker:** it never touches Docker itself. Every Docker operation goes through the root helper
  `homelab-dockerctl` (module H), and only after a fresh `docker-state` says `active`.
- **Secrets** (API keys, the VAPID private key, push endpoints) never reach a response or a log line.
  - Text copied from the apps (SABnzbd warnings and queue names, *arr health messages, container logs) is
    redacted before any truncation: `apikey=`/`api_key=`/`token=` values, `API Key:`, bearer tokens, and
    every configured key.
  - Keys are read from the config files whether or not the app is running.

## Configuration

Role `dashboard` renders `/etc/homelab-dashboard/config.json` (`roles/dashboard/templates/config.json.j2`)
and points `DASHBOARD_CONFIG` at it. Keys are snake_case. Unknown keys are logged
(`unknown config keys ignored`) and ignored; a drift test checks the template against the keys below.
- **Typing:** values are type-checked. A wrong type or out-of-range value (`remote_enabled: "false"`,
  `intervals.core: 0`, a relative path) logs a `config` warning and keeps the default.
- **Interval minimums (seconds):** core 2, media 10, sampler 10, split, boots, last-pull and reminders 60.
- **Runbook paths:** must be relative `.md` files.
Environment variables override the file: push-core's `HOST`, `PORT`, `PUSH_DIR`, `LOG_LEVEL` and
`PUSH_ENDPOINT_HOSTS`, plus the `DASHBOARD_*` names in `src/config.js` (`ENV_KEYS`). A bad value exits with
code 78 and a JSON `config error` line on stderr.

| Key | Env | Default | |
|---|---|---|---|
| `listen_host` / `listen_port` | `HOST` / `PORT` | `127.0.0.1` / `8085` | |
| `log_level` | `LOG_LEVEL` | `info` | JSON lines to stdout (journald) |
| `display_name` | `DASHBOARD_DISPLAY_NAME` | the box's hostname | shown as `<host>` |
| `timezone` | `DASHBOARD_TIMEZONE` | the process zone | calendar days: renewals, samples, uptime |
| `remote_enabled` | `DASHBOARD_REMOTE_ENABLED` | `false` | ADR 0014. The remote path exists but is off. |
| `access_jwt` | – | `{team_domain: "", audience: ""}` | only used when remote is enabled |
| `web_dir` | `DASHBOARD_WEB_DIR` | `/opt/homelab-dashboard/web` | the directory holding `index.html` (module G's `public/`) |
| `state_dir` | `DASHBOARD_DATA_DIR` | `/var/lib/homelab-dashboard` | settings, samples, uptime, reminders |
| `push_dir` | `PUSH_DIR` | `/srv/homelab/push` | `vapid.json` and `subscriptions.json` (contract 4) |
| `data_dir` | `DASHBOARD_APPS_CONFIG_DIR` | `/srv/homelab/data` | `<app>/config.xml` and `sabnzbd/sabnzbd.ini`, for API keys (read-only) |
| `media_mount` | `DASHBOARD_MEDIA_MOUNT` | `/media` | |
| `helper_socket` | `DASHBOARD_DOCKERCTL_SOCKET` | `/run/homelab-dockerctl.sock` | |
| `gate_dir` | `DASHBOARD_BOOTGATE_DIR` | `/var/lib/bootgate` | |
| `gate_rerun_file` | `DASHBOARD_BOOTGATE_RERUN` | `/run/bootgate/rerun` | contract 2 |
| `gate_retry_timer` / `gate_retry_service` | `DASHBOARD_BOOTGATE_RETRY_TIMER` | `bootgate-retry.timer` / derived `.service` | the next retry, and when a re-run finished |
| `gate_retry_minutes` | `DASHBOARD_BOOTGATE_RETRY_MINUTES` | `5` | for the copy |
| `reboot_required_file` | – | `/run/reboot-required` | plus `.pkgs` |
| `backup_status_file` | `DASHBOARD_BACKUP_STATUS` | `/var/lib/homelab-backup/status.json` | module E |
| `notify_history_file` | `DASHBOARD_NOTIFY_HISTORY` | `/var/lib/homelab-notify/history.jsonl` | module B |
| `rehearsal_flag` | `DASHBOARD_REHEARSAL_FLAG` | `/etc/homelab/rehearsal` | |
| `pinned_images` | – | `{}` | name → image ref; a fallback before the helper has ever answered |
| `runbook_base_url` | – | none | `https://` base for the Settings runbook links |
| `runbooks` | – | `[]` | `[{title, path}]`, safe relative paths under the base → `summary.links.runbooks` |
| `timezone_file` | – | `/etc/timezone` | the second source for `server.timeZone` |
| `apps`, `integrations`, `integration_timeout_ms` (5000), `intervals`, `proc_dir`, `root_path`, `push_endpoint_hosts` | – | see `src/config.js` | rarely needed |

**Host prerequisites (role `dashboard`):**
- Supplementary groups `media` (to read the *arr `config.xml`) and `bootgate`.
- `ReadWritePaths` covering the data dir, the push dir and `/run/bootgate`.
- `sabnzbd.ini` must be readable by group `media` for the Downloads tile; otherwise that tile says it
  can't read the key.
- Mounts made by the gate after the service started must propagate into its namespace (the systemd default).

## HTTP API

| Method | Path | Result |
|---|---|---|
| GET | `/healthz` | push-core's shape: `{"ok", "checks": {"vapid", "store", "writable"}}`, 200 or 503 |
| GET | `/api/vapid-public-key` | `{"publicKey"}`, or 503 |
| POST | `/api/subscribe` | `{subscription, label}` → 201/200 `{ok, created, device, devices}` (push-core) |
| POST | `/api/unsubscribe` | `{endpoint}` → `{ok, removed, devices}` (push-core; the notifier's prune) |
| POST | `/api/test` | push-core's test push to every device, exempt from rehearsal (§9c) → `{ok, total, sent, failed, removed, devices}`; 429 within 10 s |
| GET | `/api/summary` | the summary (below), `ETag` + `X-Generated-At`, 304 on `If-None-Match` |
| GET | `/api/events` | SSE: `summary` (full document) when it changed, `tick` `{generatedAt}` otherwise; `id` = ETag; resumes with `Last-Event-ID` |
| GET | `/api/apps/:name/logs?tail=500&follow=1&since=ISO` | SSE: `lines` `[{ts, level, msg}]` (batched, ≤ 200 per event), then `end` `{reason: ended\|max-duration\|error, message?}`; 409 while Docker is down |
| POST | `/api/apps/:name/{restart\|stop\|start\|update}` | 202 `{ok, action}`; 409 with the lock reason; 403 for `update` when remote |
| GET | `/api/actions/:id` | SSE: `step` `{step, at}`…, then `end` `{state: done\|failed, error, detail}`. `Accept: application/json` gives a snapshot. |
| POST | `/api/gate/rerun` | 202 `{ok, action}`; 409 when the gate is `ok` or a re-run is running; 403 when remote |
| GET | `/api/push/devices` | `{"devices": [{id, label, created}]}` |
| DELETE | `/api/push/devices/:id` | `{ok, removed, devices}`; 404 unknown id |
| GET / PUT | `/api/settings` | the settings view (below); PUT takes a partial patch, 400 with the reason |
| GET | anything else | the web build; extension-less paths fall back to `index.html`, missing assets are 404 |

**Guards on every non-GET request** (unchanged from push-core):
1. `X-Homelab: 1` (403).
2. `Sec-Fetch-Site` of `same-origin`, `none` or absent (403 otherwise).
3. `Content-Type: application/json` in utf-8 (415). This applies to `DELETE` and `PUT` too.
4. Body ≤ 8 KB (413).
5. A JSON object, where an empty body counts as `{}` (400).

There's no CORS, and every response carries a strict CSP: same-origin scripts, styles, fonts and workers,
no inline code.

**Resource limits** (§11.2 Concurrency and §11.3 Resource limits):
- **Request targets:** only origin-form paths are accepted. `//…`, backslashes, bad `%` escapes,
  absolute-form (`GET http://x/`) and `*` get a 400, never a crash.
- **Caught async paths:** every request promise is caught, every background job has a `.catch`, and a
  process-level `unhandledRejection` handler logs and keeps serving.
- **Backpressure:** every SSE stream (`/api/events`, logs, actions) honours it. A client with more than about
  1 MB queued is dropped (`sse client too slow; dropped`) and its resources are released.
- **Log follows:** the helper stream is paused until the browser drains, so the backlog stays with the helper.
- **Slots:** `/api/events` is capped at 32 streams and log follows at 8 (503 beyond that). A slot is reserved
  before any await and released exactly once, on close.
- **Updates:** at most one at a time. A second one gets 409: "Another update is running (X). Try again when
  it finishes."
- **statfs:** single-flight per path, so a hung drive holds at most one thread-pool thread.

**Access:** decided server-side. With `DASHBOARD_REMOTE_ENABLED=false` (v2.0) it is always `tailnet`. When
enabled:
- A request through Cloudflare (`Cf-Ray`) needs a valid `Cf-Access-Jwt-Assertion` (RS256, audience, issuer,
  expiry) and is `remote`. Without one it gets 403.
- `remote` gets 403 on `update` and `gate/rerun`. The summary then shows those actions locked with
  "Only available on the tailnet".

### Action steps

Every action has steps, then ends with `done` or `failed`:

| Action | Steps |
|---|---|
| restart | `restarting` → `waiting-health` |
| start | `starting` → `waiting-health` |
| stop | `stopping` |
| update | the helper's `pulling` → `recreating` → `waiting-health` |
| gate re-run | `requested` (trigger created) → `running` (`bootgate-retry.service` picked it up) |

A failed retry writes no gate history, so the re-run's outcome is the service finishing plus the gate status
afterwards:
- Success: "Check passed. Drive mounted at /media."
- Failure: "Check failed: the drive is still not verified. Next automatic retry in 5 min."

Stopping an app marks it "stopped on purpose" (`stopped.json`), which is not an issue. Starting it, or seeing
it running again, clears the mark.

## Summary schema

`GET /api/summary` and the `summary` event. Times are ISO 8601 UTC; calendar days are `YYYY-MM-DD`, local to
the host. Bytes are plain numbers. The ETag hashes everything except `generatedAt`.

```
schema: 1, generatedAt, access: tailnet|remote, rehearsal: bool
server:  {name, version, timeZone}                       timeZone: the host's IANA zone (see below)
helper:  {reachable: bool, error: code|null}
docker:  {state: active|inactive|failed|unknown}
gate:    {status: ok|waiting|failed|media-lost|unknown, line, since, powercycled, attempts, mediaStopped,
          mountPath, currentBoot, lastPassAt, retryMinutes, nextRetryAt, canRerun, rerunLock,
          action: {id, kind, step}|null,
          history: [{boot, current, startedAt, events: [{ts, event, detail, waitSeconds}]}]}   newest first
apps:    [{name, label, purpose, open: {port, path}|null, stack, mediaGated,
           status: healthy|unhealthy|starting|updating|held|stopped|down|unknown,
           stoppedReason: gate|docker-down|intentional|null,      non-null only for held/stopped
           state, health, startedAt, restartCount, image, tag, pinned, pinnedTag, updateAvailable, localBuild,
           action: {id, kind, step}|null,
           locks: {restart, stop, start, update, logs}}]          each lock: null, or the reason to show
host:    {name, load: [1m, 5m, 15m], memTotal, memUsed, rootTotal, rootUsed, rootUsedPct, bootedAt,
          rebootRequired, rebootPackages: []}
drive:   {mountPath, mounted, stale, lastKnownAt, error, total, used, free, usedPct, warnPct, warn,
          split: {tv, movies, downloads, computedAt, stale}|null,
          samples: [{date, used, total}] (last 60 days),
          forecast: {method, windowDays, samplesInWindow, slopeBytesPerDay,
                     full|at90|at97: {date, inDays, reached}} | null,
          forecastReason: not-enough-samples|not-growing|null, samplesInWindow}
backups: {status: ok|stale|failed|none|unknown, lastOk, archive, sizeBytes, lastRunOk, maxAgeHours, lastPull}
uptime:  {box: {source: "boots", pct30d, days: [{date, pct|null}] (30), downs: [{start, end, seconds, reason}]},
          apps: [{name, months: [{month, pct|null}] (6)}],
          driveFailures: [{month, count}] (6)}
media:   {jellyfin:   {state, message, sessions: [{title, user, device, positionSeconds, durationSeconds, paused,
                                               mode: direct-play|direct-stream|transcode}]},
          sabnzbd:    {state, message, paused, speedBytesPerSec, total,
                       items: [{name, pct, etaSeconds, status}], loginProblem},
          jellyseerr: {state, message, pending, requests: [{title, requestedBy, createdAt}]},
          prowlarr:   {state, message, indexers: [{name, status: ok|warn|fail|idle, detail}]},
          sonarr|radarr: {state, message, health: [{source, level: info|warn|fail, message}]},
          checks: [{key, label, status: ok|warn|fail|idle, detail}]}
          tile state: ok | error (message, detail, since) | held | down | stopped | unconfigured | pending
push:    {devices: [{id, label, created}], storeError, alertsAvailable,
          alerts: [{ts, title, body, tag, delivered, failed, state: active|resolved|info}]}   newest first
subscriptions: [{key, name, date, daysLeft, remindOn, reminderDue, reminded}]
settings: {thresholds: {diskWarnPct, backupMaxAgeH}}
links:   {runbooks: [{title, url}]}                       [] unless runbook_base_url + runbooks are configured
issues:  [{severity: fail|warn, text, target: drive|apps|media|alerts|backups|settings, app?}]   fail first
```

**Rules behind the fields:**
- **`apps[].status`:**
  - Docker not active: `held` when the gate status is `failed`, otherwise `stopped`. No helper call is made.
  - `media-lost`: the six media apps are `held`.
  - The app list is what the deployed compose files declare. The helper's `declared` list is kept on disk,
    so it survives a held reboot.
- **`apps[].open`:** the `tailscale serve` address on the tailnet name (ADR 0015). Port 443 is the dashboard
  only. Jellyfin, Sonarr, Radarr, Prowlarr and SABnzbd are on `8444` at `/watch`, `/sonarr`, `/radarr`,
  `/prowlarr` and `/sabnzbd`; Jellyseerr is on `8443` at `/`. A test checks these against CONTRACTS §6 and
  `roles/tailscale_serve`.
- **`server.timeZone`:** resolved once at start: the configured `timezone`, else `/etc/timezone`, else
  `timedatectl show -p Timezone`, else the process zone. Server-side calendar days and the times inside
  `gate.line` use the same zone.
- **`apps[].stoppedReason`:** `gate` when the status is `held` (the gate is `failed`, or `media-lost` and the
  app is in the media stack). `docker-down` when it is `stopped` because Docker isn't active for another
  reason. `intentional` when it was stopped on purpose (or paused).
- **`links.runbooks`:** built from `runbook_base_url` (plain `https://` only) and `runbooks` (`{title, path}`,
  where the path is a relative `.md` file with no `..`, `.`, leading `/` or scheme; an optional `#anchor` is
  allowed).
  A bad base or entry is dropped with a `config` warning at start, never fatal.
- **`drive`:** read only when the gate is `ok` and `/media` is in the mount table. Otherwise the last-known
  values are shown with `stale: true`. Samples are written once a day.
- **Forecast:** a least-squares fit over the last 30 days. Fewer than 7 samples gives no forecast.
- **`uptime.apps`:** sampled every 60 s. `healthy` counts as up. `held`, `down`, `unhealthy`, `starting` and
  `updating` count as down. `stopped` and `unknown` aren't counted.
- **`media`:**
  - An app that isn't running is never called.
  - Each tile has a 5 s timeout (shown as "Timed out after N s", N at least 1), and a failing tile keeps the
    time its failure started.
  - API keys come from `config.xml` and `sabnzbd.ini`, or from Settings for Jellyfin and Jellyseerr.
- **`push.alerts`:**
  - A title is a recovery only if it **ends** with a recovery phrase ("back", "recovered", "back online",
    "resolved", "healthy again", "ok again") **and** has no failure qualifier anywhere ("not", "no",
    "never", "n't", "fail…", "error…", "still", "but", "however", "except", "partial…", "degraded",
    "without", "pending", "unable", "cannot").
  - So "Media drive back, Docker did not start" stays `active` and resolves nothing, and a later plain
    "Media drive back" resolves everything with that tag. The `[REHEARSAL] ` prefix doesn't change any of this.
  - An alert is `resolved` once a later recovery with the same tag arrives, and recoveries are `resolved`
    themselves.
  - It is `info` when its tag is empty, `test` or `reminder`, or when it is unresolved and older than 7 days.
  - The rule is conservative on purpose: a false "resolved" would hide a live incident.
- **Subscriptions:** a reminder push goes out from 7 days before each renewal date. It is sent once per
  date, follows rehearsal, and is retried until delivered.

**Settings** (`GET/PUT /api/settings`; `settings.json` is 0600):

```
{thresholds: {diskWarnPct 50-99, backupMaxAgeH 1-720},
 subscriptions: [{key, name, date}] (≤ 20; key derived from name when absent),
 integrations: {jellyfin: {configured, last4}, jellyseerr: {configured, last4}}}      (GET)
PUT {integrations: {jellyfinApiKey|jellyseerrApiKey: "<key>" | null}}                 (null clears)
```

## Helper protocol

As implemented by `roles/dashboard/files/homelab-dockerctl/homelab_dockerctl.py`. One JSON request per
connection, carrying only the fields the op accepts.

- **Unary reply:** `{"ok": true, "op", "data"}` or `{"ok": false, "error": {"code", "message"}}`.
- **Stream:** `{"ok": true, "op", "stream": true}`, then `{"type": "line"|"step"|"output", ...}` events,
  then `{"type": "end", "ok", "reason"}`.

| Op | Request | Data | Used for |
|---|---|---|---|
| `docker-state` | – | `{state}` | the guard before every Docker op |
| `ps` | – | `{docker, containers: [{name, state, health, started_at, restart_count, image, pinned, stack}], declared: [{name, stack, service, pinned, local_build}]}` | apps |
| `start`/`stop`/`restart` | `name` | `{name, state}` | actions |
| `update` | `name` | stream of steps | actions |
| `logs` | `name, tail, follow, since` | stream of `{ts, text}` | the log viewer |
| `last-pull` | – | `{last_pull, ...}` | backups |
| `disk-split` | – | `{sizes: {tv, movies, downloads}, computed_at, stale}` | drive; the helper runs `du` only when the drive is verified, else returns its stale cache |
| `boots` | – | `{boots: [{boot_id, first_entry, last_entry}]}` | box uptime |

## Data files (`DASHBOARD_DATA_DIR`)

| File | Contents |
|---|---|
| `settings.json` | thresholds, renewal dates and API keys (0600) |
| `samples.jsonl` | daily drive samples |
| `uptime/YYYY-MM.json` | per-app daily rollups |
| `reminders.json` | which reminders have been sent |
| `stopped.json` | apps stopped on purpose |
| `declared.json` | the last-known declared apps |
| `drive-last.json` | the last-known drive usage |

## Push

Contract 4 is unchanged from push-core, and the dashboard is the single writer of `subscriptions.json`.
- `{{ homelab_push_dir }}/vapid.json` = `{"publicKey", "privateKey", "subject"}` (raw base64url P-256),
  re-read when it changes.
- `subscriptions.json` = `[{"endpoint", "keys": {"p256dh", "auth"}, "label", "created"}]`:
  - written atomically (temp file + fsync + rename + directory fsync), deduplicated by endpoint;
  - missing means an empty list; a corrupt file fails closed (503 on writes, never overwritten);
  - endpoints must be on a known push service.

**Payload contract** for every sender (the test button, renewal reminders, `homelab-notify`, the alert floor
and the heartbeat): one UTF-8 JSON object, encrypted with `aes128gcm` (RFC 8291):

```json
{"title": "Media drive failed", "body": "Repair ladder failed; Docker stays down.", "tag": "bootgate", "url": "/"}
```

| Field | Type | Meaning in the service worker |
|---|---|---|
| `title` | string | notification title ("Homelab alert" if empty) |
| `body` | string | notification text |
| `tag` | string or null | collapse key: a newer alert with the same tag replaces the older one and alerts again |
| `url` | string | opened on tap; must be a same-origin path, normally `/` |

Senders use VAPID from `vapid.json` (claims `{"sub": subject}`). A 404 or 410 means the subscription is gone:
the notifier prunes it through `POST /api/unsubscribe`. Every push produces a visible notification; there
are no silent pushes.

## Tests

```bash
npm ci && npm test        # node:test only: a fake helper on a real Unix socket, fixture host trees, fake media APIs
npm run coverage
```

The suite covers:
- every endpoint and guard;
- the five scenarios (normal day, bad night, clean start, one app down, Docker inactive), plus media-lost
  and an unreachable helper;
- the forecast math and reminder timing;
- SSE change detection;
- actions, including the gate re-run;
- the push tests migrated from push-core;
- the entry point as a real process;
- drift guards against module H's env template, entry point and helper op table.
