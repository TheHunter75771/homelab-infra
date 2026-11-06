# Role `mission_control`

Deploys **Mission Control** on **ai-box** (`docs/CONTRACTS.md` §12.29, module H12). It is Noah's home-screen app (a PWA):
the server MC1 (`apps/mission-control/server`), the web app MC2 (`apps/mission-control/web/public`), its own web push,
and the 05:30 America/New_York morning briefing. It's tailnet-only, through `tailscale serve` https **:8443**, a
separate origin from AI Control's 443. Playbook: `playbooks/mission-control.yml` (`hosts: ai_servers`), run after
`playbooks/ai-control.yml`.

## What it installs

| What | Where |
|---|---|
| Packages | `python3-venv` and `nftables` (the guard's `nft`) from the Ubuntu archive. |
| Account | `mc`: system, nologin, no home, **no supplementary groups** (`append: false`). It isn't in docker, backuppull or systemd-journal. |
| Venv | `/opt/mission-control/venv` (root), built in `venv.new` and swapped in only once it checks out, **only** from `files/build.lock` + `files/requirements.lock`, every file sha256-pinned (`--require-hashes`, wheels only; only http-ece's sdist is built, with the hashed setuptools from `build.lock`, `--no-build-isolation`, so nothing unhashed is fetched). `pywebpush` must equal `python_packages.pywebpush` and `aiohttp` must equal `python_packages.aiohttp` (versions.yml), or preflight refuses. The system `python3-aiohttp` is never used. The venv is rebuilt only when what's installed differs from the locks: a pin moved, a package was added or changed by hand, or a distro Python upgrade broke the interpreter. The rebuild goes to `venv.new`, is checked (exactly the locks, the push stack imports), then swapped in: the live venv is renamed to `venv.old`, `venv.new` to `venv`, and `venv.old` removed. A failed build (a hash mismatch, a network error) leaves the working venv untouched. |
| Server MC1 | `/opt/mission-control/server/mission_control/*.py`, root 0644/0755. The server dir holds only the package (no tests, README or example). Pruned. |
| Web MC2 | `/opt/mission-control/web/`: MC2's `public/` tree, only `.html .js .css .svg .png .webmanifest .json`, nothing under `test/`, `tests/`, `__pycache__` or `node_modules`. Pruned. |
| CLI | `/usr/local/bin/mission-control` (root 0755): `#!/opt/mission-control/venv/bin/python3 -IB`, runs the installed package (`runpy`). |
| Lib | `/opt/mission-control/lib/`: the two locks and the role's helper scripts (VAPID keygen and check, the journal scan, the egress probe), root 0644. |
| Guard | `/etc/mission-control/guard.nft` (root 0644, checked with `nft -c`) and `mission-control-guard.service` (oneshot, `RemainAfterExit`, enabled, `Before=mission-control.service`): its own nftables table `inet mission_control_guard`. Only root reaches the server on 127.0.0.1:8097, and serve's :8443 only takes connections arriving on the tailscale interface (see "Local-access guard"). |
| Config | `/etc/mission-control/` root:mc 0750. `config.json` root:mc 0640 has MC1's keys (below), validated at deploy time by MC1's own loader (`mission_control.config.load`) under the venv. |
| VAPID keys | `/etc/mission-control/vapid.json` root:mc 0640, MC1's layout `{publicKey, privateKey, subject}`. **Generated once, on ai-box, never rotated by a converge** (see "VAPID keys"). |
| HA token | `/etc/mission-control/ha-token`. **Noah creates it**; the role never reads or writes it. When it exists, the role only fixes its owner and mode to root:mc 0640. |
| State | `/var/lib/mission-control` mc:mc **0700**; `subscriptions.json` mc 0600, created as `[]` once and never overwritten. MC1 changes it under its lock with atomic replaces. Only mc can write anything there. |
| Units | `mission-control.service` (mc, `127.0.0.1:8097`, enabled, `Requires=` the guard), `mission-control-briefing.service` (oneshot, static), `mission-control-briefing.timer` (`OnCalendar=*-*-* 05:30:00 America/New_York`, `Persistent=true`, enabled). Both services share one sandbox (below). |
| Publishing | `tailscale serve` https **8443** → `http://127.0.0.1:8097`, next to AI Control's 443 → `http://127.0.0.1:8095`, through role `tailscale_serve` **unchanged** (parameterised; see "tailscale serve"). |

## Run order

1. **Preflight** (nothing written yet):
   - the inputs, including `mission_control_noah_login` (one `user@domain`, no whitespace or newline);
   - Money Hub, ERAU and the mediaserver are tailnet addresses (`100.64.0.0/10`), and Home Assistant's URL, when
     set, is an IPv4 literal;
   - Open WebUI's container address is inside Docker's range and isn't ollama-cpu's or the broker's;
   - the IP filter (below) denies every private range and allows back single hosts only;
   - the locks are fully pinned and hashed, and agree with versions.yml;
   - the serve set is shared with role ai_control;
   - the app sources exist.
2. Package, account. Then the venv (from the locks) and the code (MC1, MC2, the CLI). **No write or prune goes
   through a symlink:** every code dir is checked with `stat follow: false` before the first write and again right
   before each prune.
3. The config dir, the state dir and the subscriptions file, plus the HA token's mode if it exists.
4. The VAPID keys (only when there are none), then their check.
5. `config.json`, validated by MC1's loader.
6. The local-access guard (its rules file and unit).
7. The units. Then flush the handlers, and enable + start the guard, the server and the timer.
8. `tailscale serve` through role `tailscale_serve` (both handlers).
9. **Read-only checks** (not in check mode, `mission_control_verify: true`):
   - the guard, the server and the timer are active and enabled, and the briefing service is static;
   - **a VAPID token signs** with the real key and subject (py_vapid, as pywebpush does; prints only OK/FAIL);
   - `mission-control check-config` runs **as mc in the units' sandbox** (a transient `systemd-run` with exactly
     the units' properties). It checks that the config loads, that the VAPID file is MC1's layout and readable,
     that the state dir is writable, and that the zone resolves;
   - `/healthz` answers (the start-up wait). Then `/api/cards` with `Tailscale-User-Login: <Noah's login>` gives
     **200**, without the header **403**, and with another login **403**;
   - `tailscale serve status --json` has 443 → AI Control and 8443 → Mission Control, both HTTPS, no funnel;
   - **the guard, live** (see "Local-access guard"):
     - its table is loaded and has the interface rule;
     - as `nobody`, TCP to 127.0.0.1:8097 and to :8443 on ai-box's tailnet IPv4 and IPv6 is refused;
     - as root, 8097 connects and both :8443 are refused;
     - as `nobody`, a dial through tailscaled's local API to both :8443 gets no HTTP answer;
     - `ufw reload` (when UFW is active) leaves the table's rules unchanged;
   - **the IP filter and the /run allowlist, live** (below);
   - **no secrets in the journal:** both units' whole journal is searched for the VAPID private key (as stored,
     base64 and hex), the Home Assistant token, and every subscription's endpoint, auth and p256dh. Only counts and
     labels are printed.

Check mode: nothing is started, generated or probed.

## Inputs

**Host vars that already exist** (`host_vars/ai-box.yml`): `tailnet_ip`, `tailnet_fqdn`, `moneyhub_url`,
`mediaserver_tailnet_ip`, `gaming_pc_tailnet_ip`, `lan_cidr`, and `mission_control_noah_login` (private). Group vars:
`ai_stack_webui_ip`, `ai_stack_broker_ip`.

**Also required** (all in place since 2026-10-28):

```yaml
# host_vars/ai-box.yml: ai-box's tailscale serve handlers besides AI Control's 443. Read by role ai_control AND
# this role, so neither converge removes the other's handler (see "tailscale serve").
ai_control_serve_extra_handlers:
  - {port: 8443, mount: /, backend: "http://127.0.0.1:8097"}

# inventory/group_vars/all/versions.yml, python_packages (this role's lock pins both; preflight compares)
python_packages:
  pywebpush: "2.5.0"
  aiohttp: "3.14.5"
```

**Optional (defaults in `defaults/main.yml`):**

| Var | Default | What |
|---|---|---|
| `tailnet_ip6` / `mission_control_tailnet_ip6` | `""` (read from `tailscale ip -6` at preflight) | ai-box's tailnet IPv6, for the guard |
| `mission_control_ha_url` | `""` (off) | `http(s)://<IPv4>:<port>` of Home Assistant. Its address joins the IP filter. Its card says "connect HA" until `/etc/mission-control/ha-token` exists |
| `mission_control_ha_entities` | `[]` | at most 12 entity ids |
| `mission_control_mediaserver_url` | `https://mediaserver.<tailnet>` when `mediaserver_tailnet_ip` is set | its dashboard's `/api/summary` (backups) through its serve 443. `""` turns it off |
| `mission_control_mediaserver_ip` | `mediaserver_tailnet_ip` | pinned in MC1's `host_map` and allowed by the IP filter |
| `mission_control_erau_url` / `_erau_ip` | `https://homebk-edge.<tailnet>` / Money Hub's address | ERAU through homebk-edge's serve 443. ai-box is a user-owned node, so serve stamps Noah's login and no credential is needed (MC1 README, "ERAU dashboard auth") |
| `mission_control_units` | MC1's example list + `mission-control-briefing.service` | the units watched for failed / restarting, incl. `forge-api.socket` and `forge-api.service` |
| `mission_control_backups` | `owui-stage` (kind `unit`, `ai-openwebui-backup.service`, 30 h) and `mediaserver` | MC1 reads ai_backup's freshness from systemd, so mc needs no backuppull group |
| `mission_control_links` | `ai` → `http://<ai-box>:8080/` (Open WebUI, the ask box's target), `ai_control` → `https://<ai-box>/`, `money`, `school` | the cards' tap-through links |
| `mission_control_lockscreen_detail` | `minimal` | `none`, `minimal` or `names` (MC1 README, "Lock screen") |
| `mission_control_moneyhub_timezone` | `UTC` | Money Hub's naive timestamps (Lead, §12.28) |
| `mission_control_vapid_subject` | `https://<ai-box tailnet name>` | the VAPID `sub`, kept in `vapid.json`. It must be one py_vapid signs with: `https://<host>` with **no port and no path**, or `mailto:<address>`. Preflight refuses anything else. A converge rewrites only this field when it differs |
| `mission_control_config_extra` | `{}` | more MC1 keys (`alert_repeat_hours`, `disk_alert_pct`, ...). The role-owned keys are refused: `listen_host`, `listen_port`, `allowed_login`, `static_dir`, `state_dir`, `vapid_file`, `ha_token_file`, `host_map`, `systemctl` |

`config.json` is exactly: `listen_host` `127.0.0.1`, `listen_port` 8097, `allowed_login`, `timezone`, `static_dir`,
`state_dir`, `vapid_file`, `lockscreen_detail`, `aicontrol_url` (`http://127.0.0.1:8095`), `webui_url`
(`http://<Open WebUI's container address>:8080`), `moneyhub_url`, `moneyhub_timezone`, `erau_url`, `host_map` (`{homebk-edge
name: its IP, mediaserver name: its IP}`: ai-box has no MagicDNS), `mediaserver_url`, `ha_url` (null when off),
`ha_token_file`, `ha_entities`, `units`, `backups`, `links`. It is pinned literally in `tests/expected/config.json`.

## tailscale serve

Role `tailscale_serve` declares a node's **whole** serve config: anything not declared is drift, and drift means
`tailscale serve reset` and re-applying the declared handlers. Role ai_control applies it on ai-box with exactly one
handler (443 → 8095). If this role applied 443 + 8443, the next `ai-control.yml` run would reset 8443 away, and the
next `mission-control.yml` run would put it back. Each reset also drops AI Control's 443 for a moment.

**Role tailscale_serve needs no change**: it already takes any handler list (`tailscale_serve_handlers`) and keeps
its one-origin guard on AI Control's 443. **The fix is in role ai_control** (H13, landed 2026-10-28). It builds its
list as `ai_control_serve_handlers: "{{ [ai_control_serve_own_handler] + ai_control_serve_extra_handlers }}"`, with
`ai_control_serve_extra_handlers: []` by default, and its preflight refuses extras on 443. The inventory sets the
8443 handler ("Inputs").

This role declares the same expression: `[AI Control's 443 handler] + ai_control_serve_extra_handlers`. It builds
AI Control's handler from role ai_control's defaults (read with `include_vars`, never copied) and the inventory's
overrides. The offline test loads role ai_control's **real** defaults and vars and proves that both roles declare
the identical set: 443 → 8095, then 8443 → 8097.

**Preflight refuses to run** unless:
- the shared list exists and holds exactly `{port: 8443, mount: /, backend: http://127.0.0.1:8097}`;
- nothing else uses 8443 or the 8097 backend;
- nothing extra lands on 443;
- role ai_control's `vars/main.yml` has `ai_control_serve_extra_handlers` on its `ai_control_serve_handlers:` line.
  The test keeps a fixture of role ai_control as it was before H13 (443 only,
  `tests/fixtures/ai_control_vars_before_h13.yml`), so this regression guard stays checked.

The first converge resets the node's serve config once (adding 8443). AI Control's 443 is gone for a second or two
and then comes back. After that, both playbooks give `changed=0` on serve.

## IP filter

systemd's rule (`systemd.resource-control(5)`):
- an address matching `IPAddressAllow=` passes even when `IPAddressDeny=` matches it too (**allow wins**);
- an address in neither list passes.

So "public internet yes, private ranges no, except a few hosts" is:

- `IPAddressDeny=` **the private and special ranges**: loopback, RFC 1918, CGNAT/tailnet `100.64.0.0/10`,
  link-local, ULA `fc00::/7` (the tailnet's IPv6 too), `0.0.0.0/8`, `192.0.0.0/24`, `198.18.0.0/15`, multicast,
  reserved, `::/128`, IPv4-mapped and NAT64. This is research_gate's set. Never `any`: the push services (FCM,
  APNs, Mozilla) are public hosts behind CDNs, so they can't be listed.
- `IPAddressAllow=` **single hosts only**:
  - `127.0.0.1/32` (tailscale serve in, AI Control) and `127.0.0.53/32` (the resolver stub), and no other loopback
    address: not `localhost`, which would allow all of 127/8 and `::1` (Q-3);
  - homebk-edge's tailnet IP: Money Hub :8710 and ERAU :443;
  - Open WebUI's container `/32` (`webui_url` is `http://<container address>:8080`, straight to the container, no
    DNAT). **Not ai-box's own tailnet IP** (Q-6): that would open everything published there (the broker, SSH, AI
    Control's and Mission Control's own serve ports);
  - the mediaserver's tailnet IP (when configured);
  - Home Assistant (when configured).
