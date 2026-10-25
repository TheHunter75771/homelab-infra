# watchdog

Hardware watchdog and crash recovery (docs/CONTRACTS.md §7, amended 2026-10-24; module A).

## What actually recovers this box: `kernel.panic=10`
Live test on the real HP (2026-10-24): `iTCO_wdt` loads ("initialized. heartbeat=30 sec", TCO v6)
and systemd pets it, but after a forced panic (`echo c > /proc/sysrq-trigger`) with kdump unloaded
the box **stayed hung for 10 minutes** until the power button. The BIOS seems to handle the TCO
timeout itself. **On this hardware the iTCO/systemd watchdog below does not reset the box.** It's
kept because it's harmless and may work on other hardware, but crash recovery rests on the kernel:

- `/etc/sysctl.d/99-zz-homelab-panic.conf` (named to sort after every vendor file; the old
  `60-homelab-panic.conf` is removed) sets the following, applied at once by the handler
  (`sysctl --load` on this one file only):
  - `kernel.panic = 10`: reboot 10 s after any panic (the default 0 hangs forever);
  - `kernel.panic_on_oops = 1`: already 1 at runtime, persisted explicitly;
  - `kernel.hardlockup_panic = 1`: a CPU hard lockup, caught by the NMI watchdog (on), becomes a panic.
- **Not** `softlockup_panic` or `hung_task_panic`: a stalling USB media drive can trigger soft
  lockups and hung tasks, and panicking on them would make a flaky drive a reboot loop that bypasses
  the boot gate's one-power-cycle rule.
- After the handlers run, the role reads `/proc/sys/kernel/{panic,panic_on_oops,hardlockup_panic}` and
  asserts the values are live.
- Known gap: a hard freeze that never panics (and isn't a hard lockup) still needs the power button.
- Boot-time precedence: every run reads `systemd-sysctl --cat-config` and checks, with the
  `homelab_sysctl_overrides` filter (`filter_plugins/`, an identical copy of base's), that no
  later-sorting file overrides these settings at the next boot.

## kdump is removed (Noah, 2026-10-24)
- `kdump-tools` (and `linux-crashdump`, if present) is purged. Its postrm runs `update-grub`; the
  role also removes `/etc/default/grub.d/kdump-tools.cfg` (the only source of `crashkernel=`) if
  anything left it, and runs `update-grub` through its own handler. It then asserts
  `/boot/grub/grub.cfg` has no `crashkernel=`.
- `/etc/apt/preferences.d/homelab-no-kdump.pref` pins both packages to priority -1. `ubuntu-server-minimal`
  only *recommends* kdump-tools, so it stays installed, and the pin stops apt (or an upgrade) from
  putting kdump back.
- `kexec-tools`, `makedumpfile` and `crash` stay. They reserve nothing and run nothing.
- **A reboot is needed** to free the 512 MB the running kernel reserved at boot. The role never
  reboots. When the purge happens it writes Ubuntu's `/run/reboot-required` flag. On every run until the
  reboot, it prints "REBOOT NEEDED … MiB" from `/sys/kernel/kexec_crash_size`; that message isn't a change.
- Idempotent: the second run changes nothing, and on a box without kdump the purge, file removal and
  GRUB steps are no-ops.

## Hardware watchdog (kept)

1. `modprobe iTCO_wdt` now. A refusal (for example a BIOS-locked TCO) is not a failure.
2. If `/dev/watchdog0` exists afterwards:
   - `homelab-watchdog-load.service` (oneshot, `WantedBy=sysinit.target`) loads the driver on every boot;
   - `/etc/systemd/system.conf.d/10-homelab-watchdog.conf` sets `RuntimeWatchdogSec=30s` and
     `RebootWatchdogSec=10min`; the handler re-executes PID 1 to apply it;
   - the role then checks PID 1's `RuntimeWatchdogUSec` and that the device's sysfs state is `active`.
3. If there's no device, the systemd drop-in is removed (or never written) and the role says why.

**Why a unit and not `/etc/modules-load.d/`:** Ubuntu ships `blacklist iTCO_wdt` in
`/lib/modprobe.d/blacklist_linux_<kernel>.conf`, and systemd-modules-load applies the kmod blacklist
(systemd 259 logs "Module '…' is deny-listed (by kmod)" and skips it). So a modules-load.d entry
wouldn't load the driver at boot. A plain `modprobe iTCO_wdt` names the module explicitly, which the
blacklist doesn't block. Any driver loaded after PID 1 starts (this unit or modules-load.d alike)
relies on PID 1 opening the device once it appears, which current systemd does when the device is
missing at startup. Confirm after the first reboot: `busctl get-property org.freedesktop.systemd1
/org/freedesktop/systemd1 org.freedesktop.systemd1.Manager RuntimeWatchdogUSec` and
`cat /sys/class/watchdog/watchdog0/state` (`active`).

This role doesn't touch the boot gate's state.

Variables: `watchdog_module`, `watchdog_device`, `watchdog_runtime_sec`, `watchdog_reboot_sec`,
`watchdog_sysctl_file`, `watchdog_sysctl_stale_files`, `watchdog_panic_reboot_sec`, `watchdog_kdump_packages`,
`watchdog_kdump_grub_cfg` (defaults).
