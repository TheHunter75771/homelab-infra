# ai_library_share

The AI Library share (docs/CONTRACTS.md §12.17, module L2). The gaming PC serves
`/mnt/share/ai-library` over Samba to ai-box's tailnet address only. A nightly snapshot backup
goes to `/mnt/backup/ai-library`. ai-box mounts the share at `/srv/ai-library` as `rgate`.

The role has no `main`. Use `tasks_from`:

| Entry point | Host | Does |
|---|---|---|
| `server` | gaming PC (Fedora, SELinux enforcing) | preflight, samba/testparm/rsync, library package + scripts, `Library.init` + sentinel (as the force user), ownership converge, `ailib` + password, `smb.conf` + smb drop-in (IP filter), SELinux boolean, nmb masked, smb, backup timer, verify |
| `export_credentials` | gaming PC, same play, after `server` | sets the host fact `ai_library_share_password` (no_log) |
| `client` | ai-box (Ubuntu) | preflight, cifs-utils, `rgate`, library package + probe, credentials, mount + automount units, verify as `rgate` |

## Playbook shape (Lead)

This follows `playbooks/ai-control.yml`. If the PC is off, play 1 skips it and ai-box keeps its credentials file.

```yaml
- name: AI Library share (gaming PC)
  hosts: workstations_root
  become: true
  gather_facts: false
  tasks:
    - {name: Is the gaming PC reachable, ansible.builtin.ping: {}, register: ai_library_pc_ping, ignore_unreachable: true}
    - {name: PC off, ansible.builtin.meta: end_host, when: ai_library_pc_ping.unreachable | default(false)}
    - name: Share, then export the credentials
      block:
        - {name: Share, ansible.builtin.include_role: {name: ai_library_share, tasks_from: server}}
        - {name: Export, ansible.builtin.include_role: {name: ai_library_share, tasks_from: export_credentials}}
      rescue:
        - ansible.builtin.set_fact: {ai_library_play1_failed: true, ai_library_share_password: ""}

- name: AI Library mount (ai-box)
  hosts: ai_servers
  become: true
  gather_facts: false
  tasks:
    - name: Mount
      ansible.builtin.include_role:
        name: ai_library_share
        tasks_from: client
      vars:
        ai_library_share_client_password: "{{ hostvars[groups['workstations_root'][0]].ai_library_share_password | default('') }}"

# play 3: fail the run if play 1 failed (as in ai-control.yml)
```

Prerequisite: `playbooks/gaming-pc-ai.yml` has converged with `ollama_rocm_policy_privileged_ports: [445]`.
tailscale0 is in firewalld's `trusted` zone, so without module G's policy every tailnet device reaches 445.
The server preflight refuses to run until 445 is in that list.

## Inventory

- **gaming PC** (`workstations_root`):
  - `ai_library_share_force_user` and `ai_library_share_force_group`: Noah's account, the owner of
    the ntfs3 mount (`uid=`/`gid=`). There is no default; preflight checks they own `/mnt/share`.
  - `ollama_rocm_policy_privileged_ports: [445]`, plus the existing `gaming_pc_tailnet_ip`,
    `ai_box_tailnet_ip` and the `*_ip6` forms.
- **ai-box:** the existing `tailnet_ip` and `gaming_pc_tailnet_ip`. Nothing new.

## Who can reach 445 (four layers)

smbd can't bind a point-to-point tun such as tailscale0: it skips non-broadcast interfaces, then
panics. So `smb.conf` has `bind interfaces only = no` and no `interfaces` line, and smbd listens on
every address. Four layers decide who connects:

1. **firewalld LAN zone:** no active zone except `trusted` opens 445. That means no `samba`
   service, no port or range that covers 445/tcp, and no rich rule that accepts it.
2. **Module G's policy `homelab-ai-only`:** it rejects 445/tcp from everyone but ai-box's tailnet
   address, on tailscale0 and everywhere else.
3. **The `smb.service` drop-in:**
   - `IPAddressDeny=any` and `IPAddressAllow=localhost <ai-box v4> [<ai-box v6>]`, so the kernel
     drops other peers before Samba sees them;
   - `RequiresMountsFor=/mnt/share/ai-library`.

   A changed drop-in restarts smb.
4. **Samba's own access check:** `hosts allow = <ai-box>`, `hosts deny = ALL`.

The server verify reads the live state of each layer:
- `systemctl show smb.service -p IPAddressDeny -p IPAddressAllow`;
- `firewall-cmd --policy=homelab-ai-only --list-rich-rules`, which must hold G's exact 445 rules;
- the services, ports and rich rules of every active zone except `trusted`;
- `ss`, where 445 must be on the wildcard addresses only;
- strict `testparm` on the installed file;
- the SELinux boolean, and no AVC for `smbd_t`.

## Mount safety

- Both folders must be exactly `<mount>/ai-library`: `/mnt/share/ai-library` and
  `/mnt/backup/ai-library`. A path with `.` or `..` is refused. Preflight and the backup script
  both enforce this; the script also compares realpaths, so a symlink anywhere is refused.
- Nothing touches the library unless `/mnt/share` is really mounted (`findmnt --mountpoint`, fs
  type `ntfs3`). The same holds for `/mnt/backup` (`ext4`).