- Everything else passes: the public internet.

Preflight refuses:
- an allow entry wider than one host, or outside every deny range;
- `localhost`, any loopback address but those two, and ai-box's own tailnet address;
- a deny `/0`;
- a deny list that misses any of loopback, RFC 1918, CGNAT, link-local or ULA.

**Not possible with this mechanism, and so not done:** limiting egress to port 443. systemd's IP filter matches
addresses only. A port limit would need an nftables `socket cgroupv2` rule in role firewall. That is an open
question for the Lead (below).

**Proven offline:** `tests/` checks the verdict (`mission_control_ip_verdict`, systemd's semantics) for 35
addresses against the rendered lists:
- allowed: 127.0.0.1, 127.0.0.53, Money Hub, Open WebUI's container, the mediaserver;
- denied: 127.0.0.54, 127.0.0.2, `::1`, ai-box's own tailnet IPv4 and IPv6, the gaming PC, other tailnet hosts,
  MagicDNS, ollama-cpu, the broker, docker0, the LAN, the metadata address, the tailnet's IPv6, IPv4-mapped LAN,
  multicast, `0.0.0.0`;
- passed: FCM, APNs, Mozilla, Cloudflare, Google IPv6, and the edges of the CGNAT range.

**Proven live** (verify): `files/egress_probe.py` runs as mc in a transient unit with exactly the units' sandbox:
- **UDP** sends. systemd's filter is a cgroup egress program, so a denied destination fails at `send()` with `EPERM`
  at once, and an allowed or unfiltered one succeeds with no reply needed. That is a clean signal that doesn't
  depend on the far host being up. Targets:
  - every allowed host;
  - every address that must be denied: ai-box's tailnet IPv4 and IPv6, the gaming PC, the LAN's first host,
    ollama-cpu, the broker, 127.0.0.54, `::1`, MagicDNS, and samples of 10/8, 172.16/12, 192.168/16 and CGNAT;
  - one public address.
- **TCP** connects:
  - must connect: AI Control (127.0.0.1:8095), Open WebUI's container :8080, Money Hub, ERAU and the mediaserver
    (on their host_map addresses), and the three push hosts on 443 by name;
  - the server's own 127.0.0.1:8097 must be **refused**: the guard, mc isn't root;
  - must stay **blocked** (EPERM or a timeout; a reset would mean the packet got through), Q-6: the broker through
    its DNAT (tailnet IP :11434), SearXNG through its DNAT (127.0.0.1:8888), and SSH, AI Control's 443 and Mission
    Control's 8443 on the tailnet address.
- **In the sandbox** (Q-2):
  - `systemctl show` of the server answers over D-Bus;
  - a push host resolves through the stub;
  - `/run/tailscale/tailscaled.sock`, `/run/snapd.socket`, `/run/docker.sock` and `/run/aictl-helper.sock` are absent.
- A pywebpush import plus a P-256 signature (`MemoryDenyWriteExecute`, the syscall filter).

The probe plan comes from the same lists. If the filter's own verdict for a UDP deny target were "allow", the
check fails before probing.

## Sandbox (both units)

`User=mc`, `UMask=0077`, `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`,
**`TemporaryFileSystem=/run:ro` with only `BindReadOnlyPaths=/run/dbus/system_bus_socket /run/systemd/system
/run/systemd/resolve`**, **`ReadWritePaths=/var/lib/mission-control` only**, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, an empty
`CapabilityBoundingSet=` and `AmbientCapabilities=`, `SystemCallFilter=@system-service ~@privileged`, `PrivateDevices`,
`PrivateIPC`, the `Protect*` set, `ProtectProc=invisible`, `RestrictNamespaces`, `RestrictRealtime`,
`RestrictSUIDSGID`, `LockPersonality`, `MemoryDenyWriteExecute`, `RemoveIPC`, and the IP filter.

**/run is an allowlist** (Q-2). Inside both units `/run` is an empty read-only tmpfs, with three paths bound in
read-only:
- the system bus socket: MC1's unprivileged `systemctl show`;
- `/run/systemd/system`: systemctl refuses to run without it;
- `/run/systemd/resolve`: `/etc/resolv.conf` → `stub-resolv.conf`, nameserver 127.0.0.53; nsswitch is
  `files dns`.

No other daemon's socket is reachable: tailscaled's, snapd's, Docker's and the AI Control helper's are all absent.
That's checked live, and this PC confirmed the same directives in a user unit. No polkit rule is added: mc can only
read.

The list lives once, in `vars/main.yml` `mission_control_sandbox`. Both unit templates render exactly it, and both
live checks (`check-config` and the egress probe) run under it.

## Local-access guard (Reviewer Q, Q-1 and Q2-1)

MC1 trusts `Tailscale-User-Login`, which only tailscale serve should set. Two local paths would let **any process
on ai-box** act as Noah:
1. **Past serve:** connect straight to 127.0.0.1:8097 with a forged header.
2. **Through serve:** connect to ai-box's own tailnet IP :8443. ai-box is Noah's (untagged) node, so serve stamps
   Noah's login.

The role installs a **dedicated nftables table**, `inet mission_control_guard`, with two chains:
- **output hook:** TCP to 127.0.0.1:8097 is reset unless `meta skuid 0`. tailscaled proxies serve's requests to the
  server as root.
- **input hook:** TCP to ai-box's tailnet IPv4 and IPv6 on port 8443 is reset unless it arrives on
  `mission_control_tailscale_iface` (default `tailscale0`; preflight checks it exists with `ip link show`).

**Why an interface rule and not a uid rule for :8443:** tailscaled's local API (`/run/tailscale/tailscaled.sock`,
usable by every local user) dials on its caller's behalf, **as root**. Reviewer Q proved a `nobody` dial to ai-box's
own tailnet IPv6 gets a `101`, and the connection then goes through. So a uid exemption can't separate tailscaled's
own serve traffic from a local user's. The interface can:
- real serve traffic always comes in on the tailscale interface;
- a connection from ai-box itself (any uid, tailscaled's local-API dial included) comes in on `lo`;
- a container's comes in on its bridge.

Both of those last two get a reset. From the box itself, even root is refused on :8443.

- **Loading:** `mission-control-guard.service` (oneshot, `RemainAfterExit`, enabled, `After=nftables.service`,
  `Before=mission-control.service`) runs `nft -f /etc/mission-control/guard.nft`. A reload re-applies it. The
  server unit `Requires=` it, so the server never listens unguarded.
- **Scope:** the file declares, deletes and re-adds **only its own table** in one transaction. It never runs `flush
  ruleset`, never touches `/etc/nftables.conf`, and never touches UFW's or Docker's tables.
- **Proven offline** (`tests/guard_netns.py`): the rendered file is loaded twice, for real, in a private network
  namespace. A stand-in container sits behind a veth (`vctr0`), and a stand-in tailnet peer sits behind a veth
  named like the tailscale var.
  - root and uid 1000 on the box: 8097 is ok for root and refused for uid 1000; both are refused on :8443 over v4
    and v6; a control port is ok;
  - the container: refused on both :8443;
  - the tailnet peer: connects to both;
  - a UFW-style `iptables-nft-restore` of a `*filter … COMMIT`, and a Docker-style nat chain add, flush and delete,
    leave the guard table's rules unchanged. All the connects are repeated afterwards with the same results. Another
    table survives, and exactly one guard table exists.

  `tests/fake_localapi.py` then exercises `files/localapi_probe.py`: a refused dial, a dial upgraded then closed,
  and a dial upgraded then answered over TLS. Only the last reads as a leak.
- **Proven live:** verify, above (including `ufw reload`).

AI Control's 443 has the same exposure; it is queued for role ai_control.

## VAPID keys

- **Generated:** `files/vapid_keygen.py`, run on ai-box with the venv's cryptography, only when
  `/etc/mission-control/vapid.json` doesn't exist in any form. It writes a temp file `O_EXCL|O_NOFOLLOW`, sets
  root:mc 0640 before any byte is written, then `link()`s it into place. `link()` fails if the name appeared
  meanwhile, so an existing key is never replaced, even if the task's guard were removed.
- **Checked every run** (`files/vapid_check.py`, first `--keys-only`, then the whole file): a regular file, exactly
  `publicKey`/`privateKey`/`subject`, the right lengths, the public key derived from the private key, and a subject
  py_vapid will sign with. It prints `ok` or a reason, never a key.
- **A bad file is refused, never replaced.** Rotating the keys unsubscribes every phone. If one must be replaced
  (only before any phone is subscribed, or knowingly), move the file aside by hand and converge.
- **The subject (live bug, 2026-10-28):** the old default `https://<fqdn>:8443` had a port. py_vapid's `sign()`
  refuses that ("Missing 'sub' from claims"), so every push to Noah's iPhone failed with `VapidException`.
  - **The fix:** the default is now `https://<fqdn>`.
  - **Rewriting the subject:** `files/vapid_subject.py` rewrites **only the `subject`** of an existing `vapid.json`
    when it differs from `mission_control_vapid_subject`, after the keys were checked. Both key strings are kept
    exactly as read, so subscriptions (bound to the public key) stay valid. The rewrite goes to a temporary file
    with the original's owner, group and mode, then is renamed over the original; the result is re-read and the
    keys compared. It runs as a `no_log` task that prints only `updated` or `unchanged`, and an update restarts the
    server. A file without a subject (an older layout) gets one the same way.
  - **The rule:** `files/vapid_check.py` `subject_ok` and the preflight filter `mission_control_vapid_subject_ok`
    use py_vapid's own `_check_sub` pattern verbatim, plus a whole-string `https://<host>` or `mailto:<address>`.
    `tests/subject_vectors.json` pins 17 cases. Checked against the real py_vapid 1.9.6 `sign()`, the role never
    accepts a subject py_vapid rejects. It is stricter in three cases py_vapid lets through: a trailing newline, and
    a mailto with a port or a path.
  - **Verify:** `files/vapid_sign_check.py` signs one VAPID token with the real `vapid.json` (its key and its
    subject), the way pywebpush does (`Vapid.from_string` + `sign`), under the deployed venv. It runs `no_log` and
    prints only `OK` or `FAIL <exception type>`; anything but `OK` fails the run.
- **Every task touching the file is `no_log`**: the keygen, the checks, the subject rewrite, the signing check, the
  stat, the mode fix, and the journal scan (`tests/check_no_log.py` enforces it).

## Memory

Role ai_stack's `ai_stack_mem_host_services` (H2) has `mission_control: 128m` (the server's `MemoryMax=128M`) and
`mission_control_briefing: 128m` (the 05:30 oneshot, which runs alongside the server). The host_vars overcommit
grew by 256 for them.

