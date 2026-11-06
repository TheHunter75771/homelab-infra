# Role `ai_host_agent`

Installs AI Control's host agent Y (`apps/ai-control/host-agent`) on the **gaming PC**
(`docs/CONTRACTS.md` §12.15.3, §12.15.7). It is play 1 of `playbooks/ai-control.yml` (`hosts: workstations_root`).

## What it installs

| What | Where |
|---|---|
| Account | `aictl-agent`: system, `/usr/sbin/nologin`, no home, supplementary group `systemd-journal` only (`append: false`, so the list is exact). |
| Code | `/opt/ai-host-agent/ai-host-agent` (launcher, 0755) + `/opt/ai-host-agent/ai_host_agent/*.py` (0644), root-owned. No tests, README, example config or `__pycache__`. Anything else in that tree is removed, so the install dir must be absolute and end in `/ai-host-agent` (asserted). |
| Config | `/etc/ai-host-agent/` root:aictl-agent 0750; `config.json` root 0644: `binds` 127.0.0.1 + `gaming_pc_tailnet_ip`, `port` 11510, `token_file`, `ollama_url` `http://<gaming_pc_tailnet_ip>:<ollama_rocm_port, default 11434>` (Ollama binds only the tailnet address), `sudo_exes: ["/usr/bin/sudo"]`, plus `ai_host_agent_config_extra`. Validated by the installed agent's own loader before it is written. |
| Token | `/etc/ai-host-agent/token`, root:aictl-agent 0640, 64 hex chars + LF. See below. |
| Unit | `/etc/systemd/system/ai-host-agent.service`: Y's README unit line for line (the test diffs them), with two knobs: `ProtectProc=` (`ai_host_agent_protect_proc`) and the optional `IPAddressDeny=any` / `IPAddressAllow=localhost <PC tailnet> <ai-box tailnet>` (`ai_host_agent_ip_allowlist`, on by default). |

Code, config, token and unit changes restart the agent (handlers: daemon-reload, then restart).

## Run order

1. Preflight: inputs, `systemd-journal` exists, the app sources exist on the controller, and **11510 is in
   `ollama_rocm_policy_extra_ports`**. The firewall is module G's (role `ollama_rocm`, policy `homelab-ai-only`); this role only
   checks that the two agree. Run `playbooks/gaming-pc-ai.yml` after changing that list.
2. Account, code, config. **No write or prune goes through a symlink:** `/opt/ai-host-agent` and its `ai_host_agent/`
   must be absent or real directories, checked with `stat follow: false` before the first write and again right before the prune's `find`.
