#!/usr/bin/env bash
# Offline test for role finance_digest (no root, nothing applied to this machine):
# 1. Static: every task in the role's task files that touches the Notion token is no_log (check_no_log.py).
# 2. Runs test_finance_digest.yml with -vvv --diff, the role's filter plugins loaded and a fake systemd-run
#    (tests/bin) first on PATH, in a scratch dir that must sit on a real mount other than / (the preflight
#    uses that mount as the "library share"). A synthetic canary Notion token is passed in as test_canary.
# 3. Proves the canary never reached the output, as text or as base64 at any of the 3 byte alignments (so
#    base64 of a whole file that embeds it, slurp's form, is caught too), and that the fake dry run's
#    balance line (Noah's figures, in real life) never did either.
#   roles/finance_digest/tests/run.sh            (from anywhere; uses the repo's .venv)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
"$repo/.venv/bin/python" "$here/check_no_log.py" "$here"/../tasks/*.yml
tmp="$(mktemp -d --suffix=.finance-digest)"
trap '[ -n "${KEEP:-}" ] || rm -rf "$tmp"' EXIT
mnt="$(findmnt --noheadings --first-only --output TARGET --target "$tmp")"
[ "$mnt" != / ] || { echo "SKIP: $tmp is on /; set TMPDIR to a directory on another mount (a tmpfs /tmp works)"; exit 1; }
canary="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
( umask 077; printf '%s\n' "$canary" >"$tmp/canary" )
log="$tmp/run.log"
export ANSIBLE_FILTER_PLUGINS="$here/../filter_plugins"
export FD_TEST_RUN_ARGS="$tmp/systemd-run.args"
export PATH="$here/bin:$PATH"
cd "$repo"   # ansible.cfg: collections_path, roles_path
set +e
"$repo/.venv/bin/ansible-playbook" -vvv --diff -i localhost, -c local "$here/test_finance_digest.yml" \
  -e "test_tmp=$tmp/t" -e "test_canary=$canary" </dev/null >"$log" 2>&1
rc=$?
set -e
tail -n 4 "$log"
# Every form a secret can take in the output: the text, and base64 of it at each of the 3 alignments.
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
    if grep -qF -- "$form" "$log"; then echo "FAIL: the $1 appears in the output (as ${form:0:6}...)"; leaks=1; fi
  done < <(forms "$2")
}
check "canary Notion token" "$canary"
if [ -s "$tmp/t/fake/canary" ]; then
  check "dry run's balance line" "BALANCE-CANARY-$(cat "$tmp/t/fake/canary")"
else
  echo "FAIL: the fake digest's canary is missing"; leaks=1
fi
[ "$rc" -eq 0 ] || { echo "FAIL: playbook rc=$rc"; grep -nE 'FAILED|fatal|ERROR' "$log" | head -20; exit 1; }
grep -q 'TASK \[Config | notion.json (Notion on)\]' "$log" || { echo "FAIL: the Notion file task did not run"; exit 1; }
grep -q 'TASK \[Verify | a dry run as fdigest in the unit.s sandbox\]' "$log" || { echo "FAIL: verify.yml did not run"; exit 1; }
[ "$leaks" -eq 0 ] || exit 1
echo "PASS: finance_digest offline test ($(grep -c '^TASK' "$log") tasks); the canary token and the dry-run figures (text or base64) are not in the -vvv --diff output ($(wc -l < "$log") lines)"
