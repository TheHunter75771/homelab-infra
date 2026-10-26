# gpu-broker

An Ollama-compatible proxy for ai-box (contract: `docs/CONTRACTS.md` §12.5). Clients such as Open WebUI
talk to one Ollama URL. For each request the broker asks gpu-gate (§12.4) whether the gaming PC's GPU is
free. If it is, the request goes to the gaming PC's Ollama. If not, it goes to ai-box's own CPU Ollama.

## What it does

| Path | Behaviour |
|---|---|
| `/api/chat`, `/api/generate`, `/api/embed`, `/api/embeddings`, `/api/show`, `/api/ps`, `/api/version`, `/v1/*`, `/` | **Routed** by the gate. The JSON body's `model` is rewritten when it names an alias. Streaming is preserved. |
| `/api/tags`, `/v1/models` (GET) | Answered by the broker: the aliases, listed as models, followed by the CPU backend's models. |
| `/v1/models/<alias>` (GET) | Answered by the broker: the alias's entry exactly as `/v1/models` lists it. `/v1/models/<real model>` is routed like any other `/v1/*` request. |
| `/api/pull`, `/api/delete`, `/api/create`, `/api/copy`, `/api/push`, `/api/blobs/*` | **CPU backend only.** The gate is not asked and the body is passed through untouched. |
| `/broker/healthz` | `{"ok": true}`. Liveness only: the broker never calls a backend for it. |
| `/broker/status` | The last gate answer, its age, any gate error, the current routing and the alias map. It does not query the gate itself. |
| anything else | 404 `{"error": ...}`, not proxied. |
| a target that isn't already normal | 400 `{"error": ...}`, not proxied (see **Request targets** below). |

- **Routing.** `GET {gate_url}/status` is cached for `status_ttl` (5 s) and has a timeout of `gate_timeout`
  (1 s). Only `"available": true` (a JSON boolean) routes to `gpu_url`. A timeout, a refused connection, a
  non-200 status, bad JSON or a missing field all count as unavailable and route to `cpu_url`. A failed fetch
  is cached for the TTL too, so a dead gate costs at most one timeout every 5 s. Concurrent requests share a
  single fetch.
- **Aliases.** `aliases: {name: {gpu: model, cpu: model}}`. When a routed request's body has `model` (or the
  deprecated `name`) set to an alias, that field is rewritten to the model for the chosen backend. The rewritten
  JSON is re-encoded as ASCII, with non-ASCII characters escaped, so any string the parser accepted
  re-encodes, even a lone surrogate. Every other body (non-alias models, non-JSON bodies) is forwarded
  unchanged, with one exception. aiohttp decodes request bodies sent with `Content-Encoding: gzip`,
  `deflate`, `br` or `zstd` (br needs Brotli, which isn't installed, so aiohttp rejects it with 400). The
  broker forwards the decoded body and drops `Content-Encoding` and the stale `Content-Length`. Any other
  `Content-Encoding` value reaches the broker still encoded and is forwarded as-is, with its header. **Responses are not
  rewritten.** Streamed chunks and final objects carry the backend's real model name (for example
  `qwen3:8b`), not the alias. Clients we use don't care, and rewriting every NDJSON/SSE chunk would cost
  latency on every token. Management endpoints never rewrite aliases, because an alias is not a model you
  can pull or delete.
- **Model list.** Alias entries borrow the metadata (size, details, digest; `created` for `/v1/models`) of
  their CPU model when the CPU backend has it. Otherwise they get a minimal entry. A CPU model with the same
  name as an alias is hidden behind the alias. If the CPU backend can't be reached, the list still comes back
  with status 200 and the aliases only, plus a `X-Broker-Warning: cpu-model-list-unavailable` header. The
  reason is that the GPU may still be able to serve the aliases.
- **Streaming.** The response body is relayed chunk by chunk as it arrives (Ollama NDJSON and OpenAI SSE
  alike) and is never buffered whole. The backend's raw bytes are passed through with no decompression,
  along with its `Content-Type`, `Content-Encoding` and `Content-Length`. If the client disconnects (before
  or after the first byte), aiohttp cancels the handler, which closes the backend connection so Ollama stops
  generating. This depends on `handler_cancellation=True`. aiohttp's `web.run_app` defaults it to False, so
  the broker never uses `run_app`. Production (`serve`) and the tests share one runner factory,
  `server.make_runner`, which sets it.
