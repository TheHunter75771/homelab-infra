# firewall

UFW plus a DOCKER-USER guard (docs/CONTRACTS.md §7, module A).

## UFW: declared state without `ufw reset`
`vars/main.yml` builds the whole rule set (`firewall_rules`), each rule written exactly the way
`ufw show added` prints it:
- the inbound allow rules from **`firewall_allow_rules`** (group_vars, docs/CONTRACTS.md §12.2),
  items `{port, proto, iface, from (optional), comment}`, rendered by `filter_plugins/homelab_ufw.py`
  to `allow in on IFACE [from FROM ]to any port PORT proto PROTO comment 'homelab: COMMENT'`. The filter
  rejects unknown keys, a port outside 1-65535, a proto other than tcp/udp, whitespace or quotes in
  iface/from, quotes in the comment and two rules that differ only in comment. The SSH-over-tailnet
  rule must be in the list (the role fails before touching UFW otherwise) and is always ordered first.
  No rule may use `wifi_iface`. The role default is an empty list, so a host group must declare its rules;
- then the container-DNS rules (below), the same on every host.

Every run:
1. `ufw <rule>` for each declared rule, **SSH over tailnet first**. UFW skips an identical rule,
   updates a rule that differs only in comment or action in place, and appends new ones. Nothing is
   removed in this step.
2. Default policies deny in / allow out / deny routed; `ufw enable` only if UFW is off.
3. `ufw show added` is read back. If any declared rule doesn't read back exactly as declared (UFW
   normalized it, e.g. a host address given as `lan_cidr`), the role **fails before deleting
   anything**. A rule matching the tailnet SSH rule is never deleted, whatever else happens.
4. Every other rule is deleted (`ufw delete …`, `ufw route delete …` for route rules).

The tailnet SSH rule is never absent while UFW is enabled: it's added (or kept) before anything is
deleted, and UFW swaps its user chain atomically. A second run changes nothing. Media server rules
(`group_vars/media_servers`): SSH on `tailscale0`; SSH from `gaming_pc_lan_ip` on `lan_iface`; 8096/tcp
from `lan_cidr` on `lan_iface`; 9999/tcp on `tailscale0`. ai-box (`group_vars/ai_servers`): SSH on
`tailscale0`; SSH on `lan_iface` from `gaming_pc_lan_ip` and from `mediaserver_lan_ip`. Every host also
gets 53/udp+tcp in from `172.16.0.0/12` (Docker's pools) to `docker_dns_ip`
(resolved's Docker-facing stub) and **53/udp+tcp denied out** to everything, so no plaintext DNS leaves
the host (loopback to the 127.0.0.53 stub is accepted earlier by UFW's `before.rules`, and resolved
talks to its upstreams over TLS on 853). Nothing on `wifi_iface`. (No 7359/udp: Jellyfin's discovery broadcasts can't
reach a bridge-network container.) `IPV6=yes` is enforced so the policy
covers IPv6. The role refuses to run if UFW's own before/after rule files touch DOCKER-USER (the
ufw-docker recipe), since a UFW reload would then flush the guard below.

## UFW's sysctl file (docs/CONTRACTS.md §12.9)
UFW runs `sysctl -p /etc/ufw/sysctl.conf` (`IPT_SYSCTL`) on every start, after systemd-sysctl, and the
shipped file sets `log_martians=0` for `all` and `default`, undoing role base's hardening at every boot
and UFW start. `tasks/ufw_sysctl.yml` rewrites those two lines in place to `=1`, before UFW is
enabled or reloaded; no other key in the file conflicts with base. Every run then reads both live values
and sets any that aren't 1 (`sysctl -w`), so the run that fixes the file also fixes the live state.
Test: `ansible-playbook -i localhost, -c local roles/firewall/tests/test_ufw_sysctl.yml`.

## DOCKER-USER guard
Docker-published ports are DNATed before UFW's INPUT rules see them, so `homelab-docker-user.service`
(oneshot, `RemainAfterExit`, `After=`/`PartOf=`/`WantedBy=docker.service`) runs
`/usr/local/sbin/homelab-docker-user`, which inserts into DOCKER-USER:
- `-i {{ wifi_iface }} -m conntrack --ctstate NEW,INVALID -j DROP` (nothing can open a connection to a
  container from the Wi-Fi uplink, but replies to the containers' own outbound traffic still flow, so
  cloudflared and the apps keep working when the Wi-Fi is the failover route). **Only when `wifi_iface`
  is defined**; on a host without it (ai-box) the rule isn't rendered, and a stale tagged one is pruned;
- `-s 100.64.0.0/10 ! -i tailscale0 -j DROP` (the backup Wi-Fi hands out tailnet-range addresses);
- `! -i tailscale0 -m conntrack --ctstate NEW --ctorigdst {{ tailnet_ip }} -j DROP` (docs/CONTRACTS.md §12.10),
  when `tailnet_ip` is defined, and the ip6tables twin for `tailnet_ip6` when that is defined. Ports
  published on the tailnet address (ai-box: Open WebUI, the gpu-broker) are DNAT and bypass UFW, so a LAN
  host with a static route to the tailnet address via the LAN interface would otherwise reach them. Only
  new connections are matched (`--ctorigdst` is the address before DNAT), so replies and the containers'
  own outbound tailnet traffic are untouched. On the media server nothing is published on the tailnet
  address (its apps sit on 127.0.0.1 behind `tailscale serve`, wol-relay is host-network on tailscale0),
  so no legitimate flow matches;
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
docker.service and its drop-ins are not touched. If Docker isn't running (or isn't installed yet, as on
ai-box before role docker), the unit is only enabled and runs when Docker starts.

Variables: `firewall_allow_rules` (group_vars); `docker_dns_ip` (group_vars); `lan_iface`, `wifi_iface`
(optional) and whatever the allow rules reference, e.g. `lan_cidr` (network address), `gaming_pc_lan_ip`
(host_vars).

Tests: `python3 -m unittest discover -s roles/firewall/tests -v` (the filter, and the guard script
rendered with and without `wifi_iface`).
