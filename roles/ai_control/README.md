# Role `ai_control`

Deploys AI Control on **ai-box** (`docs/CONTRACTS.md` §12.15, §12.15.7). It is play 2 of `playbooks/ai-control.yml`
(`hosts: ai_servers`). Play 1 (role `ai_host_agent`) runs first and exports the host agent's token. Without it (the PC is off,
play 1 failed, or `--limit ai-box`), ai-box keeps the token file it already has. The playbook pings the PC first and ends
play 1 for it when it's unreachable; a play 1 failure is rescued (the token is cleared), play 2 still runs, and a last play
fails the run. **If the PC drops off in the middle of play 1**, `rescue` can't catch an unreachable host, so the run stops
before play 2: rerun it once the PC is back, or with `--limit ai-box`.

## What it installs

| What | Where |
|---|---|
| Package | `python3-aiohttp` and `nftables` from the Ubuntu archive; asserts Python ≥ 3.11 and that aiohttp imports. |
| Account | `aictl`: system, nologin, no home, **no supplementary groups** (`append: false`, so a stray `docker` membership is removed). |
| Server S | `/opt/ai-control/server/ai_control/*.py`, root 0644/0755. The server dir holds only the package (no tests, README or example). |
| Web W | `/opt/ai-control/web/`: W's `public/` tree, only `.html .js .css .svg .webmanifest`, nothing under `test/`, `tests/`, `__pycache__` or `node_modules`. Pruned like the server tree. |
| Helper X | `/usr/local/sbin/aictl-helper` (root 0755). One root process per connection, so a new script needs no restart. |
| Config | `/etc/ai-control/` root:aictl 0750; `config.json` root:aictl 0640 has exactly the keys of S's `config.example.json`, values from inventory (+ `ai_control_config_extra`), and is validated by the installed server's loader. |
| Token | `/etc/ai-control/agent-token` root:aictl 0640, from `ai_control_agent_token` (no_log, diff off), and left as it is when no token was exported this run. S re-reads it on every call, so it needs no restart. |
| Helper config | `/etc/ai-control/helper.json` root 0644: `user`, `containers`, `sudo_exes: ["/usr/bin/sudo.ws","/usr/lib/cargo/bin/sudo"]` (+ `ai_control_helper_config_extra`), validated by the installed helper's parser. |
| State | `/var/lib/ai-control` aictl:aictl 0750 (`gate-samples.jsonl`). |
| Units | `ai-control.service` (aictl, §11.1 hardening + `ReadWritePaths=/var/lib/ai-control`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, `MemoryDenyWriteExecute`, `ProtectProc=invisible`, IP allowlist: `localhost`, ai-box's and the PC's tailnet addresses and Docker's container range, since the broker and Open WebUI are DNATed into it, or `ai_control_container_allow` in its place, see below); `aictl-helper.socket` (`/run/aictl-helper.sock` 0660 root:aictl, `Accept=yes`, `MaxConnections=16`, a non-latching poll limit); `aictl-helper@.service` (root, `RuntimeMaxSec=30`, `CapabilityBoundingSet=CAP_DAC_READ_SEARCH`, `PrivateNetwork`, `ProtectSystem=strict`, `ProtectHome=tmpfs`, an empty read-only `/run` with only its tools' paths bound in). S's `/run` is an empty read-only tmpfs with only the helper socket bound in, and S `Requires=` the guard (below). |
| Local-access guard | `/etc/ai-control/guard.nft` root 0644 (`table inet ai_control_guard`), loaded by `ai-control-guard.service` (oneshot, `RemainAfterExit`, before `tailscaled.service` and S). See "Local-access guard". |
| Publishing | `tailscale serve` 443 → `http://127.0.0.1:8095`, plus other roles' handlers listed in `ai_control_serve_extra_handlers` (Mission Control's :8443), through role `tailscale_serve` **unchanged**, only parameterised (below). |

## Run order