- **Failover.** A GPU attempt counts as failed if any of these happen before the first response byte:
  connect error or timeout (`connect_timeout`, 3 s), the connection drops, a status in `failover_statuses`
  (default 500/502/503/504), or the optional `gpu_first_byte_timeout`. The request is then retried
  **once** on CPU, with the alias resolved again for CPU. Once a GPU byte has reached the client there is
  no failover: if the GPU breaks mid-stream, the client connection is cut so the truncation shows. CPU
  failures are never retried; when the CPU can't be reached the broker returns 502 `{"error": ...}`.
- **Headers.** Every proxied response carries `X-Backend: gpu|cpu`, and failovers also carry
  `X-Broker-Failover: gpu`. Responses the broker generates itself carry `X-Backend: broker`: the
  400/404/405/408/413 errors, the 503s for shutdown and for budget, and `/broker/*`. `/api/tags`, `/v1/models` and
  `/v1/models/<alias>` say `cpu`, because their data comes from the CPU backend. The 502 for an
  unreachable CPU also says `cpu`. Hop-by-hop headers are dropped in both directions: the RFC 9110 list,
  `Expect`, and every header named in a `Connection` header.
- **Request targets (§12.9).** A target that would change under normalization gets 400 and is never
  proxied. That covers dot segments (`.`, `..`), empty segments (`//`), and percent-encoded unreserved
  characters such as `%2e` or `%61`. Encoded path separators `%2F` and `%5C` (any case) are rejected too,
  because a backend may decode them into a separator: `/v1%2F..%2Fapi%2Fpull` would otherwise reach the
  GPU. Anything that isn't origin-form is also rejected: absolute-form `GET http://host/...` and
  `OPTIONS *`. Other encoded characters such as `%3A` pass through untouched. The query string is not
  checked.
- **Unparseable requests.** Some requests never reach the app because aiohttp's HTTP parser rejects them
  first, for example `GET *`, `GET host:80` or a malformed header. A small `RequestHandler` subclass
  (`server.BrokerRunner`) answers these with JSON 400 `{"error"}` and `X-Backend: broker`, and logs one
  WARNING line instead of aiohttp's ERROR traceback. A non-HTTP first line (for example TLS) is logged
  at DEBUG. This relies on aiohttp 3.14 internals: `AppRunner._make_server` returns a plain
  `web.Server`. The tests cover it, so an aiohttp upgrade that breaks it fails the suite.
- **Buffering (§12.9, round 2).** Routed requests and non-blob management calls are read whole into
  memory, because the body is kept for the GPU-to-CPU retry until the backend's first response byte.
  - **Small bodies (1 MiB or less)** never wait for budget and use none. A tiny `/api/show` is never
    queued behind anything.
  - **Larger bodies** reserve bytes from a shared budget, `max_buffered_bytes` (default 256 MiB), before
    they are held. A body with a real `Content-Length` reserves that size up front. A body with no
    declared size (chunked) or a decoded one (gzip and the like, where `Content-Length` is the compressed
    size) reserves `max_body_mib` once it passes 1 MiB, and that shrinks to the actual size once read. A
    reservation also tops up if the bytes read ever exceed it.
  - **Waiting for budget** lasts at most `buffer_wait_timeout` (10 s), then 503 with `X-Backend: broker`.
  - **Reading the body** must finish within `body_read_timeout` (30 s), not counting time spent waiting
    for budget, then 408 with `X-Backend: broker`. A slow-trickling client can't hold anything for longer
    than that.
  - **Too large or unreadable:** a body over `max_body_mib` gets 413. A declared size over the limit is
    refused before anything is read. A corrupt encoded body (bad gzip, say) gets 400, on buffered and
    streamed blob paths alike. The broker writes that 400 itself and closes the connection without
    draining, because aiohttp's drain would hit the same decode error and log an ERROR traceback each
    time. A client repeating it gets one WARNING line per request.
  - **Release:** the reservation is released when the request leaves that stage, whatever the reason:
    response, error, failover, client cancel, timeout.
  - **After an early error:** the connection isn't kept alive. aiohttp then drains whatever the client
    still sends for up to 10 s, so the client can read the error. The budget is already free by then.
  - Streamed blob uploads are never buffered and take no budget. `/broker/status` shows
    `buffer: {max_bytes, used_bytes, waiting}`.
