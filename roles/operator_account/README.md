# operator_account

The AI accounts (docs/CONTRACTS.md §12.13 and its amendment). They are two separate accounts, built from one validated
list, `operator_accounts`:

| | `claude` (Claude, the operator) | `localai` (Noah's local AI) |
|---|---|---|
| Shell | `/bin/bash` | `/usr/sbin/nologin` |
| Password | locked (`!`) | locked (`!`) |
| sudo | `full_logged`: `ALL` NOPASSWD, every session I/O-logged | `none`: no sudoers rule (asserted with `sudo -l -U`) |
| SSH | one key, `from=` the gaming PC (servers) or `127.0.0.1,::1` (gaming PC); key files root-owned | no `authorized_keys`; on the gaming PC sshd denies it outright |
| Home | `/home/claude` (0700) | `/var/lib/localai` (0700) |
| Groups | none (plus `operator-no-sudo-rs` where sudo-rs exists, see below) | none |

Every per-account path is derived from the account name and can't be configured. Two entries can never share one:
- `/etc/sudoers.d/20-operator-<name>`
- `/var/log/sudo-io/<name>`
- `/etc/tmpfiles.d/operator-<name>.conf`
- `/etc/ssh/sshd_config.d/20-operator-<name>.conf`

## Inputs

| Variable | Where | Meaning |
|---|---|---|
| `operator_pubkey` | host_vars / group_vars | `claude`'s public key (private key: gaming PC `~hunter/.ssh/claude_ops_ed25519`). The role fails if it's missing while `operator_ssh_login` is true. |
| `operator_key_from` | group_vars | List of allowed source addresses. Servers: the gaming PC's tailnet and LAN IPs. Gaming PC: `["127.0.0.1", "::1"]`. |
| `operator_key_from_extra` | group_vars | Appended to `operator_key_from` (e.g. the gaming PC's tailnet IPv6). Default `[]`. |
| `operator_sudo` | inventory | `full_logged` (default) or `none`: `claude`'s sudo. The revoke switch. |
| `operator_ssh_login` | inventory | `true` (default) or `false`: `claude`'s key login. When `false`, no key is needed. |
| `operator_manage_sshd_match` | group_vars (workstations) | `true` on the gaming PC: per-account sshd drop-ins (claude loopback-only, or denied when `operator_ssh_login: false`; localai always denied). |
| `operator_user` | group_vars/all (Lead) | `claude`'s name (default `claude`). |
| `operator_accounts` | defaults | The list. Keys: `name`, `shell`, `home`, `sudo` (`none`\|`full_logged`), `ssh_login`, `ssh_pubkey`, `ssh_from`, `sshd_loopback_only`, `sshd_deny`, `groups`, `comment`. |

The validation step rejects a misconfigured list before anything changes, and it reports every error at once:
- duplicate or reserved names (`root`, the admin, automation and media accounts)
- unknown keys
- sudo or `ssh_login` on a no-login shell
- `ssh_login` without a single-line key or without a non-empty, address-only `ssh_from`; a wildcard in `ssh_from`
  without negations, or a bare `*` at all ("from anywhere")
- `sshd_loopback_only` without `ssh_login`, or with a non-loopback `ssh_from`; `sshd_deny` together with either
- privileged groups (`docker`, `sudo`, `wheel`, `empower`, `adm`, `systemd-journal`, …)

At run time it also refuses:
- an entry naming an existing account with UID ≥ 1000 (a person; the role only creates system accounts)
- an entry asking for any group a polkit rule names (`unix-group:…` in `/usr/share/polkit-1/rules.d` or
  `/etc/polkit-1/rules.d`). The verify step checks actual membership against the same set, because polkit admin
  never goes through sudo's log.

## sudo and sudo-rs (read this before changing anything)

Ubuntu 26.04 has two sudo implementations: `sudo` (the `/usr/bin/sudo` alternative) is **sudo-rs**, and classic
sudo is `sudo.ws`. Ansible already becomes through `sudo.ws` (ADR 0011).

- **sudo-rs has no I/O logging.** It also reads the same `/etc/sudoers` and `/etc/sudoers.d` (unless
  `/etc/sudoers-rs` exists). So it would honour `claude ALL=(ALL) NOPASSWD: ALL` while dropping the
  `log_input`/`log_output`/`iolog_*` Defaults as "unknown setting". Through sudo-rs, `claude` would get **unlogged
  root**.
- **The role closes that path.** The guarded set is derived from the package itself. It is every setuid regular file
  that `dpkg-query -L sudo-rs` lists (including the "diverted … to:" paths), after resolving symlinks (sudo, and su or
  sudoedit if they're separate setuid files), kept only if `dpkg-query -S` attributes it to sudo-rs or its
  `--version` identifies it as sudo-rs. A package symlink pointing at another package's setuid binary is therefore
  not guarded. It also includes any of `/usr/lib/cargo/bin/sudo`, `/usr/bin/sudo-rs` and `/usr/bin/sudo` whose real
  file doesn't answer as classic sudo. The package counts as present in any dpkg state except `not-installed` and
  `config-files`. Only the members whose `--version` says `sudo-rs …` (its sudo) are asked `-l -U`; its su is
  guarded but never queried with sudo options.
  - Every `full_logged` account joins `operator-no-sudo-rs`.
  - Each guarded file gets a persistent `dpkg-statoverride` of `root:operator-no-sudo-rs 4705`. Group members can't
    execute it. Everyone else (other `r-x`) is unchanged, Noah's `sudo` included. The override survives package
    upgrades, and a new setuid file in a sudo-rs update is guarded on the next run.
  - The role fails if sudo-rs is installed but none of its setuid files is found, or if classic sudo would land in
    the guarded set. It refuses to replace a different, pre-existing override. It refuses a non-Debian host where
    sudo-rs is installed.
  - The verify step really executes, as `claude` (`runuser -u claude -- <file> --version`), every guarded file
    (the kernel must refuse it: rc 126 / "Permission denied") and classic sudo (must run and print
    `Sudo version 1.x`). It never asks `test -x`: Ubuntu 26.04's uutils `test` ignores supplementary groups.
  - **Limits (not fail-closed):** a sudo-rs that no package installed, outside the three candidate paths, isn't
    found. Neither is a copy under another package name (`operator_account_sudo_rs_packages`).
- **`claude` therefore uses `sudo.ws`** on Ubuntu (Ansible: `ansible_become_exe: /usr/bin/sudo.ws`, already set for the
  servers). On the gaming PC (Fedora) there is only classic `sudo`.
- **Side effect:** sudo-rs still parses `20-operator-claude` for everyone else and prints a diagnostic about the
  unknown Defaults on each run. The warning is harmless. Noah can switch the `sudo` alternative to `sudo.ws` to silence
  it (see the Lead notes).
- The sudoers file is checked by **classic** `visudo` (`visudo.ws` on Ubuntu). sudo-rs's visudo rejects these Defaults.

## Audit

Every sudo command is in the journal (syslog line `claude : … ; USER=root ; COMMAND=…`). On Ubuntu the process is
`sudo.ws`:

```sh
journalctl _COMM=sudo _COMM=sudo.ws --since today | grep ' claude : '
```

Full sessions (keystrokes and output, including what Ansible pipes to its modules on stdin) are in
`/var/log/sudo-io/claude`, readable by root only. They are kept for 90 days by `systemd-tmpfiles-clean`. On Ubuntu
the classic tool may be named `sudoreplay.ws`:

```sh
sudo sudoreplay -d /var/log/sudo-io/claude -l                 # list sessions (TSID = 00/00/01 …)
sudo sudoreplay -d /var/log/sudo-io/claude -l user claude command /usr/bin/apt   # filter
sudo sudoreplay -d /var/log/sudo-io/claude 00/00/2A           # replay one session
```

**Note:** I/O logs capture module arguments that Ansible marks `no_log` (they travel on stdin), so treat the
directory as secret.

`localai`'s actions are attributable by UID: `journalctl _UID=$(id -u localai)` and file ownership. Check it has no
sudo with `sudo -l -U localai` (classic; on Ubuntu `sudo.ws -l -U localai`).

## Revoke

Revoking is done in the **inventory** (host_vars or group_vars), then rerun `playbooks/operator.yml`:

| Variable | Effect on the next run |
|---|---|
| `operator_sudo: none` | `/etc/sudoers.d/20-operator-claude` is removed (`sudo -l -U claude` is then checked to grant nothing). I/O logs and their aging rule stay as evidence. |
| `operator_ssh_login: false` | `authorized_keys` is removed. `operator_pubkey` and `operator_key_from` are no longer needed. On the gaming PC the drop-in switches to denying `claude` outright. |

Revoking sudo or the key doesn't end **sessions already open**: a running SSH session (and any root shell it
started) stays until it exits. For an immediate cut: `sudo loginctl terminate-user claude` (ends every session and
process of `claude`), after removing the key/sudoers so it can't come back.

A **revoke by hand is reverted by the next run** unless the inventory changes too. Examples of hand revokes:
`sudo rm /etc/sudoers.d/20-operator-claude`, `sudo rm /home/claude/.ssh/authorized_keys`, `sudo usermod -L -e 1 claude`.
Use them for an immediate cut, then set the variables above.

Removing the account entirely: set both variables, run the play, then `userdel claude` by hand, drop `claude` from
`ssh_hardening_allow_users`, and remove the entry. Removing an entry from the list doesn't delete anything by itself.

## Logs: size and integrity

- **Size:** sudo compresses I/O logs by default. An Ansible run as `claude` logs every module's stdin and output, so
  a busy day can be tens of MB. 90 days are kept. Check with `sudo du -sh /var/log/sudo-io/claude`.
- **Integrity:** this is **tracking, not tamper-proofing.** `claude` is root through sudo, so it can delete or edit
  its own logs and the journal; only the commands that did so would show up, and only if they weren't erased too.
  The logs make honest mistakes and normal work attributable. Making them tamper-evident would need shipping them
  off the host as they're written (e.g. journal forwarding or sudo's `log_servers` to the edge VPS). That's a
  future option, not built.

## Residual risks

- `.ssh` and `authorized_keys` are root-owned, so `claude` can't edit its key without a logged sudo. But `claude`
  owns its home, so it can rename `.ssh` away and create its own. An unprivileged key swap stays possible. The
  `from=` restriction then no longer applies, but on the gaming PC sshd itself still refuses non-loopback. The full
  fix is an `AuthorizedKeysFile` outside the home (e.g. `/etc/ssh/authorized_keys/%u`), which is a change to
  `ssh_hardening` on the servers (Lead).
- `sudoedit` sessions aren't I/O-logged; they only reach syslog.
- **Upgrade window for new sudo-rs setuid files.** dpkg re-applies the existing stat overrides when it unpacks
  the same paths, so an upgraded `sudo` stays blocked. A setuid file a sudo-rs update **adds or moves** is executable
  by `claude` from the moment dpkg unpacks it until the next run of this role. That run guards it and verify proves
  it. An apt `DPkg::Post-Invoke` hook could close the window. It isn't built: it would run inside apt's own locking
  (`dpkg-statoverride` vs. the frontend lock held by apt, including unattended-upgrades). That can't be tested off
  an Ubuntu host, and a failing hook breaks every apt run. Mitigation: rerun `playbooks/operator.yml` after sudo-rs
  updates (`grep sudo-rs /var/log/apt/history.log`).

## Order and checks

1. Validate the list (also in check mode).
2. Detect classic sudo, visudo and sudo-rs.
3. Apply the sudo-rs guard.
4. Accounts: system users, locked, exact groups.
5. Per account, the sudoers file (visudo), I/O log dir (root 0700), tmpfiles rule, authorized_keys (exclusive: the
   whole file is rendered).
6. On the gaming PC, the sshd drop-ins (claude loopback-only, localai denied), each checked alone (`sshd -t -f`)
   before it's installed. Then the whole config is checked in context: `sshd -t`, and `sshd -T -C` with a
   non-loopback source (must be `denyusers claude`) and with 127.0.0.1/::1 (allowed, key-only, not shut out by
   another `AllowUsers`); denied accounts must be refused from every source. On failure the drop-ins **this run
   changed** are removed and the play fails before the reload. In check mode the step is skipped while a drop-in or
   its account isn't on the host yet. The reload handler re-runs `sshd -t` and asserts sshd is active; it never
   starts or restarts sshd.
7. Verify: classic `sudo -l -U` shows `claude`'s rule with its I/O logging and nothing for `localai`; sudo-rs (if
   present) shows nothing for `localai`; `claude` can execute classic sudo and none of the guarded sudo-rs files;
   no account is in a privileged or polkit admin group.

The drop-in's Match line is `Match User claude Address *,!127.0.0.1,!::1`. The leading `*` matters: a pattern list
made only of negations never matches, so `Address !127.0.0.1,!::1` would refuse nobody (the test proves both). A
Match block ends at the end of its included file, so the drop-in changes nothing for other users.

Test (offline, no root): `.venv/bin/ansible-playbook -i localhost, -c local roles/operator_account/tests/test_render.yml </dev/null 2>&1 | cat`
