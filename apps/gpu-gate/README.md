# gpu-gate

Runs on the **gaming PC** and answers one question for ai-box's gpu-broker: *may the LLM use this
GPU right now?* Spec: `docs/CONTRACTS.md` §12.4, amended by §12.7 and §12.9. Python 3 standard
library only.

## What it does

Every `interval` (5 s) it takes one sample:

| Value | Source |
|---|---|
| `gpu_busy` | `gpu_busy_percent` of the first `/sys/class/drm/cardN/device` that has it (`card` overrides) |
| `ollama_share` | % of the interval Ollama spent on the `drm-engine-gfx` + `drm-engine-compute` counters (`ollama_engines`) in `/proc/<pid>/fdinfo/*`, for Ollama's processes (see below). Each DRM client counts once (keyed by `drm-pdev` + `drm-client-id`); fds of other GPUs and non-DRM fds are skipped; a client's first sighting and a counter that went backwards count as 0 |
| `game_util` | `clamp(gpu_busy - ollama_share, 0, 100)` |
| `avg30` | mean `game_util` over the trailing `window` (1800 s), over the samples present while it fills |
| `game` | first rule with a hit, in this order: config `games` allow-list → Steam (`reaper SteamLaunch AppId=<n≠0> --` in cmdline) → Wine/Proton (`wine*`, `*.exe`) → `gamescope*` → `java*` whose cmdline mentions `minecraft` or Prism Launcher. Processes matching `ignore` never count |

**Which processes are Ollama's** (`ollama_match_uid`, default true): every process whose real
uid (the first field of the `Uid:` line of the world-readable `/proc/<pid>/status`, not the effective uid) equals the gate's own uid. The
gate runs as `ollama`, so that is Ollama's uid. Ollama 0.40.11's GPU worker is `llama-server`
(started by `ollama serve`), which a name match on `ollama` missed: `ollama_share` stayed 0 at 96 %
busy, live. The gate's own pid never counts. As a fallback, a process whose comm or argv0 contains
a name in `ollama_match` (default `ollama`, `llama-server`) also counts, whatever its uid. In
production that fallback can only add processes of the gate's own uid: another user's fdinfo
isn't readable without privileges, so a desktop user's own `llama-server` reads as 0.

**Uid guard.** Uid matching assumes the gate's uid belongs to Ollama alone. If the gate runs as
root (uid 0) or as a login user (uid ≥ `UID_MIN` from `/etc/login.defs`, where the last
`UID_MIN` line wins as in shadow; default 1000), it turns
uid matching off for that run, because the desktop's processes, the game included, would count as
Ollama's. It prints one warning to stderr naming the uid, and `/status` and `--once` show
`"uid_match": false` with a `uid_match_reason`. The `ollama_match` names still apply. A system
uid such as `ollama` (below `UID_MIN`) keeps uid matching.

Game detection reads only world-readable `comm`, `cmdline` and `stat`, never other users' `exe`
links or `environ`. A Steam game is named after the first non-plumbing process under its `reaper`
(found through `stat` ppid), e.g. `Spider-Man.exe`, or `steam app <n>` if there is none. Within any
rule, Steam/Wine plumbing (`srt-bwrap`, `wineserver`, `services.exe`, ...) only names the game when
nothing better matched.

Time is CLOCK_BOOTTIME, so a suspended PC's samples age out of the window instead of resuming
as if no time had passed.

**Rule (Noah):** `available = avg30 < threshold (50) OR game is None`.

| `reason` | `available` | Meaning |
|---|---|---|
| `no_game` | true | no game detected (checked first) |
| `below_threshold` | true | a game, but `avg30 < threshold` |
| `game_busy` | false | a game and `avg30 >= threshold` |
| `game_no_samples` | false | a game but no GPU sample yet (card unreadable): fail toward the game |

On every **available → unavailable** transition (including the first sample after start) it calls
Ollama `GET /api/ps`, then `POST /api/generate {"model": m, "keep_alive": 0}` per loaded model, in a
background thread. Errors are logged, never fatal. Staying unavailable does not call it again.

A sample that throws is logged and skipped; the loop carries on. The last good answer is kept but
goes stale (see the API).

## API (port 11500, binds from config)

- `GET /status` → `200 {"available", "reason", "avg30", "samples", "game", "gpu_busy", "ollama_share", "ts",
  "uid_match", "uid_match_reason"}`. `uid_match` is false (with a reason string) when uid matching
  is off, by the uid guard or by config; otherwise `uid_match_reason` is `null`.
  `avg30` and `ollama_share` are rounded to 0.1; `avg30` and `gpu_busy` may be `null`; `ts` is the
  sample's Unix time in seconds (float). **503** before the first good sample
  (`{"error": "no sample yet"}`), or when the last good sample is older than 3 intervals
  (`{"error": "stale", "ts": ...}`).