- **Shutdown.** On SIGTERM or SIGINT the listener closes, and in-flight requests get `shutdown_timeout`
  seconds (default 10) to finish. After that they are cancelled, which closes their backend connections.
  From the moment shutdown starts, a failed backend request returns 503 `{"error": "gpu-broker: shutting
  down"}` and is never retried on the other backend. The HTTP client sessions close only after every
  handler has finished. The process exits 0 with no traceback.
- **Logging.** stdout gets exactly one line per request when it completes (that is, after the stream ends):
  `path= method= alias= backend= status= latency_ms= ttfb_ms= failover= [note=]`. A client that left
  mid-stream is logged as `status=499 note='client-gone'`. A request cancelled because SIGTERM's
  `shutdown_timeout` ran out is logged as `status=- note='shutdown-cancel'`. `/broker/*` requests are logged at DEBUG so
  health checks don't flood the journal. Gate state changes are logged once each.

## Configuration

The config is a JSON file whose path comes from `--config`, or from the env var `GPU_BROKER_CONFIG`
(default `/etc/gpu-broker/config.json`). Only the three URLs are required. Unknown keys are rejected, so
typos fail at startup. `python -m gpu_broker --config FILE --check` validates a file and exits.
`python -m gpu_broker --healthcheck` exits 0 if `/broker/healthz` answers on the config's
`listen_host`/`listen_port` (`0.0.0.0` → `127.0.0.1`, `::` → `::1`). The image's `HEALTHCHECK` runs it and
gets the config path from `GPU_BROKER_CONFIG`, which the image sets to `/etc/gpu-broker/config.json`. To
use a different path, change that env var; don't pass `--config`, or the check won't find the file.

| Key | Default | Meaning |
|---|---|---|
| `gate_url` | required | gpu-gate base URL (`http://<gaming-pc-tailnet-ip>:11500`) |
| `gpu_url` | required | Gaming PC Ollama |
| `cpu_url` | required | ai-box Ollama (e.g. `http://ollama:11434` on the compose network) |
| `listen_host` / `listen_port` | `0.0.0.0` / `11434` | Listen address inside the container |
| `status_ttl` | `5` | Gate answer cache (s) |
| `gate_timeout` | `1` | Gate request timeout (s) |
| `connect_timeout` | `3` | TCP connect timeout to either backend (s) |
| `gpu_first_byte_timeout` | `null` | Optional limit on the wait for the GPU's first byte (s). Off by default because a long non-streaming GPU generation is legitimate. |
| `failover_statuses` | `[500,502,503,504]` | GPU statuses that trigger the CPU retry |
| `max_body_mib` | `64` | Largest buffered request body. Chats with base64 images can be large. Blob uploads are streamed and not limited. |
| `max_buffered_bytes` | `268435456` (256 MiB) | Byte budget for buffered bodies over 1 MiB. Must be at least `max_body_mib` MiB. |
| `body_read_timeout` | `30` | Whole-body read deadline (s); 408 after it |
| `buffer_wait_timeout` | `10` | Longest wait for budget (s); 503 after it |
| `shutdown_timeout` | `10` | Grace period for in-flight requests on SIGTERM (s) |
| `log_level` | `INFO` | |
| `aliases` | `{}` | `{name: {gpu: model, cpu: model}}`. Both keys are required. |

There are no read or total timeouts on backend responses, because CPU generation can take minutes.
`config.example.json` shows the shape and uses placeholders; real identifiers belong in host_vars.

## Why aiohttp (one dependency)

