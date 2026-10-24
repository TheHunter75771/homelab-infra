# Role: notifier

Host-side Web Push for alerts, plus the container alert floor. Module B, docs/CONTRACTS.md §4.
Pushes go straight from the host to the phones' push services using the push-core store
files. So alerts still work when Docker, and push-core with it, is held down by the boot gate.

## Installed
| Path | What |
|---|---|
| `/opt/homelab-notify/` | venv with `pywebpush` pinned exactly from `python_packages.pywebpush` in `versions.yml` (shared with the heartbeat). Rebuilt automatically if a Python upgrade breaks it |
| `/usr/local/bin/homelab-notify` | the CLI (`files/homelab_notify.py`) |
| `/etc/homelab/notify.json` | its settings (push dir, state dir, dedupe window, TTL) |
| `/etc/homelab/rehearsal` | present only while `rehearsal: true` |
| `/var/lib/homelab-notify/` | dedupe state (0700) |
| `/usr/local/sbin/homelab-alertfloor` + `homelab-alertfloor.{service,timer}` | alert floor, every 2 min (system Python, stdlib only) |
| `/var/lib/homelab-alertfloor/state.json` | alert floor memory (0700) |

## homelab-notify
`homelab-notify --title T --body B [--tag TAG] [--test]`
- Reads `{{ homelab_push_dir }}/vapid.json` and `subscriptions.json` (Contract 4). It sends to
  every well-formed `https://` subscription and skips malformed entries.
- Exits **0** if at least one push was delivered, or the push was suppressed as a duplicate.
  Exits **2** if there is no subscription to send to, **1** on error or when nothing was
  delivered.
- **Dedupe:** with `--tag`, the same tag + title is not resent within 30 min. Only a delivered
  push starts the window, so a failed one is retried.
- **Rehearsal** (`/etc/homelab/rehearsal` exists): the title gets a `[REHEARSAL] ` prefix and
  only subscriptions with `label == "test"` receive it. **`--test`** applies the same `test`
  label filter without the prefix.
- **Dead subscriptions** (HTTP 404/410): push-core is the **only writer** of
  `subscriptions.json`. The notifier never writes it, nor anything else in the push dir. It
  asks push-core: `POST http://127.0.0.1:8085/api/unsubscribe`, with `X-Homelab: 1`,
  `Content-Type: application/json`, body `{"endpoint": ...}`, a 3 s timeout, no proxy,
  loopback only (`notifier_pushcore_url`). This is best-effort. If push-core is down (e.g. the
  gate holds Docker), pruning is skipped silently, and the next push that hits the endpoint
  tries again. push-core accepts requests that have no `Sec-Fetch-Site` header (checked in
  `apps/push-core/src/app.js`, and against the real server).
- Payload (the contract with push-core's service worker): exactly
  `{"title", "body", "tag", "url": "/"}`. Sent with TTL 24 h and `Urgency: high`.
- Logs to journald as `homelab-notify` (syslog), and to stderr. Push endpoints are capability
  URLs, so every log line is cleaned the way push-core's `sanitizeText` does it: URLs become
  `<url>` and control characters are stripped. Send failures log the exception type and HTTP
  status only, never the exception text (which embeds the endpoint).

## Alert floor
Runs every 2 min, and only while `/var/lib/bootgate/status` is `ok` (otherwise the gate alerts
for itself). It watches every container labelled `homelab.watch=true` and pushes **once per
incident**, then "`<name>` recovered" when the incident clears:
- health `unhealthy` on 2 consecutive checks (counted only while the container is running);
- `exited` or `dead` without an intentional stop. A `docker stop`/`kill` (e.g.
  `docker compose stop`) in `docker events` since the last run counts as intentional. The
  intent is remembered until the container is seen running again;
- restart count up by 3 or more between checks. This clears once the count holds still while
  running;
- the Docker daemon unreachable on 2 consecutive checks (one extra incident).

A push that fails is retried on the next run. When a container is removed, its open incident
is dropped quietly. Tags are `alertfloor:<name>:<condition>`, so a phone can replace the alert
with its recovery.

## Tests
```bash
python3 -m unittest discover -s roles/notifier/files/tests -v
```
Both files use a stub sender, with no network, no pywebpush and no root:
- `test_notify.py`: delivery and exit codes, the dedupe window, rehearsal and `--test`
  filtering. It checks that 404/410 go to push-core's unsubscribe API while the push dir is
  read-only and byte-for-byte unchanged, and that a down push-core is skipped silently. The
  real HTTP request is checked against a local recording server: method, path, headers, body,
  no `Sec-Fetch-Site`, proxy env ignored, closed port and hang timeouts. Log hygiene is checked
  too.
- `test_real_sender.py` (runs only where pywebpush is installed): the real pywebpush path
  with HTTP intercepted. It covers the per-push-service VAPID `aud`, the payload decrypting on
  the device side, and a 410 handed to push-core.
- `test_alertfloor.py`: every incident type, once-per-incident, recovery, retry after a failed
  push, intent tracking, restart baselines, the daemon outage and the gate guard.
