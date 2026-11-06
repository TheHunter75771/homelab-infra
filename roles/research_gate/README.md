# research_gate

The Skill Forge research gate (module R, `apps/research-gate/`) as a hardened host service on ai-box
(docs/CONTRACTS.md §12.16.3, deploy §12.16.4, module H4). Hosts: `ai_servers`, after role `searxng`
(`playbooks/research.yml`).

| | |
|---|---|
| Account | `rgate`: system, primary group `rgate`, `/usr/sbin/nologin`, home `/nonexistent` (not created), no comment, no `groups`: exactly what role ai_library_share sets (§12.17.3), so neither role undoes the other; a read-only check fails the run if `rgate` is in any other group (never docker) |
| Code | `/opt/research-gate/`, root-owned: R's launcher `research-gate` (0755), `research_gate/`, and `forge/` + `library/` from `apps/skill-library` (0755 dirs, 0644 `*.py`) |
| Config | `/etc/research-gate/config.json` (root:rgate 0640, no secrets), dir root:rgate 0750 |
| Unit | `research-gate.service`: `python3 -I -B /opt/research-gate/research-gate --config /etc/research-gate/config.json serve` |
| Switch | `research_gate_enabled: false` (default): installed, **neither enabled nor started** (a running one is stopped and disabled) |

## What a run does
1. **Preflight** (before anything is written):
   - the switch is a boolean;
   - `research_gate_library_root` and `research_gate_library_mount` are plain absolute paths (`A-Z a-z 0-9 . _ - /`,
     no `//`, trailing `/`, `.` or `..`; they go into the unit unescaped) outside `/home /root /run /tmp /var /opt /etc
     /usr ...` (ProtectHome, PrivateTmp and ProtectSystem would hide or lock them), and the root is the mount or below it;
   - R's settings within R's own ranges (`research_gate/config.py`): poll 5-86400 s, results 1-50, max_bytes
     1 KiB-100 MiB, timeout 1-300 s, **max_redirects 0-3**, User-Agent 1-200 printable ASCII; the library cap
     (`research_gate_library_cap`, default `500GB`) has no spaces and parses with the library's own `parse_size`;
   - SearXNG's address (`searxng_ipv4` / `searxng_container_port` from the inventory, else role searxng's defaults via
     `include_vars`, never copied) is an IPv4 literal; the resolver stub is a 127.x address; the IP filter is sound:
     every allow entry is a single host inside a deny entry, no deny entry is a `/0`, and the deny list covers the
     contract's minimum (loopback v4/v6, RFC 1918, `100.64.0.0/10`, link-local v4/v6, ULA);
   - the sources exist on the controller: the launcher, `research_gate/{__init__,__main__,config}.py`, `forge/`, `library/`.
   - **Switched on only:** the library is really mounted (§12.11 mount safety): the root is a real directory (not a
     symlink); `findmnt --target <root>` names exactly `research_gate_library_mount`, and that isn't `/` (an unmounted
     share leaves an empty directory on the parent filesystem, and findmnt then names the parent); the sentinel
     `<root>/.ai-library-sentinel` is a regular file (not a link); `<root>/35-forge` is a real directory (an
     initialized library). Any failure stops the run with the gate untouched.
2. **Base:** Python 3.12+ (forge and library need it); the `rgate` group and user (the attributes above); `id -Gn rgate` must be `rgate` only.
3. **Code:** each directory checked to be absent or a real directory before the first write and again before each
   find (never write or prune through a symlink); only `*.py` of the three packages, skipping `__pycache__`, `test`,
   `tests`, `node_modules` anywhere; the launcher. Everything else under `/opt/research-gate` is pruned (R's `tests/`,
   README and example config are never deployed). A change requests a restart (only acted on when enabled).
4. **Config:** exactly R's keys: `library_root`, `searxng_url` (`http://172.29.16.2:8080`), `poll_interval`,
   `results_per_query`, `max_bytes`, `fetch_timeout`, `max_redirects`, `user_agent`, and `ipv4_only: true` (fixed:
   the unit has no AF_INET6, so R must use only IPv4 answers). Validated before install with
   **R's own loader from the installed code** (`research_gate.config.load`: unknown keys, ranges, URL and User-Agent
   rules), plus an exact key-set check and `ipv4_only` exactly true.
5. **Unit**, daemon-reload, then the switch: enabled + started, or stopped + disabled. When enabled (not in check
   mode), a read-only check: the unit becomes active and is still active with the same `NRestarts` ten seconds later.

