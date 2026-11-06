# Role `ai_backup`: Open WebUI nightly backup (CONTRACTS §12.28, module H10)

Nothing else backs up ai-box's Open WebUI data. That data is every chat, model/agent config, tool, function, prompt,
knowledge collection, upload and setting. This role has two halves, which follow the §8 pattern (`backups` /
`backup_pull`) and reuse the §12.17 AI Library backup's drive checks.

| Entry point (`tasks_from`) | Host | What |
|---|---|---|
| `stage` | ai-box | The `backuppull` account, the restricted pull key, and `ai-openwebui-backup.timer` (03:45, Persistent). The timer writes `/var/backups/ai-box/owui-YYYYmmddTHHMMSSZ.tar` and `LAST_OK`, and keeps 3. |
| `export_key` | ai-box, after `stage` | Hands the pull key's private half and ai-box's ssh host key to the PC play as host facts (`no_log`). |
| `pull` | gaming PC, as root | Installs the key and a pinned `known_hosts`. Adds `ai-openwebui-pull.timer` (06:30, Persistent), which pulls into `/mnt/backup/ai-box/openwebui`, verifies, keeps 14 and checks the `LAST_OK` age. Adds `ai-openwebui-restore-check.service` (run by hand). |

A bare `include_role` without `tasks_from` fails on purpose.

The script `files/ai_backup.py` is a single Python file that uses only the standard library. It is installed on both
hosts at `/usr/local/lib/ai-backup/ai_backup.py` and run as `python3 -I -B`. Its commands are `stage`, `pull`,
`verify ARCHIVE...` and `restore-check`. Exit codes: 0 is ok, 1 is a failure, 2 is a refusal (a safety check
failed before anything was written or deleted).

## The archive

The archive is a plain tar (no compression), root:backuppull 0640 on ai-box and root 0600 on the PC. Its members,
in this order:

| Member | Content |
|---|---|
| `sqlite/webui.db` | An online copy made with SQLite's backup API. The source connection is opened **read-only** (`mode=ro`) from the **host** volume path; nothing goes through the container or `docker exec`. The copy must pass `PRAGMA integrity_check`. |
| `sqlite/vector_db/chroma.sqlite3` | The same, for Chroma's database. |
| `uploads.tar` | `uploads/`. |
| `vector_db.tar` | `vector_db/` without `chroma.sqlite3` and its `-journal`/`-wal`/`-shm`, so it holds the HNSW index folders. |
| `stack.env` | The ai stack's `.env` (`WEBUI_SECRET_KEY`), 0600. Sessions and encrypted fields survive a restore only with it. |
| `MANIFEST.sha256` | The sha256 of every member above. It is **always the last member**, so a copy cut short at a member boundary (which `tar -tf` accepts) has no manifest and is rejected. |

**Never included:**
- `cache/` (about 1.1 GB of model downloads);
- `*.bak-*` files, anywhere;
- symlinks (neither followed nor stored);
- special files, and anything on another filesystem.

The run names any unknown top-level entry of the volume in its log, so the journal shows when Open WebUI starts
keeping data somewhere new. The script backs up only what it knows (an allowlist).

**How the databases are copied:**
- `webui.db` is in WAL mode. Its copy runs as one read transaction, so Open WebUI keeps writing throughout.
- `chroma.sqlite3` uses a rollback journal. It's copied in steps, so a writer gets the lock between steps.
- A WAL database that no connection has open has no `-wal`/`-shm`, and a read-only connection can't create them. In
  that case the script opens it `immutable=1`. It compares the file's inode, size and mtime before and after the
  copy, and retries if a writer appeared.

**On failure:** each run reads its own archive back and verifies it before publishing it (rename into place). Only
then does it replace `LAST_OK` and rotate. A failed run publishes nothing, keeps `LAST_OK` as it was and fails the
unit.

## The pull, and its safety checks

