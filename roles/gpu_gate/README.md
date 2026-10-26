# Role `gpu_gate`

Installs `apps/gpu-gate` on the gaming PC (`docs/CONTRACTS.md` §12.4, §12.9, §12.10). Applied by
`playbooks/gaming-pc-ai.yml` after role `ollama_rocm`, which creates the `ollama` user the gate runs as and
the firewalld rules for port 11500.

| What | Where |
|---|---|
| Code | `/opt/gpu-gate/gpu-gate` (launcher, 0755) + `/opt/gpu-gate/gpu_gate/*.py` (0644), root-owned. No tests, README or caches. Anything else in that tree is removed, so `gpu_gate_install_dir` must be an absolute path ending in `/gpu-gate` (asserted). |
| Config | `/etc/gpu-gate/config.json` (root 0644): `binds` = the gaming PC's tailnet IPv4 (+ IPv6 when `gaming_pc_tailnet_ip6` is set), `port` 11500, `ollama_url` `http://<tailnet IPv4>:11434`. Extra keys: `gpu_gate_config_extra`. Validated by the installed app's own loader before it's written. |
| Unit | `apps/gpu-gate/gpu-gate.service`, installed **verbatim** to `/etc/systemd/system/gpu-gate.service` (its paths already match). `User=ollama`, no capabilities. |

The gate binds with `IP_FREEBIND` (IPv4 and IPv6), so it starts before tailscaled has set the
addresses. Code, config or unit changes restart it.

## Tests
```sh
.venv/bin/ansible-playbook -i localhost, -c local roles/gpu_gate/tests/test_gpu_gate.yml </dev/null 2>&1 | cat
```
The test installs into a scratch dir twice, with a stale module, a `__pycache__` and a stray file
planted in between, and checks they're pruned. It asserts the third install changed nothing and that a
dir not named `gpu-gate` is refused before anything is written. It checks the exact file set and modes, renders and
validates the config (both families, and IPv4 only), and checks that validation leaves no bytecode.
It also runs `systemd-analyze verify` on the unit (any warning fails).