A second run is `changed=0`.

## The unit
- `User=rgate`, `NoNewPrivileges`, `ProtectSystem=strict`, `ReadWritePaths=<library root>` (the only writable
  path), `ReadOnlyPaths=/run`, `ProtectHome=yes`, `PrivateTmp=yes` (R stages fetched files there), no capabilities
  (`CapabilityBoundingSet=` and `AmbientCapabilities=` empty), `RestrictAddressFamilies=AF_INET AF_UNIX` (**no IPv6**,
  Reviewer E: ai-box has global IPv6 on the LAN and the gate needs none),
  `@system-service` minus `@privileged`, `PrivateDevices`, `PrivateIPC`, the Protect* set, `ProtectProc=invisible`
  (**no `ProcSubset=pid`**: it hides `/proc/self/net`, which R reads to refuse the host's own networks, so every fetch
  would be refused; Reviewer E), `RestrictNamespaces`, `MemoryDenyWriteExecute`, `MemoryMax=256M`, `TasksMax=32`,
  `UMask=0027`, `Restart=on-failure` (30 s).
- `Environment=AI_LIBRARY_CAP=500GB` (R and the library refuse to file past the cap).
- **Mount safety:** `RequiresMountsFor=<library root>`, `AssertPathExists=<root>/.ai-library-sentinel`,
  `AssertPathIsDirectory=<root>/35-forge`: an empty mount point fails the start, visibly.
