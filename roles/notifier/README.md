# Role: notifier

Host-side Web Push for alerts, plus the container alert floor. Module B, docs/CONTRACTS.md §4.
Pushes go straight from the host to the phones' push services, using the push store files
(Contract 4) that the dashboard (formerly push-core) owns. Nothing here depends on Docker, so
alerts still work while the boot gate holds Docker down.

## Installed
| Path | What |
|---|---|
| `/opt/homelab-notify/` | venv with `pywebpush` pinned exactly from `python_packages.pywebpush` in `versions.yml` (shared with the heartbeat). Rebuilt automatically if a Python upgrade breaks it |
| `/usr/local/bin/homelab-notify` | the CLI (`files/homelab_notify.py`) |
| `/etc/homelab/notify.json` | its settings (push dir, state dir, dedupe window, TTL) |
| `/etc/homelab/rehearsal` | present only while `rehearsal: true` |
| `/var/lib/homelab-notify/` | 0755: `history.jsonl` (0644, read by the dashboard) and the dedupe state `dedupe.json` + `.lock` (0600) |
| `/usr/local/sbin/homelab-alertfloor` + `homelab-alertfloor.{service,timer}` | alert floor, every 2 min (system Python, stdlib only) |
| `/var/lib/homelab-alertfloor/state.json` | alert floor memory (0700) |

## homelab-notify
`homelab-notify --title T --body B [--tag TAG] [--test]`
- Reads `{{ homelab_push_dir }}/vapid.json` and `subscriptions.json` (Contract 4). It sends to
  every well-formed `https://` subscription and skips malformed entries.
- Exits **0** if at least one push was delivered, or the push was suppressed as a duplicate.
  Exits **2** if there is no subscription to send to, **1** on error or when nothing was
  delivered.
- **Dedupe:** with `--tag`, the same tag + title (as sent, so rehearsal titles include the
  prefix) is not resent within 30 min. Only a delivered push starts the window, so a failed
  one is retried.
  - **A different title on a tag clears that tag's entries** (CONTRACTS §4, amended). So
    "Media drive lost" → "Media drive back" → "Media drive lost" pushes and records the second
    loss.
  - The clear happens on every send that isn't a duplicate, even one that reached nobody or
    failed. That send is in the history, so the next alert on the tag must not be hidden.
  - Other tags are untouched. The clear and the new entry are one atomic write of the 0600
    dedupe state.
  - A duplicate is suppressed (exit 0) before the subscriptions are even looked at.
- **Rehearsal** (`/etc/homelab/rehearsal` exists): the title gets a `[REHEARSAL] ` prefix and
  only subscriptions with `label == "test"` receive it. **`--test`** applies the same `test`
  label filter without the prefix.
- **Dead subscriptions** (HTTP 404/410): the dashboard (formerly push-core;
  `apps/dashboard/server/`, user `dashboard` 10101) is the **only writer** of
  `subscriptions.json`. It serves the same API on the same port (CONTRACTS §11.1/§11.3). The
  notifier never writes that file, nor anything else in the push dir. It asks the dashboard:
  `POST http://127.0.0.1:8085/api/unsubscribe`, with `X-Homelab: 1`,
  `Content-Type: application/json`, body `{"endpoint": ...}`, a 3 s timeout, no proxy,
  loopback only (`notifier_dashboard_url`).
  - The dashboard runs on the host, outside Docker (ADR 0013), so pruning now works even
    while the gate holds Docker.
  - The call is best-effort. If the dashboard itself is down, pruning is skipped silently, and
    the next push that hits the endpoint tries again.
  - The dashboard accepts requests that have no `Sec-Fetch-Site` header (see
    `checkWriteGuards` in `apps/dashboard/server/src/http/common.js`; also verified against
    the real server).
- Payload (the contract with the service worker of the dashboard, formerly push-core): exactly
  `{"title", "body", "tag", "url": "/"}`. Sent with TTL 24 h and `Urgency: high`.
- **History** (CONTRACTS §11.5, for the dashboard): every send appends one line to
  `/var/lib/homelab-notify/history.jsonl` (0644 regardless of umask), in the form
  `{"ts", "title", "body", "tag", "delivered", "failed"}`.
  - The title is recorded exactly as sent, so rehearsal entries carry `[REHEARSAL]`.
  - `delivered` and `failed` count devices; a dead 404/410 device counts as failed.
  - A send that reaches nobody is recorded with 0/0. A send that fails (e.g. no
    `vapid.json`) is recorded as all failed. Dedupe-suppressed sends are **not** recorded.
  - Entries never contain an endpoint or a key.
  - Entries older than 90 days are trimmed on write. A normal append checks only the first
    line, so it doesn't rewrite the file. A 2 MiB cap (`history_max_bytes`) keeps the newest
    half during an alert storm.
  - A history write error is logged and never affects the push.
- Logs to journald as `homelab-notify` (syslog), and to stderr. Push endpoints are capability
  URLs, so every log line is cleaned the way the dashboard's `sanitizeText`
  (`apps/dashboard/server/src/log.js`) does it: URLs become
  `<url>` and control characters are stripped. Send failures log the exception type and HTTP
  status only, never the exception text (which embeds the endpoint).

