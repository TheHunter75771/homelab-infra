# finance_digest

The daily, read-only Money Hub digest (module D, `apps/finance-digest/`) as a hardened oneshot service plus
timer on ai-box (docs/CONTRACTS.md §12.18.2, deploy §12.18.3, module H5). Hosts: `ai_servers`, after the AI
Library share is mounted (`playbooks/ai-library.yml`) and the AI stack (the broker) is up.

| | |
|---|---|
| Account | `fdigest`: system, primary group `fdigest`, **supplementary group `rgate` only** (`groups: [rgate]`, `append: false`), `/usr/sbin/nologin`, home `/nonexistent` (not created). A read-only check fails the run unless `id -Gn fdigest` is exactly `fdigest rgate` (never docker). The `rgate` group belongs to roles ai_library_share / research_gate: preflight refuses if it is missing, this role never creates it |
| Code | `/opt/finance-digest/`, root-owned: D's launcher `finance-digest` (0755), `finance_digest/` and `library/` from `apps/skill-library` (0755 dirs, 0644 `*.py`) |
| Config | `/etc/finance-digest/config.json` (root:fdigest 0640, no secrets), dir root:fdigest 0750 |
| Notion | `/etc/finance-digest/notion.json` (root:fdigest 0640, `{"token", "page_id"}`) only when both `finance_digest_notion_token` and `finance_digest_notion_page_id` are set; removed when both are unset |
| Unit | `finance-digest.service` (oneshot, static): `python3 -I -B /opt/finance-digest/finance-digest --config /etc/finance-digest/config.json run` |
| Timer | `finance-digest.timer`: `OnCalendar=*-*-* 09:30:00 America/New_York`, `Persistent=true`, `RandomizedDelaySec=120`; enabled and started |

