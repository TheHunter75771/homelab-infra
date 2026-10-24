# Role `heartbeat`

Off-site liveness watcher for the media server, applied to the edge VPS by
`playbooks/edge.yml` (spec: `docs/CONTRACTS.md` §8). Every 2 minutes it runs a tailscale ping
plus a TCP 22 check against `heartbeat_target`. After 3 failures in a row it sends one Web Push
("offline"), and one more when the box answers again. Each check is logged to
`/var/lib/homelab-heartbeat/log.jsonl` (Contract 8).

It only touches `/opt/homelab-heartbeat`, `/var/lib/homelab-heartbeat` and
`/etc/systemd/system/homelab-heartbeat.*`. The VPS runs other services.

- Re-run edge.yml after changing `rehearsal`: the flag is baked into the VPS config
  (`/opt/homelab-heartbeat/config.json`) at deploy time.
- Re-run edge.yml after a device subscribes or unsubscribes: the VPS keeps its own copy of the
  box's `vapid.json` and `subscriptions.json`, refreshed only by the playbook.
- Check delivery: `/opt/homelab-heartbeat/venv/bin/python /opt/homelab-heartbeat/homelab_heartbeat.py --test-push`
  (exit 0 delivered, 2 no subscribed device, 1 error).
- Tests: `python3 -m unittest discover -s roles/heartbeat/tests` (the real-pywebpush round trip
  runs only with a python that has pywebpush).