`aiohttp` provides both the HTTP server (its own, not ASGI) and the async client in one package, with first-class
incremental streaming on both sides (`StreamResponse.write`, `content.iter_chunks`). The obvious
alternative is starlette + uvicorn + httpx: three top-level packages, about 10 installed distributions
(anyio, h11, httpcore, certifi, click and others), and two HTTP stacks to understand. aiohttp pulls in 8
small transitive dependencies (yarl, multidict, frozenlist, propcache, aiosignal, attrs, aiohappyeyeballs,
idna). All of them ship manylinux wheels, so the image needs no compiler. JSON config keeps PyYAML out.
The tests use the stdlib `unittest` (with aiohttp's `TestServer` for the stubs only), so there are **no dev
dependencies**.

- `requirements.in`: the one top-level pin (`aiohttp==3.14.5`).
- `requirements.lock`: every installed distribution pinned with `==` and the sha256 of every file PyPI
  publishes for that version. It was resolved on CPython 3.14, which is the image's Python. Install it with
  `pip install --require-hashes --only-binary=:all: -r requirements.lock`. To regenerate it: install
  `requirements.in` into a clean 3.14 venv, `pip freeze`, and fetch the hashes from the PyPI JSON API. A
  different Python minor version may need different pins (for example `typing-extensions` below 3.13).

## Run

```sh
# Image (non-root uid 10001, wheels only, no pip in the app venv, config-aware healthcheck)
docker build -t homelab/gpu-broker:0.1.0 apps/gpu-broker
# The role should pass the digest-pinned base: --build-arg PYTHON_IMAGE=python:3.14-slim@sha256:...
docker run -v ./config.json:/etc/gpu-broker/config.json:ro -p 127.0.0.1:11434:11434 homelab/gpu-broker:0.1.0

# Local (no container)
python3 -m venv .venv && .venv/bin/pip install --require-hashes --only-binary=:all: -r requirements.lock
.venv/bin/python -m gpu_broker --config config.json
```

The broker has **no authentication**. Exposure is limited by where the port is published (tailnet only,
via role `ai_stack`).

## Test

```sh
cd apps/gpu-broker
.venv/bin/python -m unittest discover -s tests -t . -v
```

The tests run a stub gpu-gate and stub GPU and CPU Ollamas as real local HTTP servers, plus a raw socket
server that drops connections. The broker itself always runs on the **production runner**
(`server.start`), never on `TestServer`, because `TestServer` forces `handler_cancellation=True` and would
hide a production misconfiguration. `EntryPointTests` go further and run `python -m gpu_broker` as a
subprocess, then check a client leaving mid-request, SIGTERM mid-request and `--healthcheck`. The tests
cover:

- routing both ways, and gate error, bad JSON, refused connection and timeout all routing to CPU quickly
- cache TTL, a shared fetch for concurrent requests, failures cached
- alias rewrite for GPU, CPU, OpenAI bodies and the `name` field; non-alias and non-JSON bodies forwarded unchanged
- NDJSON and SSE: the first chunk arrives before the backend has sent its last one
- client disconnects (both before and after the first byte) close the upstream
- failover on a refused connection, a dropped connection, 503 and the first-byte timeout, with exactly one CPU retry
- no failover mid-stream, both backends down giving 502, CPU failures not retried
- management requests going to CPU only without consulting the gate, blobs streamed above `max_body_mib`, 413 for oversized bodies
- `/api/tags` and `/v1/models` merges, including a CPU outage
- `X-Backend` on every proxied response, `X-Backend: broker` on every broker-generated one, `/broker/*`, the log line, and config validation
- review round 2: the byte budget, covering trickling and stalled clients (small and large), four slow generations plus a tiny `/api/show`, waiting and then proceeding, chunked reserve-then-shrink, decoded sizes (including a compressed `Content-Length` over 1 MiB and a gzip bomb), release on every path, refusing an oversized declared size before reading, and a unit test of `ByteBudget`; a corrupt gzip body (buffered and blob) giving one WARNING line and no ERROR or traceback; `%2F`/`%5C` rejection; quiet JSON 400s for unparseable targets; the shutdown-cancel log note
- review round 1: production cancellation, a lone surrogate, gzip bodies (routed, management and blob), an unknown encoding, `Connection`-named headers in both directions, the path normalization and absolute-form 400s, no failover during shutdown or on a closed session, `/v1/models/<alias>`, a large body resent intact on failover, and the healthcheck following the config