## Alert floor
Runs every 2 min, and only while `/var/lib/bootgate/status` is `ok` (otherwise the gate alerts
for itself). It watches every container labelled `homelab.watch=true` and pushes **once per
incident**, then "`<name>` recovered" when the incident clears:
- health `unhealthy` on 2 consecutive checks (counted only while the container is running);
- `exited` or `dead` without an intentional stop. Each container (keyed by ID, so a
  recreated container starts fresh) keeps a small lifecycle model, which persists across runs.
  Every run replays that container's events and the inspect snapshot over it, **in timestamp
  order**:
  - `stop`/`kill`/`destroy` set intent. That covers compose stop/down/recreate, the docker
    CLI and the dashboard's Stop button.
  - `start` and `oom` clear intent. An OOM is always a crash.
  - `die` records whether this exit was intentional.
  - The snapshot is stamped with the moment `inspect` was called, the earliest moment it can
    describe. It never overrides a newer event. A "running" snapshot clears intent only after
    a `die`, which means a missed `start`.

  This fixes the live failure. Run 1 saw jellyseerr *running* just before the dashboard
  stopped it, with the stop events inside run 1's window. The old code let that older
  snapshot erase the intent, so run 2 judged the exit "nobody stopped it". Now
  stop → start → crash still alerts, and a container that came back with a newer `start`
  isn't judged on a stale "exited".
- restart count up by 3 or more between checks. This clears once the count holds still while
  running;
- the Docker daemon unreachable on 2 consecutive checks (one extra incident);
- `docker events` failing on 3 consecutive checks ("Alert floor blind"). A check that can't
  read the event history is skipped rather than guessed, and the next run re-reads the same
  window.

**No false "down" from races.** A deploy once pushed "push-core is down … nobody stopped it"
while Ansible was running `compose down`. To prevent that:
- The events window closes only **after** `ps` + `inspect`. Any stop or kill that produced an
  observed "exited" was logged before inspect answered, so it is inside the window.
- Every new "down" is **confirmed** with a second look (a fresh `ps`, `inspect` and events
  window) before it is pushed.
- A container removed mid-check is never judged. It counts as removed only when the second
  `ps` no longer lists it, or when a failed `inspect` names it in stderr ("No such
  object/container") and returns the others.
- Any other `inspect` failure means "Docker unreachable": a timeout, empty or unparsable
  output, another error line, or an ID nobody accounts for. In that case the state (stop
  intents, restart baselines, unhealthy streaks), the events window and the open incidents
  stay untouched. So a daemon hiccup can never turn into "… is down, nobody stopped it" on
  the next run.

A push that fails is retried on the next run. When a container is removed, its open incident
is dropped quietly. Tags are `alertfloor:<name>:<condition>`, so a phone can replace the alert
with its recovery.

## Tests
```bash
python3 -m unittest discover -s roles/notifier/files/tests -v
```
Both files use a stub sender, with no network, no pywebpush and no root:
- `test_notify.py`: delivery and exit codes, the dedupe window, rehearsal and `--test`
  filtering. History: the exact fields, no endpoints, no dedupe-suppressed entries, the
  rehearsal title, reaching nobody, a failed send, 0644 vs 0600 under umask 077, the 90-day
  trim, append-only on the cheap path, the size cap, an unwritable history and a garbage line.
  It checks that 404/410 go to the dashboard's unsubscribe API while the push dir is
  read-only and byte-for-byte unchanged, and that a down dashboard is skipped silently. The
  real HTTP request is checked against a local recording server: method, path, headers, body,
  no `Sec-Fetch-Site`, proxy env ignored, closed port and hang timeouts. Log hygiene is checked
  too.
- `test_real_sender.py` (runs only where pywebpush is installed): the real pywebpush path
  with HTTP intercepted. It covers the per-push-service VAPID `aud`, the payload decrypting on
  the device side, and a 410 handed to the dashboard's unsubscribe API.
- `test_alertfloor.py`: every incident type, once-per-incident, recovery, retry after a failed
  push, intent tracking, restart baselines, the daemon outage and the gate guard. The race
  cases run the **real** `observe()` against a stub `docker` CLI that can inject a stop or a
  removal while ps, inspect or the events query is running:
  - a stop between the start of the run and inspect: no alert;
  - a stop after inspect but before the re-check: no alert;
  - a container removed between ps and inspect, or before the re-check: no alert, and Docker
    not counted as unreachable. A mixed partial drops only the ID named "No such object";
  - an `inspect` that fails with empty output, times out (124), or mixes "No such object" with
    another error: state, window and incidents untouched, and no false "down" on the next
    run. This includes the Reviewer's sequence: stopped on purpose, a quiet run, one failed
    inspect, then a normal run;
  - a real crash (die with no stop or kill), and an OOM after an earlier kill: these alert;
  - failing `docker events`: no alert, and the window is kept.
