# mission-control server (module MC1)

The read-only server behind Mission Control (CONTRACTS §12.29). It collects status from Noah's apps, serves
the cards, composes the 05:30 morning briefing, raises alerts, and sends its own Web Push notifications. It
also serves MC2's static PWA at `/`.

It runs on ai-box as user `mc`, listening on `127.0.0.1:8097`. `tailscale serve` (https :8443) is the only way
in. It is Python 3 with **aiohttp** (the broker's pin) and **pywebpush** (`python_packages.pywebpush` in
versions.yml), and nothing else.

## Run

```
python3 -m mission_control [--config PATH] serve                     # the HTTP server + the alert loop
python3 -m mission_control [--config PATH] briefing --send [--force] # push today's lock-screen line (the 05:30 timer)
python3 -m mission_control [--config PATH] briefing --print          # print the full briefing
python3 -m mission_control [--config PATH] check-config              # validate config + VAPID file + state_dir
```

- **Config path:** `--config`, else `MISSION_CONTROL_CONFIG`, else `/etc/mission-control/config.json`. The role
  installs a `mission-control` wrapper for `python3 -m mission_control`.
- **Exit codes:**
  - 0 is ok. That includes "already sent today" and "no devices subscribed".
  - 1 means the briefing reached no device, while at least one was subscribed.
  - 2 is a bad config, or push not set up (VAPID file or pywebpush missing).
- **`briefing --send`:**
  - It sends at most once per local date. The date is recorded in `state_dir/briefing.json`, but only when at
    least one device received it. `--force` sends again anyway.
  - With `Persistent=true`, a boot after 05:30 sends the briefing late, once.
  - The CLI (`--send` and `--print`) waits for ERAU up to `erau_timeout_s`; it has time, and a fresh process has no
    ERAU cache (see ERAU below).
- Logs go to stderr (journald).

## Config

One JSON object. Unknown keys are refused. `allowed_login` is required. Every source URL defaults to `null`,
which means not configured: its card says "set up". See `config.example.json`, where the uppercase
placeholders and `192.0.2.10` are rendered by the role.

