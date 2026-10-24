# firewall

UFW plus a DOCKER-USER guard (docs/CONTRACTS.md §7, module A).

## UFW: declared state without `ufw reset`
`vars/main.yml` declares the whole rule set (`firewall_rules`), each rule written exactly the way
`ufw show added` prints it. Every run:
1. `ufw <rule>` for each declared rule, **SSH over tailnet first**. UFW skips an identical rule,
   updates a rule that differs only in comment or action in place, and appends new ones. Nothing is
   removed in this step.
2. Default policies deny in / allow out / deny routed; `ufw enable` only if UFW is off.
3. `ufw show added` is read back. If any declared rule doesn't read back exactly as declared (UFW
   normalized it, e.g. a host address given as `lan_cidr`), the role **fails before deleting
   anything**. A rule matching the tailnet SSH rule is never deleted, whatever else happens.
4. Every other rule is deleted (`ufw delete …`, `ufw route delete …` for route rules).

The tailnet SSH rule is never absent while UFW is enabled: it's added (or kept) before anything is
deleted, and UFW swaps its user chain atomically. A second run changes nothing. Rules: SSH on
`tailscale0`; SSH from `gaming_pc_lan_ip` on `lan_iface`; 8096/tcp from `lan_cidr` on `lan_iface`;
9999/tcp on `tailscale0`; 53/udp+tcp in from `172.16.0.0/12` (Docker's pools) to `docker_dns_ip`
(resolved's Docker-facing stub); **53/udp+tcp denied out** to everything, so no plaintext DNS leaves
the host (loopback to the 127.0.0.53 stub is accepted earlier by UFW's `before.rules`, and resolved
talks to its upstreams over TLS on 853). Nothing on `wifi_iface`. (No 7359/udp: Jellyfin's discovery broadcasts can't
reach a bridge-network container.) `IPV6=yes` is enforced so the policy
covers IPv6. The role refuses to run if UFW's own before/after rule files touch DOCKER-USER (the
ufw-docker recipe), since a UFW reload would then flush the guard below.

## DOCKER-USER guard
Docker-published ports are DNATed before UFW's INPUT rules see them, so `homelab-docker-user.service`
(oneshot, `RemainAfterExit`, `After=`/`PartOf=`/`WantedBy=docker.service`) runs
`/usr/local/sbin/homelab-docker-user`, which inserts into DOCKER-USER:
- `-i {{ wifi_iface }} -m conntrack --ctstate NEW,INVALID -j DROP` (nothing can open a connection to a
  container from the Wi-Fi uplink, but replies to the containers' own outbound traffic still flow, so
  cloudflared and the apps keep working when the Wi-Fi is the failover route);
- `-s 100.64.0.0/10 ! -i tailscale0 -j DROP` (the backup Wi-Fi hands out tailnet-range addresses);
- `-p udp --dport 53 -j DROP` and `-p tcp --dport 53 -j DROP`: containers can't query a DNS server
  directly; they resolve through the host's stub on `docker_dns_ip` (INPUT, not forwarded);
- the same rules in ip6tables (tailnet range `fd7a:115c:a1e0::/48`) when Docker has an IPv6 DOCKER-USER chain.

Each rule is checked with `-C` before `-I` and carries a `homelab-du-<name>-<fingerprint>` comment tag,
where the fingerprint is a hash of the rule's exact match. Any change to a rule (a renamed `wifi_iface`,
a new match) gives it a new tag, and tagged rules that are no longer declared are deleted after the new
ones are in place.

Docker publishes ports as DNAT rules (`userland-proxy: false`), so Jellyfin's `lan_ip:8096` is reachable
from the whole LAN regardless of UFW's 8096 rule. That's intended (docs/CONTRACTS.md §7).
Rules from anyone else are left alone. The guard fails loudly if the DOCKER-USER chain is missing.
docker.service and its drop-ins are not touched. If Docker isn't running, the unit is only enabled
and runs when Docker starts.

Variables: `lan_iface`, `lan_cidr` (network address), `wifi_iface`, `gaming_pc_lan_ip` (host_vars).
