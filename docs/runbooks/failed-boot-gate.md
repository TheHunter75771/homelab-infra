# Runbook: understanding the boot gate

The gate (`/usr/local/sbin/bootgate`) decides whether Docker may start. Its state is in `/var/lib/bootgate/`:
- `status`: `ok` · `waiting` (boot check in progress) · `failed` (gave up; Docker held) · `media-lost` (dropped while running; media stack stopped)
- `attempts`: power-cycles used in this incident (max 1; reset on success)
- `history.jsonl`: one line per event: `pass`, `repair`, `powercycle`, `fail`, `retry-pass`, `media-lost`

| You see | Meaning | Do |
|---|---|---|
| `waiting` for minutes at boot | Normal for a cold, slow USB drive. A missing drive can keep it in `waiting` for about 30 min per boot (12 min device wait, repairs, `fsck -n` capped at 15 min) | Wait; SSH works meanwhile |
| `failed`, "Media drive failed" push | The drive was missing, or didn't verify (wrong UUID, sentinel missing, write test failed), through the boot check and its one power-cycle. Non-boot runs fail without power-cycling | `media-drive-failure.md` |
| `media-lost`, "Media drive lost" push | The drive vanished while running | `media-drive-failure.md` |
| `ok` but apps are down | Not the gate: check `sudo docker ps`, alert-floor pushes, app logs | `sudo docker compose -f /srv/homelab/stacks/<stack>/compose.yaml up -d` (sudo: the `.env` files are root 0600; for `utils` this rebuilds the local wol-relay image). The dashboard itself isn't a container: `systemctl status homelab-dashboard` |
| The box rebooted by itself once at boot | The gate's single power-cycle (or the hardware watchdog after a hang) | `grep powercycle /var/lib/bootgate/history.jsonl`; `journalctl -b -1 -e` |

**Useful commands:** `systemctl status bootgate docker`, `systemctl list-timers 'bootgate*'`, `sudo journalctl -t bootgate -b`, `touch /run/bootgate/rerun` (retry now, never reboots).

**While the status is `failed`, don't run `docker` commands.** Docker is held by the gate, and any docker CLI call starts it, which starts a new gate run.

**Never** `systemctl restart bootgate` (Docker requires it, so a restart also restarts Docker).