- **Ownership.** This ntfs3 mount (`acl`) stores real owners, so a folder created by root is
  `root:root 0755`, and Samba, which writes as the force user, is denied. That happened on the
  first live run, so:
  - `Library.init` and the sentinel run as the force user and group (`runuser -u <user> -g <group> --`);
  - every converge, **before** `library.yml`, chowns anything under the folder that the force
    user:group doesn't own. Running first means a root-owned library without its sentinel can
    still get one.
    - `find -xdev ( -fstype ntfs3 )`, which never follows links, leaves the drive, or touches
      another filesystem type.
    - `-execdir chown -h --no-dereference U:G {} +` runs chown from inside each entry's own
      directory on `./name`. A directory swapped for a symlink after `find` listed it therefore
      can't redirect chown outside the library (Reviewer G's race, tested).
    - The task reports changed only when it fixed something.
  - **Residuals, root-only:** both need root on the PC, who could chown anything anyway.
    - A **bind mount of the same drive** inside the library has the same device and fs type, so
      `find` descends into it and chowns what it exposes.
    - A **different ntfs3 filesystem mounted inside** the library: `-xdev` keeps `find` out of it,
      but its root directory (the mountpoint itself) matches `-fstype ntfs3` and can be chowned.
  - The server verify then writes, reads and deletes a probe file in `00-inbox/` as the force
    user, before the client half runs.
- The library is created with `Library.init` only when the folder is absent or empty. An existing
  folder must open as a library; otherwise the role refuses. A converge never re-inits or
  re-indexes it.
- The library root carries `.ai-library-sentinel`. The backup and the client verify refuse a
  library without it, and research_gate's preflight checks it too. `library check` exempts it
  (module L).

## Password

- 48 hex characters from `secrets.token_hex(24)`, written by Python on the host straight into
  `/etc/ai-library-share/ailib-password` (root 0600, created with `O_EXCL|O_NOFOLLOW`).
- A second Python reads that file and pipes it to `smbpasswd -a -s ailib` on stdin. The password
  never appears in argv or in a registered result.
- smbpasswd runs only when the file was just generated or `pdbedit -L -u ailib` can't find the
  account. A converge never rotates the password.
- **Rotation:** delete the file and converge both plays. The client's changed credentials file
  triggers `systemctl try-restart` on the mount.

## Backup and restore

`ai-library-backup.timer` runs nightly at 03:30 with a random delay of up to 45 minutes and
`Persistent=true`. The service runs `ai-library-backup.py` as root:

1. Refuse (exit 2) before anything in the backup changes, unless all of these hold:
   - both folders are exactly `<mount>/ai-library`;
   - both drives are mounted, with the expected fs types, and each folder is on its drive. The
     "on its drive" check compares devices (`st_dev`), because the unit's
     `ReadWritePaths=`/`ReadOnlyPaths=` bind-mount a folder onto itself and `findmnt` would name
     it as its own mountpoint. A different filesystem mounted at the folder is still refused.
     When the folder is itself a mountpoint, it must be a **self-bind**: its `FSROOT` must equal
     the drive mount's `FSROOT` plus `/ai-library`. Root binding a sibling folder of the same drive
     (e.g. `restic/`) onto it is refused (Reviewer G round 5);
   - none of `next`, `ready`, `current`, `prev` or `prev.old` is a symlink or a file;
   - the sentinel is present, and `Library.open` succeeds;
   - no other run holds the lock.
2. Finish any rotation a crash interrupted (step 4), then delete a leftover `next/`, which is
   always an unfinished rsync.
3. `rsync -a -x --no-links --no-specials --no-devices --delete-after --numeric-ids --link-dest=current`
   into `next/`.
   - Any exit other than 0 or 24 (vanished files) fails the run, exit 23 included.
   - So does a snapshot without the sentinel.
   - A failed run exits 1, and `current/` and `prev/` stay as they were.

   On success, `next` is renamed to `ready`.
4. **Rotation,** one rename at a time: `prev → prev.old`, `current → prev`, `ready → current`,
   then delete `prev.old`.
   - `prev.old` is deleted only after `ready` has become `current`, and every step can be re-run,
     so a crash at any point never loses `prev`.
   - The tests simulate a crash at each step.

To restore from the run before, stop the timer and copy `prev/` back:

```
rsync -a --no-links /mnt/backup/ai-library/prev/ /mnt/share/ai-library/
```

## Client mount

- `srv-ai\x2dlibrary.mount` takes `credentials=/etc/ai-library/credentials` and these options:
  `vers=3.1.1,seal,nounix,serverino,uid=rgate,gid=rgate,file_mode=0660,dir_mode=0770,nosuid,nodev,noexec,soft,echo_interval=15,_netdev,symlink=none`.
  - `serverino` is spelled out because the library's inbox recovery needs stable inode numbers.
  - `symlink=none` is there because Samba 4.24 stores client-made SMB3 symlinks as reparse points,
    which the kernel would show as symlinks.
  - The fstab-only `x-systemd.automount,x-systemd.idle-timeout=0` become
    `srv-ai\x2dlibrary.automount` with `TimeoutIdleSec=0`.
- `ai_library_share_mount_options_extra` adds options, but preflight refuses:
  - anything that undoes the contract (`noserverino`, `mfsymlinks`, `unix`, `posix`, ...);
  - a `symlink=` value other than `none`;
  - any `reparse=`.
- **Verify as rgate:**
  - checks the sentinel and runs `Library.open`;
  - writes, reads and deletes a probe file in `00-inbox/`;
  - tries to create a symlink, which must fail;
  - checks the live options in `findmnt`.

## Tests

`roles/ai_library_share/tests/run.sh` runs as an unprivileged local user with no root, and applies nothing to this machine:

- `check_no_log.py`;
- the backup unit tests (`test_backup.py`), run under `unshare -rm` with tmpfs mounts as the
  drives;
- the `-vvv --diff` playbook, followed by the password leak grep (text and base64).

The playbook uses two real mounts as stand-ins: `/run/user/<uid>` for the share drive and `/tmp`
for the backup drive. The two test folders have the same unique name, so each is exactly
`<mount>/<name>`. Stub programs stand in for `systemctl`, `firewall-cmd`, `ss`, `findmnt`,
`ausearch` and the Samba tools.
