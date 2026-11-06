# forge_api

The Skill Forge HTTP API (module F, `apps/forge-api/`) as a hardened, socket-activated host service on ai-box, for
the Open WebUI tool "Web research (approval required)" (module T). docs/CONTRACTS.md §12.25, deploy module H8.
Hosts: `ai_servers`, after roles `searxng` and `research_gate` (`playbooks/research.yml`) and the AI stack
(open-webui's pinned address, the `ai` bridge). Modelled on roles `research_gate` (account, install, guards, the
switch) and `voice_profile` (the literal unit test, the sandboxed verify).

| | |
|---|---|
| Account | `rgate`, the research gate's account, shared (Lead decision F-4: one trust zone; a dedicated user is queued, Low). Exactly the attributes roles `research_gate` and `ai_library_share` set, so no role undoes another; a read-only check fails the run if `rgate` is in any other group (never docker) |
| Code | `/opt/forge-api/`, root-owned: F's launcher `forge-api` (0755), `forge_api/`, and `forge/` + `library/` from `apps/skill-library` (0755 dirs, 0644 `*.py`) |
| Config | `/etc/forge-api/config.json` (root:rgate 0640, no secret), dir root:rgate 0750 |
| Token | `/etc/forge-api/token` (root:rgate 0640): generated once on the host, never rotated by a converge, never printed or logged |
| Units | `forge-api.socket` (enabled with the switch) binds `172.20.0.1:879` (privileged); `forge-api.service` (static, `Requires=` the socket) runs `python3 -I -B /opt/forge-api/forge-api --config /etc/forge-api/config.json serve` on the socket it inherits |
| Switch | `forge_api_enabled: false` (default): installed (code, token, units; the config once the approver is set), but the socket and the service are stopped and the socket disabled |

