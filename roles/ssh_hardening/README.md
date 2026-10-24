# ssh_hardening

OpenSSH hardening (docs/CONTRACTS.md §7, module A).

Order, so a bad config can never be loaded:
1. Render `/etc/ssh/sshd_config.d/10-homelab.conf` (key-only via `AuthenticationMethods publickey`, no
   passwords or keyboard-interactive, `PermitRootLogin no`, `X11Forwarding no`, `MaxAuthTries 3`,
   `AllowUsers {{ admin_user }} {{ automation_user }} {{ backup_pull_user }}`, `UsePAM yes`). The template
   is checked with `sshd -t -f` before it replaces anything.
2. Check the whole config with `sshd -t`.
3. Only then remove the stale `99-hardening.conf`.
4. Read `sshd -T` (the effective config) and assert every setting above actually applies.
5. The handler **reloads** `ssh.service` (SIGHUP; open sessions stay up). It never restarts it.

sshd keeps the first value it reads and `sshd_config` includes the drop-in directory at the top, so
`10-homelab.conf` overrides the stock `X11Forwarding yes` further down. `UsePAM yes` is pinned because
the service accounts have locked passwords, and sshd only accepts keys for locked accounts when PAM is on.
Port and listen addresses are untouched, so the `ssh.socket` generator needs no restart.

Variables: `ssh_hardening_max_auth_tries`, `ssh_hardening_allow_users`, `ssh_hardening_stale_dropins` (defaults).
