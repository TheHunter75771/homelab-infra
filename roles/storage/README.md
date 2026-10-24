# Role: storage

Prepares the media drive for the boot gate (docs/CONTRACTS.md §2 and §3). Module B.

## What it does
1. Installs `/etc/systemd/system/media.mount` (`What=/dev/disk/by-uuid/{{ media_uuid }}`,
   `Type=ext4`, `Options=defaults,noatime`) with **no `[Install]` section**, so nothing mounts the
   drive at boot except `bootgate.service`.
2. Removes the drive's line from `/etc/fstab`, matched by UUID (`UUID=`, `UUID="..."` or
   `/dev/disk/by-uuid/...`, any case), with a backup. It then fails if any other fstab line still
   mounts `{{ media_mount }}`.
3. Mounts the drive through the unit if it is present but not mounted. It fails the run if
   `{{ media_mount }}` is not the drive with `media_uuid` (`storage_require_media`).
4. Creates the sentinel `{{ media_mount }}/.homelab-media-sentinel` (root 0644), but **only**
   when findmnt reports `media_uuid` on the mount point, so it can never land on the empty
   directory underneath.
5. Sets the ext4 reserved blocks to `storage_media_reserved_pct` (1%) with `tune2fs -m`. It
   reads `tune2fs -l` first and changes nothing if the value is already right.

## Removing the fstab line while the drive is mounted
Editing fstab unmounts nothing. Until the reload, systemd has two definitions of `media.mount`:
- `/run/systemd/generator/media.mount`, from the fstab generator, plus a
  `local-fs.target.wants/media.mount` link (the old line had `nofail`);
- `/etc/systemd/system/media.mount`, from this role.

`/etc/systemd/system` ranks above `/run/systemd/generator` in the unit search path (checked on
the host with `systemd-analyze unit-paths`), so the role's file wins on load. The handler's
`daemon-reload` re-runs the generators. Once the fstab line is gone, the generated unit and its
`.wants` link disappear. Systemd tracks a live mount from `/proc/self/mountinfo`, not from the
unit file, so the active mount carries on under the new definition. Its noatime option takes
effect at the next mount.

## Formatting (off; never part of a routine run)
`storage_format_media: false` by default. `tasks/format.yml` is included only when it is true,
and then it still refuses unless all of these hold:
- `storage_format_device` is set explicitly. It has no default and is never derived from
  `media_uuid`.
- The device is a block device.
- `blkid -p` (a low-level probe that skips the cache) finds **no signature at all**: no
  filesystem, no partition table, no RAID/LVM member (exit 2, empty output).
- **No ext2/3/4 backup superblock** is readable either (`dumpe2fs -h -o superblock=N -o
  blocksize=B` at block groups 1 and 3 for 4k/2k/1k blocks). A library whose primary
  superblock is damaged looks blank to blkid but is still recoverable with `e2fsck -b`, so it
  is never formatted.
- `community.general.filesystem` runs with `force: false`.

The read-only probe and the refusal live in `tasks/format_probe.yml`. Its test runs them
against sparse disk images (blank; healthy ext4; ext4 with a zeroed primary superblock, at 4k
and 1k block sizes; a bare GPT). It checks that only the blank image gets through. It also
runs the whole `format.yml` with the flag on and checks the image is byte-for-byte unchanged.
No root needed:
```bash
.venv/bin/ansible-playbook -i localhost, roles/storage/tests/test_format_guard.yml </dev/null 2>&1 | cat
```

The new ext4 gets `-U {{ media_uuid }}`, so the rest of the system needs no change:
```bash
# once, for a brand-new blank drive only
.venv/bin/ansible-playbook playbooks/site.yml \
  -e storage_format_media=true -e storage_format_device=/dev/disk/by-id/<id>-part1
```

## Variables
| Variable | Default | Notes |
|---|---|---|
| `media_uuid`, `media_mount` | inventory | contract inputs |
| `storage_mount_unit` | derived (`media.mount`) | `media_mount` must not need systemd escaping |
| `storage_mount_options` | `defaults,noatime` | |
| `storage_sentinel_name` | `.homelab-media-sentinel` | must match role bootgate |
| `storage_media_reserved_pct` | `1` | |
| `storage_require_media` | `true` | `false` only for a host without the drive |
| `storage_format_media` / `storage_format_device` / `storage_format_label` | `false` / `""` / `media` | see above |

Idempotent: a second run reports `changed=0`.