- **IP filter (defence in depth behind R's own SSRF rules):**
  - `IPAddressDeny=` `127.0.0.0/8 ::1/128 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16 fe80::/10
    fc00::/7` (the tailnet's IPv6 range is inside ULA) plus `0.0.0.0/8 192.0.0.0/24 198.18.0.0/15 224.0.0.0/4
    240.0.0.0/4 ::/128 ::ffff:0:0/96 64:ff9b::/96 64:ff9b:1::/48 ff00::/8`;
  - `IPAddressAllow=172.29.16.2/32 127.0.0.53/32`: SearXNG's pinned container address, and systemd-resolved's stub
    (every fetch's name is resolved through glibc → 127.0.0.53:53; without it no fetch could resolve);
  - everything else, the public internet, stays allowed.
  - **Why the container address and not `127.0.0.1:8888`:** systemd filters the destination of outgoing packets and
    the *source of incoming* ones. Through the loopback publish the request leaves already DNATed to the container
    (allowed), but the reply reaches the socket with source 127.0.0.1 again (conntrack undoes the DNAT before
    delivery), and loopback is denied. Direct to `172.29.16.2:8080` there is no NAT, so one /32 covers both
    directions. See role searxng's README for the host proof.

## Inputs
- `research_gate_enabled` (default false). Set it true only after Noah's AI Library share is mounted on ai-box (Phase
  4.5) and carries `.ai-library-sentinel` at the library root.
- `research_gate_library_root` (default `/srv/ai-library`) and `research_gate_library_mount` (default: the root).
- Optional: `research_gate_library_cap`, R's settings (`research_gate_poll_interval`, ...), `research_gate_dns_stub`.

## Host checks for the Integrator
Switched off (the default):
- `systemctl is-enabled research-gate` → `disabled`, `is-active` → `inactive`; `systemd-analyze verify
  /etc/systemd/system/research-gate.service` is clean; `systemd-analyze security research-gate` for the record.
- `id rgate` shows only its own group; `ls -la /opt/research-gate` shows `research-gate`, `research_gate`, `forge`,
  `library`, all root-owned.
- `sudo -u rgate python3 -I -B /opt/research-gate/research-gate --config /etc/research-gate/config.json status`
  fails cleanly ("not an initialized AI library") while the share isn't mounted (exit 2, no traceback in the output).

Switched on, against a **scratch library** (as §12.16.4 says): mount a scratch filesystem (e.g. a tmpfs or a loop
image) at a test path, `python3 -m library --root <path> init`, `touch <path>/.ai-library-sentinel`, set
`research_gate_library_root`/`_mount` to it and `research_gate_enabled: true`, converge, then:
- an approved query (ticked in `35-forge/<id>/searches.md`) appears in `sources.md` unticked within one poll;
- a ticked URL is filed under `10-sources/<topic>/` with a sidecar carrying `source:` and `forge:`;
- an unticked URL is never fetched (`gate-log.md` has no line for it);
- a ticked URL whose name resolves to a private address (e.g. a name you control pointing at 192.168.x.x, or
  `http://localhost/`) is refused and logged as `refuse`;
- the IP filter: `systemctl show -p IPAddressDeny -p IPAddressAllow research-gate` lists the ranges above; and as a
  live proof, `systemd-run -P -p IPAddressDeny="127.0.0.0/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10"
  -p IPAddressAllow="172.29.16.2/32 127.0.0.53/32" curl -m5 -s -o /dev/null -w '%{http_code}\n' https://example.org/`
  prints 200 (DNS through the stub and the public internet work), while `.../http://<ai-box lan ip>/` and
  `http://127.0.0.1:8888/healthz` time out and `http://172.29.16.2:8080/healthz` prints 200;
- `journalctl -u research-gate` shows no AVC/seccomp/`EPERM` errors (if `MemoryDenyWriteExecute`
  breaks the start, drop it and record that here);
- **`/proc/self/net` is readable in the sandbox** (R refuses the host's own networks from it):
  `sudo systemd-run -P --uid=rgate -p ProtectProc=invisible -p ProtectSystem=strict -p PrivateTmp=yes cat /proc/self/net/if_inet6 /proc/self/net/fib_trie | head`
  must print both tables (not "No such file"). Under the real unit, `journalctl -u research-gate` must show no fetch refused
  as "on a local network" for a public URL;
- **IPv4 only:** a ticked URL whose name has both A and AAAA records is fetched over IPv4. R connects to the first
  address `getaddrinfo` returns; glibc ranks destinations it can't open a socket for last, so IPv4 should come first,
  but check one dual-stack site (e.g. a Wikipedia page) is filed, not logged as `error: ... Address family not supported`;
- the library share's mount options let `rgate` write (CIFS maps file owners at mount time: `uid=rgate` or a group
  rgate is in); a write failure shows in `gate-log.md` as `error:` lines;
- then switch it off again and converge: stopped and disabled. Second run `changed=0` in both states.

## Tests
```sh
roles/research_gate/tests/run.sh   # from anywhere; uses the repo's .venv; no root; the scratch dir must not be on /
```
`tests/test_research_gate.yml` first checks that this role's `rgate` user/group tasks set exactly the agreed attributes, the same as
role ai_library_share's client tasks, and that the sentinel is `<library root>/.ai-library-sentinel`. Then it runs the role's own task files into a scratch dir with stub handlers that count
restart requests, and covers:
- **preflight:** passes switched off with no library anywhere; passes switched on against a scratch library on the
  scratch dir's real mount; and 33 refusals: a non-boolean switch; a library under /home or /tmp, relative, with a
  trailing slash, `..` or a space, or `/`; a root outside its mount, or a mount that is only a name prefix; a
  User-Agent with a line break; 0 results; 4 redirects; a cap with a space or that the library can't parse; a SearXNG
  address outside every deny range or not IPv4; deny lists with `0.0.0.0/0`, without 172.16.0.0/12, 100.64.0.0/10 or
  ::1, or with a non-strict entry; a resolver stub off loopback; R's package or the launcher missing; and switched on:
  the root missing or a symlink, the root an empty mount point (not on the declared mount), the sentinel missing, a
  symlink or a directory, 35-forge missing or a symlink;
- **install:** from scratch copies of R (launcher, package, its tests/README/example config, a planted subpackage) and
  of forge/library with junk planted everywhere (`__pycache__`, `test/`, `tests/`, `node_modules/`, README, `.txt`),
  run twice with strays planted in between (top level, inside packages, a cache dir, R's example config and tests),
  then a third run that requests no restart; the tree must equal exactly the sources' `*.py` (plus the 0755
  launcher); 4 symlink cases (app dir, `research_gate/`, `forge/`, `research_gate/sub/`) refused by the guard with
  the victim untouched;
- **config:** exactly R's keys and values as integers, 0640 in a 0750 dir, no restart on a re-run; R's loader (the
  deploy-time validator) refuses an extra key, a string number, a `file://` SearXNG URL, a relative root, 4
  redirects, a User-Agent with a line break, and `ipv4_only` false, a string or missing, and accepts the rendered file;
- **the unit:** rendered with the real paths, `systemd-analyze verify` with any warning failing, every setting above
  grepped, the deny list equal to the role's and a superset of the contract's minimum, exactly
  `IPAddressAllow=172.29.16.2/32 127.0.0.53/32` (no `localhost`, `any` or `127.0.0.1`), and the allow line following a
  different `searxng_ipv4`.

The role has no secret, so there is no canary check here (role searxng has one).
