# Role: bootgate

Docker, and every container, waits until the media drive is verified. Verified means the
drive's UUID is mounted on `{{ media_mount }}`, the sentinel exists, and a write + fsync +
delete test passes. Without that, nothing starts against an empty folder. The gate first runs
a repair ladder, then power-cycles once, then pushes an alert. It never boot-loops. A retry
timer and a drop-out watcher keep checking after boot. Module B, docs/CONTRACTS.md §3.

Requires role `storage` first, which provides `media.mount` and the sentinel. It sends pushes
through role `notifier`'s `homelab-notify`.

## Installed
| Path | What |
|---|---|
| `/usr/local/sbin/bootgate` | the gate (`files/bootgate.py`, Python 3 stdlib only) |
| `/etc/default/bootgate` | its settings (UUID, timings, method, compose path) |
| `bootgate.service` | oneshot, `RemainAfterExit`, `check --boot`, `OnFailure=bootgate-alert.service`, no network deps |
| `docker.service.d/10-bootgate.conf` | `Requires=` + `After=bootgate.service` (also covers `docker.socket` activation) |
| `bootgate-alert.service` | `After/Wants=network-online.target`; pushes "Media drive failed" (retries if the network is late) |
| `bootgate-retry.service` + `.timer` | every `bootgate_retry_minutes`; `check --retry`; never reboots |
| `bootgate-rerun.path` | Contract 2: `touch /run/bootgate/rerun` (group `bootgate`) starts the retry, which deletes the file |
| `bootgate-watch.service` + `.timer` | every 60 s; `watch` |
| `/etc/tmpfiles.d/bootgate.conf` | `/run/bootgate` 0770 root:bootgate |
| `/var/lib/bootgate/` | `status`, `attempts`, `usb-port`, `history.jsonl` (Contract 1), `media-stopped` |

It also removes the old setup's `docker.service.d/wait-for-media.conf(.disabled)`
(`RequiresMountsFor=/media`), which would let Docker pull the mount in by itself.

## State machine
```
check --boot   (bootgate.service; docker waits on it)
  status=waiting
  wait <= bootgate_wait_seconds for /dev/disk/by-uuid/<uuid>   (30 s when not booting)
  start media.mount  ->  verify (UUID + MAJ:MIN not stale, sentinel, write+fsync+delete)
     pass -------------------------------------------> attempts=0, status=ok, "pass", exit 0
     fail -> repair ladder, one "repair" event per rung:
              1 udevadm trigger --action=add --subsystem-match=block + settle
              2 USB re-bind of the recorded port: unbind/bind via /sys/bus/usb/drivers/usb,
                or, if the device is off the bus, power-cycle its hub port (port/disable 1->0)
              3 retry the mount + verify
              4 fsck -n   (read-only diagnostics; never -y/-p/-a)
            pass ------------------------------------> attempts=0, status=ok, "pass", exit 0
            fail:
              attempts<1 AND systemd is still booting AND attempts=1 written+read back
                   -> "powercycle", reboot | rtcwake -m off -s 60      (once per incident)
              otherwise -> status=failed, "fail", exit 1
                           docker stays down; bootgate-alert pushes "Media drive failed"

check --retry  (timer + rerun path; acts on failed | media-lost | stale waiting)
  wait 30 s -> mount (replacing a stale/foreign mount) -> verify
     pass -> attempts=0, status=ok, "retry-pass"; systemctl start docker.service;
             docker compose up -d of the media stack if the watcher had stopped it;
             push "Media drive back" (Docker up AND media apps back, rc 0),
                  "Media drive back, media apps did not restart" (rc 1, retried next run),
               or "Media drive back, Docker did not start" (rc 1). Only the plain title
             counts as a recovery on the dashboard; the qualified ones keep the incident open.
     fail -> nothing changes (no history spam). NEVER reboots, never runs the USB rungs.

watch          (every 60 s; acts only when status=ok)
  verify fails twice, 5 s apart -> status=media-lost, "media-lost", mark media-stopped,
  docker compose -f <stacks>/media/compose.yaml stop, push "Media drive lost; media apps stopped"
```
The drive's USB port id (e.g. `2-2`) is written to `usb-port` every time it is verified, so a
re-bind still knows where to look once the drive has dropped off the bus.

## Why a reboot loop is impossible
- A power-cycle needs `attempts < 1`. The gate writes `attempts=1` and reads it back from disk
  (fsynced) *before* rebooting. If that write fails, there is no reboot. A garbage attempts
  file counts as 1.
