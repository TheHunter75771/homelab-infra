# watchdog

Hardware watchdog (docs/CONTRACTS.md §7, module A).

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

Variables: `watchdog_module`, `watchdog_device`, `watchdog_runtime_sec`, `watchdog_reboot_sec` (defaults).
