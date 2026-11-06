#!/usr/bin/env bash
# Offline test for role mission_control (no root, no network, nothing applied to this machine):
# 1. Static: every task in the role's task files that touches a secret (the VAPID key file and its scripts, the
#    journal scan, the HA token file, the subscriptions file) is no_log (check_no_log.py).
# 2. Runs test_mission_control.yml with -vvv --diff against tests/inventory.yml, the role's filter plugins loaded and
#    the fakes in tests/bin (fake pip, fake journalctl, fake systemd-run) wired through MC_TEST_* paths.
# 3. Proves no secret reached the output: the VAPID private key the run generated (as stored, base64 and hex),
#    run.sh's canary, which the test plants in the HA token and the subscriptions, and (B2) run.sh's notify canary,
#    which the test makes the two senders' tokens from (NTA-<canary>, NTB-<canary>): text and base64 at 3
#    alignments, plus each token's sha256 (config.json carries it; its render task is no_log, so even that is hidden),
#    and any 12 consecutive characters of either canary (cut, pasted or swapped tokens).
#   roles/mission_control/tests/run.sh          (from anywhere; uses the repo's .venv)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
"$repo/.venv/bin/python" "$here/check_no_log.py" "$here"/../tasks/*.yml
# ...and it catches its mutants: an assert message that templates the tokens, a token read without no_log (and lets
# an assert's prose name the variable).
mut="$("$repo/.venv/bin/python" "$here/check_no_log.py" "$here/fixtures/check_no_log_mutants.yml" || true)"
for want in 'an assert whose message prints the tokens (its fail_msg' 'reads the tokens without no_log'; do
  grep -qF -- "$want" <<<"$mut" || { echo "FAIL: check_no_log.py missed the mutant: $want"; exit 1; }
done
if grep -qF 'prose names the tokens' <<<"$mut"; then echo "FAIL: check_no_log.py flagged prose"; exit 1; fi
echo "PASS: check_no_log.py catches its 2 mutants"
tmp="$(mktemp -d --suffix=.mission-control)"
trap '[ -n "${KEEP:-}" ] || rm -rf "$tmp"' EXIT
t="$tmp/t"
mkdir -p "$t"
canary="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
notify_canary="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
( umask 077; printf '%s' "$canary" >"$t/canary"; printf '%s' "$notify_canary" >"$t/notify-canary" )
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
check "notify canary (the senders' tokens)" "$notify_canary"
for prefix in NTA NTB; do
  check "sha256 of the $prefix notify token" "$(printf '%s' "$prefix-$notify_canary" | sha256sum | cut -d' ' -f1)"
done
# Any 12 consecutive characters of either canary: a cut, pasted or swapped token (the test plants 32-, 33- and
# 43-character pieces of the notify canary as sender names and values). 12 random hex characters can't match by
# chance in a log this size (about 10^-6); 8 could.
python3 - "$log" "$canary" "$notify_canary" <<'PY' || leaks=1
import re, sys
log = open(sys.argv[1], encoding="utf-8", errors="replace").read()
bad = 0
for label, secret in (("canary", sys.argv[2]), ("notify canary", sys.argv[3])):
    m = re.search("|".join(re.escape(secret[i:i + 12]) for i in range(len(secret) - 11)), log)
    if m:
        print(f"FAIL: 12 characters of the {label} appear in the output (offset {m.start()})")
        bad = 1
sys.exit(bad)
PY
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
            'The renders equal tests/expected/ literally' 'Serve | both roles declare the identical set' \
            'Notify | preflight passes with every B2/B1 variable set' 'Notify | the render holds exactly the keys set' \
            'Notify | config_file.yml with every B2/B1 variable set' 'Notify | MC1.s loader on the full render' \
            'Preflight | notify senders have valid names'; do
  # Every ERE metacharacter in a name is literal except `.` (the list writes an apostrophe as `.`). Unescaped, " | "
  # was an alternation that matched any task, and "(...)" a group that never matched the real name.
  pat="$(printf '%s' "$task" | sed 's/[][()|*+?{}^$\\]/\\&/g')"
  grep -qE "TASK \[$pat" "$log" || { echo "FAIL: task did not run: $task"; exit 1; }
done
[ "$leaks" -eq 0 ] || exit 1
echo "PASS: mission_control offline test ($(grep -c '^TASK' "$log") tasks); no secret (VAPID key, canary, notify tokens; text, base64 or any 12 characters; token hashes) in the -vvv --diff output ($(wc -l < "$log") lines)"
