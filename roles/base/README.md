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

## Hardening (2026-10-24 audit)
- **mDNS off.** `avahi-daemon` announced the host's name and services on every link, including
  the public Wi-Fi backup uplink (UFW doesn't block outbound multicast). Nothing here needs it: the
  apps run in Docker with their own DNS, Jellyfin's DLNA is off, and resolved has `MulticastDNS=no`
  and `LLMNR=no` (role `dns`).
  - It's **purged**, not masked: on the box, `apt-get -s purge avahi-daemon` removes only
    `avahi-daemon` and `avahi-utils`. avahi-daemon was manually installed, and `libnss-mdns` merely
    recommends it, so no metapackage depends on it.
  - `/etc/apt/preferences.d/homelab-never-install.pref` pins `avahi-daemon` to -1 so nothing pulls it back in.
  - `libnss-mdns` and the avahi libraries stay; nothing is autoremoved. With no daemon, nss-mdns returns
    "unavailable" at once and lookups fall through to DNS.
- **Apport is stopped and masked**: `apport.service`, `apport-autoreport.path`, `apport-autoreport.timer`
  and `apport-forward.socket`. It can't be purged, because `ubuntu-server` and `ubuntu-server-minimal`
  depend on it. Its stop hook puts `fs.suid_dumpable` back to 0 and `kernel.core_pattern` to `core`.
- `/etc/sysctl.d/99-zz-homelab-hardening.conf`, applied by the handler with `systemd-sysctl` on that file only:
  - `send_redirects = 0` for `all`, `default` **and every interface** (`net.ipv4.conf.*`). The kernel
    ORs `all` with each interface's own value, so `all=0` alone wouldn't stop the LAN interface or docker0. systemd's
    udev rule applies the glob to interfaces created later. The box routes for nobody: Docker forwards
    between its bridges and the LAN, and Tailscale isn't a subnet router or exit node.
  - `log_martians = 1` (`all`, `default`). With loose `rp_filter` and a default route, Docker and
    tailnet sources are routable, so they aren't logged. Only truly bogus sources are, and the kernel
    rate-limits those messages (burst 10, one per 5 s after that).
  - `fs.suid_dumpable = 0`.
  - `rp_filter` stays at Ubuntu's 2 (loose), which Tailscale needs.
- After the handlers run, the role asserts:
  - the live values, including the LAN interface's `send_redirects`;
  - that avahi is absent or not found and apport is masked;
  - that nothing listens on UDP 5353.

- Kernel and BPF (2026-10-25 Lynis run), in the same sysctl file and live assert:
  `fs.protected_fifos = 2`, `kernel.kptr_restrict = 2`, `net.core.bpf_jit_harden = 2`,
  `kernel.unprivileged_bpf_disabled = 1` (locked; dockerd runs as root and doesn't need unprivileged BPF).
  The assert also checks that `kernel.sysrq` isn't 0 (Ubuntu's 176 stays).
- **Deliberately not set** (comments in the file):
  - `kernel.modules_disabled=1`: the boot gate's USB repair and the iTCO loader need module loading;
  - `net.ipv4.conf.all.forwarding=0`: Docker needs it;
  - `rp_filter=1`: Tailscale needs 2;
  - `kernel.sysrq=0`: 176 stays as the emergency keyboard path, since the hardware watchdog doesn't reset this HP.
- `/etc/modprobe.d/homelab-blacklist-net.conf` blacklists `dccp`, `sctp`, `rds` and `tipc`
  (`blacklist` plus `install … /bin/false`). None was loaded or referenced on the box; dccp isn't even
  in this kernel any more. The role checks with `modprobe --dry-run` that none would load, and that
  none is in `/proc/modules`.

- **Boot-time precedence.** systemd-sysctl merges `/usr/lib`, `/run` and `/etc` `sysctl.d` files in
  filename order, and the last setting wins. A glob also skips any key that's set explicitly, or
  excluded with `-key`, anywhere.
  - Our file is named `99-zz-…`, so it sorts after every vendor file, including Debian's `99-sysctl.conf`.
  - The old `60-homelab-hardening.conf` is removed after the new file is written.
  - Every run, the role reads `systemd-sysctl --cat-config` and checks, with the `homelab_sysctl_overrides`
    filter (`filter_plugins/`), that every assignment in our file takes effect at boot. The filter models
    systemd-sysctl's option table:
    - a later *different* value overrides ours, while the same value again is fine;
    - a later `-key` line cancels our setting;
    - an explicit or `-key` entry anywhere makes our glob skip that key;
    - a later different glob overlapping ours wins on the keys both match;
    - `.`/`/` separators and a leading `/` are normalized.
  - So a vendor override fails the run without waiting for a reboot. A later file writing anything but 1 to
    `kernel.unprivileged_bpf_disabled` would also make `systemd-sysctl.service` fail at boot, because 1 is
    locked; the failure message says so.
  - The live-value assert stays.
  - The filter is duplicated in `roles/watchdog/filter_plugins/`, and the unit tests fail if the two copies differ.

## Tests (CI job `role-tests`)
- `python3 -m unittest discover -s roles/base/tests -v`: the filter's rules, run against a synthetic,
  Ubuntu-shaped vendor config (`tests/fixtures/vendor-cat-config.txt`) plus the base and watchdog sysctl
  files rendered from their real templates. It also checks that the two plugin copies are byte-identical.
- `tests/test_sysctl_files.yml` (localhost, scratch paths, no root) runs the role's real
  `tasks/sysctl_files.yml` twice. The first run writes the new name, removes the old one and notifies
  the handler (a stub here) once; the second run changes nothing. It then puts seven synthetic merged
  configs through the real `tasks/sysctl_boot_check.yml` and checks each verdict: pass when clean or
  re-setting the same value; fail on a later override, a later `-key`, bpf=2, an overlapping later glob, or
  the old file name. The run command is in its header; `tests/templates` is a symlink to `../templates`, so
  the task file finds its template outside role context.

### Considered and declined (Lynis suggestions)
- USB-storage blocking: the media drive is USB.
- GRUB password: the box is in the house; a password would only block recovery at the console.
- Password ageing: every login is key-only and the accounts have locked passwords.
- Legal banners: nobody but the owner logs in.
- auditd: extra disk and log volume for no reviewer; journald plus sshd's `LogLevel VERBOSE` cover logins.
- AIDE (file integrity): the box's state is declared in this repo and re-converged by Ansible; a nightly hash scan adds noise, not a reviewer.
- Malware scanners: no untrusted uploads or mail; the attack surface is the tailnet and one Cloudflare tunnel.
- Compiler removal: Docker builds run inside images; the host toolchain isn't an exposure on a tailnet-only box.

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
`base_journald_system_max_use`, `base_wait_online_timeout`, `base_ntp_fallback_servers`,
`base_purge_packages`, `base_never_install`, `base_apport_units`, `base_sysctl_file`,
`base_sysctl_file_owner`, `base_sysctl_file_group`, `base_sysctl_stale_files`, `base_blacklisted_modules` (defaults).
