# ai-control server (module S)

The read-only API behind AI Control (contract §12.15.5), plus the static web app (module W) at `/`.
Python 3, aiohttp from the Ubuntu archive (`python3-aiohttp`, 3.13 on 26.04), no other dependencies.
It runs on ai-box as `ai-control.service` (user `aictl`), listening on 127.0.0.1:8095 behind `tailscale serve`.

## Run

```
python3 -m ai_control --config /etc/ai-control/config.json          # serve
python3 -m ai_control --config /etc/ai-control/config.json --check  # validate the config and exit
```

The config path can also come from `AI_CONTROL_CONFIG`. Logs go to stderr (journald). Exit status 2 is a bad config.

## Config

One JSON object. Every key is optional, and unknown keys are refused. See `config.example.json`, where the
uppercase placeholders are rendered by the deploy role.

| Key | Default | Meaning |
|---|---|---|
| `listen_host`, `listen_port` | `127.0.0.1`, `8095` | where the server listens |
| `gate_url` | `http://127.0.0.1:11500` | gpu-gate (`/status`) |
| `broker_url` | `http://127.0.0.1:11434` | gpu-broker (`/broker/status`, `/broker/recent`, `/api/tags`, `/api/ps`) |
| `webui_url` | `http://127.0.0.1:8080` | Open WebUI (`/health`) |
| `helper_socket` | `/run/aictl-helper.sock` | helper X |
| `agent_url`, `agent_token_file` | `http://127.0.0.1:11510`, `/etc/ai-control/agent-token` | host agent Y and its bearer token (the file is re-read on every call) |
| `chronyc`, `proc_root`, `disk_mounts` | `/usr/bin/chronyc`, `/proc`, `["/"]` | ai-box host facts |
| `static_dir` | `/opt/ai-control/web` | module W's files (`index.html` is the SPA fallback) |
| `gate_samples_path` | `/var/lib/ai-control/gate-samples.jsonl` | gate history persistence |
| `sample_interval`, `persist_interval`, `history_hours` | `10`, `300`, `24` | gate sampler (s, s, h) |
| `gate_threshold`, `gate_window` | `50`, `1800` | shown as `routing.threshold` and `routing.window_s` (the gate doesn't report them) |
| `source_timeout` | `4` (max 4) | per-source timeout in the summary |
| `request_timeout` | `10` (max 10) | timeout for pass-through endpoints (logs, audit, requests) |
| `summary_interval`, `summary_ttl` | `10`, `5` | background rebuild period; how long `/api/summary` reuses a build |
| `sse_max_clients`, `sse_buffer_limit`, `sse_write_timeout`, `sse_keepalive` | `8`, `1048576`, `30`, `15` | `/api/events` limits |
| `disk_low_pct` | `10` | the disk alert (warn below it, crit below half of it) |
| `log_sources` | X's allowlist + `pc:ollama`, `pc:gpu-gate` | `[{id, label}]`, in picker order: the sources `/api/logs` accepts and `/api/logs/sources` lists. ai-box ids go to helper X; `pc:` ids (only those two) go to host agent Y |
| `timezone` | `null` | `server.timeZone` (null: `/etc/timezone`, then the `/etc/localtime` link, then UTC) |
| `log_level` | `INFO` | |

## API

GET only. Every other method is a 405. Errors are JSON `{"error": "..."}` with the right status, and never include a traceback.

| Endpoint | Params | Reply |
|---|---|---|
| `/api/summary` | none | the §12.15.5 summary, with an `ETag` (`If-None-Match` gives a 304) |
| `/api/gate/history` | `minutes` 30–1440 (default 30) | `[{ts, gpu_busy, ollama_share, avg30, available}]`, oldest first, ≤ 300 points |
| `/api/requests` | `limit` 1–200 (default 50) | the broker's `/broker/recent` (pinned: a bare array; anything else is a 502), newest first, only the contract fields; `alias` is null for a plain model name |
| `/api/logs/sources` | none | `[{id, label}]` from the config |
| `/api/logs` | `source` (required, an id from `/api/logs/sources`), `lines` 1–1000 (default 200) | `["line", ...]` (X's `text`), oldest first, each ≤ 2000 chars |
| `/api/audit/sudo` | `user` claude\|localai (required), `host` aibox\|pc (default aibox), `since` epoch s, `limit` 1–500 (default 100) | `[{ts, user, runas, cwd, command, event, ambiguous}]`, `event` ∈ `command\|refused\|auth_fail`; `ambiguous` (bool, required from X and Y) is true when the row's cwd/command may be mis-split |
| `/api/audit/sessions` | `user` (required) | `{"sessions": [{id, ts, runas, tty, cwd, command}], "skipped": N}` (ai-box, newest first, `ts` epoch s; `skipped` = sessions the helper left out because their metadata is damaged) |
| `/api/audit/replay` | `user` (required), `id` (required), `output` 0\|1 (default 0) | X's replay object as reported (`id` in X's path form, `ts`, `runas`, `tty`, `cwd`, `command`, `output`, `output_truncated`, `output_damaged`?) plus `user`; `output` is null unless `output=1` |
| `/api/audit/localai` | `since` epoch s, `limit` 1–500 (default 100) | `[{ts, unit, comm, message}]` |
| `/api/events` | none | SSE: `event: summary` / `data: {"hash": "..."}` when the summary changes, plus `: keepalive` comments; 503 when 8 streams are open |
| `/healthz` | none | `{"ok": true}` |
| anything else not under `/api` | | a static file from `static_dir`, else `index.html` |

- **Upstream failures:** a source that is down or misbehaving gives a 502. A helper refusal `not-found` gives a 404, `bad-request`/`not-allowed` a 400, and `damaged` a 422 `{"error": "this session log is damaged"}`. A replay's `output_truncated` is also true when a line was over 8 KiB.
- **Helper X shapes are pinned** (`apps/ai-control/helper/README.md`) and parsed strictly: `{"containers"}`, `{"lines"}`, `{"sessions"}`, `{"commands"}`, `{"entries"}`, `{"timers"}`, and a replay object with a string `id`. Any other shape is that summary section's `{error}`, or a 502 on an endpoint. Model `modified` and `expires_at` are epoch seconds on both sides; the CPU side converts Ollama's RFC 3339 text. Host agent Y's shapes are pinned too (`apps/ai-control/host-agent/README.md`) and parsed just as strictly: `{"services"}`, `{"models", "loaded"}` (each a list or its own `{error}`), `{"disk"}`, `{"lines": [str]}`, `{"entries"}`, with integer epoch timestamps. A Y reply in the wrong shape marks the agent down and errors only its own section.
- **Query parameters:** unknown or repeated parameters are a 400. Integers are plain digits only.
- **Session ids** (`/api/audit/replay?id=`): they must match the §12.15.2 regex, with no `..`, no leading or trailing `/`, and no `//`.

**Guards.**
- `/api/*` refuses `Sec-Fetch-Site` values other than `same-origin`, `none` or absent, with a 403.
- Every response carries the strict CSP, `nosniff`, `Referrer-Policy: no-referrer` and `X-Frame-Options: DENY`. `/api/*` and `/healthz` are also `Cache-Control: no-store`.
- Requests the HTTP parser rejects get a JSON 400 with the same headers.

**Identity.** `server.identity` is read from `Tailscale-User-Login` / `Tailscale-User-Name`, but only when the TCP peer is
127.0.0.1. Control characters are stripped and each value is capped at 256 chars. P1 shows the identity and doesn't enforce it.

**Summary sources.** All sources are fetched in parallel, each with its own timeout of 4 s or less, so a dead or slow source only turns its
own section into `{"error"}`. The routing section comes from the sampler's latest gate answer plus the broker's `routing_to`. A broker
that's down gives `to: null, reason: "broker_down"`. The CPU model list and the CPU loaded list come through the broker (`/api/tags`, `/api/ps`)
and are refreshed at most once a minute. The CPU loaded list is known only while the broker routes `/api/ps` to the CPU.

**Alerts.**
- A service down: `crit` for the broker and the CPU Ollama, `warn` for the rest.
- The gate can't be read.
- An alias model is missing on its side.
- Disk is below `disk_low_pct` free.
- chrony isn't synchronised.

## Tests

```
python3 -m unittest discover -s tests -t . -v
```

The tests run against fake gate, broker, Open WebUI and agent HTTP servers, a fake helper Unix socket, a fake `chronyc` and a fake `/proc`.
All data is synthetic.
