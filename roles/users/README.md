# users

Accounts and groups (docs/CONTRACTS.md §7, module A).

- Asserts `automation_user` exists (`playbooks/bootstrap.yml` creates and owns it).
- Removes stale sudoers drop-ins listed in `users_stale_sudoers` (`homepage-actions`, from the retired
  homepage container). `10-homelab-automation` (bootstrap) and `README` are never touched.
- Groups `mediasvc` (`mediasvc_gid`) and `media` (`media_gid`) with fixed GIDs; `render` and `docker`
  exist (system groups, created only if missing).
- `mediasvc` (`mediasvc_uid`, nologin, no home) is in `media` and `render`, matching existing media files.
- `bootgate` exists as a plain system group (no fixed GID; role `bootgate` ensures the same), and
  `admin_user` is added to `media`, `docker` and `bootgate` (`append`, so other groups are kept). The
  `bootgate` membership lets the admin trigger a gate re-run via `/run/bootgate/rerun` without sudo.
- `backup_pull_user`: system user and group, home `/var/lib/backuppull` (0750), `/bin/sh` (sshd runs the
  forced command through the shell), locked password. Its only authorized key is `backup_pull_pubkey` with
  `restrict,from="{{ gaming_pc_lan_ip }}",command="/usr/bin/rrsync -ro {{ homelab_backups_dir }}"`.
  The role fails if rrsync is missing. Role `backups` owns `homelab_backups_dir` and its permissions.

## Hosts without media (docs/CONTRACTS.md §12.2)
Everything from the media groups through the backup pull key runs only when `users_media_accounts`
is true (the default, so the media server is unchanged). With `users_media_accounts: false` (group
`ai_servers`) the role only asserts `automation_user`, removes the stale sudoers drop-ins, and adds
`admin_user` to `docker` **if that group already exists**. This role never creates the group there:
Docker is installed later in the play, and role `docker`'s install path (`roles/docker/tasks/install.yml`)
creates the group and adds `admin_user` in that same run. So the next run finds the membership in place
and is `changed=0`; this role's docker step only matters if the membership is ever removed.

Variables: `admin_user`, `automation_user`, `backup_pull_user`, `mediasvc_*`, `media_*`,
`homelab_backups_dir` (group_vars); `gaming_pc_lan_ip`, `backup_pull_pubkey` (host_vars);
`users_media_accounts`, `users_backup_pull_home`, `users_rrsync_path`, `users_stale_sudoers` (defaults).
