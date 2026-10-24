# push-core

A small Web Push subscription service plus the "Homelab alerts" PWA. Phones and browsers subscribe
here. The host notifier (`homelab-notify`, pywebpush), the alert floor and the off-site heartbeat then
send Web Push straight to the stored subscriptions. Spec: `docs/CONTRACTS.md` §5 (contract 4).

- Node 22, `node:http`. The only runtime dependency is `web-push` (pinned exactly, lockfile committed).
- Listens on `0.0.0.0:8085` in the container. Compose publishes it on `127.0.0.1:8085` only, and it is
  reachable only through `tailscale serve` (HTTPS, tailnet members only).
- Writes only to `${PUSH_DIR}` (default `/data`), so it runs with a read-only root filesystem.

## HTTP API

| Method | Path | Result |
|---|---|---|
| GET | `/` | The PWA page. The device list (label + created) is embedded as JSON. |
| GET | `/manifest.webmanifest`, `/sw.js`, `/app.js`, `/style.css`, `/icons/*` | Static PWA files (`Cache-Control: no-cache`, ETag) |
| GET | `/api/vapid-public-key` | `{"publicKey": "<b64url>"}`, or 503 if `vapid.json` is missing or invalid |
| POST | `/api/subscribe` | `{"subscription": PushSubscription.toJSON(), "label": str}` → 201 created / 200 updated |
| POST | `/api/unsubscribe` | `{"endpoint": str}` → 200 `{"removed": bool}` (idempotent) |
| POST | `/api/test` | Pushes "Test from homelab" to every subscription → `{"total","sent","failed","removed"}`. Human-triggered, so **exempt from rehearsal** (see below). |
| GET | `/healthz` | 200 `{"ok":true,"checks":{...}}`, or 503 naming the failing check (`vapid`, `store`, `writable`) |

POST responses also carry `devices: [{id, label, created}]`, where `id` is the first 16 hex characters of
SHA-256(endpoint). Endpoints and keys are never returned or logged.

