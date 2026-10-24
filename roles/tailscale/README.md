# tailscale

Server-side Tailscale preferences (docs/CONTRACTS.md §7, module A). It configures an installed,
joined node; it doesn't install or join.

- Asserts `/usr/bin/tailscale` exists; keeps `tailscaled` enabled and running.
- Reads `tailscale debug prefs` (JSON) and runs `tailscale set` **only with the flags whose value
  differs**: `--ssh=false` (`RunSSH`), `--accept-dns=false` (`CorpDNS`), `--accept-routes=false`
  (`RouteAll`). Already-correct prefs mean no command and no change. After a `set`, the prefs are
  read again and asserted.
- `accept-dns=false` keeps tailscaled away from `resolv.conf` (role `dns` keeps it on the
  systemd-resolved stub), so internet lookups never depend on MagicDNS.

The mapping lives in `vars/main.yml` (`tailscale_required_prefs`). The tailnet policy is the repo
file `tailscale/policy.example.hujson` (tags and groups only; logins are added privately).