1. **Preflight:**
   - the inputs;
   - **Docker's container range is found** (`docker_address_pools_container`: the inventory's value, else role docker's
     defaults, read with `include_vars`). It goes in the IP allowlist (see "Units" below);
   - **`ai_control_container_allow`, when set:** a non-empty list of IPv4 host addresses (`a.b.c.d/32` or bare `a.b.c.d`),
     none `0.0.0.0`, nothing wider than /32, no names, IPv6 or trailing newline, each inside that container range, and none
     equal to ollama-cpu's pinned address (`ai_stack_ollama_cpu_ip`, when defined). Anything else is refused;
   - the token (all no_log): a well-formed one was passed in, **or** none was and `/etc/ai-control/agent-token` already exists
     (the PC-off converge). Neither is a failure that says "run both plays once with the PC on";
   - **the agent port agrees three ways:** what S calls (`ai_control_agent_port`), what the agent serves (exported by play 1 as
     `ai_host_agent_exported_port`, else the PC's inventory `ai_host_agent_port`, else role ai_host_agent's default) and G's
     `ollama_rocm_policy_extra_ports` on the `workstations_root` host;
   - **`kernel.apparmor_restrict_unprivileged_userns` is 1** (read-only; without it `_EXE` can be forged through a user namespace);
   - the app sources exist.
2. Package, account.
3. Code: S, W, X. **No write or prune goes through a symlink:** the code dirs (`/opt/ai-control`, `server`,
   `server/ai_control`, `web` and every subdirectory of `web`) must be absent or real directories, checked with `stat follow: false`
   before the first write and again right before each `find`/prune.
4. `config.json`, token (only when one was passed in), state dir.
5. `helper.json`, socket + template units.
6. The local-access guard: the allowed users resolved to uids (a missing one skipped with a warning), `guard.nft` (validated
   with `nft -c`) and its unit.
7. The server unit. Then flush the handlers (daemon-reload, reload the guard, restart the socket, then the server), and enable +
   start the guard, the socket and the server.
8. `tailscale serve`: `include_role: tailscale_serve` with the handlers set to `[{port: 443, mount: /, backend: http://127.0.0.1:8095}]`
   followed by `ai_control_serve_extra_handlers` (default `[]`; see "Other roles' serve handlers"), and its one-origin guard (`tailscale_serve_dashboard_port/backend`) pointed at 8095. The role reads `tailscale serve status --json`,
   compares it as sorted JSON, and changes nothing when they match. On any drift it runs `tailscale serve reset`, then
   `tailscale serve --bg --yes --https=443 --set-path=/ http://127.0.0.1:8095`. Its defaults, and so the mediaserver, are untouched.
   Its precondition needs host var `tailnet_fqdn` to equal ai-box's tailnet name. **Every serve handler on ai-box is declared here:**
   anything else (for example an Open WebUI handler someone adds by hand) counts as drift and is removed, unless it is listed in
   `ai_control_serve_extra_handlers`.
9. **Read-only checks** (not in check mode):
   - the guard, the server and the socket are active;
   - `/healthz` is 200 on 127.0.0.1:8095;
   - `/` serves HTML with the CSP;
   - a fixed client run **as aictl** (`setpriv`, no PAM) sends `{"op":"containers"}` and gets `ok`, `docker: active` and the
     three allowlisted names, none missing;
   - the same client as root gets `forbidden` (the peer check);
   - `/api/summary` shows `helper`, `broker` and `webui` **ok**: the helper over the real peer check, and the broker and Open
     WebUI through the IP allowlist (Docker DNATs them into its container range). It also requires `agent` **ok** when play 1
     exported a token this run, which proves the token and the firewall path end to end. With no token, a down agent is allowed
     (the PC is off) **unless S says the agent refused the token or the token file is bad** ("agent rejected the token (401)",
     "agent token file ..."): the kept token is then stale, and the run fails. S reports this only as text, so the check matches
     the agent row's `detail`. Preflight prints a note when no token was exported. `ai_control_verify_agent: false` drops the
     "agent ok" requirement, never the 401 rule.

   - **the local-access guard holds** (`tasks/verify_guard.yml`; see "Local-access guard").

Check mode: nothing is started or probed. Before any real run there may be neither a token nor a token file; that is allowed
in check mode only.

## Inputs

- **Host vars:** `tailnet_ip`, `tailnet_fqdn`, `gaming_pc_tailnet_ip` (they already exist in `host_vars/ai-box.yml`).
- **Play var:** `ai_control_agent_token`, from play 1: `hostvars[groups['workstations_root'][0]].ai_host_agent_token | default('')`.
  It is never set in inventory. Empty means "keep ai-box's token file", which must then exist.
