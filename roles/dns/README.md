# dns

systemd-resolved, fully declared (docs/CONTRACTS.md §7, module A).

- Installs `systemd-resolved` and keeps it enabled and running.
- `/etc/systemd/resolved.conf.d/zz-homelab.conf` is the only homelab resolved drop-in:
  `DNSStubListener=yes`, `DNS={{ dns_upstreams }}` (1.1.1.1 9.9.9.9), `FallbackDNS={{ dns_fallback }}`
  (1.0.0.1 149.112.112.112), `DNSOverTLS=opportunistic`, `DNSSEC=allow-downgrade`. The `zz-` name sorts
  last, and each list is reset (`DNS=`) before it's set, so earlier files can't add to or override it.
  The host's `resolved.conf` still has the stub off from when AdGuard held port 53; this overrides it.
- The hand-made `custom-dns.conf` (same servers) is removed, so a rebuild reproduces the box from the repo.
- The backup Wi-Fi is an open network, so its DHCP/RA-learned DNS servers are ignored:
  `/etc/systemd/network/10-netplan-{{ wifi_iface }}.network.d/zz-homelab.conf` sets `UseDNS=no` under
  `[DHCPv4]`, `[DHCPv6]` and `[IPv6AcceptRA]`. Otherwise they'd join resolved's lists, including
  `/run/systemd/resolve/resolv.conf`, which Docker gives containers. It's a drop-in for netplan's generated
  `10-netplan-<iface>.network`; netplan isn't edited. The handler runs `networkctl reload`, which
  reconfigures only the Wi-Fi link (never `netplan apply`).
- `/etc/resolv.conf` is a symlink to `../run/systemd/resolve/stub-resolv.conf`.
- After the handler restarts resolved, the role checks the stub answers on `127.0.0.53:53`, the global
  servers are exactly `dns_upstreams`, and MagicDNS (`100.100.100.100`) isn't in use. Tailscale
  `accept-dns` is off (role `tailscale`), so tailscaled never rewrites `resolv.conf`. Per-link servers
  from DHCP stay as they are.

`meta: flush_handlers` runs here so the checks see the restarted resolver; it also runs any handlers
earlier roles have queued.

Variables: `dns_upstreams`, `dns_fallback`, `dns_over_tls`, `dns_dnssec`, `dns_stale_dropins` (defaults).
