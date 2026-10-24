# base

Host baseline (docs/CONTRACTS.md §7, module A).

- Sets the timezone from `timezone`.
- Installs `base_packages` (sqlite3, rsync, jq, smartmontools, unattended-upgrades, needrestart, curl).
- journald: `Storage=persistent`, `SystemMaxUse=500M`, via `/etc/systemd/journald.conf.d/10-homelab.conf`.
  The handler restarts journald, which also trims the journal to the cap.
- unattended-upgrades: `/etc/apt/apt.conf.d/52homelab-unattended-upgrades` clears the origin lists
  from earlier files and allows only the release and security pockets, with `Automatic-Reboot "false"`.
  It's a drop-in, so the package's own `50unattended-upgrades` conffile is never edited (a modified
  conffile makes unattended-upgrades hold that package back). The file is checked with `apt-config`
  before it's installed.

- network-online: netplan's generated wait-online drop-in also waits for the Wi-Fi uplink, which is
  normally down, so `systemd-networkd-wait-online` timed out and failed every boot, delaying
  `network-online.target`. `/etc/systemd/system/systemd-networkd-wait-online.service.d/zz-homelab.conf`
  resets `ExecStart` and waits only for `lan_iface` to be routable (60 s max). It's named `zz-` because
  drop-ins apply in file-name order across all directories, and it must come after netplan's
  `10-netplan.conf` (a `10-homelab.conf` would be overridden by it). Netplan itself isn't edited:
  `netplan apply` can bounce the LAN link mid-run. The handler only reloads unit files; the new
  command applies from the next boot.

- Time: chrony stays installed and running, and `/etc/chrony/sources.d/homelab-fallback.sources` adds
  IP-literal plain-NTP servers (`base_ntp_fallback_servers`: Cloudflare's 162.159.200.123 and
  162.159.200.1). The handler runs `chronyc reload sources`; chronyd isn't restarted.

## Clock recovery (why the fallback exists)
DNS here is strict DNS-over-TLS (role `dns`), and TLS needs a roughly correct clock. Ubuntu's NTS
sources (`ubuntu-ntp-pools.sources`) are hostnames. So if the clock is badly wrong (dead RTC battery,
BIOS reset), TLS fails, then DNS fails, then the NTS hostnames can't be resolved and the clock never
gets fixed. The IP-literal sources need no DNS: chrony steps the clock from them during its first
updates (`makestep 1 3`), then DoT and NTS work again. The NTS pools are marked `prefer`, so once
they're reachable chrony prefers them over the plain sources. UDP 123 isn't blocked.

If the clock drifts badly *after* boot (chrony only slews then), or it's stuck:
```bash
chronyc sources -v            # the 162.159.200.x sources should show as reachable (^+ or ^*)
sudo chronyc makestep         # step to the current estimate now
resolvectl query example.com  # DNS (DoT) works again once the clock is right
```
If no source is reachable at all, set the clock by hand (`sudo date -s '2026-10-24 12:00'`), then
`sudo chronyc makestep`.

Variables: `timezone` (group_vars), `lan_iface` (host_vars), `base_packages`,
`base_journald_system_max_use`, `base_wait_online_timeout`, `base_ntp_fallback_servers` (defaults).
