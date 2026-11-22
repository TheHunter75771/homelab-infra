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

- Sudo (docs/CONTRACTS.md §12.13, Noah's decision): classic sudo instead of sudo-rs. Ubuntu 26.04 ships both
  behind the `sudo` alternative: sudo-rs (`/usr/lib/cargo/bin/sudo`, priority 50, the auto choice) and classic
  sudo (`/usr/bin/sudo.ws`, priority 40). Each alternative carries `visudo`, `sudoedit` and the man pages as
  slaves, so selecting `base_sudo_alternative` (`/usr/bin/sudo.ws`) in **manual** mode switches
  `/usr/bin/sudo` and `/usr/sbin/visudo` (to `visudo.ws`) together, and a sudo-rs upgrade can't switch back.
  `tasks/sudo.yml`, only on Debian-family hosts where that path is a registered alternative (empty variable:
  left alone):
  - before switching (also in check mode), or the role stops without switching: `/etc/sudoers-rs` doesn't
    exist (sudo-rs reads it, classic sudo doesn't, so its rules would silently stop applying); classic
    `visudo.ws -c` accepts the sudoers files; and classic sudo keeps each user's root access, checked by
    asking classic sudo itself (`tasks/sudo_probe.yml`). What that means depends on the account, read with
    `passwd -S <user>` (shadow's passwd: `<user> L|NP|P <date> …`, L for a field starting with `!` or `*`, NP
    for an empty one). It never prints the hash, so the hash never reaches the controller (no_log too), and
    any other output stops the role:
    - no usable password (`L` or `NP`, as on ai-box), and always `automation_user`:
      the only way into root is a passwordless rule, so as root `sudo.ws -u <user> -- sudo.ws -n
      /usr/bin/id -u` must print exactly `0` (`-n` fails instead of prompting);
    - a usable password (the media server's admin, who types it for sudo; bootstrap.yml): a prompt is
      normal, so `sudo.ws -n -l -U <user> -u root <cmd>` must allow `/usr/bin/id` and `/bin/sh` (a root
      shell is ALL). That is classic sudo's own policy decision, last-match-wins and negations included.

    Both are behavioural; a text match of `sudo -l` can be fooled (a `NOPASSWD` rule for one command next to
    a password-asking `ALL`, or last-match-wins). Comparing with what sudo-rs lists wasn't used: its output
    has another format, and sudo-rs isn't the spec of what's wanted;
  - the switch is `update-alternatives --set sudo /usr/bin/sudo.ws` (`tasks/sudo_switch.yml`), run only when
    `tasks/sudo_decide.yml` says the value isn't already classic in manual mode. `--set` always makes the
    choice manual, also when auto already points at the classic path (after sudo-rs is removed), which
    `community.general.alternatives` would skip. Check mode reports it as a would-be change;
  - after: `/usr/bin/sudo --version` says `Sudo version …` (sudo-rs says `sudo-rs …`), `/usr/sbin/visudo`
    resolves to `visudo.ws`, and the group is in manual mode on that path.
  The play itself already becomes through `/usr/bin/sudo.ws` (group_vars `ansible_become_exe`), so the switch
  can't break a run in progress. Both implementations read the same `/etc/sudoers` and `/etc/sudoers.d`.
- Time: chrony stays installed and running (see "Clock recovery" below for why the extra settings exist).
  - `/etc/chrony/sources.d/homelab-fallback.sources` adds two IP-literal plain-NTP servers, one per operator
    (`base_ntp_fallback_servers`: Cloudflare 162.159.200.123, Google 216.239.35.0; where they come from is in
    `defaults/main.yml`). Its handler runs `chronyc reload sources`.
  - `/etc/chrony/conf.d/homelab.conf` sets `authselectmode ignore` and `makestep 1 -1`. Ubuntu's
    `chrony.conf` reads `conf.d` last, so these win. A change restarts chronyd (conf.d isn't reloadable).
  - After the handlers, `chronyc -n selectdata -a` must show no source with the "required" option (`R`)
    and both fallback addresses without `noselect` (`N`) (`tasks/chrony_selectdata.yml`; skipped in check
    mode). UDP 123 out is open: UFW's default is allow out, and its only outbound rules deny port 53.

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
  - the live values, including the LAN interface's `send_redirects` (`tasks/sysctl_live.yml`, expected
    values in `vars/main.yml`). Something can reset them after boot: UFW applies `/etc/ufw/sysctl.conf`,
    which ships `log_martians=0`, on every start (docs/CONTRACTS.md §12.9; role firewall now fixes that
    file too). So if any live value differs, the role re-applies the hardening file with `systemd-sysctl`
    (the only time it reports changed), reads the values again, and only then asserts. A value that won't
    stick still fails the run, naming only the keys that drifted (UFW is mentioned only for those, not for
    sysrq). **Check mode** re-applies nothing: drift is reported as a would-be change (a `changed` debug
    task naming the keys) and doesn't fail the run, since the values can't be judged until they're re-applied;
    the `kernel.sysrq` rule still fails it (docs/CONTRACTS.md §12.9);
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
- `tests/test_sudo_decide.yml` runs the real `tasks/sudo_decide.yml` on `update-alternatives --query sudo`
  fixtures (`tests/fixtures/sudo-alternatives.yml`, Ubuntu 26.04's sudo and sudo-rs alternatives): switch
  from the default sudo-rs, from manual sudo-rs, and from classic-in-auto; no change when already classic
  and manual; left alone when the classic alternative isn't registered, there's no group, or the variable
  is empty.
- `tests/test_sudo_switch.yml`, run normally and with `--check`, puts a stub `update-alternatives` (it only
  records its arguments) first on PATH and runs the real decide and switch steps on every fixture case: it
  must call `--set sudo /usr/bin/sudo.ws` exactly for the cases that need it (including auto already on the
  classic path), and nothing in check mode. It checks that the stub is what PATH resolves before anything runs.
- `tests/test_sudo_probe.yml` runs the real `tasks/sudo_probe.yml` with a stub `passwd` first on PATH (`-S`
  statuses) and `base_sudo_alternative` pointed at a stub `sudo.ws` (records its arguments; answers the
  NOPASSWD probe and the `-l -U` checks per user). Passes: `L` + NOPASSWD; `NP` counts as not usable;
  `P` + root allowed for both commands. Fails: `L` + password required, another uid, extra output; `P` + no
  rule, or only `/usr/bin/id`; the automation user needing a password even with `P`; malformed `passwd -S`
  output (then classic sudo isn't asked at all). Each case also checks which probe each user got (the exact command lines).
- `tests/test_chrony_selectdata.yml` runs the real `tasks/chrony_selectdata.yml` on `chronyc -n selectdata -a`
  fixtures (`tests/fixtures/chrony-selectdata.yml`, chrony 4.8's output format): it passes with NTS down
  or in normal operation, and fails on a required (R) source, a noselect fallback or a missing one.
- `python3 -m unittest discover -s roles/base/tests -v`: `test_chrony.py` renders both chrony drop-ins
  (exactly two IP-literal `server … iburst` lines, one per operator; exactly `authselectmode ignore` and
  `makestep 1 -1`; no `nocerttimecheck`) and checks, against the
  directive lines of Ubuntu's packaged `chrony.conf` (`tests/fixtures/ubuntu-chrony.conf`), that `confdir`
  comes after Ubuntu's `makestep`. Also the filter's rules, run against a synthetic,
  Ubuntu-shaped vendor config (`tests/fixtures/vendor-cat-config.txt`) plus the base and watchdog sysctl
  files rendered from their real templates. It also checks that the two plugin copies are byte-identical.
- `tests/test_sysctl_live.yml` (localhost, a scratch tree for `/proc/sys`, a stub apply command, no root)
  runs the real `tasks/sysctl_live.yml` per case (`tests/live_case.yml`): converged is not changed; a
  UFW-style `log_martians` drift, a LAN-interface `send_redirects` drift alone and a `kptr_restrict` drift
  alone are each re-applied once and pass, and the run after is not changed; a value that won't stick is
  re-applied and fails, naming only that key; `kernel.sysrq` 0 fails with no re-apply and no UFW hint.
  `tests/live_guard.yml` runs before any write, at each case and before the recursive cleanup: the scratch
  directory must come from this run's `tempfile` (an `-e live_test_tmp` override is refused), resolve to a
  directory directly under the system temp dir, and `base_sysctl_proc_dir` must resolve to exactly its
  `proc` (never under `/proc`, never a symlink). On a refusal nothing is written and nothing is deleted.
- `tests/test_sysctl_live_check.yml`, run with `--check`: a drift is reported as a would-be change, left
  untouched, and passes; converged reports nothing; sysrq 0 still fails.
- The unit tests also check that `vars/main.yml`'s expected live values are exactly the keys and values
  the hardening template sets (the glob through the LAN-interface entry).
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
The deadlock (ai-box, 2026-10-26, after a power-off with a dead RTC battery): the box came up months
behind. With the clock that wrong, TLS fails everywhere: Tailscale, DNS-over-TLS (role `dns`; plaintext
DNS is blocked by UFW), and NTS, the authenticated NTP that Ubuntu's chrony pools use. So chrony had
nothing it could use to fix the clock, and nothing else could fix it either.

The IP-literal sources were meant to break that loop: plain NTP needs neither DNS nor TLS. They didn't,
because chrony 4.8's default `authselectmode mix` treats every NTS source as *required*. The pools that
couldn't even be resolved still count (as `ID#…` placeholder sources), and while a required source is
unusable, chrony refuses to select any unauthenticated one. That's the `R` that `chronyc -n selectdata -a`
showed for every NTS source.

The fix:
- `authselectmode ignore`: authentication no longer decides what may be selected. In normal operation
  nothing changes, because the NTS pools are marked `prefer`, so chrony uses only them whenever one is
  selectable. When none is, chrony falls back to the two IP sources, fixes the clock, and then TLS (and
  with it DNS and NTS) works again.
- Only two IP sources, so the five NTS pools stay the majority when chrony votes out falsetickers.
- `makestep 1 -1`: the fallback can become usable long after boot, and chrony must then step a
  months-off clock, not slew it (Ubuntu's own `makestep 1 3` only steps during the first updates).
- `nocerttimecheck` is deliberately not used: NTS keeps checking certificate times.

If the clock is still wrong (no source reachable yet), or it's stuck:
```bash
chronyc -n selectdata -a     # no R in EOpts; 162.159.200.123 and 216.239.35.0 without N
chronyc sources -v            # the two IP sources should be reachable (^+ or ^*) while NTS is down
sudo chronyc makestep         # step to the current estimate now
resolvectl query example.com  # DNS (DoT) works again once the clock is right
```
If no source is reachable at all, set the clock by hand (`sudo date -s '2026-10-24 12:00'`), then
`sudo chronyc makestep`.

Variables: `timezone` (group_vars), `lan_iface` (host_vars), `base_packages`,
`base_journald_system_max_use`, `base_wait_online_timeout`, `base_ntp_fallback_servers`,
`base_chrony_makestep_threshold`, `base_chrony_makestep_limit`, `base_chrony_authselectmode`,
`base_sudo_alternative`, `base_sudo_visudo`,
`base_purge_packages`, `base_never_install`, `base_apport_units`, `base_sysctl_file`,
`base_sysctl_file_owner`, `base_sysctl_file_group`, `base_sysctl_stale_files`, `base_blacklisted_modules` (defaults).