## What a run does
1. **Preflight** (before anything is written):
   - `moneyhub_url` is required and must be `http(s)://<IPv4 literal>[:port][/]` inside `100.64.0.0/10` (the
     tailnet): no hostname (the IP filter pins one address), no user info, path, query or fragment;
   - `tailnet_ip` is ai-box's tailnet IPv4; the broker port, `ai_summary` (a boolean), the time zone (it goes into
     `OnCalendar`), the resolver stub (a 127.x address), `finance_digest_config_extra` (a mapping that can't
     override the URLs, the library root or the Notion file);
   - Notion: both values or neither; the token 1-256 printable ASCII without spaces, the page id 32 hex (D's rules).
     This check is `no_log`;
   - the IP filter is sound (see below), and no `finance_digest_broker_ranges` entry overlaps SearXNG's subnet
     (Reviewer H, H5-1: an inventory override can't re-open the path to SearXNG);
   - D's sources and the library exist on the controller; the group `rgate` exists on the host;
   - **the library is really mounted** (§12.11): the root is a real directory on exactly
     `finance_digest_library_mount` (not `/`, not the disk under an empty mount point), `.ai-library-sentinel` is a
     regular file and `50-finance/` a real folder.
2. **Base:** Python 3.12+; the account above.
3. **Code:** every directory checked to be absent or a real directory before the first write and again before each
   find (never write or prune through a symlink); only `*.py`, skipping `__pycache__`, `test`, `tests`,
   `node_modules`; the launcher. Everything else under `/opt/finance-digest` is pruned (D's tests, README and
   example config are never deployed).
4. **Config:** `moneyhub_url`, `broker_url` (`http://<tailnet_ip>:<ai_stack_broker_port|11434>`), `library_root`,
   `timezone`, `ai_summary`, `notion_file`, plus `finance_digest_config_extra`. Validated before install with **D's
   own loader from the installed code** (`finance_digest.config.load`: unknown keys, URLs, types, zone). notion.json
   is validated with D's own reader (`finance_digest.notion.load_config`) and written `no_log`, `diff: false`.
5. **Unit + timer**, daemon-reload, timer enabled and started. A converge never runs the real digest.
6. **Verify** (not in check mode; `finance_digest_verify: false` skips it): `systemd-run --wait --pipe` runs
   `finance-digest run --dry-run` as fdigest in a transient service with **exactly the unit's sandbox and IP
   filter** (one list, `finance_digest_sandbox`, renders the unit and builds the `--property=` arguments). It must
   exit 0 and print the freshness line (`Data freshness: last good sync …`, or the ⚠ line that replaces it). So
   a converge proves live that Money Hub and the broker are reachable through the filter. The entry holds Noah's
   balances, so the output stays out of the play log (`no_log`); the assert shows only the exit code, whether
   the line was found, and the last stderr lines.

A second run is `changed=0`.

## The unit
- `User=fdigest` (its supplementary groups come from `/etc/group` via initgroups, so `rgate` applies),
  `NoNewPrivileges`, `ProtectSystem=strict`, `ReadWritePaths=/srv/ai-library/50-finance` (the only writable
  path), `ReadOnlyPaths=/run`, `ProtectHome`, `PrivateTmp`, no capabilities, `RestrictAddressFamilies=AF_INET
  AF_UNIX`, `@system-service` minus `@privileged`, `PrivateDevices`, `PrivateIPC`, the Protect* set,
  `ProtectProc=invisible` and **no `ProcSubset=pid`** (§12.17 Reviewer E), `RestrictNamespaces`,
  `MemoryDenyWriteExecute`, `MemoryMax=256M`, `TasksMax=32`, `UMask=0027`, `TimeoutStartSec=10min`.
- **Mount safety:** `RequiresMountsFor=/srv/ai-library`, `AssertPathExists=<root>/.ai-library-sentinel`,
  `AssertPathIsDirectory=<root>/50-finance`.
- No IPv6 socket: both URLs are IPv4 literals; for Notion, Python's `create_connection` skips the AAAA answer
  whose socket can't be opened and uses the A record.

### IP filter
`IPAddressAllow=` (always):
- `<Money Hub IP>/32` (parsed from `moneyhub_url`);
- `<tailnet_ip>/32` **and** the broker's DNAT target range. The broker is published on `tailnet_ip` with the
  userland proxy off, so the request is DNATed in nat OUTPUT to the broker container's bridge address (systemd's
  egress filter sees that), and the reply is un-NATed before delivery (the ingress filter sees `tailnet_ip`).
  Both are needed (§12.15 Reviewer D, §12.16.4). The broker's container address isn't pinned (queued for
  ai_stack P2), so the range is role docker's `docker_address_pools_container` (172.16.0.0/12, as ai_control)
  **minus SearXNG's pinned subnet** (`searxng_subnet`): SearXNG forwards query strings to public search engines,
  so reaching it would be a way out to the internet for the figures. Both values are read from the inventory, else
  from roles docker's and searxng's defaults (`include_vars`, never copied). Set `finance_digest_broker_ranges`
  (a list of CIDRs) to narrow it to the broker network's subnet or a /32;
- `127.0.0.53/32`, systemd-resolved's stub.

`IPAddressDeny=`:
- **Notion off: `any`.** Nothing else is reachable.
- **Notion on: the private and special ranges** (`finance_digest_ip_deny_public`, the same set as research_gate:
  loopback, RFC 1918, `100.64.0.0/10`, link-local, ULA, plus unspecified, multicast, reserved, benchmark, the
  IETF block, NAT64 and IPv4-mapped). Chosen over "allow `0.0.0.0/0` minus the private ranges" because an allow
  entry wins over a deny entry in systemd: listing the public internet as allow entries would take ~30 CIDRs to
  express the complement, and one mistake would silently open a private range; the deny-list form is the
  reviewed research_gate pattern, keeps loopback, the LAN and the rest of the tailnet closed, and lets the
  `/32`s and the broker range through as the only exceptions.

Preflight checks: no allow entry is a `/0` or malformed; Notion off means the deny list is exactly `[any]`;
Notion on means no deny entry is a `/0`, every allow entry sits inside a deny entry, and the deny list covers
loopback v4/v6, RFC 1918, `100.64.0.0/10`, link-local v4/v6 and ULA.

## Inputs
- **Required (host_vars, git-ignored):** `moneyhub_url` (e.g. `http://<Money Hub tailnet IP>:<port>`), `tailnet_ip`.
- Optional: `ai_stack_broker_port` (default 11434), `finance_digest_notion_token` (vault) +
  `finance_digest_notion_page_id`, `finance_digest_broker_ranges`, `finance_digest_ai_summary`,
  `finance_digest_timezone`, `finance_digest_config_extra` (D's `model`, timeouts, `bills_window_days`),
  `finance_digest_library_root` / `_mount`, `finance_digest_verify`.

## Host checks for the Integrator
- `id fdigest` → groups `fdigest rgate` only; `ls -la /opt/finance-digest` shows `finance-digest`,
  `finance_digest`, `library`, all root-owned; `/etc/finance-digest` is root:fdigest 0750, `config.json` 0640, and
  `notion.json` absent (or 0640 with Notion on).
- `systemd-analyze verify /etc/systemd/system/finance-digest.{service,timer}` clean; `systemctl list-timers
  finance-digest.timer` shows the next 09:30 America/New_York; `systemd-analyze security finance-digest` for the
  record; `systemctl show -p IPAddressDeny -p IPAddressAllow finance-digest` lists the ranges above.
- The converge's verify passed (a sandboxed dry run reached Money Hub and, with `ai_summary`, the broker). Compare a
  manual dry run with Money Hub's own numbers (§12.18.3):
  `sudo systemctl start finance-digest` is the real run; for a dry run use the same `systemd-run` line as
  `tasks/verify.yml`, or `sudo -u fdigest python3 -I -B /opt/finance-digest/finance-digest --config
  /etc/finance-digest/config.json run --dry-run` (no sandbox).
- A real run (`systemctl start finance-digest`) writes `50-finance/log/YYYY-MM.md`; a second run the same day
  replaces that day's entry; `journalctl -u finance-digest` shows no seccomp/`EPERM`/AVC errors.
- The AI summary passed D's number guard or was skipped ("AI summary skipped …") — if it is always skipped, check
  the broker reachability first (the DNAT range).
- **No write calls reach Money Hub:** D is GET-only (§12.18.2); on the edge VPS, Money Hub's access log shows only
  GETs from ai-box's tailnet address during the run.
- The filter, live: `systemd-run -P -p IPAddressDeny=any -p IPAddressAllow="<the unit's allow list>" curl -m5 -s -o
  /dev/null -w '%{http_code}\n' http://<searxng_ipv4>:8080/healthz` must time out (SearXNG cut out), while the same
  against `http://<tailnet_ip>:11434/api/version` prints 200.
- **Notion on** (the dry run never calls Notion): a real run appends the entry to the page; a Notion failure is
  logged and never fails the run.

## Tests
```sh
roles/finance_digest/tests/run.sh   # from anywhere; uses the repo's .venv; no root; the scratch dir must not be on /
```
1. `check_no_log.py`: every task that names the Notion token, the notion.json content or its path is `no_log`.
2. `test_finance_digest.yml` at `-vvv --diff`, with a canary Notion token and a fake `systemd-run` (tests/bin):
   - the account task's exact attributes (`groups: [rgate]`, `append: false`, ...) and the `id -Gn` check;
   - preflight passes with Notion off and on against a scratch library on the scratch dir's real mount; the exact
     allow/deny lists for both, and the broker ranges proven to tile 172.16.0.0/12 minus SearXNG's /28 with no
     overlap; 45 refusals (Money Hub URL: empty, hostname, off-tailnet, path, user info, query, `file:`, short
     form, space; tailnet address; broker port; `ai_summary` string; zone with a space; resolver stub; extra
     config overriding the URL or the Notion file; Notion half-set, bad page id, token with a space; a `/0`,
     malformed or empty broker range; 3 broker overrides reaching SearXNG (the whole container range,
     a /32 inside its subnet, a moved subnet) and SearXNG's subnet resolving to empty; with Notion a deny list with `0.0.0.0/0` or without the tailnet or RFC
     1918 range; library paths; the mount checks; missing sources; a missing `rgate` group);
   - install twice with junk and strays, a third run touching nothing (inodes and mtimes), 4 symlink cases refused
     by the guard with the victim untouched;
   - config.json exactly D's keys, 0640 in 0750; D's loader refuses 8 bad configs and accepts the rendered one;
     notion.json written with Notion on (0640, D's reader accepts it, a re-run changes nothing), removed with
     Notion off; a symlinked config dir refused;
   - the units for Notion off / on / a narrowed range: `systemd-analyze verify` with any warning failing,
     `[Service]` exactly the sandbox list plus four fixed keys, the IP lines exact, the timer's schedule;
   - verify.yml: the fake systemd-run gets `--wait --pipe --collect`, every sandbox property and the dry-run
     command; a good dry run passes, a failing one and one without the freshness line are refused.
3. The output is grepped for the canary token and the fake dry run's balance line, as text and as base64 at all
   three byte alignments (so a slurp of a file that embeds them is caught).