3. Token (generate if absent), SELinux labels (`restorecon`, drift repair only), unit, enable + start.
4. **Read-only checks** (not in check mode):
   - the unit is `active`;
   - `/healthz` is 200 with the token on 127.0.0.1 and on the tailnet bind, and 401 with no token or a wrong one;
   - `/v1/services` has no `error` entry, which shows `systemctl show` works under the unit;
   - `/v1/logs?unit=ollama` returns a line, which shows journalctl works through the `systemd-journal` group;
   - `/v1/ollama` returns a `models` list, not `{error}`: the agent reaches Ollama on the tailnet address, through the unit's
     IP allowlist (which carries this PC's own tailnet address);
   - `/v1/sudo?user=<connection user>` returns this play's own sudo row, which shows the `_EXE`/`_UID` match works on real lines. It runs only when the connection user is `claude` or `localai`;
   - with SELinux on, the agent runs as `unconfined_service_t`, and `ausearch` for its uid must exit 1 with `<no matches>`. Any
     other outcome fails, including a missing `ausearch` (`tasks/verify_avc.yml`, tested with stub programs).

Then the playbook includes `tasks_from: export_token` (below). The playbook pings the PC first and ends play 1 for it
when it's unreachable (ai-box keeps its token). It rescues a failure, clearing `ai_host_agent_token`, lets play 2 run,
and fails the run at the end. **If the PC drops off in the middle of play 1**, `rescue` can't catch an unreachable host,
so the run stops before play 2.

## The token

- **Created once, on the host.** Python on the gaming PC runs `secrets.token_hex(32)` and writes the result into a new file
  (`O_CREAT|O_EXCL|O_NOFOLLOW`; chown and chmod happen before the write), only when the file is absent.
  - The value never passes through Ansible.
  - A converge never rotates it.
  - An existing token is kept byte for byte.
  - A symlink or non-regular file is refused (the agent would refuse it too).
- **Exported** by `tasks/export_token.yml`: `slurp` (no_log) sets the host fact `ai_host_agent_token` (no_log), checks
  that it's well-formed (no_log), and fails if the file is missing. In check mode before the first real run, the fact is `""`.
  It also sets `ai_host_agent_exported_port` (not secret), which ai_control checks against the port it calls.
- **Rotation (manual):** delete `/etc/ai-host-agent/token` on the gaming PC, then run both plays of `playbooks/ai-control.yml`.
- **Never in output:** every task that holds the value is `no_log`. `tests/check_no_log.py` enforces that statically on every
  task file, including `verify.yml`, which needs a live agent. The test runs at `-vvv --diff` and greps the whole output for a
  planted canary and for the generated token, each as text, as base64 and as base64 with a trailing LF (slurp's form).

## Host checks for the Integrator

- **`ProtectProc=invisible` vs `systemctl show`:** the role's `/v1/services` check proves it under the real unit. If it fails,
  set `ai_host_agent_protect_proc: default` in inventory, converge, and record the change in CONTRACTS.
- **SELinux:** no custom policy, port label or boolean, the same as `gpu_gate`/`ollama_rocm`. systemd (`init_t`) runs
  `/usr/bin/python3` (`bin_t`) as `unconfined_service_t`. `/opt` is `usr_t` and `/etc/ai-host-agent` is `etc_t` by default
  file contexts. The role asserts the domain and checks `ausearch -m AVC,USER_AVC -ts recent -ui <aictl-agent uid>`. Also
  run `ausearch -m avc -ts recent` after a few dashboard refreshes.
- **`_EXE` sudo lines:** the role's `/v1/sudo` check covers `claude`. For `localai`, run `sudo -l` once as localai (a refused row),
  then confirm the Audit tab shows it as "Refused".
- **Firewall:** from a LAN host, `curl -m3 http://<gaming-pc-lan-ip>:11510/healthz` and `curl -m3 http://<gaming-pc-tailnet-ip>:11510/healthz`
  must both fail. From ai-box, the second gets 401 without a token.
- **The IP allowlist** is a BPF cgroup filter (cgroup v2). `systemctl show -p IPAddressAllow ai-host-agent` lists the addresses.

## Tests

```sh
roles/ai_host_agent/tests/run.sh     # from anywhere; runs from the repo root with the repo's .venv
```

`tests/test_ai_host_agent.yml` runs the role's own task files against a scratch dir, with stub handlers, no root, and synthetic addresses:
- **install:** run twice with strays in between, and checked for pruning, idempotence and the refusal of a wrongly named dir.
  Two symlink cases (the install dir, and its `ai_host_agent/`, each pointing at a victim dir) must be refused by the guard, with
  the victim untouched;
- **preflight:** passes, and refuses a role-owned extra key or a port outside G's list;
- **config:** validated by the agent's loader;
- **token:** generated (0640, 64 hex, already right at creation: the owner/mode task changes nothing), kept on a rerun (same inode and checksum), an existing token kept, a symlink refused by
  both the token and export tasks, and accepted by the real agent's `--check`;
- **export:** the fact equals the file, and the exported port is 11510;
- **AVC check** (`verify_avc.yml`) with stub `ausearch` programs: it passes only on exit 1 with `<no matches>`. Denials (rc 0),
  another error message, a crash and a missing program all fail;
- **unit:** rendered in all four knob combinations, `systemd-analyze verify` with any warning failing, and line-for-line equal to
  Y's README unit.

`run.sh` runs `check_no_log.py` first, then the playbook. It fails if the canary or the generated token appears anywhere in
the `-vvv --diff` output, as text or base64.