| Key | Default | Meaning |
|---|---|---|
| `listen_host`, `listen_port` | `127.0.0.1`, `8097` | `listen_host` must be `127.0.0.1` (refused otherwise) |
| `allowed_login` | **required** | Noah's Tailscale login (`mission_control_noah_login`, private host_vars). Compared byte-for-byte with `Tailscale-User-Login`: no trimming, no case folding, no normalisation. The role must render exactly the login tailscale serve sends |
| `timezone` | `America/New_York` | "today", the 48 h / 7 day windows, and every displayed time |
| `static_dir` | `/opt/mission-control/web` | MC2's `public/` files (`index.html` is the SPA fallback) |
| `state_dir` | `/var/lib/mission-control` | `subscriptions.json`, `alerts.json`, `restarts.json`, `briefing.json`, `notify.json` and their `.lock` files (mc 0700; files 0600) |
| `vapid_file` | `/etc/mission-control/vapid.json` | `{"publicKey", "privateKey", "subject"}`: base64url, no padding, a P-256 point (65 bytes) and scalar (32 bytes), subject. The house push-store layout. root:mc 0640. **Subject:** `https://host` with no port, path or trailing slash, or `mailto:address`: py_vapid (1.9.6) refuses to sign anything else, so `load_vapid` applies py_vapid's own pattern (no whitespace or control characters either, so a trailing newline is refused) and also asks py_vapid at load. If that check itself breaks (a crypto backend error, a library change), it logs a warning (exception type only) and the mirrored pattern alone decides. `check-config` fails with "VAPID subject must be https://host (no port or path) or mailto:address"; a signing failure at send time is logged as "VAPID config invalid" and never prunes a device |
| `push_ttl_s`, `push_timeout_s` | `14400`, `10` | push TTL (how long a push service holds it) and the per-send HTTP timeout |
| `push_endpoint_hosts` | FCM, Mozilla, `push.apple.com`, `notify.windows.com` | a subscription's endpoint host must equal one of these or end in `.` + one (https only) |
| `max_subscriptions` | `20` | |
| `lockscreen_detail` | `minimal` | `none` \| `minimal` \| `names` (see Lock screen) |
| `cache_ttl_s` | `60` | how long a collection is reused by `/api/cards` and `/api/briefing` |
| `source_timeout_s` | `5` (max 15) | per source; a source past it becomes that card's error |
| `alert_interval_s`, `alert_repeat_hours` | `300`, `24` | the alert loop period; when a still-active alert is pushed again |
| `aicontrol_url` | `http://127.0.0.1:8095` | AI Control `/api/summary` |
| `webui_url` | `null` | Open WebUI `/health` (ai-box's tailnet IP: it listens on 100.x:8080, not on localhost) |
| `moneyhub_url`, `moneyhub_timezone` | `null`, `UTC` | Money Hub (homebk-edge, tailnet IP); its naive timestamps are read in `moneyhub_timezone` |
| `erau_url` | `null` | `https://homebk-edge.<tailnet>.ts.net` (see ERAU below) |
| `erau_timeout_s`, `erau_max_age_s` | `20` (5-60), `1800` (60-86400) | the background ERAU fetch's own timeout; how old a last good ERAU result may be when it is served while a fetch runs |
| `host_map` | `{}` | `{host name: IP}` used instead of DNS. ai-box has no MagicDNS, so the ERAU name must be mapped to homebk-edge's tailnet IP. TLS still verifies the name |
| `mediaserver_url` | `null` | the mediaserver dashboard (`/api/summary` → `backups`), only used by a `mediaserver` backup entry |
| `ha_url`, `ha_token_file`, `ha_entities` | `null`, `/etc/mission-control/ha-token`, `[]` | Home Assistant: `GET /api/states/<entity>` for each entity (at most 12), only once the token file exists |
| `systemctl`, `units` | `/usr/bin/systemctl`, `[]` | units to watch (at most 32), read with an unprivileged `systemctl show`. An alert fires when one is failed, in `auto-restart`, or restarting repeatedly (next row) |
| `unit_restart_alert`, `unit_restart_window_s` | `2`, `3600` | "restarting repeatedly": NRestarts rose by 2 or more within 1 h. The alert loop keeps the samples in `restarts.json`; the cumulative NRestarts alone never alerts, and a counter that goes down starts over |
| `backups` | `[]` | `[{id, label, kind, ...}]`, see Backups |
| `bank_sync_alert_hours` | `48` | Money Hub's last good sync older than this raises `bank-sync` |
| `disk_alert_pct` | `90` | warn at ≥ 90 % used, crit at ≥ (100 + 90) / 2 = 95 % (ai-box and PC disks, via AI Control) |
| `deadline_window_hours`, `bills_window_days` | `48`, `7` | the briefing / card windows |
| `links` | `{}` | `{card id: URL}`, the tap-through link per card. `links.ai` is also `config.open_webui_url` (the AI card's ask box) |
| `notify_senders` | `[]` | who may `POST /api/notify`: at most 32 `{name, token_sha256, allow_from?}`. `name` matches `[a-z0-9-]{1,32}`, unique. `token_sha256` is the SHA-256 of the sender's token, 64 lowercase hex, unique; the hash of the empty string is refused. `allow_from` is an optional list of at most 16 IP literals (no CIDR, no duplicates; normalised, so `FD7A::1` = `fd7a::1`). `[]` means the route answers 401 to everyone |
| `quiet_hours` | `null` | `{"start": "HH:MM", "end": "HH:MM"}` (exactly those keys, 00:00-23:59, start ≠ end) in `timezone`; it may cross midnight; `null` is off. See Notify |
| `notify_dedupe_seconds` | `1800` | integer 0-86400; `0` turns dedupe off |
| `notify_rate_per_hour` | `30` | integer 1-1000: accepted messages per sender per rolling hour |
| `app_links` | `[]` | the Apps card: at most 12 `{label, url}` (exactly those keys), in page order. `label` 1-32 printable characters (not all spaces); `url` an http(s) URL without credentials or fragment, at most 300 characters (the `links` rule). `[]` = no Apps card |
| `log_level` | `INFO` | |

**Backups:**

| `kind` | Extra keys | Read from |
|---|---|---|
| `unit` | `unit`, `max_age_hours`, `timer` (default `<unit>.timer` for a `.service`; `null` for none) | `systemctl show`: Result + ExecMainExitTimestamp of the backup's oneshot service. Its LAST_OK moves only on success, so "last successful run" is the same thing. This is how ai_backup's staging is read: `/var/backups/ai-box` is root:backuppull 0750, so `mc` can't read LAST_OK. **After a reboot** systemd has forgotten the service's run; then the Persistent=true timer's `LastTriggerUSec` (kept on disk) stands in. It says only that a run started, not how it ended (Reviewer Z7-1), so within the max age the backup is **unknown** ("result lost at reboot; timer last fired Wed 3:45 AM"), which holds the alert state: no false alarm, and a failure raised before the reboot stays raised until a real run reports. "Running now" during the catch-up run is OK; a trigger older than the max age is stale. A failure since boot always wins |
| `file` | `path`, `max_age_hours` | an ISO 8601 UTC timestamp on the first line (a LAST_OK / `.last_pull`) |
| `mediaserver` | none | the mediaserver dashboard's own verdict (`ok`/`stale`/`failed`; `none` counts as stale) |

Statuses are `ok`, `stale`, `failed` and `unknown`. Unknown (unreadable, not configured, timestamp in the
future) never raises an alert, but it shows on the card.

## Identity and guards

- **Identity:** every request except `GET /healthz` must come from TCP peer 127.0.0.1. It must also carry a
  exactly one `Tailscale-User-Login`, byte-for-byte equal to `allowed_login` (two such headers are refused; the HTTP
  parser itself drops whitespace around a field value, RFC 9110 §5.5, but nothing else is trimmed). Anything
  else gets a 403 `{"error": "forbidden"}`, before any source is touched. That includes the static app.
- **The one exception: `POST /api/notify`** (B2). Senders are tagged servers with no login, so for that path and
  method only, and only when the router sends it to the notify handler, the login check gives way to the handler's
  bearer-token check (see Notify). It still needs TCP peer 127.0.0.1. Every other method on `/api/notify`, and every
  other spelling of the path (`/api/notify/`, `/API/notify`, `/api//notify`, ...), keeps the login guard: 403 without
  the login, 405 with it.
- **Methods:** GET everywhere, except POST on the three push endpoints and `/api/notify`. Anything else is a 405,
  and HEAD counts as anything else.
- **Fetch site:** `/api/*` refuses `Sec-Fetch-Site` values other than `same-origin`, `none` or absent, with a 403.
- **POST rules** (the push endpoints; `/api/notify` has its own, see Notify):
  - needs `X-Mission-Control: 1` (403 without it) and `Content-Type: application/json` (415 otherwise);
  - the body must be at most 8 KiB and a JSON object. It is read to the end (or past the limit), however many
    packets it arrives in.
- **No query parameters:** `/api/cards` and `/api/briefing` answer any query with a 400.
- **Requests aiohttp can't parse** (a header or request line over 8190 bytes, a control character in a header, a
  bad chunk size, ...) never reach the app or the guard: aiohttp answers them itself, and by default its 400 body
  and its ERROR log line quote the parser's message, which holds raw request bytes (a bearer token included).
  `server.start` stops both: such a request gets the fixed body `400 Bad Request` and a log line with the error
  type only ("refused a request that couldn't be parsed: HTTP 400 (LineTooLong)"), and a filter on the
  `aiohttp.server`, `aiohttp.web`, `aiohttp.internal` and `aiohttp.access` loggers replaces any exception text and
  traceback with the exception's type ("... (ValueError; details withheld)"). aiohttp 3.14 has no option for the
  body, so `start` gives the runner's server a subclass whose connections use a `RequestHandler` subclass with
  its own `handle_error`; `tests/test_review_b2.py` pins the result, so an aiohttp upgrade that breaks it fails
  the tests.
- **Headers:** every response has the CSP (`default-src 'self'`, `frame-ancestors 'none'`, ...), `nosniff`,
  `no-referrer` and `X-Frame-Options: DENY`. `/api/*` also gets `Cache-Control: no-store`.

## API (the MC1 ↔ MC2 interface)

`GET /api/cards`:
```
{ generated_at: epoch s,
  config: { open_webui_url: str|null, vapid_public_key: str|null },
  cards: { ai|ai_control|money|school|home|servers|alerts:
           { status: "ok"|"warn"|"down"|"unknown"|"setup", headline: str,
             items: [{label: str, value: str, status: <status>|null}],
             link: str|null, error: str|null, updated_at: epoch s|null },
           apps:   # only while app_links isn't empty (B1)
           { status: "ok", headline: "4 apps", items: [{label, value: "", status: null, url: str}],
             link: null, error: null, updated_at: null } } }
```
- `unknown` + `error`: the source failed (timeout, refused, HTTP status, bad JSON). `setup`: not configured,
  or Home Assistant has no token yet ("Connect Home Assistant").
- `ai` is Open WebUI's health plus where answers run (GPU or CPU). `servers` is the watched units, the backups
  and the disks. `alerts` lists every active alert, worst first, with "since …".
- `apps` (B1) lists `app_links` in config order, each item's `url` the tap-through. With `app_links` empty the key is
  absent, so the page is unchanged. It has no source and never fails.

`GET /api/briefing` (today's, composed on request from the cached collection):
```
{ date: "YYYY-MM-DD", generated_at, headline: "3 deadlines, 2 bills, 1 alert", summary: str,
  sections: [{title, items: [{label, value, status|null}], empty: str}],   # School, Bills this week, Alerts, Backups
  counts: {deadlines: int|null, bills: int|null, alerts: int, backups_bad: int},
  lockscreen: {title, body} }
```

**Push endpoints** (all POST, JSON):

| Endpoint | Body | Answer |
|---|---|---|
| `/api/push/subscribe` | `{subscription: PushSubscription.toJSON(), label}` | 200 `{ok}`. 400 when the endpoint isn't https on a known push service, the keys are malformed, or there are too many devices. A known endpoint is replaced |
| `/api/push/unsubscribe` | `{endpoint}` | 200, or 404 if not subscribed |
| `/api/push/test` | `{endpoint}` | 200 when delivered. 404 if not subscribed. 410 if the push service says it expired (it is removed). 502 on another failure. 503 if push isn't set up |

**Push payloads** (what MC2's `sw.js` receives): `{title, body, tag, url}`.

| Push | title | tag | url |
|---|---|---|---|
| briefing | `Mission Control` | `mc-briefing` | `/#card-briefing` |
| alerts | `Mission Control alert` | `mc-alerts` | `/#card-alerts` |
| test | `Mission Control` | `mc-test` | `/` |
| notify | `<sender>: <title>` | `n.<sender>.<tag>`, or `n.<sender>.~<hash>` past 64 characters | the message's `url`, else `/` (only MC's own origin opens; anything else opens `/`) |
| held summary | `Held overnight: N` | `n.held` | `/` |

`GET /healthz` gives `{"ok": true}` and needs no identity.

## Notify (`POST /api/notify`, B2)

Any host or service sends Noah a push through MC's own Web Push, instead of running its own sender. The client is
`mc-notify` (`roles/mc_notify_client`). Code: `mission_control/notify.py` (rules and state), `app.py` `notify` and
`State.notify_accept` / `notify_flush`.

- **Auth:** `Authorization: Bearer <token>`, exactly one header; the scheme is case-insensitive, the token non-empty,
  printable, with no whitespace. The server holds only `notify_senders[].token_sha256`. The presented value (empty
  when the header is missing or malformed) is hashed and compared with **every** sender's hash
  (`hmac.compare_digest`, no early exit), so every failure takes the same path to the same **401**
  `{"error": "unauthorized"}` with `WWW-Authenticate: Bearer`. The login header doesn't count. Nothing is read or
  written before the token checks out.
- **`allow_from`:** when a sender has one, the **last** `X-Forwarded-For` hop (the address `tailscale serve` saw;
  with several headers, the last hop of the last one) must parse as an IP and equal one of them. No header, an
  unparseable hop or another address → **403** `{"error": "forbidden"}`. `::ffff:a.b.c.d` is not `a.b.c.d`.
- **Then, in order:** `Content-Type: application/json` (a charset parameter is fine) else **415**; no query string
  else 400; at most **4096** bytes (Content-Length or counted while streaming) else **413**; a JSON object else 400;
  the fields below else **400** `{"error": "<field>"}` (`"unknown field"` for a key outside the table); a push that
  fits Web Push else **413** `{"error": "too large to push"}` (below). None of the fields may be `null`: leave an
  absent optional field out.

  | field | rule |
  |---|---|
  | `title` | string, 1-80 characters after strip; no control characters (Cc), format characters (Cf: bidi overrides such as U+202E, zero-width characters, the soft hyphen, so also emoji joined with U+200D), line breaks (Zl, Zp) or lone surrogates. It is one line of the summary and must read as it looks. Sent stripped |
  | `body` | string, 0-300 characters, optional (default `""`). Sent as given (line breaks allowed) |
  | `tag` | `[a-z0-9][a-z0-9._-]{0,63}` (whole string) |
  | `url` | optional (default `/`): a path starting with `/` with no `//` in it, or an `https://` URL (lowercase scheme) with no credentials whose host ends in `.ts.net`. Printable ASCII with no space or backslash, at most 512 characters. **A tap opens only MC's own origin:** `sw.js` opens a same-origin url, and anything else (another `*.ts.net` app) opens `/` instead |
  | `urgent` | optional JSON boolean, default `false` |
- **Too large to push:** a valid message can still make a push over what Web Push carries. The payload (as
  `send_all` encodes it: compact JSON with non-ASCII escaped, so a 4-byte UTF-8 emoji in the request is 12 bytes
  in the push) may be at most `push.MAX_PAYLOAD` = 3900 bytes: aes128gcm adds 103 and push services take a
  4096-byte body (measured with the real pywebpush in `tests/test_review_b2.py`). Over it → **413**
  `{"error": "too large to push"}`, nothing counted or held, rather than a 202 that every push service would then
  refuse. `send_all` itself also refuses (sends nothing, logs the size) any payload over the cap.
- **Push not set up** (no VAPID file / pywebpush) → **503**, after the body is checked; nothing is counted or held.
- **Dedupe:** the same sender + tag + title + body accepted within `notify_dedupe_seconds` → **202**
  `{"status": "deduped"}`, nothing sent. The window runs from the accepted copy; repeats don't stretch it. `url` and
  `urgent` aren't part of the key. If a sent copy reached no device while devices exist, its dedupe entry is taken
  back, so the sender's retry goes out.
- **Rate:** at most `notify_rate_per_hour` accepted (sent or held) messages per sender in the rolling hour; deduped
  ones don't count → **429** `{"error": "rate limited"}` with `Retry-After` (seconds until the oldest leaves the
  hour, 1-3600). Urgent messages are rate-limited too.
- **Quiet hours** (`quiet_hours`, in `timezone`, start included, end excluded): a non-urgent message inside the
  window → **202** `{"status": "held"}`. At most 50 are held; past that the oldest is dropped and counted. The
  background loop checks every 60 s; once the window is over (or `quiet_hours` is turned off), everything held goes
  out as **one** push: title `Held overnight: N` (N includes the dropped ones), body up to 5 `<sender>: <title>`
  lines (oldest kept first) plus `…and K more`, tag `n.held`, url `/`. Urgent messages are always sent at once.
  - **The body is at most 400 UTF-16 units** (what `sw.js` cuts at), the "more" line included. Each line gets an
    equal share of the room, the shortest first, so a title that fits leaves its spare room to the longer ones; a
    title over its share is cut and ends in `…`. The sender is never cut. So the summary always fits a push.
  - **Back-off:** a summary that reached no device while devices exist, or that can't go out because push is
    disabled, stays held and is tried again after 1, 2, 4, 8, 16, 32 minutes, then hourly; a delivery resets it.
    The back-off is in memory, so a restart tries at once.
- **Otherwise** → **202** `{"status": "sent"}`, payload `{title: "<sender>: <title>", body, tag, url}`. "Sent"
  means handed to the push services; the log line has the delivered count.
- **The pushed tag is at most 64 characters**, since `sw.js` cuts a longer one (and two tags cut to the same 63
  characters would replace each other): `n.<sender>.<tag>` when that fits, else `n.<sender>.~` + the first 16 hex of
  the tag's SHA-256. The same tag always gives the same pushed tag, so replace-on-device still works, and since a
  tag can't start with `~`, a hashed one never equals a literal one.
- **State:** `state_dir/notify.json` (0600, the server its only writer, atomic write under the file lock, read and
  written under one lock per request): `dedupe` (a SHA-256 of sender, tag, title and body: never the text), `rate`
  (accept times), `held` (`{sender, tag, title, at}`: the titles are kept at rest until the summary goes out; the
  body and url never are) and `dropped`. Loaded like `alerts.json`: bad entries are dropped, numbers must be finite
  and within 10 years of now, a file that can't be parsed is logged and treated as empty. It survives a restart.
  **Future times:** a dedupe or rate entry more than 5 min after now is dropped, on load and again on every use, so
  a poisoned file or a clock stepped back can't silence a message or give a false 429. A held message is never
  dropped for its time (that would silence it): its time is clamped to now.
- **Logging:** the sender name, the tag (only once it passed its rule) and the outcome. Never the title, body, url,
  a token or a hash; a 400 names the field, not the value.

## Lock screen

`lockscreen_detail`:
- **`none`:** "Your briefing is ready." and "N new alert(s)."
- **`minimal`** (the default): counts and fixed wording only.
  - The briefing: "Briefing ready: 3 deadlines, 2 bills, 1 alert". A source that failed shows as "deadlines
    unavailable".
  - Alerts: "Bank sync failing; Backup stale; Service failed".
  - The briefing's summary line counts backups by status, e.g. "Backups: 2 OK, 1 unknown." It never says
    "Backups OK" while one isn't.
- **`names`:** adds names, never amounts, deadline titles or text from a source. That means course codes, bill
  names, and the backup label or unit name in alerts:
  "3 deadlines (ABCD 101, MATH 111), 2 bills (Phone, Rent), 1 alert (Backup stale: Mediaserver)".

The in-app briefing and cards show amounts and titles. They are served only to `allowed_login`.

## Sources

Each source runs in parallel under its own timeout. A failure becomes that card's `error`, never a 500.
Error text is a fixed description, such as "timed out after 5s", "connection refused or unreachable",
"HTTP 403" or "invalid JSON reply". It never contains a URL, a body or a token. Redirects are never followed.
Replies are capped while they stream, after decompression (2 MiB; Home Assistant 256 KiB). The ERAU snapshot was about
0.5 MB (78 KB gzipped) on 2026-10-28. No proxies are used (`trust_env=False`).

- **AI Control** `GET /api/summary` (127.0.0.1:8095, no auth; AI Control P1 doesn't enforce identity).
  - Used: `routing.to/reason`, `services[{id,status,detail}]`, `resources.{aibox,pc}.disk[{mount,total,avail}]`
    and the count of AI Control's own alerts.
  - The broker-down alert comes from `services[id=broker]`.
- **Open WebUI** `GET /health` → 200 means up. Without `webui_url`, AI Control's `services[id=webui]` is used.
- **Money Hub.** Money Hub has no auth, so the client refuses every method but GET, and every path outside the
  frozen allowlist, before a request is built:
  - the allowlist is `/api/state`, `/api/bills?period=YYYY-MM` and `/api/budget?month=YYYY-MM`;
  - **sync:** `sync_last_at` (naive UTC), `sync_failing`, `sync_error`;
  - **bills due:** unpaid bills from today through today + 6. They come from `due_rule` (`5th`, `qtrly 7/20`;
    `biweekly`/`monthly` have no fixed day and are left out). This month, plus next month when the window
    reaches it;
  - **budget left:** the sum over capped categories of cap − spent, with the counts of categories over the cap
    and near it (≥ 85 %);
  - one failed read blanks only its own part.
- **ERAU dashboard** `GET <erau_url>/api/snapshot`.
  - Only open deadlines are kept, computed like the dashboard's own `openDeadlines()`. That excludes
    done/skipped/lapsed/optional tasks, hidden courses, done milestones, and soft milestones already past.
  - Kept: course code, milestone label, title, time, early/soft. Also the overdue count, and the `stale` /
    `needsSignIn` flags.
  - Nothing else of the payload (inbox, settings, devices) is stored or passed on.
- **ERAU is stale-while-revalidate** (Lead decision after the first deploy). ERAU's own cache takes 7-9 s to
  rebuild when cold; a fetch cut off at the round's 5 s budget never let it finish, so the card timed out forever.
  - The fetch runs in a background task bounded by `erau_timeout_s`. A round (cards, briefing, alert loop) never
    cancels it and never waits on it longer than `source_timeout_s`.
  - If it hasn't landed, the last good result is served while it is at most `erau_max_age_s` old: status as
    usual, the headline says "(as of HH:MM)", and the briefing adds "School data as of HH:MM" once it is over
    10 min old. With no usable result the card says "School data is loading" (status unknown, error "still
    loading", plus "(last try: <error>)" once a fetch has finished with an error, e.g. a hung ERAU's "timed out
    after 20s"); this is never an alert.
  - One fetch in flight at a time; it updates the cache when it lands. A fetch that fails reports its error. The
    server's shutdown cancels an in-flight fetch.
- **Units:** `systemctl show --timestamp=unix -p Id,LoadState,ActiveState,SubState,Result,NRestarts,... -- <units>`.
  - Unit names are validated by the config (they can't start with `-`). It runs with a minimal environment and
    is killed on timeout.
  - One call covers the watched units and the `unit`-kind backups.
- **Mediaserver dashboard** `GET /api/summary` → `backups.{status,lastOk}`. Its tailnet access needs no identity.
- **Home Assistant:** `GET /api/states/<entity>` with `Authorization: Bearer <token file>`, only while the
  token file exists. The token is read on each collection, never logged or returned, and redacted from error
  text.

### ERAU dashboard auth (research, 2026-10-28)

- **How ERAU checks identity:**
  - It binds a Unix socket in a 0700 runtime dir. The only way in is homebk-edge's `tailscale serve --https=443`.
  - Its guard refuses any request without a `Tailscale-User-Login` header (403 "Open this through Tailscale").
  - `ALLOWED_LOGINS` is unset in the deployed unit, so any login is accepted.
  - There is no token and no API key.
- **ai-box gets in as Noah:**
  - ai-box is a **user-owned** tailnet node (no tags), so `tailscale serve` on homebk-edge stamps its requests
    with ai-box's owner login.
  - Verified read-only from ai-box: a GET to the ERAU read endpoint through serve answered 200.
  - **MC1 needs no credential for ERAU.**
- **ai-box has no MagicDNS:** `homebk-edge.<tailnet>.ts.net` doesn't resolve there, so the config maps that name
  to homebk-edge's tailnet IP (`host_map`).
- **Caveats for the Lead:**
  1. If ai-box is ever tagged, serve stops adding identity and ERAU answers 403. The School card then shows
     "HTTP 403".
  2. The ACL must keep allowing ai-box → homebk-edge:443 (it does today).
  3. Any process on ai-box can read ERAU as Noah. That's true today, independent of MC.

## Alerts

| Id | Level | When |
|---|---|---|
| `bank-sync` | warn | Money Hub's last good sync is older than `bank_sync_alert_hours` (or there is none). A `sync_last_at` more than 1 h in the future counts as unknown; then the alert follows `sync_failing` |
| `backup:<id>` | warn (stale) / crit (failed) | a configured backup is stale or failed |
| `unit:<name>` | crit (failed) / warn (auto-restart, or restarting repeatedly) | a watched unit. forge-api goes in `units` |
| `service:webui`, `service:broker` | crit | Open WebUI's health check failed / AI Control reports the broker down |
| `disk:<aibox\|pc>:<mount>` | warn ≥ `disk_alert_pct`, crit ≥ midway to 100 | from AI Control's disks |

**Hysteresis and dedup** (Reviewer Z-1, Lead decision 2026-10-28). The alert loop runs every `alert_interval_s`.
Each alert id has a state in `alerts.json`: `pending`, `active` or `clearing`.
- **Raising:** the "down" alerts (`service:*`, and `unit:*` failed) are raised once seen in **2 of the last 3
  rounds** (Lead decision, Z2-3), so a source failing every other round is raised too, once. Until then they are
  `pending`; a pending alert not seen in any of the last 3 rounds is forgotten. The others are raised at once.
- **An episode** runs from raising until the alert has been clean for **30 min**. A cleared alert stays
  `clearing` for that time; if it comes back meanwhile, it is the same episode and isn't pushed again.
- **Pushes within an episode:** when it is raised; when its level goes **above the highest level already
  delivered** in this episode (a disk swinging 94/96 % after a crit push stays quiet); and every
  `alert_repeat_hours` after the last delivery while it stays active.
- **Held:** an alert whose source couldn't be read this round keeps its state unchanged. That covers Money Hub
  down, systemd unreadable, AI Control down, a backup unknown, and **a host whose disks are missing from AI
  Control's summary** (the PC asleep): its disk alerts are held, not cleared.
- A push that reached no device (while devices exist) isn't marked sent, so it is retried next round.
- The server is the only writer of `alerts.json` and `restarts.json`.
- **The briefing** (`/api/briefing` and `briefing --send` alike) counts and lists only the alerts **raised** in
  `alerts.json`: never a pending one, never a clearing one. Each raised alert is checked against the briefing's
  own evaluation: still reported → listed with fresh text; its source unreadable now (held) → listed with its
  stored text; source readable and no longer reported (it recovered and the alert loop hasn't run since, e.g. the
  server was down) → left out (Reviewer Z3-1). `briefing --send` reads `alerts.json` and `restarts.json` but
  never writes them, so its count matches the server's. Before the alert loop's first round it counts none.
- The Alerts **card** lists every alert the rules see right now, pending ones included, with "since" for the
  raised ones.
- **State files are untrusted input:** every number in `alerts.json` and `restarts.json` must be finite, in
  0 ≤ x < 2^53, and a timestamp within 10 years of now. A bad entry is dropped; a file that can't be parsed
  (bad JSON, an integer past Python's digit limit) is logged and treated as empty.

## Push and files

- **Subscriptions** live in `state_dir/subscriptions.json`, a list of `{endpoint, keys{p256dh, auth}, label,
  created_at}`.
  - Both the server (subscribe/unsubscribe/prune) and `briefing --send` (prune) change it.
  - So every change is a read-modify-write under an exclusive `flock` on `subscriptions.json.lock`, then an atomic
    replace: tmp in the same dir, fsync, rename, fsync of the dir. A failed write leaves the old file and no tmp
    file.
- **Size:** a payload is at most `push.MAX_PAYLOAD` = 3900 bytes of JSON (aes128gcm adds 103; push services take
  4096). `send_all` sends a bigger one to no device and logs its size.
- **Pruning:** a push service answering **404 or 410** removes the subscription. 429, 5xx, other 4xx and network
  errors keep it.
- **Logging:** endpoint URLs are device capabilities. A log line names a device by its label and gives the HTTP
  status or exception type only.

## What the deploy role (H12) must provide

- **User and files:**
  - user `mc`;
  - `state_dir` mc 0700;
  - `/etc/mission-control/config.json` root:mc 0640;
  - `vapid.json` root:mc 0640, generated once, never rotated by accident (rotation unsubscribes every device).
- **Python:** a venv with aiohttp and `pywebpush=={{ python_packages.pywebpush }}`. pywebpush pulls requests,
  cryptography, http_ece and py_vapid. Tested with Python 3.14 and aiohttp 3.14.5. ai-box's system
  `python3-aiohttp` is 3.13.3, which AI Control uses; the code uses no 3.14-only API, but only 3.14.5 was run.
- **Network:**
  - `IPAddressAllow`: localhost, homebk-edge's tailnet IP (Money Hub :8710 and ERAU :443), **ai-box's own
    tailnet IP** (Open WebUI listens on 100.x:8080, not on lo), the mediaserver and Home Assistant IPs when
    configured, and egress 443 to the push services.
  - systemctl talks to systemd over D-Bus (`/run/dbus/system_bus_socket`), so the unit needs `AF_UNIX` and must
    not hide `/run/dbus`.
- **Timer:** a 05:30 America/New_York timer with `Persistent=true`, running `mission-control briefing --send`.
- **Verify step:** `curl -H 'Tailscale-User-Login: <login>' http://127.0.0.1:8097/api/cards` gives 200, and
  without the header 403. `GET /healthz` needs no header.

## Tests

```
python3 -m unittest discover -s tests -t . -v      # needs aiohttp; the real-pywebpush test runs only if pywebpush imports
ruff check .                                       # settings in ruff.toml (same result from any directory)
```

Every source is a fake: Money Hub, AI Control, ERAU, Open WebUI, the mediaserver dashboard, Home Assistant and a
push service are local HTTP servers, and systemctl is a script. All data is synthetic.

The tests cover:
- identity refusal;
- timeouts and errors becoming error cards;
- Money Hub's read-only client;
- the briefing content and lock-screen texts;
- alert hysteresis, dedup, repeat and hold (incl. Reviewer Z's flapping cases, `tests/test_review_z.py`);
- subscription pruning, atomic writes and the lock;
- no secrets in logs;
- the config rules;
- the CLI;
- `POST /api/notify` (`tests/test_notify.py`): every status code; the guard kept on every other route, method and
  path spelling; the token compared with every sender; `allow_from` with and without `X-Forwarded-For`; dedupe, the
  rolling hour and `Retry-After`; quiet hours across midnight, the held cap and drop count, the single summary,
  urgent; the state file across a restart and poisoned; no message text or token in the logs; the new config keys;
  the Apps card;
- the Lead's follow-ups (`tests/test_review_b2.py`): requests aiohttp can't parse (oversized and control-character
  `Authorization`, a bad header, request line, method or chunk) never echo or log the token; the push cap measured
  with the real pywebpush, 413 for a valid message too big to push, the summary always fitting; future times in
  `notify.json` (poisoned, a clock stepped back, the 5 min slack); the summary's back-off; pushed tags within 64 and
  the collision case; the summary body within 400; format characters refused in titles.