ai-box isn't trusted to behave (Reviewer X): a compromised or mis-clocked ai-box must not be able to push genuine
copies out, or fill a drive that also holds restic, the AI Library and a LUKS header image. So before fetching
anything, the pull lists what ai-box offers (`rsync --list-only`, the same filters) and **refuses** (exit 2, nothing
fetched) when:
- the listing holds more than `ai_backup_pull_max_remote` archives (default: ai-box's keep + 2 = 5);
- any archive in it is over `ai_backup_pull_max_size` (8G);
- the drive would keep less than `ai_backup_pull_min_free_mb` (20 GiB) free after the transfer (statvfs).

These limits trust the sizes ai-box's rsync reports in its listing, and rsync's own `--max-size` on the fetch trusts
them too. A hostile ai-box could under-report. These are the backstops that don't depend on ai-box:
- **`LimitFSIZE=8G` on the pull unit.** The kernel stops any file the pull writes at the archive size limit. The
  transfer fails and nothing is promoted.
- **A recount after the fetch.** More files than `--max-remote` in staging promotes none and fails.
- **Cleanup of rsync's partial temp files.** They're removed from staging at the start of every run.
- **The free-space floor.** It's checked again before every run, so a drive that is filling refuses loudly.

Then it fetches. The fetch uses `rsync -rtp --delete-after --no-links --no-specials --no-devices --max-size=8G`
over `ssh -F none -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes` with a pinned
`known_hosts`. It copies only `owui-*.tar` and `LAST_OK`, into `<dest>/.incoming` (a mirror of ai-box's 3 archives).
Then:
1. **Future-dated names are rejected.** A name dated more than `ai_backup_pull_max_future_minutes` (60) ahead of the
   PC's clock never gets in. Retention orders by name, so `owui-2099…` would outrank every genuine copy. The unit
   fails.
2. **Too many new archives promotes none.** If more than `ai_backup_pull_max_new` (5) archives are new in one run, none
   is promoted and the unit fails. A normal day brings 1; a week with the PC off brings at most ai-box's 3.
3. **Every new archive is verified.** Its manifest is checked: regular-file members only, the expected names,
   `MANIFEST.sha256` last, every hash right.
4. **Only good archives are kept.** A good archive is hard-linked into `<dest>`. A bad one is deleted from staging, so
   it's fetched again next run, and the unit fails.
5. **`LAST_OK` and `.last_pull` move only when the newest archive verified.**
6. **Stale `LAST_OK` fails the unit.** That covers a missing `LAST_OK`, one that isn't a timestamp, one more than 1 h
   in the future, or one older than 48 h.
7. **Retention comes last, and only after a fully clean run (Reviewer X-1b).** A clean run means rsync exit 0,
   nothing rejected, no future-dated archive in `<dest>`, and a fresh `LAST_OK`. A failing run never deletes
   anything, so `Restart=on-failure` can't repeat a deletion.
8. **Retention deletes only archives older than 14 days.** It deletes beyond the newest 14, but only archives whose
   names are dated more than 14 days ago. It never touches the newest archive or other files. A flood of fresh or
   near-future names from ai-box can't push the real history out: it piles up until the free-space floor refuses,
   loudly.

**Retries (X-8).** A failure (exit 1) is retried after 5 min, at most 4 starts in 2 h. That covers a boot catch-up
that runs before ai-box or the tailnet answers. A refusal (exit 2: the drive, the listing's bounds) is never retried
(`RestartPreventExitStatus=2`).

The pull **refuses** (exit 2, nothing fetched or deleted) unless all of these hold. These are the §12.17 checks,
plus the queued stacked-mount fix:
- `<dest>` is exactly `/mnt/backup/ai-box/openwebui`: no `..` and no symlink anywhere (realpath equality).
- `/mnt/backup` is a mountpoint (`findmnt`) of type ext4.
- Every mount stacked at `/mnt/backup` has the same SOURCE and FSROOT, so a sibling folder bound over the drive path
  is refused.
- `<dest>`, and each folder between it and the mount, is on the drive's device (`st_dev`).
- A mount *at* one of those folders is allowed only as a self-bind (its FSROOT is that same folder of the drive).
  `ReadWritePaths=` creates exactly that kind of self-bind.
- `/mnt/backup/ai-box` and `/mnt/backup/ai-box/openwebui` are real directories, owned by root and not
  group/other-writable (X-7). `/mnt/backup` itself belongs to the desktop user, who could plant a folder there. A
  missing folder is created root 0700, but only after the drive checked out. The play makes the same check and refuses
  rather than adopting a folder someone else made.
- Another pull isn't running (`flock`).

## Why a root play on the PC (and not a user timer as in §8)

`/mnt/backup` is owned by `hunter:hunter 0755` (checked read-only on 2026-10-28), so either choice could work. Root
is the better one, for five reasons:
1. **The data is the most sensitive on the box.** The archives hold every chat (a family member's soon too), every
   upload and `WEBUI_SECRET_KEY`. As root they're `root 0700/0600`, beside `/mnt/backup/ai-library` (already
   `root 0700`). A user timer would leave them, and the pull key, readable by every process in Noah's desktop
   session (browser, games, Flatpaks).
2. **The sandbox only works for system units.** `IPAddressDeny=any` + `IPAddressAllow=<ai-box>` is enforced for
   system services only (Integrator finding, §12.17). So are the mount-namespace settings (`ProtectSystem=strict`,
   `ReadWritePaths=` limited to the destination).
3. **It runs without a login.** A system timer with `Persistent=true` catches up at boot. A user timer needs an
   active session or lingering.
4. **It matches what's already there.** The AI Library backup on the same drive is a root unit with the same checks.
5. **Cost:** the play needs root on the PC. That's the same constraint as `gaming-pc-ai.yml` and `ai-library.yml`
   (Noah present).

The unit runs as root with an **empty** capability bounding set: root owns everything it touches, so it needs no
capabilities.

## The key

- **On ai-box:** `ssh-keygen -q -t ed25519` runs once into `/etc/ai-backup/pull_ed25519` (root 0600). Every task that
  can see the private half is `no_log`, and a static check proves it (`tests/check_no_log.py`). Half a key pair (one
  of the two files) is refused, never overwritten. To rotate, delete both files and converge.
- **The public half is `backuppull`'s only key** (`exclusive`):
  `restrict,from="<PC tailnet v4>[,<v6>]",command="/usr/bin/rrsync -ro /var/backups/ai-box" ssh-ed25519 …`.
  `restrict` means no pty, no forwarding of any kind, no X11 and no `~/.ssh/rc`. `rrsync -ro` limits the account to
  read-only rsync of the staging dir.
- **ai-box keeps the private half,** so it can hand it over on each converge. It grants nothing that root on ai-box
  doesn't already have (a read-only copy of ai-box's own backups).
- **Handover:** `export_key` slurps it (`no_log`) into a host fact. The PC play writes it to
  `/etc/ai-backup-pull/id_ed25519` (root 0600, `no_log`, `diff: false`). It also writes `known_hosts` as
  `<ai-box tailnet IP> ssh-ed25519 …`, without the host key's comment. If ai-box wasn't in the run, the PC keeps its
  files (preflight requires them to exist).

## Wiring (Lead)

**Playbook.** The key moves through a cross-play fact, so both halves go in one playbook, in this order. I propose a
new `playbooks/ai-backup.yml`; the scratch copy passes `--syntax-check` and ansible-lint production:

```yaml
- name: Open WebUI backup (ai-box stage)
  hosts: ai_servers
  become: true
  gather_facts: false
  tasks:
    - {name: Stage, ansible.builtin.include_role: {name: ai_backup, tasks_from: stage}}
    - {name: Export the pull key for play 2, ansible.builtin.include_role: {name: ai_backup, tasks_from: export_key}}

- name: Open WebUI backup (gaming PC pull)
  hosts: workstations_root
  become: true
  gather_facts: false
  tasks:
    - name: Pull
      ansible.builtin.include_role: {name: ai_backup, tasks_from: pull}
      vars:
        ai_backup_pull_private_key: "{{ hostvars[groups['ai_servers'][0]].ai_backup_pull_private_key | default('') }}"
        ai_backup_pull_host_pubkey: "{{ hostvars[groups['ai_servers'][0]].ai_backup_host_pubkey | default('') }}"
```

The plays could also be appended to `gaming-pc-ai.yml`, but that playbook is PC-only today.

**Inventory:**
- **`ssh_hardening_allow_users` for `ai_servers` must add `backuppull`** (module A's role renders `AllowUsers`). The
  stage's verify fails with this message until it's done. `ssh_hardening` must then converge on ai-box.
- `group_vars/ai_servers/main.yml` says "§12.2: no media or backup-pull accounts on this host". That's still true of
  role `users`; this role creates `backuppull` on ai-box for §12.28 only. The comment needs an amendment.
- **ai-box host_vars:** `gaming_pc_tailnet_ip` (exists), `gaming_pc_tailnet_ip6` (exists; optional, added to
  `from=`).
- **PC host_vars:** `ai_box_tailnet_ip` (exists for L2). It must be an IPv4 literal: it's the rsync target and the
  unit's `IPAddressAllow`.
- ai-box's UFW already allows 22/tcp on tailscale0, so no firewall change is needed.

**Memory budget.** `ai-openwebui-backup.service` has `MemoryMax=256M` (`ai_backup_memory_max`). It's a nightly
oneshot, but `ai_stack_mem_host_services` lists every capped host service. Add `ai_backup: 256m` there, or decide
that oneshots are exempt.

**AI Control.** The contract says a failure is "visible in AI Control". The unit fails properly. If AI Control shows
units from a list (helper `log_units`), add `ai-openwebui-backup.service` to it.

## Restore

The checked way first. Run it once now (the Integrator does), and before any real restore:

```sh
sudo systemctl start ai-openwebui-restore-check.service
journalctl -u ai-openwebui-restore-check.service -n 20
```

It verifies the newest archive's manifest (or the one named with `--archive`) and extracts it to a private temp dir on
the backup drive. It runs `PRAGMA integrity_check` on both databases and reports the user/chat/knowledge and
collection/embedding counts. It reads every member of both inner tars and **refuses unsafe ones**: absolute names,
`..`, names outside `uploads/` or `vector_db/`, or anything but files and folders. It checks that `stack.env` has
`WEBUI_SECRET_KEY` (the value is never printed), then deletes the temp dir.

**Full restore, on ai-box, as root.** `V=/var/lib/docker/volumes/ai_open-webui-data/_data`. Use the newest good
archive from `/var/backups/ai-box/` or from the PC's `/mnt/backup/ai-box/openwebui/` (copy it over the tailnet).

1. **Check exactly this archive, the same way restore-check does,** before anything is extracted as root (X-6). That
   covers the manifest, both databases' `integrity_check`, and the inner tars' names (nothing absolute, no `..`,
   nothing outside `uploads/` or `vector_db/`, no links). Put the archive in a root-only folder, then:
   ```sh
   python3 -I -B /usr/local/lib/ai-backup/ai_backup.py restore-check --dir <folder> --archive owui-<stamp>.tar
   ```
   Go on only if it ends with `restore-check ok: owui-<stamp>.tar`.
2. **Stop Open WebUI only:**
   `cd /srv/homelab/stacks/ai && docker compose stop open-webui`
3. **Unpack the archive privately:**
   `install -d -m 0700 /root/owui-restore && tar -xf owui-<stamp>.tar -C /root/owui-restore`
4. **Move the current data aside** (don't delete it until the restore is proven):
   `mkdir -m 0700 $V.before-restore && mv $V/webui.db* $V/uploads $V/vector_db $V.before-restore/`
5. **Put the backup in place:**
   ```sh
   tar -xpf /root/owui-restore/uploads.tar -C $V
   tar -xpf /root/owui-restore/vector_db.tar -C $V
   cp -p /root/owui-restore/sqlite/webui.db $V/webui.db
   cp -p /root/owui-restore/sqlite/vector_db/chroma.sqlite3 $V/vector_db/chroma.sqlite3
   ```
   No `-wal`/`-shm` files may be left beside the restored databases.
6. **Secret key:**
   - If ai-box was rebuilt, install `stack.env` as `/srv/homelab/stacks/ai/.env` (root 0600) **before** role
     `ai_stack` runs. It only generates a new key when the file is missing.
   - If ai-box wasn't rebuilt, compare it with the live `.env` (`cmp`).

   With a different key, everyone is logged out and encrypted settings can't be read.
7. **Start Open WebUI:**
   `docker compose up -d open-webui`. Log in, then check chats, models, tools and Knowledge.
8. **If a Knowledge search returns nothing or errors:** the HNSW index files were copied while Chroma may have been
   writing them. Run *Admin Panel > Settings > Documents > Reindex Knowledge Base Vectors*. The database rows
   (`chroma.sqlite3`) are a consistent copy; only the index files might not be.
9. **Re-downloads:** `cache/` isn't backed up. Open WebUI downloads its embedding/whisper models again on first use,
   which needs internet.
10. **Clean up:** once it's all good, delete `/root/owui-restore` and `$V.before-restore`.

## Variables

The defaults are in `defaults/main.yml`; the contract's fixed paths and the sandboxes are in `vars/main.yml`.
Commonly set:
- `ai_backup_stage_keep` (3)
- `ai_backup_pull_keep` (14)
- `ai_backup_pull_max_age_hours` (48)
- the pull's bounds: `ai_backup_pull_max_remote` / `ai_backup_pull_max_new` (ai-box keep + 2),
  `ai_backup_pull_max_size` (8G), `ai_backup_pull_min_free_mb` (20480), `ai_backup_pull_max_future_minutes` (60)
- the retries: `ai_backup_pull_restart_sec` (5min), `ai_backup_pull_start_burst` (4) per
  `ai_backup_pull_start_interval` (2h)
- `ai_backup_stage_calendar` / `ai_backup_pull_calendar`
- `ai_backup_memory_max`
- `ai_backup_volume_dir`, `ai_backup_env_file`, `ai_backup_pull_dest_mount` / `ai_backup_pull_subdir`, in case Open
  WebUI's volume or the backup drive ever moves.

## Tests

`roles/ai_backup/tests/run.sh` needs no root and applies nothing to this machine. It needs `unshare -rm`, `rsync`,
`ssh-keygen`, `systemd-analyze`, and a tmpfs `/tmp`. It runs three things:

1. **`check_no_log.py`:** every task that touches the private key is `no_log`.
2. **`test_script.py`** (65 tests, under `unshare -rm`). The fake volume is bind-mounted read-only, as under the unit,
   and the "backup drives" are tmpfs mounts. It covers:
   - a live WAL database written to during the copy, and an idle WAL database on a read-only mount;
   - a corrupt database and a non-database: no archive, `LAST_OK` kept;
   - a missing volume, `uploads/` or database;
   - symlinks (the volume, `uploads/`, a database, the `.env`, entries inside `uploads/`);
   - the exclusions;
   - manifest verification (manifest first, cut short at a boundary and mid-member, a flipped byte, missing, extra
     or unlisted members, a link member);
   - retention on both sides;
   - another filesystem inside `uploads/`, the stage's read-back verify, a writer opening an idle WAL database
     mid-copy;
   - the pull through a fake ssh/rrsync and a recording rsync. That includes:
     - every backup-drive refusal, including the chain's owner and mode, and creating a missing chain 0700;
     - future-dated names (the genuine copies survive), slight clock skew, a future-dated archive already kept;
     - the reviewer's E4 sequence: three failing flood runs back to back, 5 near-future archives each. Then three
       successful flood runs. Both keep all 14 genuine copies;
     - retention skipped after rsync exit 24;
     - an oversized `LAST_OK`;
     - the promotion cap, the remote count, `--max-size`, and the free-space floor;
     - rejected archives;
     - a stale, missing, garbled or future `LAST_OK`, and an already-kept newest still moving `LAST_OK`;
     - an rsync failure, the ssh options, and the lock;
   - `restore-check`: ok; a manifest-valid archive with a corrupt database; unsafe inner names; the drive check;
     `--archive`.
3. **`test_ai_backup.yml`** with `-vvv --diff`. It runs the role's task files into a scratch dir:
   - both preflights and their refusals;
   - install, key, `authorized_keys`, export, pull key and the destination folders (0700, a group-writable or
     foreign-owned one refused), each idempotent, with both key dirs 0700;
   - the five units, through `systemd-analyze verify`, with each `[Service]` section compared exactly to its
     sandbox list.

   Then it proves the generated private key never reached the output (text, body lines, and base64 at all 3
   alignments).

**Not tested offline (needs the real hosts, for the Integrator):**
- account creation;
- `sshd -T`;
- the real rrsync forced command;
- SELinux on the PC. The pull runs `ssh` and `rsync` from a system unit, and the files are labelled `etc_t` and
  `unlabeled_t` on the backup drive. Check `ausearch -m avc -ts recent` after the first run.