**Guards on every POST** (checked in this order):
1. `X-Homelab: 1` header, else **403**. This is the CSRF guard: a custom header forces a CORS preflight,
   and the server never answers one. A `Sec-Fetch-Site` value other than `same-origin` or `none` also gets **403**.
   A **missing** `Sec-Fetch-Site` is allowed: browsers always send it, and server-to-server callers (the host
   notifier's prune) never do.
2. `Content-Type: application/json` (utf-8 only), else **415**.
3. Body ≤ 8192 bytes, else **413**. Both the declared `Content-Length` and the streamed bytes are checked.
4. Valid JSON object, else **400**.

Other responses: 400 for an invalid subscription, 409 at the 100-subscription cap, 429 for `/api/test`
inside its 10 s cooldown (with `Retry-After`), 502 when every test send failed, and 503 when the store is
corrupt or the VAPID keys are missing.

## Payload contract

Every sender (push-core's test button, `homelab-notify`, the alert floor, the edge heartbeat) encrypts
**one UTF-8 JSON object** with exactly these fields:

```json
{"title": "Media drive failed", "body": "Repair ladder failed; Docker stays down.", "tag": "bootgate", "url": "/"}
```

| Field | Type | Meaning in the service worker |
|---|---|---|
| `title` | string | Notification title. If it is empty or missing, the worker shows "Homelab alert". |
| `body` | string | Notification text. |
| `tag` | string or `null` | Collapse key. A new alert with the same tag replaces the old one and alerts again (`renotify`). `null` means it never replaces. |
| `url` | string | Opened on tap. It must be a same-origin path, normally `"/"`. Anything else is replaced by `/`. An open window is reused only if it is already on exactly that path. Otherwise a new window opens: Sonarr, Radarr, Prowlarr and SABnzbd share the origin, and their tabs are never focused or navigated. |

Rules for senders:
- **Encoding `aes128gcm`** (RFC 8291). Apple's push service requires it. pywebpush defaults to it in
  current releases, but pass it explicitly anyway.
- **VAPID** comes from `vapid.json`: the private key as raw base64url, and claims `{"sub": <subject>}`.
  pywebpush fills in `aud` and `exp`.
- Keep the JSON well under 3 KB (the push-service limit is 4 KB after encryption).
- Suggested TTL: 3600 s for alerts. push-core uses `TTL=3600` and `Urgency: high`.
- A **404 or 410** response means the subscription is gone. Prune it with `POST /api/unsubscribe` (see below).
  Treat every other failure as transient.
- Every push produces a visible notification. Never send silent pushes: Safari revokes subscriptions that get them.

pywebpush reference call:

```python
from pywebpush import webpush, WebPushException
payload = json.dumps({"title": title, "body": body, "tag": tag, "url": "/"})  # tag may be None
webpush(
    subscription_info={"endpoint": s["endpoint"], "keys": s["keys"]},
    data=payload,
    vapid_private_key=vapid["privateKey"],
    vapid_claims={"sub": vapid["subject"]},
    content_encoding="aes128gcm",
    ttl=3600,
)
# WebPushException.response.status_code in (404, 410) -> remove that endpoint
```

## Store contract (contract 4)

`${PUSH_DIR}/vapid.json` is read-only for push-core. It is written by role `stacks` from the vault and
re-read automatically when it changes. `subject` must be an `https://` URL or a `mailto:` address. The
deployed value is an https URL (group var `vapid_subject`), which Apple's push service accepts:

```json
{"publicKey": "<b64url, 65-byte P-256 point>", "privateKey": "<b64url, 32 bytes>", "subject": "<https URL from group var vapid_subject>"}
```

`${PUSH_DIR}/subscriptions.json` is **owned by push-core**:

```json
[{"endpoint": "https://…", "keys": {"p256dh": "<b64url>", "auth": "<b64url>"}, "label": "Pixel 9", "created": "2026-10-23T12:00:00.000Z"}]
```

- **Missing file** reads as an empty list. push-core creates the file on the first subscribe (mode 0640).
- **Corrupt file** (invalid JSON, not an array, an entry without `endpoint`/`keys`, or **empty**) fails
  closed. The page still renders. Subscribe, unsubscribe and test return 503, `/healthz` returns 503,
  and the file is **never overwritten**. Fix: repair the JSON, or write `[]` if the subscriptions are lost.
- **Atomic writes:** push-core writes `.subscriptions.json.<pid>.<rand>.tmp` in the same directory,
  fsyncs it, renames it over the target, then fsyncs the directory. Stale temp files are deleted at startup.
- **Dedupe by endpoint:** subscribing again updates `keys` and `label` and keeps `created`. Any duplicates
  already in the file are collapsed on the next write.
- **Every mutation re-reads the file,** so removals made by the notifier are never undone. Unknown
  fields on entries are preserved.
- Endpoints must be `https` on a known push service (FCM, Mozilla, Apple, WNS). This stops anyone using
  push-core, or the notifier that sends to every entry, to POST to arbitrary URLs.
  `PUSH_ENDPOINT_HOSTS` overrides the allowlist.

**Pruning dead subscriptions (host notifier):** call push-core instead of editing the file, so push-core
stays the only writer:

```
POST http://127.0.0.1:8085/api/unsubscribe
X-Homelab: 1
Content-Type: application/json

{"endpoint": "<endpoint that returned 404/410>"}
```

The call is idempotent (`{"removed": false}` if the entry is already gone). It returns 503 while the store
is corrupt, and the file is left untouched.

If a tool ever has to edit the file directly (for example while push-core is down), it must:
1. Re-read the file immediately before removing, and remove only the dead endpoints. Never write back a list read before the sends.
2. Write atomically: a temp file in the **same directory**, fsync, `os.replace()`.
3. Leave it readable by push-core: `chown 10101:10101`, mode `0640`, before the rename.
4. If the file doesn't parse, don't touch it. Exit 1 and log.

**Rehearsal (Lead decision L1):** the host notifier's rehearsal mode (`/etc/homelab/rehearsal`: a
`[REHEARSAL]` title prefix, label `test` only) does **not** apply to `POST /api/test`. A person presses
that button on a device, so it always goes to every subscription with the plain title "Test from
homelab". push-core can't see the host marker anyway.

## Configuration

| Env | Default | |
|---|---|---|
| `PUSH_DIR` | `/data` | Directory with `vapid.json` and `subscriptions.json` |
| `PORT` / `HOST` | `8085` / `0.0.0.0` | Listen address |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warn` or `error`. Logs go to stdout as JSON lines; healthy `/healthz` probes aren't logged. |
| `PUSH_ENDPOINT_HOSTS` | built-in list | Comma-separated push-service host suffixes, or `*` to allow any https host |

**Host prerequisites** (for roles `stacks`/`users`):
- The container runs as **uid:gid 10101:10101**.
- `{{ homelab_push_dir }}` must be writable by 10101, because atomic writes create files in the directory.
  Use owner `10101:10101`, mode `0750`.
- `vapid.json` is `0640 root:pushcore`, so the host group `pushcore` must be **gid 10101**.

## Build and run

```bash
# build (the Docker image format keeps HEALTHCHECK; podman needs --format docker)
NODE_BASE=$(sed -n 's/^ *node_base: *"\(.*\)"$/\1/p' inventory/group_vars/all/versions.yml)
docker build --build-arg NODE_BASE="$NODE_BASE" -t homelab/push-core:0.1.0 apps/push-core
podman build --format docker --build-arg NODE_BASE="$NODE_BASE" -t homelab/push-core:0.1.0 apps/push-core

# run the way compose should: read-only root fs, no capabilities, only /data writable
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
  -v /srv/homelab/push:/data -p 127.0.0.1:8085:8085 homelab/push-core:0.1.0
```

On the host, role `stacks` deploys it from `roles/stacks/templates/push-compose.yaml.j2`. That template
passes the base image as build arg `NODE_BASE` from `images.node_base`, and adds `read_only`, `cap_drop: ALL`,
`no-new-privileges`, uid 10101 and the `/data` bind mount. The Dockerfile default for `NODE_BASE` is only
for manual builds, and a test keeps it equal to `versions.yml`. There is deliberately no `# syntax=`
directive: it would pull an unpinned BuildKit frontend image on every build.

Bump `version` in `package.json` together with `local_images.push_core` (a test checks they match).

## Tests

```bash
npm ci && npm test        # node:test, no network: web-push is stubbed, or exercised offline via generateRequestDetails
npm run coverage          # same, with a coverage table for src/
```

The tests cover the store (atomic write, crash safety, dedupe, removal, corrupt-file handling,
validation), the HTTP guards and every endpoint, the send path (payload, VAPID, 404/410 cleanup, no
secrets in logs), a real offline aes128gcm round trip (web-push encrypts, the test decrypts as a
browser would), the service worker in a VM, the entry point and healthcheck as real processes, and
drift guards against `versions.yml`.

## iOS and other notes

- **iOS and iPadOS 16.4+:** Web Push works only from the **installed Home Screen app** over HTTPS. In a
  Safari tab the page shows an "Add to Home Screen first" hint and disables Subscribe.
- When the browser rotates a subscription, the service worker's `pushsubscriptionchange` handler
  re-registers it under the stored label.
- Icons are generated, not downloaded: `npm run icons` rewrites `public/icons/` from `scripts/gen-icons.js`.
