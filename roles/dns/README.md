# dns

systemd-resolved with encrypted-only DNS (docs/CONTRACTS.md §7, §9d; module A).

- Installs `systemd-resolved` and keeps it enabled and running.
- `/etc/systemd/resolved.conf.d/zz-homelab.conf` is the only homelab resolved drop-in:
  - `DNSStubListener=yes` and `DNSStubListenerExtra={{ docker_dns_ip }}` (a second stub on the Docker
    bridge address, which containers use through daemon.json `"dns"`);
  - `DNS=1.1.1.1#cloudflare-dns.com 9.9.9.9#dns.quad9.net`, `FallbackDNS=1.0.0.1#cloudflare-dns.com
    149.112.112.112#dns.quad9.net` (the `#name` is checked against the server's certificate and sent as SNI);
  - `DNSOverTLS=yes` (strict: no plaintext fallback) and `DNSSEC=allow-downgrade`.
  - `MulticastDNS=no` and `LLMNR=no` (no multicast name resolution on any link; Ubuntu already ships
    `MulticastDNS=no`, this makes both explicit). The post-check reads all three modes from resolved.

  The `zz-` name sorts last, and each list is reset before it's set, so earlier files can't add to or
  override it. The host's `resolved.conf` still has the stub off from AdGuard days; this overrides it.
- The hand-made `custom-dns.conf` is removed, so a rebuild reproduces the box from the repo.
- DHCP- and RA-learned DNS servers are ignored on the LAN and Wi-Fi links:
  `/etc/systemd/network/10-netplan-<iface>.network.d/zz-homelab.conf` sets `UseDNS=no` under `[DHCPv4]`,
  `[DHCPv6]` and `[IPv6AcceptRA]` for `lan_iface` and `wifi_iface` (`dns_ignore_link_dns`). These are
  drop-ins for netplan's generated `.network` files; netplan isn't edited.
- `/etc/resolv.conf` is a symlink to `../run/systemd/resolve/stub-resolv.conf`.

## Applying the link settings without bouncing the LAN
The handler **restarts systemd-networkd**; it never runs `networkctl reload`/`reconfigure` or
`netplan apply`. The LAN address is DHCPv4, and Ansible's SSH runs over Tailscale on top of it.
- `networkctl reload` reconfigures every link whose `.network` changed ("all interfaces that matched
  the file are reconfigured", networkctl(1), systemd 259). That stops the link's DHCP client.
  `SendRelease=` defaults to yes, so the lease is released and the address is gone until it's re-acquired.
- `KeepConfiguration=dynamic` would avoid that only by ignoring DHCP lease lifetimes forever
  (systemd.network(5): "contrary to the DHCP specification").
- A networkd **restart** keeps DHCPv4, DHCPv6, NDISC and IPv4LL configuration unconditionally since
  systemd 257 (NEWS: "systemd-networkd can be restarted without disturbing ongoing connections"),
  then applies the new `.network` settings. The address and routes stay in place.

## Tailscale's ip rules
`/etc/systemd/networkd.conf.d/zz-homelab.conf` sets `ManageForeignRoutingPolicyRules=no`. Without it,
networkd deletes every routing policy rule it didn't configure whenever it starts (this role's restart,
or a systemd package upgrade). That includes Tailscale's rules 5210–5270 (`lookup 52`), which would cut
the tailnet path and Ansible's SSH. The drop-in is written before the restart handler runs, so the
restart already uses it. `ManageForeignRoutes` stays at its default: Tailscale's routes are all on
`tailscale0`, which networkd doesn't manage.

tailscaled puts deleted rules back within about a second, so "`lookup 52` is present" alone proves
nothing. The role notes the time just before the handlers run, waits until the link reconfiguration has
finished (the `resolvectl dns` retry loop), then asserts both that `ip rule show` has `lookup 52` and that
`journalctl -u tailscaled --since @<time>` has no "deleted ip rules" line (tailscaled logs "somebody
(likely systemd-networkd) deleted ip rules; restoring …" when it repairs them).
`systemctl restart tailscaled` restores the rules if they ever go missing.

## Checks
After the handlers run, the role checks:
- both stubs answer: 127.0.0.53, and `docker_dns_ip` once that address exists;
- resolved's `DNSOverTLS` is `yes`;
- the global servers are exactly `dns_upstreams`;
- no link has servers of its own (retried for up to 30 s while networkd hands the new settings to
  resolved);
- MagicDNS (100.100.100.100) isn't in use.

`meta: flush_handlers` runs here, so earlier roles' queued handlers run at this point too.

Variables: `dns_upstreams`, `dns_fallback`, `dns_over_tls`, `dns_dnssec`, `dns_ignore_link_dns`,
`dns_stale_dropins` (defaults); `docker_dns_ip` (group_vars); `lan_iface`, `wifi_iface` (host_vars).