- **Optional, `ai_control_container_allow`** (§12.23, R12-5; see "Container allow list" below). Unset (`null`, the default)
  keeps Docker's whole container range.
- **`ai_control_guard_allow_users`** (default `[root]`; see "Local-access guard"). **Lead, for ai-box:** `[root, mc]`.
- **`tailnet_ip6`** (ai-box's tailnet IPv6, `fd7a:115c:a1e0::…`; shared with role mission_control). Unset, preflight reads
  `tailscale ip -6`. **Lead, set it in `host_vars/ai-box.yml`** so the guard doesn't depend on a live read.
- **Optional, `ai_control_serve_extra_handlers`** (§12.29; see "Other roles' serve handlers" below). Default `[]`: AI Control's
  443 alone, exactly as before.
- **Optional, keep these equal to the gate's config:** `ai_control_gate_threshold`/`ai_control_gate_window` (defaults 50/1800,
  the gate's defaults).

## Container allow list (§12.23, R12-5)

S's only peers on a Docker bridge are **gpu-broker** and **Open WebUI**. It calls them on `tailnet_ip` (`broker_url`,
`webui_url`), and Docker DNATs those to their container addresses, which is what systemd's IP filter sees. Everything else S
talks to is off the bridges: the gate and the host agent on the gaming PC's tailnet address, the helper over its Unix socket,
chronyc on 127.0.0.1:323, and the CPU Ollama's state comes through the broker (`/broker/status`, `/api/tags`, `/api/ps`), never
from ollama-cpu itself. S never calls SearXNG, Tika, ollama-cpu or anything on `docker0`.

So the whole container range (`172.16.0.0/12`) is wider than S needs: it also lets S reach ollama-cpu directly on its container
address, around the broker's management lock-down (§12.23). **Lead, set this for ai-box** (e.g. in `host_vars/ai-box.yml`; the
two pinned addresses are in `group_vars/ai_servers`):

```yaml
ai_control_container_allow: ["{{ ai_stack_broker_ip }}/32", "{{ ai_stack_webui_ip }}/32"]
```

