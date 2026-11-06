#!/usr/bin/env bash
# Offline test for role mission_control (no root, no network, nothing applied to this machine):
# 1. Static: every task in the role's task files that touches a secret (the VAPID key file and its scripts, the
#    journal scan, the HA token file, the subscriptions file) is no_log (check_no_log.py).
# 2. Runs test_mission_control.yml with -vvv --diff against tests/inventory.yml, the role's filter plugins loaded and
#    the fakes in tests/bin (fake pip, fake journalctl, fake systemd-run) wired through MC_TEST_* paths.
# 3. Proves no secret reached the output: the VAPID private key the run generated (as stored, base64 and hex), and
#    run.sh's canary, which the test plants in the HA token and the subscriptions (text and base64 at 3 alignments).
#   roles/mission_control/tests/run.sh          (from anywhere; uses the repo's .venv)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
"$repo/.venv/bin/python" "$here/check_no_log.py" "$here"/../tasks/*.yml
tmp="$(mktemp -d --suffix=.mission-control)"
trap '[ -n "${KEEP:-}" ] || rm -rf "$tmp"' EXIT
t="$tmp/t"
mkdir -p "$t"
canary="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
( umask 077; printf '%s' "$canary" >"$t/canary" )
log="$tmp/run.log"
export ANSIBLE_FILTER_PLUGINS="$here/../filter_plugins"
export MC_TEST_PIP_LOG="$t/pip.log"
export MC_TEST_JOURNAL="$t/journal.txt" MC_TEST_JOURNAL_ARGS="$t/journalctl.args"
export MC_TEST_RUN_ARGS="$t/systemd-run.args" MC_TEST_PROBE_OUT="$t/probe.out"
export PATH="$here/bin:$PATH"
cd "$repo"   # ansible.cfg: collections_path, roles_path
set +e
"$repo/.venv/bin/ansible-playbook" -vvv --diff -i "$here/inventory.yml" "$here/test_mission_control.yml" \
  -e "test_tmp=$t" </dev/null >"$log" 2>&1
rc=$?
set -e
tail -n 4 "$log"
forms() {
  python3 - "$1" <<'PY'
import base64, sys
s = sys.argv[1].encode()
print(s.decode())
for i in range(3):
    c = s[i:]
    c = c[: len(c) // 3 * 3]
    print(base64.b64encode(c).decode())
PY
}
leaks=0
check() {  # name, secret
  local form
  while IFS= read -r form; do
    [ "${#form}" -ge 16 ] || continue
    if grep -qF -- "$form" "$log"; then echo "FAIL: the $1 appears in the output (as ${form:0:6}...)"; leaks=1; fi
  done < <(forms "$2")
}
check "canary (HA token, subscriptions)" "$canary"
vapid="$t/etc/mission-control/vapid.json"
if [ -s "$vapid" ]; then
  while IFS= read -r line; do
    name="${line%% *}"; value="${line#* }"
    check "VAPID private key ($name)" "$value"
  done < <(python3 - "$vapid" <<'PY'
import base64, json, sys
k = json.load(open(sys.argv[1]))["privateKey"]
raw = base64.urlsafe_b64decode(k + "=" * (-len(k) % 4))
print("stored", k)
print("hex", raw.hex())
print("base64", base64.b64encode(raw).decode().rstrip("="))
PY
)
else
  echo "FAIL: the run generated no VAPID key file"; leaks=1
fi
[ "$rc" -eq 0 ] || { echo "FAIL: playbook rc=$rc"; grep -nE 'FAILED|fatal|ERROR' "$log" | tail -20 || true; exit 1; }
for task in 'Venv | create venv.new from the system Python' 'VAPID | generate the key pair (only when there is none)' \
            'Guard | 8097 root only; :8443 only from the tailscale interface' 'Guard | only the answered dial reads as a leak' \
            'Wiring | include order' 'Venv | failed in venv.new at the hash check' \
            'VAPID | the subject was rewritten and nothing else' 'VAPID | every vector agrees' \
            'Verify | the probe.s results match the IP filter' 'Verify | the journal holds none of them' \
            'The renders equal tests/expected/ literally' 'Serve | both roles declare the identical set'; do
  grep -qE "TASK \[$task" "$log" || { echo "FAIL: task did not run: $task"; exit 1; }
done
[ "$leaks" -eq 0 ] || exit 1
echo "PASS: mission_control offline test ($(grep -c '^TASK' "$log") tasks); no secret (VAPID key, canary; text or base64) in the -vvv --diff output ($(wc -l < "$log") lines)"
