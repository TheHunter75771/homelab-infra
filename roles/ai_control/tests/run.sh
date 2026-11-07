#!/usr/bin/env bash
# 1. Static: every task in the role's task files that touches the token is no_log (check_no_log.py).
# 2. Runs test_ai_control.yml with -vvv --diff against the synthetic tests/inventory.yml, then proves
#    the agent token (a synthetic canary passed in as ai_control_agent_token) never reached the
#    output, as text or base64 (slurp returns base64 of the file: token + LF).
#   roles/ai_control/tests/run.sh            (from anywhere; uses the repo's .venv)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
"$repo/.venv/bin/python" "$here/check_no_log.py" "$here"/../tasks/*.yml
tmp="$(mktemp -d --suffix=.ai-control)"
trap '[ -n "${KEEP:-}" ] || rm -rf "$tmp"' EXIT
canary="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
log="$tmp/run.log"
cd "$repo"   # ansible.cfg: collections_path, roles_path
set +e
"$repo/.venv/bin/ansible-playbook" -vvv --diff -i "$here/inventory.yml" "$here/test_ai_control.yml" \
  -e "test_tmp=$tmp" -e "test_canary=$canary" </dev/null >"$log" 2>&1
rc=$?
set -e
tail -n 4 "$log"
[ "$rc" -eq 0 ] || { echo "FAIL: playbook rc=$rc"; grep -nE 'FAILED|fatal|ERROR' "$log" | head -20; exit 1; }
leaks=0
for form in "$canary" "$(printf '%s' "$canary" | base64 -w0)" "$(printf '%s\n' "$canary" | base64 -w0)"; do
  if grep -qF -- "$form" "$log"; then echo "FAIL: the canary token appears in the output (as ${form:0:6}...)"; leaks=1; fi
done
[ "$leaks" -eq 0 ] || exit 1
echo "PASS: playbook ok; the canary token (text or base64) is not in the -vvv --diff output ($(wc -l < "$log") lines)"
