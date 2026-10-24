# Runbook: media drive lost or failed

Use this when you get **"Media drive lost"** (it dropped while running) or **"Media drive failed"** (missing at boot, after the one power-cycle).

## What the system already did
- **Lost while running:** the watcher stopped the media stack (Jellyfin, *arr, SABnzbd, Jellyseerr). Everything else keeps running.
- **Failed at boot:** the gate waited 12 min, ran the repair ladder (udev re-trigger, USB port reset or power-cycle, mount retry, read-only `fsck -n`), power-cycled **once**, waited again, then gave up **without rebooting again**. **Docker is held down**, so no containers run, including the tunnel and push page. SSH over the tailnet still works.
- In both cases a retry runs every 5 minutes. When the drive comes back, everything restarts on its own and you get **"Media drive back"**.

## Steps
1. **Physical check:** is the drive's USB cable seated (rear port, no hub)? Is its power brick on, and is the light on? Re-seat the cable.
2. **Wait up to 5 minutes** for "Media drive back". Or trigger the retry now: `ssh <admin>@<host> touch /run/bootgate/rerun` (works without sudo for the admin user).
3. **Still failing? Look at what the gate saw:**
   ```bash
   cat /var/lib/bootgate/status; tail -5 /var/lib/bootgate/history.jsonl
   sudo journalctl -t bootgate -b --no-pager | tail -30
   sudo journalctl -k -b | grep -iE 'usb|uas|sd[a-z]|reset|disconnect' | tail -30
   lsblk -o NAME,SIZE,TRAN,MOUNTPOINT,LABEL
   ```
   - The drive isn't in `lsblk`: a power, cable or port problem. Try another rear port, or the drive's own power cycle.
   - It's in `lsblk` but the gate says verify failed: check `findmnt /media` and the history detail (wrong UUID, sentinel missing, write test failed).
4. **Filesystem errors** (the history says `fsck -n` found problems). This only happens after a boot-time failure, so Docker is held and nothing is using `/media`. The gate never repairs the filesystem itself. By hand, **in this order**:
   ```bash
   sudo systemctl stop bootgate-watch.timer bootgate-retry.timer bootgate-rerun.path   # pause the gate FIRST
   systemctl is-active bootgate.service bootgate-retry.service   # neither may print "activating"; if one does, wait and re-check
   findmnt /media && sudo umount /media      # unmount only if something is mounted
   sudo e2fsck -f /dev/disk/by-uuid/<media_uuid>   # if that link is missing (damaged superblock), use the lsblk name, e.g. /dev/sdX1
   sudo systemctl start bootgate-watch.timer bootgate-retry.timer bootgate-rerun.path
   touch /run/bootgate/rerun
   ```
   - **Don't run any `docker` command while the status is `failed`.** Docker is held by the gate, and any docker CLI call starts it, which starts a new gate run (USB reset, remount) in the middle of your repair.
   - Answer e2fsck's prompts yourself, and read them.
5. **Never** remove the sentinel file or edit `/var/lib/bootgate/attempts` to force things. The attempts counter is what prevents boot loops. A successful retry resets it.

## After recovery
- Check that everything is healthy: `sudo docker ps`. Expect 9 healthy containers: cloudflared, jellyfin, jellyseerr, prowlarr, push-core, radarr, sabnzbd, sonarr, wol-relay.
- If this keeps happening, it's hardware (the evidence on file points to power or cabling). Plan a new cable, a powered hub or a replacement drive.