- `GET /healthz` → `200 {"ok": true}` if the last good sample is at most 3 intervals old, else
  `503 {"ok": false}`. That's the same rule as the stale check on `/status`.

No auth; exposure is limited by firewalld (role `ollama_rocm`, policy `homelab-ai-only`: only ai-box reaches 11500). At most 16 connections are handled at
once, and more are closed on accept. Each connection has a 5 s per-read socket timeout and an
overall 10 s deadline from accept through response (§12.9). When the deadline passes, the socket
is shut down, so neither idle nor slow-trickling clients can hold the handler slots. Binds use `IP_FREEBIND`, so the tailnet address can be bound before
tailscaled has assigned it at boot.

## Run

```sh
python3 -m gpu_gate --config /etc/gpu-gate/config.json   # from this directory
./gpu-gate --config config.example.json                 # launcher; works under python3 -I
./gpu-gate --once | jq .                                 # 2 samples one interval apart, JSON on stdout, exit
./gpu-gate --once --samples 4                            # more samples (no HTTP, no unload either way)
```

Config: `config.example.json` lists every key with its default (a key missing from the file takes
its default; unknown keys are rejected). Bounds: `interval` 0.5–3600 s, `window` ≥ `interval`,
`threshold` 1–100, `ollama_timeout` 0.5–60 s, `log_level` one of DEBUG/INFO/WARNING/ERROR/CRITICAL.
`sysfs_root` / `proc_root` exist for tests. Logs go to stderr (journald keeps both streams);
`--once` prints only the status JSON on stdout. It takes 2 samples by default because a DRM
client's first sighting counts 0, so one sample always reports `ollama_share: 0.0`. Example unit:
`gpu-gate.service` (a reference for the role; not installed by this app).

## Test

```sh
python3 -m unittest -v        # from apps/gpu-gate
```

The tests use fake `/sys` and `/proc` trees in a tmp dir, a fake clock and a stub Ollama HTTP
server. Fixture formats are copied from the real RX 7600 XT and a real Proton process tree. In the
fixtures, `exe` and `environ` are unreadable. A test spies on `open`, `io.open` (and so pathlib),
`io.FileIO`, `os.open` and `os.readlink`, and asserts detection never opens them.

## Privileges (none)

Decision (§12.9): **`User=ollama`, no capabilities** (`CapabilityBoundingSet=` empty).

| Read | Readable because |
|---|---|
| `gpu_busy_percent`, every process's `comm`, `cmdline`, `stat` | world-readable |
| every process's `/proc/<pid>/status` (real uid, Ollama meter only) | world-readable |
| Ollama's `/proc/<pid>/fdinfo/*` | same uid as Ollama (the kernel's ptrace-read check passes for the same uid) |
| other users' `exe`, `environ`, `fdinfo` | **not read** |

CAP_SYS_PTRACE was rejected because it also opens every process's `/proc/<pid>/root|cwd`, which
walks past ProtectSystem, ProtectHome and PrivateTmp.

Sharing Ollama's uid means sharing ptrace rights, so the unit filters `@debug` (ptrace,
pidfd_getfd, perf_event_open), `process_vm_readv/writev`, `process_madvise` and `kcmp`.
`ProtectSystem=strict` keeps Ollama's model store read-only to the gate.

`systemd-analyze security --offline=yes gpu-gate.service` reports exposure 1.7 (OK).

This depends on Ollama running natively as the `ollama` user. In a container, or as another uid,
`ollama_share` would read 0.

## ROCm vs Vulkan (resolved 2026-10-26, §12.9)

`ollama_share` only sees work the kernel accounts per DRM client. Confirmed live: under **ROCm**,
the worker's fdinfo has `drm-memory-vram` but no `drm-engine-*` lines (its compute goes through
`/dev/kfd` user queues), so `ollama_share` is 0 whatever the load. Under **Vulkan**
(`OLLAMA_VULKAN=1`), `drm-engine-compute` advances in step with `gpu_busy`; hence the default
`ollama_engines: ["gfx", "compute"]`. Noah's decision: Ollama runs on Vulkan. To check the
gate, run `./gpu-gate --once` (2 samples, one interval apart) as the `ollama` user during
inference: `ollama_share` should track `gpu_busy`.
