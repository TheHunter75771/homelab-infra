# ai_stack

The ai-box compose stack (docs/CONTRACTS.md §12.10, module H2). Hosts: `ai_servers`, after role `docker`
in `playbooks/ai-server.yml`.

| Service | Image | Published | Memory |
|---|---|---|---|
| `ollama-cpu` | `images.ollama` | nothing (compose network only) | 5g |
| `gpu-broker` | `local_images.gpu_broker`, built on the host | `{{ tailnet_ip }}:11434` | 256m |
| `open-webui` | `images.open_webui` | `{{ tailnet_ip }}:8080` (read-only binds `/skills`, `/writing`) | 2g |
| `tika` | `images.tika` (`-full`: Tesseract included) | nothing (internal network `extract` only) | 1g |

All four: `restart: unless-stopped`, `memswap_limit` = `mem_limit` (no swap: OOM-killed rather than
thrashing the HDD), `no-new-privileges`, label `homelab.watch=true` (the label the
notifier's alert floor and the dashboard's `homelab-dockerctl` select on; neither runs on ai-box yet, so
today it only marks the containers as homelab-managed), healthchecks (`ollama list`; the broker image's own
`--healthcheck`; Open WebUI's `/health`; Tika's `GET /tika` via `wget`). The project network `ai` is a plain
compose bridge (its subnet comes from Docker's pinned address pools). Volumes: `ollama-models`,
`open-webui-data`.

`tika` (docs/CONTRACTS.md §12.18b) is on its own **internal** network `extract` (`internal: true`: no
gateway, so no egress to the internet, LAN or tailnet), which only `open-webui` shares; it is not on `ai` and
publishes nothing. It parses untrusted uploads, so it also runs as the image's user (35002) with a read-only
root, `cap_drop: ALL`, `no-new-privileges`, `pids_limit` 256, and a 128 MiB `/tmp` tmpfs (its only writable
path; tmpfs pages count against the 1g limit). `open-webui` gets `gw_priority: 1` on `ai`, so its publish
and egress stay there. There is no `depends_on`: chat never waits on the extractor; uploads fail until it's
healthy.

## What a run does
1. **Preflight:** `tailnet_ip` and `gaming_pc_tailnet_ip` are IPv4, the model variables are unset or plain
   names, and `tailnet_ip` is on the host (tailscale is up).
2. **Layout:** `{{ homelab_stacks_dir }}/ai/` and `/etc/homelab-ai/` (root 0755).
3. **Open WebUI secret:** `{{ homelab_stacks_dir }}/ai/.env` (root 0600) holds only `WEBUI_SECRET_KEY`,
   64 hex chars generated **once on the host** (Python `secrets`, `O_EXCL`, `creates:`). It is never in the
   repo, the vault or the output. Open WebUI reads it through `env_file`. Without it, Open WebUI makes its
   own key inside the container (not on the data volume), so every container recreate would sign everyone
   out. Losing the file costs the same (a re-login), nothing more. No vault variable is needed.
4. **Broker image:** `apps/gpu-broker` is synced to `/opt/homelab-ai/gpu-broker-src` (only `Dockerfile`,
   `.dockerignore`, `requirements.lock` and `gpu_broker/`; `--delete-excluded`). A source hash (every
   file's path + sha256, plus `images.python_base`) is compared with the image's `homelab.source-hash`
   label. It's rebuilt (`docker_image_build`, `PYTHON_IMAGE={{ images.python_base }}`) only when they
   differ: first run, source change, base bump, or a lost/overwritten image. Same gate as role `stacks`.
5. **Broker config** `/etc/homelab-ai/gpu-broker.json` (root 0644), the broker's real keys
   (`gpu_broker/config.py`): `gate_url` `http://{{ gaming_pc_tailnet_ip }}:11500`, `gpu_url`
   `http://{{ gaming_pc_tailnet_ip }}:11434`, `cpu_url` `http://ollama-cpu:11434`, and `aliases`:
   `{assistant: {gpu: ai_gpu_model, cpu: ai_cpu_model}}` only when **both** are set, else `{}`. `gpu_num_ctx`
   (the broker's fixed GPU context, apps/gpu-broker README "Fixed context") is `ai_stack_broker_gpu_num_ctx`,
   which follows the shared `ai_gpu_num_ctx` (group_vars/all, 32768; the gaming PC's `OLLAMA_CONTEXT_LENGTH`
   reads it too). It renders as a JSON number, or `null` when `ai_gpu_num_ctx` is unset (the client's value
   passes through). The preflight (`tasks/broker_num_ctx_check.yml`) refuses anything but null or an integer
   2048–262144, such as a quoted `"32768"` or a boolean. `cpu_num_ctx` is never set. Everything
   else keeps the broker's defaults. The file is validated before it's installed by the built image itself
   (`docker run --rm --network none … --check`), so a bad config never reaches the running broker. It's
   bind-mounted read-only at `/etc/gpu-broker/config.json` (`GPU_BROKER_CONFIG`), with
   `create_host_path: false`. Its sha256 is a label on the broker container, so a config change recreates
   the broker in the same `compose up` (it reads the config only at start, and an existing single-file bind
   mount would keep the old inode).
6. **Compose:** `compose.yaml` (validated with `docker compose config`), the boot helper's script and unit,
   then, **under the stack lock** (below), `docker_compose_v2` `state: present`, `pull: missing`,
   `build: never`, `wait: true` (600 s: Open WebUI's first start is slow on the HDD).
7. **Heal:** the boot helper's own check in `--heal` mode (no address wait, same lock): any tailnet-published
   service that is up without exactly `tailnet_ip:<port>` is recreated (`compose up --detach --pull never
   --force-recreate --no-deps <services>`). The task is `changed` only when the helper printed a `RECREATE`
   line, so a healthy host stays `changed=0`. In check mode it runs with `--dry-run` and reports what it
   would recreate. Then every binding is read back with `docker port` and asserted. Finally the boot helper
   unit is enabled and started.
8. **Models:** for `ai_cpu_model` and `ai_embed_model` (each when set), `ollama show` inside `ollama-cpu`,
   and `ollama pull` only if it's missing. Unset: nothing. `ai_gpu_model` is the gaming PC's (role
   `ollama_rocm`).
9. **Extraction checks** (read-only, `changed=0`, skipped in check mode; `tasks/verify_extraction.yml`):
   `tika` is healthy; `files/ocr_check.py`, run inside `open-webui` on stdin, draws a PNG with three words
   and a fresh 5-digit number (Pillow, which the Open WebUI image ships) and saves the same picture as an
   image-only PDF, then `PUT`s the PNG to `http://tika:9998/tika` and both files to `/tika/text` exactly as
   Open WebUI's TikaLoader does (no Content-Type, JSON reply); every reply must contain every word. Then
   `files/webui_extraction_config.py` reads Open WebUI's **effective** document settings from its database
   (read-only) and fails the play with the fix for the admin if they differ (below).

The preflight (step 1) also checks the **memory budget** (below). Before the compose file, the role checks
and creates the pinned networks (**Networks**, below) and sets up the skills view (**Skills**, below).

A second run is `changed=0`.

## Tailnet publishing at boot
Both ports are published on `tailnet_ip`, which tailscaled adds on its own schedule, possibly after
docker.service has started the containers. **Seen on the real box (docs/CONTRACTS.md §12.9, Integrator
finding 2):** Docker 29 then runs the containers healthy but **without** those port bindings (no error, an
empty `docker port`, no DNAT rule), and `docker restart` doesn't bring them back. Only recreating the
container does. (The repo's other tailnet-only service, wol-relay, binds inside the app and retries, which a
Docker publish can't do.)

So `homelab-ai-stack-start.service` (oneshot, `After=docker.service tailscaled.service`,
`PartOf=`/`Requisite=`/`WantedBy=docker.service`, the same shape as the firewall's DOCKER-USER guard) runs
`/usr/local/sbin/homelab-ai-stack-start` whenever Docker starts:
1. It waits up to 300 s for `tailnet_ip` (bounded; the unit fails if the address never appears, and Docker is
   never called).
2. For each tailnet-published service (`ai_stack_tailnet_publishes` in `vars/main.yml`; the test checks
   it against the compose file's `ports:`), it marks the service for recreation when it is `running` or
   `restarting` but `docker port` doesn't list `tailnet_ip:<port>`, or when it is down with a Docker start
   error (`State.Error`).
3. It runs one `docker compose -f …/ai/compose.yaml up --detach --pull never --force-recreate --no-deps
   <those services>`, then checks the bindings again.

**Stopped on purpose is left alone.** With `restart: unless-stopped`, Docker brings every container back at
boot (and restarts any that exit) except one stopped by hand (`docker stop`, `docker compose stop`, the
dashboard's Stop). So a container that is down (`exited`/`created`) **with no start error** was stopped on
purpose and isn't touched. A container that doesn't exist (`compose down`, `docker rm`) isn't touched either;
the next playbook run recreates it.

**Stack lock.** The helper's check-and-recreate and the playbook's `compose up` and heal never run at
once: both use `flock` on `/run/homelab-ai-stack.lock` (`ai_stack_lock_file`; root-only `/run`, not the
world-writable `/run/lock`) and give up after 300 s
(`ai_stack_lock_wait`) with a message naming who may hold it. Ansible tasks are separate processes, so the
playbook holds the lock through a transient unit, `homelab-ai-stack-lock-<random>` (`systemd-run …
flock --wait 300 <lock> sleep infinity`). The play waits for the unit's "held" marker (its
RuntimeDirectory), runs `compose up`, and stops the unit in an `always:` step. `RuntimeMaxSec`
(`ai_stack_lock_hold_max`: lock wait + compose wait + 120 s = 1020 s) frees the lock even if the play dies mid-run. The heal step then takes the lock
itself, through the helper. Check mode takes no lock (nothing changes); a `--dry-run` doesn't either.

No `set -e`: a failure is reported, the other services are still checked, and the unit fails (visible in
`systemctl --failed`) if anything failed: a lookup, the recreate, or a binding still missing afterwards.
docker.service, its drop-ins and sysctls are untouched.

## Exposure
- **What listens:** only `tailnet_ip:8080` (Open WebUI, login required) and `tailnet_ip:11434` (the broker,
  **no auth**). `ollama-cpu` isn't published. Nothing is on the LAN address, `0.0.0.0` or IPv6.
- **UFW doesn't decide this.** Published ports are DNAT rules, forwarded before UFW's INPUT chain sees them
  (docs/CONTRACTS.md §9b). The `firewall_allow_rules` entries for 8080 and 11434 document the intended
  policy and would apply if a service ever listened on the host directly; the `from` limits on 11434 do
  **not** restrict the DNATed broker today.
- **What restricts it:** (1) the publish address: only packets addressed to `tailnet_ip` are DNATed, and
  tailnet peers reach that address over `tailscale0`; (2) the tailnet policy (who may reach ai-box at all);
  (3) the firewall role's DOCKER-USER guard, which drops tailnet-range sources arriving on any interface but
  `tailscale0`, so a LAN host can't spoof a tailnet peer.
- **Known gap (for module A):** a LAN host that adds a route to `tailnet_ip` via ai-box's LAN address, with
  its own LAN source address, is DNATed too: the guard only checks sources. Closing it takes a DOCKER-USER
  rule on the original destination, for example
  `! -i tailscale0 -m conntrack --ctstate NEW --ctorigdst <tailnet_ip> -j DROP`. Limiting 11434 to the
  mediaserver and gaming PC for real also belongs in DOCKER-USER (`--ctorigdstport 11434`) or in the
  tailnet policy.

## First login (the admin)
1. Open `http://<ai-box tailnet IP>:8080` from a tailnet device and create the first account. Open WebUI
   makes the first account the admin. The role never creates accounts.
2. Close sign-ups: Admin Panel → Settings → General → *Enable New Sign Ups* off, and save. Then set
   `ai_webui_enable_signup: false` (group_vars `ai_servers`) and re-run the playbook, so a fresh data volume
   also starts closed.
   - Open WebUI keeps this setting in its database once the admin saves it, so the admin panel is what
     counts from then on. The variable decides the value until then and on a new volume.
   - Until it's off, anyone on the tailnet who can reach 8080 can sign up, but they get the `pending` role
     (`DEFAULT_USER_ROLE`), so they can't use anything until the admin approves them.

## Document extraction settings (Tika, OCR, 25 MB)
`open-webui` gets `CONTENT_EXTRACTION_ENGINE=tika`, `TIKA_SERVER_URL=http://tika:9998`,
`TIKA_SERVER_VERSION=3`, `RAG_FILE_MAX_SIZE=25` and `CONTENT_EXTRACTION_SUPPORTED_MEDIA_MIME_TYPES`
(png, jpeg, tiff, bmp, gif). The last one matters: in 0.12.4 an image upload is refused ("File type image/png
is not supported for processing") **before** any engine sees it, unless its type is in that list.

**OCR.** Open WebUI's TikaLoader sends no OCR header, so Tika's own config decides
(`templates/tika-config.xml.j2` → `/etc/homelab-ai/tika-config.xml`, root 0644, mounted read-only, its hash a
container label so a change recreates `tika`): `TesseractOCRParser` (English, 120 s per image) for images,
and `PDFParser` with `ocrStrategy` `auto` (pages with no text layer are rendered at 300 dpi and OCRed). The
forked worker JVM gets `-Xmx512m`, `-XX:+ExitOnOutOfMemoryError`, `-Dpdfbox.fontcache=/tmp` (PDFBox's font
cache would otherwise go to the read-only home directory) and `taskTimeoutMillis` 600000 (one document; a long
scan on 4 cores takes minutes).

**Batch uploads.** `OMP_THREAD_LIMIT=1` makes each Tesseract run use one thread instead of every core, so a
burst of images queues on the CPU instead of oversubscribing it. Tika 3.2.3's server has no setting that
caps concurrent parses (its `TikaServerConfig` has none), so a burst is bounded only by `pids_limit` 256 and
the 1g limit; if the worker JVM runs out, it exits and tika-server restarts it, and the uploads in flight
fail (re-upload them). **Integrator check:** upload about 35 PNGs to one Knowledge collection at once; every
file must reach *completed* with text, and `docker stats tika` must stay under 1 GiB (`docker logs tika`
shows no worker restart). `.eml` files go to Tika too (`message/rfc822` isn't text), which parses the mail and its
attachments instead of indexing raw base64.

**Why env alone isn't enough (Open WebUI 0.12.4, `open_webui/models/config.py`).** Settings live in the
`config` table, **one row per key**. At every start Open WebUI inserts the environment's value only for keys
that have **no row yet**; every request reads the row (no cache). This box's database was created before
these variables existed, so it already holds rows for them (engine `""`, max size `null`, media types
`null`), and those win. The options:
- `ENABLE_PERSISTENT_CONFIG=false`: **every** key then comes from the environment, and admin-panel changes
  live only in memory until the next restart. All of the admin's other saved settings (sign-ups closed,
  default models, permissions, …) would stop applying. Rejected.
- `RESET_CONFIG_ON_START=true`: deletes the **whole** `config` table at start. Rejected.
- Writing or deleting those rows from Ansible: not a supported interface, and it would undo a deliberate
  admin choice on every run. Rejected.
- **Chosen:** set the env (it decides on a fresh volume), and **verify** the effective values read-only
  (sqlite `mode=ro` + `PRAGMA query_only`, 5 s lock timeout; a missing row means the env value applies),
  compared the way Open WebUI uses them (the engine exactly `tika`; a trailing `/` on the URL, an unset
  version, `image/*` and extra types are fine). If the checker can't read the settings (database locked or
  missing, no `config` table, an unreadable row: exit 2), the play says it **can't tell** and asks for a
  manual look, not that the settings are wrong. A mismatch fails the play, listing the keys
  and what to set. The admin's settings are never touched.

**The admin, once** (and whenever the check fails): Admin Panel → Settings → Documents:
*Content Extraction Engine* = **Tika**; *Tika Server URL* = `http://tika:9998`; *Tika Server Version* =
**Tika 3.x**; *Supported Media MIME Types* = `image/png, image/jpeg, image/tiff, image/bmp, image/gif`;
*Max Upload Size* = `25`; **Save**. Then re-run the playbook. Extra media types (or `image/*`) and a trailing
`/` on the URL are accepted. Files uploaded before this keep their old extraction: re-upload them (the 35
document-screenshot PNGs, the `.eml`).

## Payday runs (`/payday-runs`, docs/CONTRACTS.md §12.27): the one read-write share
The "Payday (calculator + log)" tool saves each confirmed run as a Markdown file in the AI Library's
`50-finance/payday-runs/`. It's the third entry of `ai_stack_ro_shares`, with `mode: rw`. Everything else is
the same design as the read-only shares: its own CIFS mount of that subfolder **only**
(`var-lib-homelab\x2dai-payday\x2druns.mount`), the same reconcile timer and service, the never-stat
probes, the marker `PAYDAY-RUNS-NOT-MOUNTED`, and the in-container top-mount check.
- **Options:** the read-only ones with `ro` → `rw`, `file_mode=0640`, `dir_mode=0750`. Still `nosuid,nodev,noexec`,
  `uid=0,gid=0`, the library client's credentials, `soft`.
- **Ownership:** on ai-box the files show as root (`uid=0,gid=0`, 0640/0750). open-webui runs as root in the
  container, so it can write; no other local account can read or write there. On the PC, every file and
  folder written through the share is created by Samba as the share's **force user and group** (the desktop
  user's account, `roles/ai_library_share`, with Samba's `create mask 0664` / `directory mask 0775` on the server
  side). ai-box's 0640/0750 are client-side only. The PC never sees "root".
- **Fail-safe while it isn't mounted:** open-webui's bind of the local view is `rw` (it has to be, to write
  to the CIFS mount on top). So while the share is down, the local directory `/var/lib/homelab-ai/payday-runs`
  and its marker are **immutable** (`chattr +i`, set by `tasks/skills.yml` only while the mount unit is
  inactive). File modes can't stop root with `CAP_DAC_OVERRIDE`, but the immutable attribute can: changing
  it needs `CAP_LINUX_IMMUTABLE`, which no container here has. So nothing in open-webui can create or
  change a file on ai-box's own disk there (EPERM). Mounting the share on top of the immutable directory
  works as usual. A marker whose text must change is unlocked (`chattr -i`) first, only while unmounted.
  `tasks/verify_skills.yml` checks the attribute (`lsattr -d`, local inode only, only while not mounted).
- **Check in open-webui:** while mounted, the top mount at `/payday-runs` must be the CIFS mount itself with
  `rw,nosuid,nodev,noexec`. A local disk on top fails, and so does a read-only mount. `/skills` and
  `/writing` stay read-only, checked as before.
- **The folder on the PC:** the mount needs `50-finance/payday-runs` to exist; mount.cifs can't mount a
  missing subfolder, and until then the reconcile's start fails and the marker stays. It must also be
  declared in the AI Library's layout. `apps/skill-library` (`library/core.py`) `LAYOUT_DIRS` and
  `Library.check` allow only `50-finance/log/` today, so `library check` would report `payday-runs` as
  misplaced. That's module L's change, and the folder is created on the PC side as the force user (role
  `ai_library_share`). Not this role: it never creates folders through the share.
- **Integrator** (no exec into open-webui; the play's own check reads its mount table):
  1. With the PC up and the folder there: `findmnt /var/lib/homelab-ai/payday-runs` on the host shows cifs with
     `rw,nosuid,nodev,noexec`, and the play's check passes. The Payday tool's save (module P's acceptance)
     writes a file that appears on the PC owned by the force user.
  2. Stop `var-lib-homelab\x2dai-payday\x2druns.mount`: `lsattr -d /var/lib/homelab-ai/payday-runs` and its
     marker show `i`, and as root on the host `touch /var/lib/homelab-ai/payday-runs/x` fails with "Operation
     not permitted" (the same EPERM open-webui would get). The play's check passes.
  3. `/skills` and `/writing` are still read-only.

## Search settings (docs/CONTRACTS.md §12.22)
**Why:** a test chat over the private document collection cited 2 passages and missed the one document that
held the answer. The live settings were the
in-process MiniLM embedder (batch 1), Top K 3, hybrid search off, no task model, and the default title
prompt, whose placeholder ("your concise title here") the small model copies.

**What the role does**
- **Embedding model (`ai_embed_model`, optional, no default):** it's pulled into ollama-cpu like
  `ai_cpu_model`, and the broker gets a second alias, `ai_stack_embed_alias` (`embed`) →
  `{gpu: ai_embed_model, cpu: ai_embed_model}`. The same model on both backends keeps the vectors
  compatible whichever one answers. `apps/gpu-broker` already takes any number of aliases and proxies
  `/api/embed`, so it's unchanged. The preflight requires the two alias names to differ. It also requires
  `ai_embed_model` to be in the gaming PC's `ollama_rocm_extra_models` (`tasks/embed_pc.yml`, Reviewer L,
  L-5). That list is read through `hostvars` of the `workstations_root` host, as `roles/ai_control` reads
  the PC's vars, and the names are compared the way `ollama_rocm` normalises them (lower case, no default
  registry or `library/`, `:latest` when untagged). Without that host in the inventory, the check is
  skipped with a message.
- **ollama-cpu keeps both models loaded:** `OLLAMA_MAX_LOADED_MODELS=2`, `OLLAMA_KEEP_ALIVE=10m`,
  `OLLAMA_NUM_PARALLEL=1`, so an embedding call never evicts the chat model. Memory against the 5g
  (5120 MiB) limit, **estimated**:

  | | MiB |
  |---|---|
  | qwen3.5:4b weights (3.3 GB file, mmapped) | ≈ 3150 |
  | its KV cache at Ollama's default context, f16 (architecture-dependent) | ≈ 400–600 |
  | its compute buffers | ≈ 250–300 |
  | nomic-embed-text:v1.5 weights (274 MB, f16) | ≈ 260 |
  | its compute buffers | ≈ 100–150 |
  | **total** | **≈ 4200–4500** |

  That leaves about 0.6–0.9 GiB of headroom. It isn't measured yet: no model was loaded on ai-box at the
  read-only check (`ollama ps` was empty, ollama-cpu used 46 MiB), and nomic isn't pulled there yet. The
  Integrator measures it (below). If Ollama judges that both don't fit, it evicts despite the setting, so
  the check is `ollama ps` listing both.
- **Compose env:** these only seed a **fresh** database. Open WebUI 0.12.4 reads the database first, as
  with §12.18b (verified by reading `config.py` in the live container, read-only).

  | Env | Key | Kind | Value |
  |---|---|---|---|
  | `RAG_EMBEDDING_ENGINE` | `rag.embedding_engine` | PersistentConfig | `ollama` (with `ai_embed_model`) |
  | `RAG_EMBEDDING_MODEL` | `rag.embedding_model` | PersistentConfig | `embed` |
  | `RAG_OLLAMA_BASE_URL` | `rag.ollama.base_url` | PersistentConfig | `http://gpu-broker:11434` |
  | `RAG_EMBEDDING_BATCH_SIZE` | `rag.embedding_batch_size` | PersistentConfig | `16` |
  | `RAG_EMBEDDING_CONCURRENT_REQUESTS` | `rag.embedding_concurrent_requests` | PersistentConfig | `2` |
  | `RAG_TOP_K` | `rag.top_k` | PersistentConfig | `8` |
  | `RAG_TOP_K_RERANKER` | `rag.top_k_reranker` | PersistentConfig | `8` (used only if hybrid is on) |
  | `ENABLE_RAG_HYBRID_SEARCH` | `rag.enable_hybrid_search` | PersistentConfig | `false` |
  | `TASK_MODEL` | `task.model.default` | PersistentConfig | `assistant` (with both chat models) |
  | `TITLE_GENERATION_PROMPT_TEMPLATE` | `task.title.prompt_template` | PersistentConfig | the template below |
  | `RAG_EMBEDDING_QUERY_PREFIX` | none | **plain `os.getenv`** | `search_query: ` (with `ai_embed_model`) |
  | `RAG_EMBEDDING_CONTENT_PREFIX` | none | **plain `os.getenv`** | `search_document: ` (with `ai_embed_model`) |

  The two prefixes (nomic's task prefixes) apply on **every start**, whatever the database says. Open WebUI
  prepends them to the text for queries and documents. So between the deploy and the admin's switch below,
  queries to the old MiniLM index carry the prefix. Retrieval is a little worse for that short window, and
  the reindex fixes it.
- **Hybrid search stays off (Reviewer I, R10-1).** In 0.12.4 with Chroma and an empty reranking model,
  nothing is downloaded. But `RerankCompressor` then re-ranks the BM25 + vector union by cosine similarity
  with the **same** embedding function, so the result is the vector top-k again. Meanwhile BM25 reloads
  the whole collection on every query. It adds cost and nothing else. So this role does **not** promise
  exact-token matching: a token like a document number ("D042") is found only as far as the embedding finds it. Exact tokens
  would need a reranker model or per-passage labels in the collection's files; that's a later decision (the operator's).
  The checker still counts min(Top K, Top K Reranker) if hybrid is ever turned on; with it off, Top K alone
  must be at least 8.
- **One settings exec per converge (R10-3).** open-webui runs close to its memory limit, so the extraction
  settings (§12.18b) and the search settings are read by **one** `docker exec` of the checker
  (`tasks/verify_extraction.yml`, `ai_stack_webui_settings_expected`). `tasks/verify_search.yml` only reads
  that result.
- **Check (`tasks/verify_search.yml`):** the effective values are read from the database (read-only). It
  requires engine `ollama`, model `embed`, the broker URL, and at least `ai_stack_rag_top_k` passages. It
  also reports hybrid (wanted off), batch size, concurrent requests and the task model. It always
  **reports**, and fails the play only with `ai_stack_verify_search_settings: true`; set that once the admin has
  saved the settings. It's skipped without `ai_embed_model`.
- **Old chat uploads lose retrieval (R10-2, accepted).** *Reindex Knowledge Base Vectors* re-embeds files
  that are **in a Knowledge base** only. Files attached straight to chats (243 on ai-box) keep their
  384-dimension MiniLM vectors. After the switch, the query is a 768-dimension nomic vector. In 0.12.4,
  Chroma's `search()` catches the dimension error and returns nothing (`retrieval/vector/dbs/chroma.py`,
  `except Exception: return None`; `query_collection` skips it). So such a file **silently contributes
  nothing**: no error, no 500, and the answer just doesn't cite it. To use an old chat file again,
  **re-attach it** (a new upload is embedded with nomic). Or switch that attachment to *Using Entire
  Document*: full-context files are read whole, without vectors (`get_sources_from_items`).
  (Read from the image on the host with `docker cp`, never inside the container.)

**Order (R10-7).** The gaming PC must have the model before anything points at it. For a missing model
the GPU Ollama answers 404, and a 404 doesn't fail over to the CPU.
1. `playbooks/gaming-pc-ai.yml` (role `ollama_rocm` pulls `nomic-embed-text:v1.5` on the PC).
2. `playbooks/ai-server.yml` (this role: the CPU pull, the `embed` alias, the env).
3. The admin's clicks below.

**The admin's clicks (after both playbooks, with `ai_embed_model` set)**
1. Admin Panel → Settings → **Documents**:
   - *Embedding Model Engine* = **Ollama**; URL `http://gpu-broker:11434`; *Embedding Model* `embed`;
     *Embedding Batch Size* **16**.
   - *Embedding Concurrent Requests* = **2**. The field appears once the engine is Ollama (0.12.4
     `Documents.svelte`); 0 would mean unlimited.
   - *Top K* = **8**. Leave *Hybrid Search* **off** (see above).
   - **Save**, then **Reindex Knowledge Base Vectors** (once). Old files attached to chats are not
     reindexed (see above): re-attach one if it's needed.
2. Admin Panel → Settings → **Interface**:
   - *Task Model* (local) = `assistant`.
   - *Title Generation Prompt* = the template below, exactly. It has no example values, so there's no
     placeholder text to copy.

     ```text
     Name the subject of the chat below in 2 to 5 words.
     Use the chat's own topic, names and numbers. No quotation marks and no emojis.
     Reply with one JSON object and nothing else: the key is "title" and the value is the title.
     <chat_history>
     {{MESSAGES:END:2}}
     </chat_history>
     ```

     Open WebUI parses the reply as JSON and reads `title` (`utils/middleware.py`), so it must ask for JSON.
     `{{MESSAGES:END:2}}` is Open WebUI's own placeholder (the last two messages).
3. Admin Panel → Settings → **Models**: **hide** both `embed` and `nomic-embed-text:v1.5`. Neither is a chat
   model. The raw name is listed too: the broker's `/api/tags` lists the aliases, then ollama-cpu's own models.
4. Set `ai_stack_verify_search_settings: true` (inventory) and re-run. The play then checks these from now on.

**Integrator (§12.22.3, plus):**
- `docker exec ollama-cpu ollama ps` lists both `qwen3.5:4b` and `nomic-embed-text:v1.5` after a chat and
  a Knowledge query, and `docker stats --no-stream ollama-cpu` stays under 5 GiB (that's the measured RSS
  for the table above).
- The reindex completes with open-webui under its limit (2560m on ai-box). Switching the engine to Ollama
  unloads the in-process MiniLM model.
- **Old chat attachment (R10-2):** open an old chat that has an attached file and send a question about it.
  Expected: an answer with no error and no 500, simply without that file's passages. Then re-attach the
  file, or set it to *Using Entire Document*, and the file is used again.

## Broker management lock-down (docs/CONTRACTS.md §12.23)
**Why:** the broker forwarded model management (`/api/pull`, `/api/push`, `/api/create`, …) to ollama-cpu,
which has internet egress. The host's broker-only units (voice_profile, skill_drafter, finance_digest)
allowed the whole `ai` /24, so they could also reach ollama-cpu's API directly.

**This role's part**
- **Pinned addresses on `ai`:** ollama-cpu `172.20.0.2`, gpu-broker `172.20.0.3`, open-webui `172.20.0.4`
  (`ai_stack_ollama_cpu_ip`, `ai_stack_broker_ip`, `ai_stack_webui_ip`). These are exactly the addresses
  Docker had given them on ai-box (read-only `docker network inspect`, 2026-10-27), so the pin changes no
  address.
  - Docker 29.5.2 accepts a static address on a network whose subnet it allocated itself. It only checks
    that the network is user-defined and the address is inside its pool (`validateEndpointSettings`). The
    older "user configured subnets only" rule is gone. So `ai_ai`, created by compose before the pin, needs
    no recreate.
  - `tasks/networks.yml` asserts the three addresses are inside `ai_stack_network_subnet`, are not its
    network, broadcast or gateway (`.1`) address, and are distinct. It also checks, read-only, that no
    other container on `ai_ai` holds one of them; compose would fail to start the service otherwise.
- **Broker config:** `management_clients: ["172.20.0.4"]` (open-webui's pinned address). apps/gpu-broker
  (module B2) refuses management calls from any other TCP peer, and `/api/push` from everyone.
  open-webui reaches the broker over `ai` (`http://gpu-broker:11434`), so its peer address is exactly that
  one.
- **open-webui's address is never the gateway.** Traffic through the published port can arrive from the
  `ai` network's gateway, so a pin on the gateway would make the host a management client.
  `ai_stack_pinned_ip_problems` refuses the subnet's first host (`.1`). `tasks/networks.yml` also reads
  the live gateway (read-only), and `tasks/gateway_assert.yml` refuses any pin equal to it, open-webui's
  included. That covers a gateway other than `.1`.
- **No raw sockets on `ai` (§12.25, Reviewer S, S-8).** ollama-cpu and open-webui drop `NET_RAW`;
  gpu-broker and tika already drop `ALL`. Without NET_RAW a compromised container can't ARP-spoof the
  gateway or its neighbours. None of their healthchecks needs it (read-only `docker inspect` on ai-box:
  `ollama list`, curl to `/health`, the broker's own python check).
- **Admins can still manage models, by design (N-6).** Open WebUI's admins, and any Function or Tool an
  admin installs (it runs inside open-webui, so its calls come from `172.20.0.4`), can pull, create, copy
  and delete models through the broker. `/api/push` stays refused for everyone. Installing a Function or
  Tool is therefore a trust decision.
- **`ai_stack_broker_ip`:** the three pinned addresses are also set in the inventory
  (`group_vars/ai_servers`), and the broker-only units' ranges are built from `ai_stack_broker_ip` there.
  That's what other roles read. The role also sets it as a host fact at run time, but `ansible.cfg` has no
  fact cache, so that fact lives only for the rest of the same `ansible-playbook` run (a later play in that
  run can read `hostvars['ai-box'].ai_stack_broker_ip`), not across runs.
- **Check (`tasks/verify_broker.yml`):** from the host, `POST http://<tailnet_ip>:11434/api/push` with a model
  name that can't exist must answer **403**, with apps/gpu-broker's refusal text `/api/push is disabled` in
  the body (so the 403 can't come from anything else). The allow path (open-webui's management calls) is proven by
  apps/gpu-broker's own tests, not by a live pull.

**The host units can narrow to the broker's /32.** voice_profile, skill_drafter and finance_digest reach the
broker through `tailnet_ip`'s published port. With the userland proxy off, that's a DNAT rule. systemd's IP
filter runs at the cgroup egress hook, after the nat OUTPUT chain, so it sees the **DNAT'd destination**: the
broker container's address, not `tailnet_ip`. Replies come back from `tailnet_ip` (conntrack reverses the
DNAT), which is the ingress side. That's why their allow lists had `tailnet_ip/32` plus the whole `ai` /24:
an unpinned broker could come back on another address after a recreate. With the broker pinned, `tailnet_ip/32`
plus `172.20.0.3/32` is enough. That also denies them ollama-cpu (`.2`) and open-webui (`.4`) directly,
which is the point. The inventory change is the Lead's (`*_broker_ranges` → `<ai_stack_broker_ip>/32`).

**Integrator:**
- From the host: `curl -s -o /dev/null -w '%{http_code}' -X POST -d '{"model":"x:none"}'
  http://<tailnet_ip>:11434/api/push` gives `403`. The play checks the same.
- **No `:cloud` egress (Reviewer N, N-1).** Ollama 0.40.11 has a cloud passthrough: `*:cloud` chat, show and
  `/v1` requests go to ollama.com, signed with the server's key, signed in or not. So ollama-cpu runs with
  `OLLAMA_NO_CLOUD=1`. That's fixed in the compose file, not in `ai_stack_ollama_env`, and the preflight
  refuses the key there; the gaming PC's Ollama gets the same through role `ollama_rocm`. Check: through
  the broker, `curl -s -X POST http://<tailnet_ip>:11434/api/chat -d '{"model":"gpt-oss:20b-cloud",
  "messages":[{"role":"user","content":"hi"}],"stream":false}'` is refused, both with the GPU free and with
  the gate forcing CPU. And nothing leaves ollama-cpu: no connection to ollama.com during the call
  (`ss -tnp` in its network namespace, or the egress logs).
- From each broker-only unit's sandbox (`systemd-run` with the unit's IP filter, after the Lead narrows the
  ranges to the /32): `curl` to `172.20.0.2:11434` (ollama-cpu) is refused, and `/api/push` via the broker
  gives 403.
- Chat, embeddings and the Reindex still work. Admin → Models still lists the models. Say whether pulling
  from Open WebUI works (nice-to-have).

**Deploy note:** the pin changes the three services' compose definitions, so the first converge
**recreates** ollama-cpu, gpu-broker and open-webui. A running reindex or upload is interrupted. Converge when
nothing is running. The broker config also carries `management_clients`, which the broker image's own
`--check` refuses until module B2's broker is built. The broker image and this config must ship in the
**same** converge (this role builds the image from `apps/gpu-broker` before it validates the config, so a
tree with B2's change does both). With an old image it fails closed at the config step, before touching the
stack.

## Rerankers (docs/CONTRACTS.md §12.30)
For the retrieval experiments (E2 hybrid + MiniLM reranker, E3 hybrid + bge reranker), two cross-encoder
rerankers sit ready in open-webui's Hugging Face cache. Its Hugging Face libraries never fetch them: they run
offline (below), and the play seeds the models from the host.
- **Pinned** (`ai_stack_hf_models`): each repo under its canonical name, its exact revision (commit), and every
  file with its SHA-256 and size:
  - `cross-encoder/ms-marco-MiniLM-L6-v2` @ `233902d25c44…`: config, `model.safetensors` (91 MB),
    tokenizer, vocab. Hugging Face renamed it from `ms-marco-MiniLM-L-6-v2` (the old name redirects); the
    commit and files are the same, and the role uses only the new name (R16-3). The preflight refuses the
    old one as a reranker.
  - `BAAI/bge-reranker-base` @ `2cfc18c9415c…`: config, `model.safetensors` (1.1 GB), tokenizer,
    sentencepiece.

  The hashes come from the Hugging Face API at that revision: the LFS SHA-256 for the large files, the
  content hash for the small ones, checked against their git blob ids. Every file was downloaded once on
  the controller and its SHA-256 matched before it was pinned.
- **Seeded on the host, outside the data volume** (`tasks/hf_models.yml`, R16-1). Root in open-webui can write
  its data volume and plant symlinks there, so the host never writes into it. The models live in
  `ai_stack_hf_seed_dir` (`/var/lib/homelab-ai/hf-models`, root 0755 folders, root 0644 files), in HF's cache
  layout: `models--<org>--<name>/snapshots/<revision>/<file>` as real files, and `refs/main` holding the
  revision. compose.yaml binds each `models--<org>--<name>` folder **read-only** onto the same name under
  open-webui's HF cache (`/app/backend/data/cache/embedding/models`, `ai_stack_hf_container_cache`), with
  `create_host_path: false`. So the seeding runs **before** the Compose definition and `compose up`. The
  preflight refuses a seed folder that isn't a plain absolute path, or one under `/var/lib/docker`.
  - **No symlinks:** every folder from the seed folder's parent (`/var/lib/homelab-ai`) down to each pinned
    snapshot is checked without following links, as roles/ai_backup does: absent, or a real directory owned
    by root and not group/other-writable. Every file and `refs/main` must be absent or a regular file. Anything
    else stops the play before it writes.
  - **refs/main (R16-5):** a model with a file missing or different loses `refs/main` FIRST. Then only those
    files are fetched from `https://huggingface.co/<repo>/resolve/<revision>/<file>` (`get_url` checks the
    SHA-256 of each download before moving it into place) and hashed again. `refs/main` is written last. A model
    that isn't complete never looks complete to huggingface_hub, even when a run dies halfway. A file whose
    hash matches but whose owner or mode is off is fixed in place, without a download, and keeps `refs/main`.
    Every run hashes the 1.2 GB once, about 10–20 s on the HDD.
  - **Mount point:** the old seeding into the volume checked the volume's mount point first. With the bind
    design the play never touches the volume, so that check is gone (R16-4). The test instead checks that
    nothing in `tasks/hf_models.yml` names Docker or a volume.
  - **Tested** (tests/test_render.yml, "1k"): a scratch seed folder and a local HTTP server in place of
    huggingface.co. The cases cover a fresh seed, a check run before and after, and a wrong download (refs/main
    gone, the bad file never lands, the other model untouched). They also cover a missing file, a mode drift,
    another owner, a group-writable folder, a symlink at each folder on the path, and a symlinked `refs/main`
    and snapshot file. A refused run writes nothing through the link.
  - **Tested with the HF libraries** with Open WebUI 0.12.4's pinned versions (transformers 5.5.4,
    huggingface_hub 1.33.0, sentence-transformers 5.5.1) and `HF_HUB_OFFLINE=1`. `snapshot_download`
    resolves both models to their pinned snapshot, whether `local_files_only` is true or false, which is the
    path Open WebUI's `get_model_path` takes. Both load as CrossEncoders and score a relevant passage above
    an unrelated one. An unseeded model raises `LocalEntryNotFoundError` instead of downloading. Rechecked for
    R16 with huggingface_hub 1.33.0: a write-protected tree of this layout resolves the same way and nothing is
    written to it, and the old MiniLM name isn't found.
- **Offline means the HF libraries only** (R16-2): open-webui runs with `HF_HUB_OFFLINE=1`,
  `TRANSFORMERS_OFFLINE=1`, `RAG_EMBEDDING_MODEL_AUTO_UPDATE=false` and `RAG_RERANKING_MODEL_AUTO_UPDATE=false`;
  all plain getenv. The container itself still has internet egress. An admin's "download a GGUF from a URL"
  (`POST /ollama/models/download`, admin only) fetches a `https://huggingface.co/` or `https://github.com/` URL
  with Open WebUI's own aiohttp client, then hands the file to Ollama (read in 0.12.4's `routers/ollama.py`).
  The offline switches don't block that, and web search and tools reach the internet too.
  The models already in the cache keep loading from it: `sentence-transformers/all-MiniLM-L6-v2`,
  `TaylorAI/bge-micro-v2`, and Whisper `faster-whisper-base`, each with its `refs/main` and snapshot
  (read-only look on ai-box). Embeddings go to Ollama anyway since §12.22. The cost: picking a model in the
  Admin UI that isn't in the cache now fails, by design.
- **Memory, measured** (CPU, torch 2.15 with sentence-transformers 5.5.1, 8 and 16 query–passage pairs of
  about 512 tokens):

  | | load | steady after use | peak | 16 pairs |
  |---|---|---|---|---|
  | ms-marco-MiniLM-L6-v2 | +27 MiB | +125 MiB | +258 MiB | 0.7 s |
  | bge-reranker-base (278M params, fp32) | +380 MiB | +763 MiB | +1103 MiB | 5.0 s |

  The weights are memory-mapped, so they become resident as they're used, and the peak includes the
  attention buffers. open-webui used about 1.6 GiB without a reranker. With bge it needs about 2.7 GiB at
  peak, plus room for uploads and a reindex. Hence **4g** (it was 2g by default, 2560m on ai-box). MiniLM
  alone would fit in 3g.
- **The expected state:** `ai_stack_expect_hybrid_search` (default `false`) and
  `ai_stack_expect_reranker_model` (default empty) are what the search check expects, as asserted keys
  behind `ai_stack_verify_search_settings`. They also seed a fresh database's env (`ENABLE_RAG_HYBRID_SEARCH`,
  `RAG_RERANKING_MODEL`). The preflight refuses a reranker that isn't one of `ai_stack_hf_models`. The admin sets
  the real settings in the Admin UI (Documents → Hybrid Search on, Reranking Model = the repo id, Top K
  Reranker 8). With a reranker, hybrid search is no longer the plain vector top-k that R10-1 found (that
  was cosine re-ranking only).

## Memory budget
`tasks/memory_budget.yml` (preflight) adds every container limit on the host, `ai_stack_mem_limits` plus
`ai_stack_mem_other_containers` (other roles' containers: SearXNG 384m), and the host's own share,
`ai_stack_mem_host_services`, and asserts the sum is at most `ansible_facts.memtotal_mb +
ai_stack_mem_overcommit_mb`. The host share is counted by owner: the `MemoryMax` caps of the systemd services
on ai-box (each role's `*_memory_max` default) plus an estimate for the uncapped daemons.

| | MiB |
|---|---|
| ollama-cpu | 5120 |
| open-webui (role default 4g since §12.30: room for a cross-encoder reranker) | 4096 |
| tika | 1024 |
| searxng | 384 |
| gpu-broker | 256 |
| ai-control.service (`MemoryMax`) | 256 |
| research-gate.service (`MemoryMax`) | 256 |
| finance-digest.service (`MemoryMax`) | 256 |
| skill-drafter.service (`MemoryMax`) | 256 |
| voice-profile.service (`MemoryMax`) | 256 |
| forge-api.service (`MemoryMax`, §12.25) | 256 |
| ai-backup.service (`MemoryMax`, nightly oneshot, §12.28) | 256 |
| mission-control.service (`MemoryMax`, §12.29) | 128 |
| Mission Control's 05:30 briefing oneshot (`MemoryMax`, §12.29; overlaps the server briefly) | 128 |
| host daemons (estimate): dockerd, containerd and shims, tailscaled, journald, CIFS client, sshd, systemd, some page cache | 768 |
| **total** (role defaults) | **13696** |
| ai-box RAM (since the 2026-10 upgrade) | 15357 (MemTotal) |

**It fits without overcommit** (§12.30): 13696 MiB needed against ai-box's 15357 MiB MemTotal, with
`ai_stack_mem_overcommit_mb: 0` and 1661 MiB to spare. The tests run this case, and the refusal with open-webui at
5758m (one MiB over). Limits are ceilings, not reservations, and the peaks rarely coincide (Ollama holds a
model only while it's used, Tika peaks only during an upload, the services sit far below their caps).
`ai_stack_mem_overcommit_mb` still defaults to **0**, so a host where the limits don't fit is refused until
that is a decision: lower a limit in `ai_stack_mem_limits`, or set `ai_stack_mem_overcommit_mb` (host_vars)
to accept the overcommit. (Before the RAM upgrade ai-box ran with 7305 MiB and an overcommit of 4968.)

## Networks
- **`ai`** (`ai_stack_network_name`, `ai_ai`) is pinned to `ai_stack_network_subnet` (`172.20.0.0/24`, the
  subnet Docker gave it from role docker's first pool). Role `finance_digest` can derive its broker range from
  that variable. The role creates it (`community.docker.docker_network`, `appends: true`) and compose uses it
  as `external`. Why not `ipam:` in compose: Compose stamps a config hash on the networks it creates and
  **removes and recreates** a network whose definition changes (`pkg/compose/reconcile.go`,
  `reconcileNetworks`), stopping every container on it, even when the subnet ends up the same. The module
  compares only the keys it's given, so the live network (same subnet) is left alone: no change, no
  recreate. Before it runs, `tasks/networks.yml` checks (read-only) that the live `ai_ai` has exactly that
  subnet and that no other network overlaps it. If not, the play stops and changes nothing.
- **`extract`** (`ai_stack_extract_network_name`, `ai_extract`): `internal: true`, pinned to `ai_stack_extract_subnet`
  (`172.23.255.240/28`, the far end of the same pool, where Docker's own /24 allocations, taken from the
  start of the pool, won't reach first). It is new with §12.18b, so compose creates it with that subnet.
  Changing the variable later recreates it (only tika and open-webui are on it). Overlaps are checked as for
  `ai`. `filter_plugins/ai_stack_net.py` does both checks.

## Skills (`/skills`, §12.19) and writing (`/writing`, §12.21): read-only AI Library shares
The AI Library's `30-skills` is read-only at `/skills` in open-webui, for the "Skills (read-only)" tool.
`40-writing` is read-only at `/writing`, for the "My writing voice (read-only)" tool (W3).

**One design for both.** `ai_stack_ro_shares` (`vars/main.yml`) lists them. Each entry has a name, the CIFS
source, the local view directory, the container target, the marker file and the mount unit:

| Share | Source | View (host) | Target | Marker | Mount unit |
|---|---|---|---|---|---|
| skills | `//<pc>/ai-library/30-skills` | `/var/lib/homelab-ai/skills` | `/skills` | `SKILLS-NOT-MOUNTED` | `var-lib-homelab\x2dai-skills.mount` |
| writing | `//<pc>/ai-library/40-writing` | `/var/lib/homelab-ai/writing` | `/writing` | `WRITING-NOT-MOUNTED` | `var-lib-homelab\x2dai-writing.mount` |

These are all driven by that list: the mount template (`templates/homelab-ai-share.mount.j2`), the compose
binds, `tasks/skills.yml`, `tasks/verify_skills.yml` and the reconcile service. Everything below, written
for `/skills`, applies to each share the same way. They share the CIFS options, the credentials, the
tailnet wait and **one** reconcile timer and service, which probe the PC once per pass and decide for each
mount. The service keeps its first name, `homelab-ai-skills-reconcile`, so the live unit isn't replaced.
The shared settings keep their `ai_stack_skills_*` names.
- **Its own read-only CIFS mount.** `var-lib-homelab\x2dai-skills.mount` mounts
  `//<gaming PC>/ai-library/30-skills` at `/var/lib/homelab-ai/skills`. The options are the library client's
  (`roles/ai_library_share`: `vers=3.1.1,seal,nounix,serverino,symlink=none,soft,echo_interval=15,_netdev`,
  `TimeoutSec=60`, its credentials file `/etc/ai-library/credentials`), plus
  `ro,nosuid,nodev,noexec,uid=0,gid=0,file_mode=0440,dir_mode=0550`. Ownership is root with no access for
  anyone else: open-webui reads as root, and no other local account (rgate, localai, …) gets the skills
  through this mount, just as the library itself is rgate-only (0660/0770). It needs nothing from the
  library mount (`/srv/ai-library`).
- **Why not a bind of the library mount (Integrator, 2026-10-27).** Ubuntu 26.04's util-linux 2.41.3 has no
  fd-based mount. On the classic mount(2) path, a bind is created writable and then remounted read-only,
  and that remount doesn't propagate: the copy in open-webui (root, `CAP_DAC_OVERRIDE`) would stay writable.
  A **new CIFS superblock** gets `MS_RDONLY` and nosuid/nodev/noexec when it's created, even via mount(2),
  so the copy that rslave carries into the container has them from the start.
- **open-webui never waits on it.** The container binds the **local** directory underneath
  (`create_host_path: false`, `read_only`, `propagation: rslave`). The CIFS mount appears on top when the PC
  is reachable, and rslave carries it into the running container (and its unmount back out).
- **No automount, on purpose.** Docker resolves a bind source when the container starts. If the view were
  an automount point, that lookup would fire it, and open-webui's start would wait on the gaming PC (up to
  the mount timeout) and fail while the PC is off.
- **The reconcile service follows the PC (Reviewer I, R5-1).** A soft CIFS mount whose server is gone stays
  mounted, and Docker stats the bind source at every container start, so a stale mount would block
  open-webui's (re)start. `homelab-ai-skills-reconcile.timer` (enabled; 30 s after boot or start, then every
  minute) runs `homelab-ai-skills-reconcile.service` (oneshot, root, `files/skills_reconcile.py`, network
  limited to the PC's address):
  - a TCP connect to `<pc>:445` with a 4 s timeout;
  - unreachable while the mount is active: `systemctl stop`. The unit has `LazyUnmount=yes` and
    `ForceUnmount=yes`, so the stop never hangs, and the unmount propagates into open-webui, which sees the
    marker again;
  - reachable while the mount is inactive: `systemctl start`;
  - otherwise nothing.

  `start`/`stop` run with `--no-block`, so systemd runs the mount jobs in parallel, each bounded by its
  own `TimeoutSec=60`, and a pass returns in seconds. A mount that then fails shows in its own unit and the
  journal. Each `systemctl` call is capped at 10 s. A call that hangs or can't run is logged for that
  mount, the other mounts are still handled, and the pass exits 1 (Reviewer I, R8-1).
  The script never stats, opens or lists the mount path, and derives the unit name from the path the way
  `systemd-escape` does. So for at most about a minute after the PC goes away, a container restart can
  still hit the stale mount.
- **While it isn't mounted**, `/skills` contains only `SKILLS-NOT-MOUNTED` (root 0444, in the local directory
  under the mount point), and open-webui runs normally. The tool should report "skills folder not available"
  when that file exists (W2, `apps/skills-tool`).
- **The play never touches the path or mounts the share (R5-2).** "Mounted" is `systemctl is-active --quiet`
  on the mount unit (systemd tracks the mount table; nothing stats the path). `mountpoint` is never used
  (it stats, and would hang on a stale soft mount), and the tests ban it in every task file. The play
  installs the script and the three units, removes the round-3 leftover
  `srv-ai\x2dlibrary.mount.wants/var-lib-homelab\x2dai-skills.mount`, and enables and starts the timer. It
  touches the mount itself only to **stop** it (lazily) when its unit changed while it was active; the
  next reconcile mounts it with the new options. The marker and the directory's mode are only touched
  while the mount is inactive.
- **Preflight:** `/etc/ai-library/credentials` must exist as a regular file, root 0600, and
  `/etc/systemd/system/ai-library-tailnet-wait.service` must exist (both written by `ai_library_share`'s
  client; this role only reads their metadata, never their content), or the play stops with "run
  playbooks/ai-library.yml first".
- **In-container check (`tasks/verify_skills.yml`):** reads `/proc/1/mountinfo` inside open-webui first, then
  the host's `systemctl is-active` on the mount unit. The **top** mount at `/skills` must never be `rw` and
  must always be `ro`. While the host mount is active, it must also have `nosuid,nodev,noexec`, however
  many mounts are stacked there. There is one if the timer mounted before Docker created the container
  (Docker's bind captured the CIFS mount itself, as seen live on 2026-10-27), and two if it was propagated
  in afterwards. If the two reads disagree (the reconcile timer acted in between), both are read again
  once before the play fails and says to stop the mount and the timer.
- **What open-webui can see.** Its own `/proc/self/mountinfo` shows this mount's source
  (`//<pc tailnet IP>/ai-library/30-skills`) and the CIFS options, including the SMB **username** (from the
  credentials file). No password: that stays in `/etc/ai-library/credentials` (root 0600), which is not
  mounted into the container. The PC's tailnet IP and the share name are not secrets on this tailnet.
- **Soft mount.** With `soft`, a read while the PC is unreachable returns an I/O error after the retry
  period instead of hanging; the reconcile unmounts it within a minute.
- **Integrator:**
  1. With the PC up: within a minute `systemctl is-active 'var-lib-homelab\x2dai-skills.mount'`.
     `docker exec open-webui ls /skills` lists the skills. `docker exec open-webui touch /skills/x` fails
     (read-only file system). `docker exec open-webui grep ' /skills ' /proc/1/mountinfo` shows the cifs
     mount on top with `ro,nosuid,nodev,noexec`.
  2. **PC off, mount previously up (R5-1):** within one timer period (about a minute),
     `journalctl -u homelab-ai-skills-reconcile` shows `unreachable … -> stop`. Then
     `docker restart open-webui` succeeds promptly, and `docker exec open-webui ls /skills` shows only
     `SKILLS-NOT-MOUNTED`.
  3. PC back on: within a minute the skills reappear in the running container, with no restart.
  4. The play's verify step passes in both states. A second run is `changed=0` in both.
  5. **Writing (§12.21):** the same 1–4 for `/writing`. `docker exec open-webui ls /writing` shows
     `samples/` etc. while the PC is up, and `WRITING-NOT-MOUNTED` while it's off.
     `journalctl -u homelab-ai-skills-reconcile` shows one line per mount on each pass.

## Variables
| Variable | Default | Meaning |
|---|---|---|
| `ai_cpu_model` | **unset** | CPU model, pulled into ollama-cpu when set (the operator's choice) |
| `ai_gpu_model` | **unset** | GPU model on the gaming PC; used here only for the alias |
| `ai_stack_alias_name` | `assistant` | The broker alias, rendered only when both models are set |
| `ai_webui_enable_signup` | `true` | Open WebUI `ENABLE_SIGNUP` (see First login) |
| `ai_embed_model` | **unset** | Embedding model (§12.22): pulled into ollama-cpu, broker alias `ai_stack_embed_alias` |
| `ai_stack_embed_alias` | `embed` | Broker alias for the embedding model (must differ from `ai_stack_alias_name`) |
| `ai_stack_rag_top_k` / `ai_stack_rag_top_k_reranker` / `ai_stack_rag_hybrid_search` | `8` / `8` / `false` | Retrieval (fresh-database env; top_k is the minimum the search check expects; hybrid stays off, see Search settings) |
| `ai_stack_rag_embedding_batch_size` | `16` | Embedding batch size (fresh-database env) |
| `ai_stack_rag_embedding_concurrent_requests` | `2` | Embedding Concurrent Requests (fresh-database env; the admin's click) |
| `ai_stack_embed_query_prefix` / `ai_stack_embed_content_prefix` | `search_query: ` / `search_document: ` | nomic's prefixes (env, every start) |
| `ai_stack_verify_search_settings` | `false` | Fail the play on wrong search settings (true once the admin has saved them) |
| `ai_stack_title_prompt` | see Search settings | Title prompt (fresh-database env, and the template the admin pastes) |
| `ai_stack_hf_models` | MiniLM + bge rerankers, pinned | Hugging Face models pre-seeded into open-webui's cache (§12.30) |
| `ai_stack_expect_hybrid_search` / `ai_stack_expect_reranker_model` | `false` / `""` | The hybrid and reranker state the search check expects (§12.30) |
| `ai_stack_mem_limits` | 5g / 256m / 2g / 1g | Per-container memory limits (ollama-cpu, broker, open-webui, tika) |
| `ai_stack_mem_other_containers` | `{searxng: 384m}` | Other roles' containers on this host, for the budget |
| `ai_stack_mem_host_services` | 7 × 256m services (ai-control, research-gate, finance-digest, skill-drafter, voice-profile, forge-api, ai-backup) + 2 × 128m (Mission Control and its briefing) + 768m daemons = 2816m | The host's own memory, for the budget |
| `ai_stack_network_name` / `ai_stack_network_subnet` | `ai_ai` / `172.20.0.0/24` | The pinned main network (role-owned) |
| `ai_stack_ollama_cpu_ip` / `ai_stack_broker_ip` / `ai_stack_webui_ip` | `172.20.0.2` / `.3` / `.4` | Pinned addresses on `ai` (§12.23); `ai_stack_broker_ip` is for the other roles' egress filters |
| `ai_stack_broker_probe_model` | `homelab-lockdown-probe-does-not-exist:none` | Model name for the `/api/push` refusal check |
| `ai_stack_extract_network_name` / `ai_stack_extract_subnet` | `ai_extract` / `172.23.255.240/28` | Tika's internal network |
| `ai_stack_writing_cifs_source` / `ai_stack_writing_view` / `ai_stack_writing_target` / `ai_stack_writing_unavailable_marker` | `//<pc>/ai-library/40-writing` / `/var/lib/homelab-ai/writing` / `/writing` / `WRITING-NOT-MOUNTED` | Writing share (§12.21) |
| `ai_stack_skills_cifs_source` / `ai_stack_skills_view` / `ai_stack_skills_target` | `//<pc>/ai-library/30-skills` / `/var/lib/homelab-ai/skills` / `/skills` | Skills: share subfolder, local view, path in open-webui |
| `ai_stack_skills_mount_options` | see Skills | The read-only CIFS options |
| `ai_stack_skills_reconcile_first` / `ai_stack_skills_reconcile_every` | `30s` / `1min` | Reconcile timer: first run, then period |
| `ai_stack_skills_reconcile_port` / `ai_stack_skills_reconcile_timeout` | `445` / `4` | The reachability probe (s) |
| `ai_stack_mem_overcommit_mb` | 0 | Accepted overcommit (MiB) beyond RAM; see Memory budget |
| `ai_webui_rag_file_max_size_mb` | 25 | Open WebUI `RAG_FILE_MAX_SIZE` |
| `ai_webui_extraction_media_types` | png, jpeg, tiff, bmp, gif | Open WebUI `CONTENT_EXTRACTION_SUPPORTED_MEDIA_MIME_TYPES` |
| `ai_stack_tika_pdf_ocr_strategy` | `auto` | PDFParser `ocrStrategy` (`auto`, `ocr_and_text_extraction`, `ocr_only`, `no_ocr`) |
| `ai_stack_tika_ocr_language` | `eng` | Tesseract language(s) (`eng+spa`; the image has eng ita fra spa deu jpn) |
| `ai_stack_tika_ocr_timeout_seconds` | 120 | Tesseract timeout per image |
| `ai_stack_tika_task_timeout_ms` | 600000 | tika-server's per-document timeout |
| `ai_stack_tika_worker_heap` | 512m | Heap of Tika's forked worker JVM |
| `ai_stack_tika_tmpfs_size` | 128m | Tika's `/tmp` tmpfs |
| `ai_stack_ollama_env` | keep-alive 10m, 1 model, 1 request | ollama-cpu environment |
| `ai_stack_compose_wait_timeout` | 600 | `compose up --wait` timeout (s) |
| `ai_stack_tailnet_wait_seconds` | 300 | Boot helper's wait for the tailnet address (s) |
| `ai_stack_lock_file` | `/run/homelab-ai-stack.lock` | Stack lock (boot helper and playbook) |
| `ai_stack_lock_wait` | 300 | Longest wait for the stack lock (s) |
| `ai_stack_lock_hold_max` | lock wait + compose wait + 120 (1020) | Longest a playbook run's lock holder lives (s) |

From host_vars: `tailnet_ip`, `gaming_pc_tailnet_ip`. From versions.yml: `images.ollama`,
`images.open_webui`, `images.python_base`, `images.tika`, `local_images.gpu_broker`.

## Tests
```sh
.venv/bin/ansible-playbook -i localhost, -c local roles/ai_stack/tests/test_render.yml </dev/null 2>&1 | cat
python3 -m unittest discover -s roles/ai_stack/tests -p 'test_*.py' -v
```
The unittests (stdlib only) cover `files/webui_extraction_config.py` (run as the role runs it, on stdin,
against throwaway sqlite databases: correct, stale first-start seed, missing rows, admin-panel variants, the
exact engine match, persistence off; never writes (hashes and mtimes unchanged, and a spy on
`sqlite3.connect` for `mode=ro` and a short timeout); a WAL database with an open writer; an exclusive lock,
no table, bad JSON, not a database, postgres, no database → exit 2), `files/ocr_check.py` against a fake Tika
(the three requests, `/tika/text` with **no** Content-Type, a missing word, HTTP 500, a non-JSON reply,
Tika unreachable; the real Pillow rendering where Pillow is installed) and `filter_plugins/ai_stack_net.py`.
The render test also checks statically that `preflight.yml` imports `memory_budget.yml`, and that `main.yml`
imports `networks.yml` (before the compose file), `skills.yml` (before the stack) and `verify_extraction.yml`
(last), with no `when:`. It checks that the `docker_network` task has exactly `name`, `driver: bridge`, one
`ipam_config` subnet, `appends: true` and `state: present` (no `force`, labels or other keys, no task-level
switches), after both read-only checks. It renders the skills mount, reconcile service and timer
(exact options, `LazyUnmount`/`ForceUnmount`, no `[Install]` on the mount, no library-mount reference, the
service's command line and IP filter, the timer's target and schedule, the names against
`systemd-escape`, `systemd-analyze verify` where available). It checks that no task file uses `mountpoint`,
that `skills.yml` and `verify_skills.yml` probe only with `systemctl is-active --quiet`, that the play starts
only the timer, stops the mount only while it is active, and removes the round-3 link.
`tests/test_skills_reconcile.py` covers the reconcile logic with an injected probe and a `systemctl` stub
(PC up or down × mount active or not, a failed call, no access to the mount path, the timeout bound), the
real TCP probe, and the unit name against `systemd-escape`. It runs the credentials preflight against scratch files (good, wrong owner, 0644,
missing, a directory, no wait unit), and the in-container check against sample mount tables.
Renders `compose.yaml` and checks its shape (services, tailnet-only ports, labels, limits, Open WebUI
settings incl. the extraction env, the config-hash labels; `tika` on the internal network only, no
publish, hardening, tmpfs, its read-only config mount); renders `tika-config.xml`, parses it and reads the
OCR settings back, and checks its hash label and the Tika 3.x API pin; checks that the verify step expects
exactly what compose sets; runs the role's own `tasks/memory_budget.yml` (`tests/budget_case.yml`) against a
7.1 GiB host (refused), the same with the overcommit accepted, and a 16 GiB host; renders the broker config for every model combination and validates each
with the broker's own `python -m gpu_broker --check` (from `apps/gpu-broker/.venv`, see that README), and
checks that `--check` rejects a bad file; runs the boot helper (`tests/boot_case.yml`, one case each)
against fake `ip` and `docker` commands: up without a binding, restarting without one, the wrong address
bound, a refused start (all recreated, in one compose call); everything bound, stopped on purpose, removed
(nothing happens); the compose call failing, and a recreate that still doesn't bind (exit 1); and no
address (bounded wait, exit 1, Docker never called). `--heal` cases: recreate, a healthy host (no
`RECREATE` line, so `changed=0`), compose failing, `--dry-run` (reports, calls no compose), and no address
(fails at once, no wait). Every recreate must run with the stack lock held (the fake `docker compose`
checks it with `flock --nonblock`); with the lock held elsewhere the helper times out (1 s in the test) and
calls no Docker command, while a dry run still works. It also checks that the helper's service list matches
the compose file.
No Docker or root needed.
§12.22 tests:
- **Render:** the search env with and without `ai_embed_model`; the title template (Open WebUI's
  placeholder kept literally, JSON asked for, no example value or placeholder text); the embed alias alone
  and next to the chat alias, each validated by the broker's own `--check`; the CPU pull list per case.
- **Statically:** the search verify, models and preflight checks.
- **The search check** against sample checker output, with the gate off (report only) and on (good,
  Top K Reranker trap, unreadable, a crash, a report-only key wrong).
- **Unittests** (`SearchCheckerTest`) run the checker against throwaway databases: saved settings, the
  live seed, the min(top_k, top_k_reranker) trap, the reranker ignored without hybrid, admin-panel variants,
  exact names, a fresh database (env), Open WebUI's defaults, read-only and locked, and the extraction keys
  unchanged.