## Install on the phone (Noah)

1. On the phone, **turn Tailscale on** and open **`https://<ai-box's tailnet name>:8443`** (`tailnet_fqdn` in
   ai-box's private host_vars) in Safari (iPhone) or Chrome (Android).
2. **Add to Home Screen:**
   - iPhone: Share → **Add to Home Screen** → Add;
   - Android: ⋮ menu → **Install app** (or Add to Home screen).
3. **Open Mission Control from the Home Screen icon.** iPhone only allows web push for a Home Screen app (iOS 16.4
   or later).
4. In the notifications panel, tap **Enable notifications**, then **Allow**.
5. Tap **Send a test**. A "Mission Control" notification should arrive within seconds. Tapping it opens the app.
6. The 05:30 briefing comes on its own from tomorrow. To check now, run this as root on ai-box (it sends again even
   if today's was sent): `sudo -u mc /usr/local/bin/mission-control briefing --send --force`. Or start the unit:
   `systemctl start mission-control-briefing.service` (it sends at most once per day).

**The phone must be logged in as Noah and the node left untagged.** serve stamps `Tailscale-User-Login` only for
user-owned nodes. A tagged node (or another login) gets 403. The same goes for ai-box itself: if ai-box is ever
tagged, ERAU's School card goes 403 as well.

If **Enable notifications** is greyed out, the server has no VAPID key: check `journalctl -u mission-control` and
`mission-control check-config`. Only Noah's tailnet login gets in. A family device can't reach :8443 (the ACL
allows `tag:family` only ai-box:8080), and any other login gets 403.

## Host checks for the Integrator

- **IP filter:** the verify's probe line must say so: "IP filter proven: N private addresses denied (EPERM), ...".
  By hand, as root:
  `systemctl show -p IPAddressAllow -p IPAddressDeny mission-control` lists exactly the rendered lines.
- **D-Bus under the sandbox:** the Servers card lists the watched units with real states, not "unknown".
- **`MemoryDenyWriteExecute=yes`:** if `journalctl -u mission-control` shows an mprotect/`EPERM` crash at start,
  set `mission_control_memory_deny_write_execute: false` and record it. It isn't expected: the egress probe already
  signs with cryptography under the same sandbox.
- **ACL:** Noah's devices must be allowed to reach ai-box:**8443**. If the tailnet ACL lists ports per destination,
  add 8443 there. The phone step fails with a connection error otherwise.
- **Exposure:**
  - from the LAN, 8097 and 8443 are unreachable (8097 is bound to 127.0.0.1);
  - from a tailnet client with another login, https://ai-box…:8443/ gives 403;
  - **on ai-box itself** (the guard; verify checks all of this):
    - `sudo -u nobody curl -m3 http://127.0.0.1:8097/healthz` fails with "connection refused"; as root it answers;
    - `curl -m3 -k https://<ai-box tailnet IP>:8443/` fails with "connection refused" for everyone, root included;
    - a local-API dial (`tailscale nc <ai-box tailnet IP> 8443`) gets no HTTP answer.
- **The guard survives the other firewalls:** verify runs `ufw reload` and compares the table's rules before and
  after. A Docker restart isn't needed (Lead); the offline test covers Docker-style nat churn.
  - **Caveat:** `/etc/nftables.conf` has `flush ruleset`, and `nftables.service` is disabled on ai-box. If it is
    ever started by hand, the guard is gone until `systemctl reload mission-control-guard` or the next boot (the
    guard is ordered after it at boot).
- **Idempotence:** a second run gives `changed=0`, and `vapid.json` keeps its checksum.

## Tests

```sh
roles/mission_control/tests/run.sh       # from anywhere; the repo's .venv; no root, no network
```

1. `check_no_log.py`: every task touching a secret (the VAPID file or its scripts, the journal scan, the HA token
   file, the subscriptions file) is `no_log`.
2. `test_mission_control.yml` with `-vvv --diff` against `tests/inventory.yml`. Two local hosts:
   - **`box`** runs the role's task files into a scratch dir with stub handlers:
     - **preflight:** passes, plus 50 refusals: the login (empty, newline, space, not a login), the account, the
       time, the zones, the subject, role-owned extras, units/backups/links shapes, Money Hub/ERAU/mediaserver
       addresses, the container allow list, the IP filter widened (LAN, tailnet, any, deny without 10/8, without
       CGNAT, deny everything), the pins (pywebpush and aiohttp moved or missing, a hashless lock), and the serve
       set (missing, without MC, MC on 443, a second handler on 8443, MC's backend elsewhere, role ai_control as
       it was before H13). It also refuses `localhost`, another loopback address and ai-box's own tailnet address in
       the allow list, user info in Money Hub's and ERAU's URLs, and a bad or off-tailnet guard IPv6. The good case
       reads role ai_control's real vars file, and never asks this machine's tailscale;
     - **venv:** a real `python3 -m venv` with a fake pip (`tests/bin/fake-pip` records the flags and writes
       dist-info). The first build runs the two pip steps with the exact hashed flags. A rerun changes nothing. A
       drifted package, an extra package and a broken interpreter are each rebuilt (in venv.new, then swapped). **A
       lock with a moved pin and a flipped hash** fails in venv.new at the hash check, and the working venv still
       runs the old version, with no venv.old left behind;
     - **install:** the real MC1/MC2 trees with junk planted; strays pruned; the exact installed file list; MC2's
       service-worker precache list (`SHELL` in `sw.js`) equals the deployed web files (`index.html` as `/`, `sw.js`
       left out); 4 symlink cases refused by the guard, with the victim untouched;
     - **config dir and state:** modes; the subscriptions file and Noah's HA token (a canary) are never replaced,
       only re-moded; a symlinked subscriptions file is refused, with the victim untouched;
     - **VAPID:** generated once; on a rerun the generator is skipped. **The subject rewrite:** with the deployed
       state (`:8443`) on disk, a converge rewrites only the subject. The key strings stay identical, the mode stays
       0640 with the same owner, and no temporary file is left. A second converge is `unchanged` (same bytes, same
       mtime). A file without a subject gets one. The 17 subject vectors agree for both copies of the rule; the checksum and mtime are unchanged after a
       rerun **and** after a run with the generator forced; MC1's own `load_vapid` accepts it; 4 bad files refused and left untouched;
     - **config.json:** the exact key set and values, validated by MC1's real loader. An unknown key is refused
       and the good file stays;
     - **the launcher:** over a stand-in package (MC1's CLI imports aiohttp): the units' argv passes through,
       `-I` and `-B` hold, and `PYTHONPATH` is ignored;
     - **verify files against fakes:**
       - `verify_http.yml` against `tests/fake_server.py` in 5 modes; only the correct one passes;
       - `verify_check_config.yml` and `verify_egress.yml` through a fake `systemd-run`. The recorded argv must
         carry the whole sandbox, the `/run` allowlist included. Canned probe results drive 25 egress cases:
         - reachable when they must not be: the LAN, the PC, ollama-cpu, MagicDNS, ai-box's tailnet IP, 127.0.0.54,
           `::1`, the broker or SSH by TCP;
         - SearXNG answering with a reset, which means the packet got through;
         - mc reaching the server past the guard;
         - blocked when they must not be: Money Hub, the mediaserver, Open WebUI, the public internet;
         - a push host or ERAU timing out;
         - inside the sandbox: tailscaled's socket visible, systemctl failing, names not resolving;
         - crypto broken, a target missing, garbage.

         A widened plan fails before probing;
       - `verify_guard_assert.yml` on 13 canned answer sets: nobody reaching the server or IPv6 serve; root refused
         on the server; root reaching :8443 over v4 or v6 from the box; a local-API dial getting a 200 or a 403;
         a timeout instead of a reset; the table missing, changed by `ufw reload`, or without the interface rule;
         garbage;
       - `verify_serve.yml` on 5 status shapes;
       - `verify_journal.yml` with a fake journalctl on 8 journals (the real VAPID key in 3 encodings, a push
         endpoint, the HA token, journalctl failing, no secret to look for at all);
     - **the egress probe script** itself, on loopback.
   - **`render`** runs `check_wiring.py` (Q-5: the include order, verify's `when` and its default, the guard, the
     server and the timer enabled, is-active covering all three, pip as `python -I -m pip` into venv.new with
     `--require-hashes`, the check-config/http/serve/guard/egress/journal includes, the probes under the sandbox,
     the handler order). It then renders the units, the guard, the CLI and `config.json` with the role's **real**
     paths:
     - they must equal `tests/expected/` **literally** (Reviewer M's lesson). The contract's settings are also
       named one by one: the timer's exact `OnCalendar` with the zone, `Persistent=true`, both ExecStarts, mc, no
       capabilities, the one `ReadWritePaths`, the exact `IPAddressAllow`/`IPAddressDeny` lines on both units;
     - the guard file is loaded for real in a private netns (see "Local-access guard");
     - `systemd-analyze verify` runs on a copy with the CLI swapped for `/usr/bin/true`;
     - the IP-filter verdict table;
     - role tailscale_serve's validate/desired/compare with the role's handler set: exactly 443 → 8095 and
       8443 → 8097, with drift for ai-box before the first converge (443 only), an empty config and funnel;
     - role ai_control's real defaults (under a name, so the inventory's extra list isn't reset) and real vars:
       both roles declare the identical set, 443 → 8095 then 8443 → 8097.
3. Then `run.sh` greps the whole output for the generated VAPID private key (stored, base64, hex) and a canary
   planted in the HA token and the subscriptions (text and base64 at 3 alignments). Any hit fails.

**Mutants** (each must fail the suite; run only in a scratch copy of the repo, never in the shared checkout):
- the IP filter widened: an allow `/16`, deny without 10/8, deny without CGNAT, the whole Docker range allowed,
  `localhost` back, ai-box's tailnet IP back;
- the VAPID key rotated each run, and the generator's guard dropped;
- `no_log` removed (keygen, journal scan);
- AI Control's handler replaced or pointed at Mission Control;
- the timer's zone dropped, and `Persistent` dropped;
- `AF_UNIX` dropped;
- `/run` opened again (`ReadOnlyPaths=/run`);
- the guard: missing from main.yml, its skuid check removed, the wrong port, its IPv6 rule dropped, `flush ruleset`
  added; the :8443 interface rule removed (v4, v6), the uid exemption back on :8443 instead of the interface rule,
  the wrong interface name (in the template, and as the default);
- the venv built in place instead of in venv.new;
- verify off by default, the timer not enabled, is-active missing the timer, pip without `-I`, the egress probe's
  include removed;
- subscriptions left 0644;
- the journal scan missing an encoding, and the `secrets >= 1` check dropped;
- the probe check ignoring the plan's verdicts;
- the identity check not requiring 403 without the header;
- the pywebpush pin unchecked;
- venv drift ignored;
- ERAU missing from `host_map`;
- the web install dropping the PNG icons;
- the VAPID subject: a port allowed at preflight, the default back to `:8443`, the subject rewrite rotating the
  keys, the rewrite skipped, and the signing check removed from verify;
- preflight no longer checking role ai_control's vars;
- the test loading role ai_control's defaults unnamed.

## Open items (Lead)

1. Egress is limited by address, not port ("IP filter"). If 443-only egress is wanted, that is a role firewall
   nftables rule on mc's cgroup.
2. The tailnet ACL must allow Noah's devices → ai-box:8443, and ai-box → homebk-edge:443 and mediaserver:443
   (ERAU and the mediaserver dashboard through their serve).
3. `links.ai` points at Open WebUI on `http://<ai-box>:8080/`. MC1's example had `https://<ai-box>/`, which is AI
   Control. Confirm.
4. The PC pull's `.last_pull` (§12.29 "if readable") and X-4's PC-side alerts aren't wired. The file is on the
   gaming PC, which the IP filter denies; it needs a push from the PC or a read endpoint there.
