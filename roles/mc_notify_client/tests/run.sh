#!/usr/bin/env bash
# Offline test for role mc_notify_client (no root, no network, nothing applied to this machine, nothing sent):
# 1. Static: every task that names the token is no_log, and no assert checks or prints it (check_no_log.py);
#    the checker is run on its own mutants too.
# 2. The CLI's unit tests (files/tests: exit codes, config and token-file checks, tags, --unit, the token never in
#    stdout/stderr, HTTPS verified), against a local fake Mission Control.
# 3. test_mc_notify_client.yml with -vvv --diff against tests/inventory.yml (system scope, user scope, the units).
# 4. Proves the token never reached the output: run.sh's canary (the test's token is MCN-<canary>), as text and as
#    base64 at 3 alignments, and any 12 consecutive characters of it.
#   roles/mc_notify_client/tests/run.sh          (from anywhere; uses the repo's .venv)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
"$repo/.venv/bin/python" "$here/check_no_log.py" "$here"/../tasks/*.yml
# ...and it catches its mutants (an assert message or condition on the token, a write without no_log) but not prose.
mut="$("$repo/.venv/bin/python" "$here/check_no_log.py" "$here/fixtures/check_no_log_mutants.yml" || true)"
for want in 'message prints the token (its fail_msg templates the token)' 'checks the token itself (an assert must check' \
            'writes the token without no_log'; do
  grep -qF -- "$want" <<<"$mut" || { echo "FAIL: check_no_log.py missed the mutant: $want"; exit 1; }
done
if grep -qF 'prose names the token' <<<"$mut"; then echo "FAIL: check_no_log.py flagged prose"; exit 1; fi
echo "PASS: check_no_log.py catches its 3 mutants"
python3 -m unittest discover -s "$here/../files/tests" 2>&1 | tail -n 3
tmp="$(mktemp -d --suffix=.mc-notify-client)"
trap '[ -n "${KEEP:-}" ] || rm -rf "$tmp"' EXIT
t="$tmp/t"
mkdir -p "$t"
canary="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
( umask 077; printf '%s' "$canary" >"$t/token-canary" )
log="$tmp/run.log"
cd "$repo"   # ansible.cfg: collections_path, roles_path
set +e
"$repo/.venv/bin/ansible-playbook" -vvv --diff -i "$here/inventory.yml" "$here/test_mc_notify_client.yml" \
  -e "test_tmp=$t" </dev/null >"$log" 2>&1
rc=$?
set -e
tail -n 5 "$log"
leaks=0
while IFS= read -r form; do
  [ "${#form}" -ge 16 ] || continue
  if grep -qF -- "$form" "$log"; then echo "FAIL: the token canary appears in the output (as ${form:0:6}...)"; leaks=1; fi
done < <(python3 - "$canary" <<'PY'
import base64, sys
s = sys.argv[1].encode()
print(s.decode())
for i in range(3):
    c = s[i:]
    c = c[: len(c) // 3 * 3]
    print(base64.b64encode(c).decode())
PY
)
# Any 12 consecutive characters of the canary (a cut or pasted token). 12 random hex characters can't match by chance
# in a log this size; 8 could.
python3 - "$log" "$canary" <<'PY' || leaks=1
import re, sys
log = open(sys.argv[1], encoding="utf-8", errors="replace").read()
s = sys.argv[2]
m = re.search("|".join(re.escape(s[i:i + 12]) for i in range(len(s) - 11)), log)
if m:
    print(f"FAIL: 12 characters of the token canary appear in the output (offset {m.start()})")
    sys.exit(1)
PY
[ "$rc" -eq 0 ] || { echo "FAIL: playbook rc=$rc"; grep -nE 'FAILED|fatal|ERROR' "$log" | tail -20 || true; exit 1; }
# Task names matched literally (every ERE metacharacter escaped except `.`).
for task in 'Main with token with a newline' 'Config | the token (vault; 0600)' 'Idempotent (same checksums and mtimes)' \
            'Token and config back to the role.s content and 0600' 'The CLI.s verdict (user scope)' \
            'The renders equal tests/expected/ literally' 'Systemd-analyze verify (an instance of each unit)' \
            'The config tasks run in order' 'Refused by the directory guard' 'Main with user scope with home /'; do
  pat="$(printf '%s' "$task" | sed 's/[][()|*+?{}^$\\]/\\&/g')"
  grep -qE "TASK \[$pat" "$log" || { echo "FAIL: task did not run: $task"; exit 1; }
done
[ "$leaks" -eq 0 ] || exit 1
echo "PASS: mc_notify_client offline test ($(grep -c '^TASK' "$log") tasks); the token (text, base64 or any 12 characters) is not in the -vvv --diff output ($(wc -l < "$log") lines)"