- `attempts` goes back to 0 only after the drive passes verification. So each incident gets at
  most one power-cycle.
- A power-cycle also needs `systemctl is-system-running` to say `initializing` or `starting`.
  Running the gate any other way can never reboot the host: Ansible starting the unit, a
  `docker restart`, `docker.socket` activation after a failure, a retry, a manual run. Those
  runs also use the 30 s wait instead of 720 s.

## Design notes
- **Python, not bash.** Contract 1 needs exact JSON lines. Every step needs a timeout, writes
  must be atomic and fsynced, and the decision logic has to be tested without root. Python 3 is
  in the base image (`/usr/bin/python3`). The script uses only the stdlib and starts in ~30 ms.
- **A hung USB device can only hang a child, never the gate.** The gate process never
  touches the drive's filesystem itself. The sentinel stat and the write + fsync + delete
  run in one child (`bootgate _probe`). sysfs unbind/bind/disable writes run in another
  (`_write-sysfs`), since they can block in the kernel while a hung device is torn down.
  Every child runs under `Host._run`: `Popen`, with output to an unlinked temp file (never a
  pipe). On timeout it sends SIGKILL and waits at most 5 s. A child stuck in uninterruptible
  I/O ignores SIGKILL, so it is abandoned and the call fails. (`subprocess.run(timeout=)`
  would wait for it forever after the kill.) Its pid goes to `/run/bootgate/stuck-probe`.
  While that process is still alive, no new probe is started, so stuck processes can't pile
  up. The watcher then reports `media-lost` and releases its lock.
- `media.mount` has no `BindsTo=` on the device. After a drop-out the dead mount stays and
  returns I/O errors, so apps never see an empty folder they could write into. The gate spots
  it: the UUID goes missing, or the mount's MAJ:MIN differs from the device that now holds the
  UUID. It unmounts only a mount like that, never a good one.
- Retry releases its lock before `systemctl start docker.service`, which can itself start
  `bootgate.service` (that run passes at once, because the drive was just verified).
- The watcher writes once a minute, which keeps the USB disk from spinning down.
- Never restart `bootgate.service` by hand or from a handler. Docker `Requires=` it, so a
  restart restarts Docker. Use the re-run trigger instead.

## Operating it
```bash
bootgate status                         # status, attempts, usb-port, last 10 events
sudo touch /run/bootgate/rerun          # retry now (members of group bootgate need no sudo)
sudo bootgate --dry-run check --boot    # trace what a boot run would do; changes nothing
journalctl -t bootgate -t bootgate-alert
```
Emergency override, to run Docker without the drive (e.g. the disk is dead and the other
stacks, such as the tunnel or wol-relay, must come up; the dashboard runs outside Docker and
needs no override). Systemd cannot cancel a dependency from another drop-in, so move this one away:
`sudo mv /etc/systemd/system/docker.service.d/10-bootgate.conf /root/ && sudo systemctl daemon-reload && sudo systemctl start docker`.
Then stop the media stack by hand. The next Ansible run puts the drop-in back.

## Tests
```bash
python3 -m unittest discover -s roles/bootgate/files/tests -v
```
- `test_gate_logic.py` uses a fake host and a fake clock: pass, late arrival (559 s), missing
  → ladder → exactly one power-cycle, then the next boot fails with no reboot. It also covers
  attempts ≥ 1, never power-cycling outside boot, unwritable or garbage attempts, repair by
  udev and by USB re-bind, fsck on mount failure, the sentinel, stale and foreign mounts, the
  history format, retry (pass, never reboots, lock, stale waiting), media-lost and restore,
  the boot-pass-after-media-lost restore, and alert retries.
- `test_cli_e2e.py` runs the real CLI and `Host` class against stub `systemctl`, `findmnt`,
  `udevadm`, `fsck`, `docker` etc. in a fake root. It checks the exact commands run, sysfs
  re-bind writes, that fsck only ever gets `-n`, the rtcwake argv, that `--dry-run` writes
  nothing, the re-run trigger, and watch → retry. Hung-device tests use a fake process whose
  `wait()` never returns and ignores kill. They check that `_run` abandons it in bounded time,
  that `watch()` still reports `media-lost`, stops the stack, pushes and frees its lock, that
  only one stuck probe is ever started, and that the gate process never stats the sentinel
  itself.

Run as an unprivileged user; nothing touches the real system.
