#!/usr/bin/env bash
# 1. Static: every task in the role's task files that touches the token is no_log (check_no_log.py;
#    this covers tasks/verify.yml, which needs a live agent and can't run offline).
# 2. Runs test_ai_host_agent.yml with -vvv --diff, then proves no token reached the output, neither
#    the synthetic canary the test plants as an existing token nor the token the role generated, as
#    text or base64 (slurp returns base64 of the file: token + LF).
#   roles/ai_host_agent/tests/run.sh            (from anywhere; uses the repo's .venv)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
"$repo/.venv/bin/python" "$here/check_no_log.py" "$here"/../tasks/*.yml
tmp="$(mktemp -d --suffix=.ai-host-agent)"
trap '[ -n "${KEEP:-}" ] || rm -rf "$tmp"' EXIT
canary="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
( umask 077; printf '%s\n' "$canary" > "$tmp/canary-token" )
chmod 0640 "$tmp/canary-token"
log="$tmp/run.log"
cd "$repo"   # ansible.cfg: collections_path, roles_path
set +e
"$repo/.venv/bin/ansible-playbook" -vvv --diff -i localhost, -c local "$here/test_ai_host_agent.yml" \
  -e "test_tmp=$tmp" -e "test_canary=$canary" </dev/null >"$log" 2>&1
rc=$?
set -e
tail -n 4 "$log"
[ "$rc" -eq 0 ] || { echo "FAIL: playbook rc=$rc"; grep -nE 'FAILED|fatal|ERROR' "$log" | head -20; exit 1; }
gen="$(tr -d '\n' < "$tmp/generated-token")"
[ "${#gen}" -eq 64 ] || { echo "FAIL: no generated token was kept for the check"; exit 1; }
leaks=0
for name in canary generated; do
  if [ "$name" = canary ]; then tok="$canary"; else tok="$gen"; fi
  for form in "$tok" "$(printf '%s' "$tok" | base64 -w0)" "$(printf '%s\n' "$tok" | base64 -w0)"; do
    if grep -qF -- "$form" "$log"; then echo "FAIL: the $name token appears in the output (as ${form:0:6}...)"; leaks=1; fi
  done
done
[ "$leaks" -eq 0 ] || exit 1
echo "PASS: playbook ok; neither the canary nor the generated token (text or base64) is in the -vvv --diff output ($(wc -l < "$log") lines)"