## What a converge does
1. **Preflight** (before anything is written):
   - the switch is a boolean; **switched on, `forge_api_approver_user_id` must be set** (no default) and, whenever it is
     set, match F's rule (1-64 of `A-Z a-z 0-9 . _ @ -`);
   - the library paths are plain absolute paths outside the hidden/system trees, the root on or below the mount;
   - the port is **privileged** (1-1023, and below the host's `net.ipv4.ip_unprivileged_port_start`, read with
     `sysctl`: 1024 on ai-box; Reviewer S / Lead), so no non-root process can ever bind it, even while the socket is
     closed; `MemoryMax`, `TasksMax`, `LimitNOFILE` (they go into the units unescaped); a plain account name;
   - the addresses: the `ai` subnet (`ai_stack_network_subnet`, else role ai_stack's default via `include_vars`),
     the bind address and open-webui's address (`ai_stack_webui_ip`) both inside it and different; the IP filter is
     deny exactly `any` and allow exactly one /32 that isn't loopback, link-local or the bind address;
   - the UFW rule line (below) is exactly `allow from <open-webui> to <bind> port 879 proto tcp comment '...'`;
   - F's and the library's sources exist on the controller, and **F's own loader** (`forge_api.config.validate`)
     accepts the role's config (when the approver is set);
   - **switched on only:** the library is really mounted (§12.11): the root a real directory on exactly
     `forge_api_library_mount` (not `/`), `.ai-library-sentinel` a regular file, `35-forge/` a real folder.
2. **Base:** Python 3.12+; the `rgate` group and user.
3. **Code:** every directory checked to be absent or a real directory before the first write and again before each
   find; only `*.py` of the three packages (no `__pycache__`, `test(s)`, `node_modules`) and the launcher;
   everything else under `/opt/forge-api` is pruned (F's tests, README and example config are never deployed).
4. **Token:** see below.
5. **Config** (only with an approver): exactly F's keys: `library_root`, `bind`, `port`, `allowed_peers`
   (`[ai_stack_webui_ip]`), `token_file`, `approver_user_id`. Validated before install with an exact key-set check
   and **F's own `check-config` from the installed code**, which also loads the token file with F's rules
   (regular file, root/rgate owner, not group-writable or world-readable, b64token, at least 32 characters); switched
   on with `--require-library` (the library must open).
6. **Units**, daemon-reload, restarts of what changed (switched on only), then the switch: on, the socket enabled
   and started, then the service started; off, the socket stopped and disabled first (no connection can start the
   service again), then the service stopped.
7. **Verify** (switched on, not in check mode), read-only:
   1. **the socket ships** (Reviewer Q, Q2-1): `forge-api.socket` is `active` and `enabled`, SubState `listening` or `running` (with `Accept=no` it is
      `running` while its service runs: healthy), with
      `Listen=<bind>:879 (Stream)`; the service is active and still active, with the same `NRestarts` and
      `MainPID`, a few seconds later;
   2. `check-config --require-library` as rgate through `systemd-run` with **exactly the unit's sandbox** exits 0;
   3. `ip -4 addr show to <bind>/32` finds the bind address on exactly one interface with the `ai` network's prefix
      (the bridge gateway Docker made);
   4. **the service serves the inherited socket and nothing else listens** (Q2-1): `ss -ltnp sport = :879` shows
      exactly one listening socket on any address, on `<bind>:879` only, held by exactly systemd (pid 1, the socket
      unit) and the service's `MainPID` (the fd systemd passed). A socket the service bound itself (no pid 1), a
      second listener (`0.0.0.0`, `[::]`) or a third holder all fail;
   5. from the host, `curl http://<bind>:879/requests` gets **no answer at all** (curl rc 7 or 28, HTTP `000`; an empty reply
      (52) or a reset (56) means the connection was accepted, and fails): the
      socket's IP filter drops the host (Lead decision F-2; F's tests prove the 403 path);
   6. `ufw show added` holds exactly the declared rule for port 879 and no other inbound allow that could admit
      it: a rule for the port, a range or list holding it, or an address rule with no port at all
      (`forge_api_ufw_port_rules`; deny, outbound and route rules don't count);
   7. `nsenter --net=/proc/<open-webui pid>/ns/net curl ... /requests` without the token gets **401** (Lead decision
      F-3: the host's curl in open-webui's network namespace; `docker inspect` gives the pid; never `docker exec`);
   8. the token isn't in `journalctl -u forge-api.service -u forge-api.socket` (grep reads the pattern from the
      token file, so the token never enters Ansible, an argv or the play's output), and the journal holds the
      service's own lines (proof it was read);
   9. **Open WebUI's trust settings** (Reviewer Q, Q-1; Reviewer S, S-2 to S-4), read from its database on the host
      (`docker inspect` gives the `open-webui-data` volume's path; `files/owui_trust_check.py` first opens
      `webui.db` with `O_NOFOLLOW|O_NONBLOCK` and requires a regular file, then sqlite opens it `mode=ro` with
      `PRAGMA query_only` and the same inode is re-checked; a 30 s alarm and a 60 s task timeout bound it; never
      through the container). Six checks:
      - `admins`: **the approver is the only admin**;
      - `default-permissions`: the default user permissions give **no workspace tools** (`tools`, `tools_import`,
        `tools_export`, or anything named like a function);
      - `group-permissions`: **no group** grants them either (every group, members or not: new users join
        `ui.default_group_id`, which the detail names, and groups inherit);
      - `tool-write-grants`: **no `access_grant` gives write on a tool**, to a user, a group or `*`;
      - `tool-owners`: **every tool belongs to the approver**;
      - `iframe-same-origin`: **`iframeSandboxAllowSameOrigin` is off** in the admin's Interface defaults
        (`ui.default_interface_settings`) and in every user's own settings.

      Truthiness is Open WebUI's own (S-3): Python `bool()` for permissions (`has_permission`; so the string
      `"false"` is on), JavaScript truthiness for the iframe setting (`value ?? false`). Each check is printed; a
      failure (or an unreadable database) fails the play only with `forge_api_verify_owui_trust` (default true).

A second converge is `changed=0`.

## Socket activation (Reviewer Q, Q-F3)
`forge-api.socket` (`ListenStream=<bind>:879`, `FreeBind=yes`, `Accept=no`, `TriggerLimitIntervalSec=0`) is what
the switch enables: systemd binds the bridge gateway (before Docker has created the bridge, thanks to `FreeBind`)
and holds the port while the service is down. **The port is privileged** (Reviewer S / Lead), so even if the socket
were ever closed, no non-root process could bind it and phish the token; the service needs no capability since
systemd binds it. **Reviewer S (S-1):** `TriggerLimitIntervalSec=0` on the socket and `StartLimitIntervalSec=0` in
the service's `[Unit]`, so neither a burst of connections nor a crash loop ever makes systemd close the socket
(`Restart=on-failure` keeps retrying every 30 s). The service gets the socket through `LISTEN_FDS` (F checks it
matches its config) and **binds nothing itself**: `SocketBindDeny=any` with no allow (stricter than the contract's
`SocketBindAllow=tcp:<port>`, which only made sense while the service bound the port itself). The service has no
`[Install]` section; `Requires=` and `After=` the socket, `After=docker.service` (ordering only).

**IP filter, on both units.** systemd applies a socket unit's `IPAddressDeny=`/`IPAddressAllow=` to the sockets it
creates (the listener and the connections accepted on it), and a service's filter does **not** apply to a socket it
inherits. So the socket carries `IPAddressDeny=any` + `IPAddressAllow=172.20.0.4/32` (inbound only from open-webui;
the host itself, loopback, the LAN and the tailnet are dropped), and the service carries the same pair for anything
it might open itself (no outbound connection at all).

## The service's sandbox
`User=rgate`, `NoNewPrivileges`, `ProtectSystem=strict`, **`ReadWritePaths=<library>/35-forge` only** (requests,
proposals, ticks and the forge lock; items in `10-sources/` are read-only), `ReadOnlyPaths=/run`, `ProtectHome`,
`PrivateTmp`, `InaccessiblePaths=/dev/shm`, `RestrictAddressFamilies=AF_INET AF_UNIX`, `SocketBindDeny=any`, no
capabilities, `@system-service ~@privileged` (`EPERM`, native), `PrivateDevices`, `PrivateIPC`, the `Protect*` set,
`ProtectProc=invisible` **with `ProcSubset=pid`** (F reads nothing in `/proc/net`, unlike the gate),
`RestrictNamespaces`, `RestrictRealtime`, `RestrictSUIDSGID`, `LockPersonality`, `MemoryDenyWriteExecute`,
`RemoveIPC`, `MemoryMax=256M`, `TasksMax=64`, `LimitNOFILE=256`, `UMask=0027`, `Restart=on-failure` (30 s). No
environment lines. Mount safety: `RequiresMountsFor=<library>`, `AssertPathExists=<library>/.ai-library-sentinel`,
`AssertPathIsDirectory=<library>/35-forge`, and `AssertPathIsSymbolicLink=!<library>/35-forge` (systemd follows a
symlink in `ReadWritePaths`; Reviewer M's M-4).

## UFW (the Lead's part)
Role `firewall` owns UFW: it converges to its declared list and **prunes every other rule**, so a rule this role
added would be deleted by the next `site.yml`/`ai-server.yml` converge and re-added by `research.yml` (they would
undo each other). **This role never writes UFW.** It defines the rule (`forge_api_ufw_rule`) and the verify requires
it, alone, for the port:

```
ufw allow from 172.20.0.4 to 172.20.0.1 port 879 proto tcp comment 'homelab: forge-api from open-webui'
```

Matched by address (Lead decision F-1), not by the bridge interface (`br-<network id>`, not pinned). Role firewall
renders it from group_vars/ai_servers (module A5's `to:`, approved by Reviewer U):
`{ port: 879, proto: tcp, from: "{{ ai_stack_webui_ip }}", to: 172.20.0.1, comment: "forge-api from open-webui" }`.

## The token
Generated on the host from `secrets.token_urlsafe(32)` (43 characters) by a task that runs only while the file
doesn't exist (`creates`) and opens it `O_CREAT|O_EXCL|O_NOFOLLOW` (it can't replace a file or write through a
planted symlink). Then: a `stat` (no checksum), a regular-file assert (a symlink or a dangling symlink there fails
the run) and owner/group/mode (0640 root:rgate, content untouched). Every one of these tasks is `no_log`; no task
reads, prints or templates the token (Q-F4). **Noah reads it himself** on ai-box and pastes it into the tool's Valve:

```sh
sudo cat /etc/forge-api/token
```

To rotate it: `sudo rm /etc/forge-api/token`, converge (a new one is made and the service restarted), paste the new
one into the Valve.

## Inputs
- **Required to switch on (host_vars, git-ignored):** `forge_api_enabled: true` and `forge_api_approver_user_id`
  (Noah's Open WebUI user id: Admin Panel > Users).
- From the inventory already: `ai_stack_webui_ip` (group_vars/ai_servers), `ai_stack_network_subnet` (else role
  ai_stack's default). Optional: `ai_stack_network_gateway` (role ai_stack has no such variable; the bind defaults to
  the subnet's first host, the address Docker gives a bridge's gateway, which the verify checks on the host).
- Optional: `forge_api_library_root` / `_mount`, `forge_api_port`, `forge_api_memory_max`, `forge_api_tasks_max`,
  `forge_api_limit_nofile`, `forge_api_verify_owui_trust`.
- **Memory budget (role ai_stack, H2's file):** `ai_stack_mem_host_services` needs `forge_api: 256m` (this unit's
  `MemoryMax`); host_vars' `ai_stack_mem_overcommit_mb` may need +256 to match.

## Host checks for the Integrator
- `systemd-analyze verify /etc/systemd/system/forge-api.socket /etc/systemd/system/forge-api.service` clean;
  `systemd-analyze security forge-api` for the record; `systemctl is-enabled forge-api.socket` → `enabled` (on) or
  `disabled`; `systemctl is-enabled forge-api.service` → `static`.
- `ls -la /opt/forge-api` → `forge-api`, `forge_api`, `forge`, `library`, root-owned; `/etc/forge-api` root:rgate
  0750, `config.json` and `token` 0640 root:rgate.
- A first boot with the switch on: the socket listens on 172.20.0.1:879 before Docker is up (`FreeBind`), and
  open-webui reaches it once the stack runs.
- From a LAN host and a tailnet host: `curl -m5 http://<ai-box address>:879/` and, with a static route, to
  172.20.0.1:879 → no answer. As a non-root user on ai-box, `python3 -c 'import socket; socket.socket().bind(("172.20.0.1", 879))'`
  → `PermissionError` (also with the socket stopped).
- The converge's verify prints the six Open WebUI trust checks.

## Tests
```sh
roles/forge_api/tests/run.sh   # from anywhere; the repo's .venv if present, else PATH; no root; scratch dir not on /
# CI: TMPDIR=/dev/shm roles/forge_api/tests/run.sh
```
`test_forge_api.yml` at `-vvv --diff`, with stub handlers that count reloads and restarts, and fakes first on PATH
(`tests/bin`: `systemctl`, `systemd-run`, `sysctl`, `ip`, `ss`, `curl`, `nsenter`, `docker`, `ufw`, `journalctl`; each answers
only the exact read-only call the verify makes and fails on anything else, so `docker exec` or a UFW write would
fail the test):
- the defaults (bind 172.20.0.1 from role ai_stack's subnet, following `ai_stack_network_subnet` and
  `ai_stack_network_gateway`; open-webui's /32; the exact UFW line; the config keys equal F's example config);
- the UFW port filter on 18 sample lines (ranges, lists, address rules without a port, `limit`; deny, outbound,
  route rules and application profiles skipped);
- static rules over every task file: the `rgate` user/group tasks equal role research_gate's and ai_library_share's;
  `service.yml` installs exactly the socket and the service unit, each notifying exactly its handlers;
  the step order; **every token task `no_log`**, the generator `creates` + `O_EXCL|O_NOFOLLOW`, created `0o600`
  then group, mode and content in that order, never truncating (S-6); the journal grep reads the token only as a
  pattern file (`-f "$3"`), never on a command line (S-6);
  nothing reads the token (no slurp, file lookup, cat); UFW only read (`ufw show added`); docker only
  `inspect`; the systemd actions are exactly the socket switch, the service start/stop, the reload and the two
  restarts (switched on only); the templates;
- preflight passes switched off (no approver, no library), with an approver, and switched on against a scratch
  library (initialized with the library CLI) on the scratch dir's real mount; **59 refusals** (switch, approver
  missing/null/bad/too long/with a line break, library paths, ports (the old 8790, 1024, 0, 70000, a suffix; a host
  whose unprivileged ports start at 0 or 879, or whose sysctl can't be read), unit settings, account, bind outside the subnet
  /0.0.0.0/loopback/network address/short form, open-webui outside or on the bind, no subnet, IP allow widened to the
  /24, `any`, plus the host, loopback, empty, a deny that isn't `any`, six bad UFW lines (from any, from the /24, to
  any, another port, udp, no comment), missing sources, F's loader, and the switched-on library mount cases);
- install twice with junk and strays (a symlink too), a third run touching nothing and requesting no restart; the
  installed launcher imports only the installed copies; 4 symlinked directories refused with the victim untouched;
- the token: 43 characters + newline, 0640, in a 0750 dir; a second run keeps inode, mtime and content and requests
  no restart; a pre-existing token is kept (only its mode fixed); a symlink and a dangling symlink at its path are
  refused with nothing written; **`run.sh` then searches the whole play output for every token the run made: none
  appears**;
- the config: exactly F's keys and values, 0640, unchanged by a second run; not rendered without an approver; the
  validator (F's `check-config`) refuses 16 bad configs (extra key, missing keys, bad approver, bind 0.0.0.0, peer
  loopback/bind/CIDR, string port, relative root, token missing/world-readable/group-writable/short/symlink/empty)
  without printing a token (compared with the real token files, S-7), and switched on (`--require-library`) refuses an uninitialized library;
- **both units spelled out literally, every line in order** (`tests/units_case.yml`, Reviewer M's M-1), for the
  default network and another one, `systemd-analyze verify` with any warning failing (except one about the machine's
  own unit files); the service's `[Service]` lines are exactly the role's sandbox list (which the verify's
  `systemd-run` uses);
- **the switch**: `service.yml` itself runs through the fake systemctl, which emulates systemd on the unit files it
  installed (not-found units, `enable` refused without `[Install]`, `Requires=` pulled up on start and stopping
  dependants on stop). On, first run: exactly `enable forge-api.socket`, `start forge-api.socket`,
  `start forge-api.service`, one reload and one restart request each; on again: nothing; off: exactly
  `disable forge-api.socket`, `stop forge-api.socket` (the service stops with it); off again: nothing;
- verify.yml: a good run (the real installed forge-api runs `check-config --require-library` against the scratch
  library through the fake systemd-run, which gets every sandbox property; the real checker reads a scratch Open
  WebUI database), and **65 cases**, each refused by the right check (or passing, where nothing is wrong): socket
  listening with no service (passes), in another SubState, disabled/stopped/listening on every address, the service
  failed/restarting/replaced, check-config failing, the bind address missing/wrong prefix/on two interfaces, six
  listener states, the host getting 403/401/200 or accepted then empty (52) or reset (56), seven UFW states (an extra deny passes), open-webui stopped, the netns call 000/200/403, the
  token in the journal, no service lines, an unreadable journal, and the trust checks (a second admin, the approver
  not admin, no admin, tools/tools_import/functions on, a group granting tools, the default group granting
  tools_import as the string "false", write grants on a tool to a user, a group and `*` (read grants pass), a tool
  owned by another user, a permission and the iframe setting stored as "false" (on), falsy spellings (pass),
  iframe same-origin on by default or for one user, a symlink and a FIFO where `webui.db` should be, an unreadable
  or missing database, no data mount; with the switch off a failed check is reported, not fatal); each
  trust case fails exactly its own check, and the scratch database is byte-identical afterwards;
- `tests/test_owui_trust_check.py` (17 stdlib unittests, run by `run.sh`): usage, a missing database (not
  created), a symlink, a FIFO (no hang) and a directory refused, each missing table, bad JSON, the timeout (and the
  alarm cleared), Python `bool()` and JavaScript truthiness tables, every S-2 case, other permissions and grants
  ignored, read-only on a 0444 file, no emails in the output.
