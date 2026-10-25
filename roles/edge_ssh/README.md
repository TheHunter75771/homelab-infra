# Role `edge_ssh`

SSH hardening and fail2ban for the edge VPS, applied by `playbooks/edge.yml` after `heartbeat`.
The VPS also runs a finance app, a school dashboard and an nftables game relay. This role touches
nothing of theirs. It writes only:

- `/etc/ssh/sshd_config.d/10-homelab-edge.conf`, then reloads `ssh.service`;
- the `fail2ban` package (no recommends), `/etc/fail2ban/jail.d/homelab-sshd.local` and
  `/etc/fail2ban/fail2ban.d/homelab.local`.

It never runs `nft -f`, and never flushes or edits existing firewall rules or tables.

## sshd

sshd keeps the **first** value it reads for a keyword. On Ubuntu 24.04, `/etc/ssh/sshd_config`
includes `sshd_config.d/*.conf` in name order on line 12, before its own settings. So `10-` wins
over `50-cloud-init.conf` and over the main file. The role checks this before writing anything:
the Include must come before any line that sets one of our keywords, and no drop-in may sort
before ours.

| Setting | Value | Why |
|---|---|---|
| `PermitRootLogin` | `prohibit-password` | Same meaning as today (cloud-init sets it). Ansible logs in as root, by key only. `sshd -T` on OpenSSH 9.6 prints it as `without-password`. |
| `PasswordAuthentication` | `no` | Keys only. |
| `KbdInteractiveAuthentication` | `no` | Closes the PAM keyboard-interactive path to passwords. |
| `PubkeyAuthentication` | `yes` | Stated explicitly. |
| `X11Forwarding` | `no` | Nothing on a headless VPS needs it. |
| `AllowAgentForwarding` | `no` | A compromised VPS can't use a forwarded agent to reach other hosts. |
| `MaxAuthTries` | `3` | Cuts brute-force connections sooner (was 6). Every key a client offers counts, so a client with several agent keys should use `IdentitiesOnly=yes` (see Lockout). |
| `LoginGraceTime` | `30` | Unauthenticated connections are dropped after 30 s (was 120). |
| `ClientAliveInterval` / `ClientAliveCountMax` | `300` / `2` | Dead sessions are closed after about 10 min. Idle but live sessions answer the keepalive and stay open. |
| `LogLevel` | `VERBOSE` | Logs which key logged in (its fingerprint) and every failed key, for audit and fail2ban. |

**Applying it.** The template is checked on its own with `sshd -t -f`. Then the full
configuration is checked with `sshd -t`, and every value is asserted from `sshd -T`. If anything
fails, the previous drop-in is put back (or the new one removed) and sshd is **not** reloaded.

`ssh.service` is then **reloaded, not restarted**. On this VPS, `ssh.socket` is enabled and
socket-activated with `Accept=no`, so `ssh.service` stays up as the listener. Its `ExecReload`
runs `sshd -t` and then sends SIGHUP; open sessions are untouched. Changing `Port` or
`ListenAddress` would need the socket instead (`systemctl daemon-reload && systemctl restart
ssh.socket`); this role sets neither. Finally the role drops Ansible's persistent connection and
runs `ping`, which proves a brand-new login works under the new settings.

## fail2ban

The sshd jail lives in `jail.d/homelab-sshd.local`. Being a `.local` file, it's read after
`jail.conf` and the packaged `jail.d/*.conf`, so its values win:

- `backend = systemd`. The filter matches `_COMM=sshd`, which is how this VPS's sshd logs
  (unit `ssh.service`).
- `maxretry 5` within `findtime 10m` gives a `bantime 1h` ban. Repeat offenders get longer bans
  (`bantime.increment`), up to `bantime.maxtime 1w`.
- `fail2ban.d/homelab.local` sets `dbpurgeage = 8d`. fail2ban's default of 1 day would forget
  repeat offenders before the increments could reach 1 week.
- `ignoreip = 127.0.0.0/8 ::1 100.64.0.0/10 fd7a:115c:a1e0::/48`: loopback and the whole
  tailnet, our only admin path, are never banned. The values are written in canonical form
  because that's how fail2ban prints them back, so the deploy-time assert can compare exactly.
- Failed public-key offers are counted only when the connection doesn't end in a login (the
  filter's default `publickey = nofail`). The workstation's backup jobs, which log in over the
  public IP after offering one wrong agent key, are not at risk.

The jail file is written **before** the package is installed. The package starts fail2ban on
install, so it starts with our jail and never with packaged defaults.

**Ban action: `nftables`.** This VPS's firewall is nftables: `nftables.service` loads
`/etc/nftables.conf`, which holds the tables `inet filter` and the relay's NAT table. `iptables`
is the nft backend with empty chains, and ufw is inactive. fail2ban's `nftables` action
(`action.d/nftables.conf`, 1.0.2) addresses every command to its own table, `inet f2b-table`:

- `actionstart`: `nft add table inet f2b-table`, `add chain inet f2b-table f2b-chain {type filter
  hook input priority -1}`, `add set` and `add rule` in that table;
- `actionban` / `actionunban`: add or delete an element of its own set;
- `actionflush`: `flush set inet f2b-table <set>` (its own set, never a ruleset or another table);
- `actionstop`: deletes its own rules and set, then its own table only if it has no sets left.

There is no `flush ruleset` and no reference to another table. The chain's priority -1 runs just
before the existing `inet filter` input chain (priority 0), and only rejects banned addresses on
port 22. The relay's ports are untouched. The `iptables-*` actions were not chosen: they would
insert jump rules into the shared `INPUT` chain and create `ip filter`/`ip6 filter` tables.

**Caveat.** `/etc/nftables.conf` starts with `flush ruleset`. Anything that reloads or stops
`nftables.service` wipes `f2b-table`, and the current bans with it. fail2ban notices on its next
ban: the action's `actioncheck` fails and fail2ban rebuilds its table. To restore bans at once,
run `systemctl restart fail2ban` after reloading nftables.

**Daily use:** `fail2ban-client status sshd` (bans), `fail2ban-client set sshd unbanip <ip>`
(unban), `nft list table inet f2b-table` (what's enforced).

## Lockout

Deploy only over the tailnet (fail2ban never bans it), with a second root session already open
as a fallback. `MaxAuthTries 3` counts every key the client offers. Today the workstation offers
one wrong agent key before the right one, so it logs in on the second try. Set
`IdentitiesOnly=yes` with an explicit key for this host (Ansible `ansible_ssh_common_args` /
`ansible_ssh_private_key_file`, and this host's entry in the workstation's `~/.ssh/config`) so that adding
agent keys later can't lock Ansible or the backup jobs out.

## Considered, not done

- **SSH reachable only over the tailnet** (no public port 22). This is the biggest win against
  the roughly 1,400 bot attempts a day, but it needs firewall changes on a box whose firewall
  belongs to other services. It would also lock everyone out if Tailscale breaks, since the
  public IP is the only way in then (plus the provider's console). Revisit with a tested
  console-access fallback.
- **Another port.** Cuts log noise only; it's no security boundary, and on this VPS the port is
  owned by `ssh.socket`, so the change would mean socket and firewall edits.
- **`AllowTcpForwarding no`.** Left as it is (`yes`): unknown tooling may tunnel through this host
  (the workstation's jobs, ad-hoc port forwards). It's worth revisiting once anything that
  depends on it is known.