Then `IPAddressAllow=localhost <tailnet_ip> <gaming_pc_tailnet_ip> 172.20.0.3/32 172.20.0.4/32` (today's pins). If a pin moves,
the unit follows on the next converge (both come from the same vars as role ai_stack's compose file). Leaving it unset changes
nothing.

## Other roles' serve handlers (§12.29)

Role `tailscale_serve` declares ai-box's **whole** serve config: anything this role doesn't declare is drift, and drift means
`tailscale serve reset` and re-applying the declared handlers. Role `mission_control` adds https **:8443** →
`http://127.0.0.1:8097` on ai-box. If this role declared only its 443, every `ai-control.yml` run would reset 8443 away, the next
`mission-control.yml` run would put it back, and each reset drops AI Control's 443 for a moment.

So the declared set is `[AI Control's 443 handler] + ai_control_serve_extra_handlers`: AI Control's handler first and unchanged,
then the extras in order. Role `mission_control` declares the same expression from the same inventory var, so both roles agree
and neither converge touches the other's handler. **Lead, set this for ai-box** (`host_vars/ai-box.yml`, read by both roles):

```yaml
ai_control_serve_extra_handlers:
  - {port: 8443, mount: /, backend: "http://127.0.0.1:8097"}
```

The first converge after it is set resets the serve config once (adding 8443; 443 blinks for a second or two); after that both
playbooks give `changed=0` on serve. Unset (`[]`), nothing changes for an existing deploy.

Preflight refuses, before anything is written, an extras value that is not a list; an entry that is not a mapping, lacks
`port`, `mount` or `backend`, or has any other key; a port that isn't an integer 1-65535, is `ai_control_serve_port` (443:
nothing lands on AI Control's origin), or is used by another extra (one handler per extra origin); a mount other than `/` or
`/name[/...]` (a `.` or `..` segment, `//` and a trailing `/` are refused; dots inside a name are fine); and a backend other than `http://127.0.0.1:<port>` (loopback literal, port 1-65535, no path, no trailing newline)
or AI Control's own `http://127.0.0.1:8095`. Role `tailscale_serve`'s own guard still keeps 443 to AI Control alone.

## Local-access guard (Reviewer Q)

**The hole.** S trusts the `Tailscale-User-Login` header that `tailscale serve` adds, and ai-box is Noah's own node. So
every connection that reaches serve's 443 **from ai-box itself** is stamped as Noah: a plain connect to ai-box's tailnet IPv4
or IPv6, and tailscaled's local API (`/run/tailscale/tailscaled.sock`, world-usable), which answers `101` to a
`POST /localapi/v0/dial` for ai-box's own address and connects for its caller **as root**. And `127.0.0.1:8095` takes a forged
header from any local process.

**The guard.** A dedicated `table inet ai_control_guard` (`templates/guard.nft.j2`), loaded by `ai-control-guard.service`
(oneshot, `RemainAfterExit`):

- **input:** `ip daddr <tailnet_ip> tcp dport 443 iifname != "tailscale0" reject with tcp reset`, and the same for
  `ip6 daddr <tailnet IPv6>`. Real serve traffic always arrives on `tailscale0`; a local connection (any uid, root and
  tailscaled's dial included) arrives on `lo`, so it gets a reset. Decided by interface, not by uid, because the local-API dial
  is made by root.
- **output:** `ip daddr 127.0.0.0/8 tcp dport 8095 meta skuid != { <uids> } reject with tcp reset`. The uids come from
  `ai_control_guard_allow_users` (default `[root]`; root must stay: tailscaled proxies serve to S as root, and the role's own
  checks run as root). **Lead, set `[root, mc]` for ai-box**: Mission Control reads `/api/summary` as `mc`.
- One nft transaction declares, deletes and re-adds **only this table**. Never `flush ruleset`, never `/etc/nftables.conf`,
  never UFW's or Docker's tables, so `ufw reload` and Docker restarts leave it alone. The rules file is checked with `nft -c`
  before it is written.
- The unit is ordered **before `tailscaled.service` and `ai-control.service`** (and after `nftables.service`, whose
  `flush ruleset` would otherwise run after it). S `Requires=` it, so S never listens unguarded. A changed file is applied with
  a reload (`nft -f`), never a restart (which would stop S).

**Users that don't exist yet.** Names are resolved to uids on every converge. A listed user that doesn't exist is **skipped
with a warning**, never opened; root must resolve. Why this and not "fail": `mc` is created by role mission_control, and
`playbooks/mission-control.yml` runs after `playbooks/ai-control.yml`, so on the very first deploy `mc` can't exist yet, and a
hard failure would block AI Control itself. Why not a reload from mission_control: that is another module, and the guard's
content would then depend on which playbook ran last. The cost: until `ai-control.yml` runs again, `mc` is refused (Mission
Control's AI Control card shows its source down). **Lead: on the first deploy, run `ai-control.yml`, `mission-control.yml`,
then `ai-control.yml` once more.** If `mc` is ever deleted and recreated with another uid, the same rerun is needed.

**Q-2: the units can't see tailscaled's socket.** Both units get `TemporaryFileSystem=/run:ro` (an empty read-only `/run`)
with only what they use bound back in, read-only:
- S: `/run/aictl-helper.sock` only. S needs nothing else there: chronyc falls back to UDP 127.0.0.1:323 (as before; aictl
  could never use `/run/chrony`), and every peer is an IP literal. The socket is bound by inode, so S is `PartOf=` the socket
  unit: when the socket restarts (its file is recreated), S restarts and binds the new one.
- The helper: `-/run/docker.sock` (the docker CLI), `/run/systemd/private` and `/run/systemd/system` (systemctl as root),
  `-/run/dbus/system_bus_socket` (systemctl's fallback bus), `-/run/log/journal` (the volatile journal). `-` = may be
  absent. Each request is a fresh instance, so it always binds the current sockets.

**Checked live on every converge** (`tasks/verify_guard.yml`; asserts in `verify_guard_assert.yml`):
- the table is loaded;
- as **root** and as **nobody**, TCP to `<tailnet_ip>:443` and `[<tailnet IPv6>]:443` is refused;
- `127.0.0.1:8095` is accepted for root and for every other allowed user that exists (`mc`), refused for nobody;
- as nobody, tailscaled's local API dialling ai-box's own IPv6 and IPv4 :443 gets **no HTTP answer at all** through the dial
  (no 200, no JSON, not even a 502). `files/guard_probe.py` sends the dial and, only on a `101`, a TLS handshake plus one
  `GET /api/summary`;
- S's mount namespace (`/proc/<MainPID>/root/run`) has the helper socket and not `tailscaled.sock`;
- when ufw is active: `ufw reload`, then the table and the root/nobody probes again (`ai_control_verify_guard_ufw_reload`).

**The one thing only ai-box can prove** is that tailscaled's dial goes through the kernel (and so through the input hook). If
tailscaled ever dialled its own address in userspace (netstack), nft would never see it: the dial check above would then fail
the converge rather than pass silently.

## Host checks for the Integrator

- **`_EXE` sudo lines:**
  - On ai-box, `journalctl -o verbose SYSLOG_IDENTIFIER=sudo -n 1` must show `_EXE=/usr/bin/sudo.ws`. If it shows another path,
    add it to `ai_control_helper_sudo_exes`.
  - The Audit tab (host ai-box, user claude) must show this converge's own sudo rows.
  - Run `sudo -l` once as localai, then confirm it shows as "Refused".
- **Helper sandbox:**
  - Watch `journalctl -t aictl-helper -n 20` while the Overview and Logs tabs load: no `failed`/`internal` results.
  - In the Logs tab, each source shows lines: docker, tailscaled, chrony, ai-control, aictl-helper, homelab-ai-stack, and the three containers.
  - Audit sessions list and replay `claude` sessions (`/var/log/sudo-io`).
  - If a journal or sudo-io read fails with EACCES, the one capability (`CAP_DAC_READ_SEARCH`) is the place to look, not more.
- **`ProtectProc=invisible`** on the server: the resources card shows load and memory (`/proc/loadavg`, `/proc/meminfo` aren't pid dirs).
  On the helper it is harmless, since it runs as root.
- **`MemoryDenyWriteExecute=yes`** on the server: if `journalctl -u ai-control` shows an mprotect/`EPERM` crash at start, drop
  it and record that. It isn't expected for CPython + aiohttp.
- **IP allowlist vs Docker DNAT:** with the userland proxy off, `<tailnet_ip>:11434` and `:8080` are DNATed in nat OUTPUT to
  the container's bridge address, and systemd's IP filter sees that address. Prove the allowlist (as root on ai-box):
  `systemd-run -P -p IPAddressDeny=any -p IPAddressAllow="localhost <tailnet_ip> <gaming_pc_tailnet_ip> 172.16.0.0/12" curl -m3 http://<tailnet_ip>:11434/`
  prints "Ollama is running" (the same with `:8080/health`). Without the range it times out. `systemctl show -p IPAddressAllow ai-control`
  must list the range (`docker_address_pools_container`), or with `ai_control_container_allow` set, exactly those addresses
  and not the range. Then, with the list set, as root on ai-box (the unit's own filter, read-only GETs):
  `systemd-run -P -p IPAddressDeny=any -p IPAddressAllow="localhost <tailnet_ip> <gaming_pc_tailnet_ip> <broker_ip>/32 <webui_ip>/32" curl -sm3 http://<tailnet_ip>:11434/`
  prints "Ollama is running" and `.../:8080/health` answers, while `curl -m3 http://<ollama_cpu_ip>:11434/` under the same
  filter times out. The summary still shows `broker`, `webui` and `cpu-ollama` ok.
- **chronyc:** the clock card is green. chronyc talks UDP to 127.0.0.1:323, which the IP allowlist (`localhost`) permits.
- **Exposure:** from the LAN, 8095, 443 and 11510 are all unreachable. From a tailnet client, `https://<ai-box>.<tailnet>.ts.net/`
  loads and the summary is green.
- **Local-access guard:** the converge's guard checks pass (above). By hand, as root: `nft list table inet ai_control_guard`
  shows the three rules; `sudo -u nobody curl -m3 -k https://<tailnet_ip>/` and `sudo -u nobody curl -m3 http://127.0.0.1:8095/healthz`
  both fail with "connection refused"; Reviewer Q's local-API reproduction as nobody gets no `/api/summary`. From the phone,
  AI Control still loads (serve traffic arrives on tailscale0). After `systemctl restart docker` (in a window), the table is
  still there. **Caveat:** `/etc/nftables.conf` has `flush ruleset`; if `nftables.service` is ever started by hand, the guard is
  gone until `systemctl reload ai-control-guard` or the next boot.
- **Helper with the new `/run`:** the Overview, Logs (docker, tailscaled, chrony, ...) and Audit tabs load as before; the
  converge's own helper and summary checks already prove `containers`, docker state and the timers through it.
- **Idempotence:** a second run of both plays gives `changed=0`.

## Tests

```sh
roles/ai_control/tests/run.sh       # from anywhere; runs from the repo root with the repo's .venv
```

`tests/test_ai_control.yml` runs against `tests/inventory.yml` (synthetic; a fake `pc` host carries G's port list). It runs the
role's own task files into a scratch dir, with stub handlers and no root, and covers:
- **preflight:** passes, and 102 refusals: no, short or spaced token; no token and no token file to keep; userns 0 or missing; the
  agent serving another port; S and the agent agreeing on a port that isn't in G's list; a port not in G's list; role-owned config or helper keys; the account named docker;
  and 23 `ai_control_container_allow` values (empty list, a string, a mapping, `0.0.0.0/0`, `0.0.0.0`, `0.0.0.0/32`, a /24 after a
  good /32, a /31, the whole pool, `/320`, an octet over 255, two addresses in one entry, a name, IPv6, a number, a nested list, a
  LAN host, the addresses just past and just before the container range, a /32 and a bare address with a trailing newline, and
  ollama-cpu's address bare and as a /32); and 41 `ai_control_serve_extra_handlers` values (an extra on 443, AI Control's own
  handler repeated, a second extra on 443 after Mission Control's, two extras on one port, the same extra twice, a missing port,
  mount or backend, another key, null, a mapping, a string, a string entry, a string, boolean, 0 or 65536 port, a mount without a
  slash, with a trailing newline or a number, nine traversal-shaped mounts (`/..`, `/.`, `/api/..`, `/../etc`, `/api/./v1`,
  `/api/../x`, `//`, `/api//v1`, `/api/`), and a backend on a tailnet address, on a name, over https, with a path, with a
  trailing slash, with a trailing newline, without a port, on port 0 or 65536, a bare number, AI Control's own, and a bad
  backend after a good extra); and 26 guard inputs (allowed users without root, empty, a string, a mapping, root twice, a uid
  number, a name with a space, a trailing newline, a brace or upper case; the tailscale interface missing, a name that breaks the
  nft string, over 15 characters or with a trailing newline; the IPv6 off the tailnet, with a prefix length, a trailing newline,
  upper case, two `::`, `:::`, a 5-digit group, two addresses or ending in `::`; the IPv4 in 100.128/10, 100.63/16 or the LAN).
  It passes with the README's ai-box value (templated
  from the pinned addresses) plus the range's first and last addresses; `ai_control_ip_allow` is then exactly the tailnet entries
  and the two /32s, and unset it is exactly the tailnet entries and `172.16.0.0/12`;
- **install:** from scratch copies of S and W with junk planted (`__pycache__`, `test/`, `tests/`, `node_modules/`, nested
  `js/tests/`), run twice with strays, pruning, idempotence, the refusal of a wrongly named tree, and **5 symlink cases**: app dir,
  server dir, `server/ai_control`, web dir, and `web/js`, each pointing at a victim dir. Each must be refused by the guard, with
  the victim untouched;
- **`verify_summary.yml` against a fake S** (`tests/fake_summary.py`, a small http.server on 127.0.0.1): it passes when everything
  is ok, and when tokenless with the agent unreachable. It fails when the broker, Open WebUI or the helper is down; when the agent
  is down with a token (a broker or Open WebUI failure must name `ai_control_container_allow` as a cause); and, tokenless, when the agent rejects the kept token (401), the token file is unreadable, or the broker
  is down;
- **W's service worker:** its precache list (`SHELL` in `sw.js`) equals the deployed files, with `index.html` as `/` and `sw.js` excluded;
- **config:** exactly S's example keys and the expected values, modes, and the token content (no_log) unchanged on a rerun
  **and** on a tokenless converge (which also drops the agent from the checks);
- **helper.json:** passes the deploy-time validator and the helper's run-time loader; an unknown key is refused;
- **local-access guard:**
  - preflight took the IPv6 from the inventory and never ran `tailscale ip -6`;
  - `guard.yml` (its `nft -c` run in a private user+network namespace) with `[root, <test user>, <missing user>]`: the
    missing one skipped, uids exactly `[0, <test uid>]`, the three rules exactly, only its own table (declare, delete,
    re-add; no flush), the unit's ExecStart/ExecReload; a rerun leaves the file unchanged; the default `[root]` gives `{ 0 }`;
    an interface name nft can't parse is refused by `nft -c` and the file is not written;
  - **netns proof** (`tests/guard_netns.py`, unprivileged user + network namespaces): ai-box with the tailnet addresses on a
    veth named `tailscale0`, a tailnet peer behind it and a LAN peer behind `eth9`, a stand-in for UFW's table, the guard
    rendered for uids 0 and 1000 and loaded twice. Without the guard every path is open. With it: from ai-box, 443 on both
    addresses is refused for uid 0, 1000 and 1001; 8095 accepts 0 and 1000 and refuses 1001; another serve port and another
    loopback port stay open; from the tailnet peer 443 is open on both addresses for any uid; from the LAN peer it is
    refused; the other table survives, the guard table exists once with its 3 rules (a reload replaces, never appends);
  - the local-API dial probe against a stand-in for tailscaled (`tests/fake_localapi.py`): the request it sends (POST
    `/localapi/v0/dial`, `Upgrade: ts-dial`, `Dial-Host`, `Dial-Port`), and how it reads a 403, a reset after 101, a 200 JSON
    (exposed), a 403 JSON (exposed), a 502 text, and a missing socket;
  - `verify_guard_assert.yml` with canned answers: 3 healthy shapes pass; 20 fail (table missing, root reaching serve on IPv4
    or IPv6, root refused by S, nobody reaching S or serve, a timeout instead of a reset, a crashed or empty probe, mc refused
    or crashed, a dial answered as Noah, JSON or any HTTP answer through the dial, the probe's own exposure flag, a crashed dial probe, one dial only, S
    seeing tailscaled.sock or not the helper socket, S not running);
  - wiring: the import order (guard after helper, before the units), the handler order (daemon-reload, guard reload, socket,
    server), the guard enabled and started before the socket and S, `verify.yml` including `verify_guard.yml` and checking the
    guard active, and `nftables` in the packages;
- **units:** all four rendered (S's `Requires=`/`After=` the guard and `PartOf=` the socket; both S and the helper with
  `TemporaryFileSystem=/run:ro` and exactly their `BindReadOnlyPaths=`, no `ReadOnlyPaths`; the guard's oneshot, ExecStart and
  ExecReload, `Before=ai-control.service tailscaled.service`, `After=nftables.service`) with the real paths and the allowlist on
  and off, `systemd-analyze verify` with any warning failing (except one about the machine's own unit files), and the contract
  settings grepped, including exactly one `IPAddressAllow=localhost <tailnet> <pc> 172.16.0.0/12` line (and
  `IPAddressDeny=any`) with the allowlist on, none with it off; then again with a container allow list (a templated /32 and a bare
  address), whose line is exactly `IPAddressAllow=localhost <tailnet> <pc> 172.20.0.3/32 172.20.0.4`;
- **`tailscale_serve`:** validate, desired and compare, parameterised: by default the handler set is exactly AI Control's 443
  (as before the extras), the declared config exactly the one handler; drift for empty, funnel, an extra port and a wrong backend;
  none for the same config in any key order;
- **extra handlers (§12.29):** preflight passes with mounts whose names contain dots (`/.well-known`, `/a..b/v1.2/..x`); with
  Mission Control's :8443 as the extra, preflight passes; the set is exactly AI Control's 443
  (first, equal to its own handler, integer port) then the extra; `tailscale_serve`'s validate passes with its guard on 443; the
  declared config is exactly 443 → 8095 and 8443 → 8097; drift for today's ai-box (443 alone), for Mission Control alone, and for
  funnel on 8443; none for the pair;
- **both roles agree:** a second play loads role `mission_control`'s own defaults and vars (its definition, not a copy) next to
  this role's, and for three inventories (no extras; Mission Control's :8443; the same with `ai_control_serve_port` and
  `ai_control_listen_port` moved) `ai_control_serve_handlers` and `mission_control_serve_handlers` are equal and exactly the
  expected list.

`run.sh` first runs `check_no_log.py`, a static check that every task in `tasks/*.yml` touching the token (its variables, a slurp
of the token file) has `no_log: true`. Then it runs the playbook, and fails if the canary token appears anywhere in the
`-vvv --diff` output, as text, as base64, or as base64 with a trailing LF (slurp's form).
