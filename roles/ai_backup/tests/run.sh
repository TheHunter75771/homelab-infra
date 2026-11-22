#!/usr/bin/env bash
# Offline tests for role ai_backup (no root, nothing applied to this machine):
#  1. Static: every task that touches the pull key's private half is no_log (check_no_log.py).
#  2. The script's unit tests (test_script.py, under `unshare -rm`: a read-only bind of a fake volume with a live
#     WAL database, tmpfs "backup drives"): stage, verify, pull, retention, restore-check, every refusal.
#  3. test_ai_backup.yml with -vvv --diff, then proves the private key the role generated never reached the
#     output: neither its text, nor any line of its base64 body, nor base64 of the whole file at any of the 3
#     byte alignments (slurp's form).
#   roles/ai_backup/tests/run.sh            (from anywhere; uses the repo's .venv)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
"$repo/.venv/bin/python" "$here/check_no_log.py" "$here"/../tasks/*.yml
findmnt -n --mountpoint /tmp >/dev/null || { echo "FAIL: /tmp must be a mount (it stands in for the backup drive)"; exit 1; }
unshare -rm true || { echo "FAIL: unshare -rm (unprivileged user namespaces) is needed for the script tests"; exit 1; }
command -v rsync >/dev/null || { echo "FAIL: rsync is needed for the pull tests"; exit 1; }
( cd "$here" && unshare -rm python3 -m unittest test_script 2>&1 ) | tail -n 3 | tee /dev/stderr | grep -q '^OK' \
  || { echo "FAIL: script unit tests"; exit 1; }
tmp="$(mktemp -d /tmp/ai-backup.XXXXXX)"
trap '[ -n "${KEEP:-}" ] || rm -rf "$tmp"' EXIT
log="$(mktemp /tmp/ai-backup-log.XXXXXX)"
trap '[ -n "${KEEP:-}" ] || rm -rf "$tmp" "$log"' EXIT
cd "$repo"   # ansible.cfg: collections_path, roles_path
set +e
"$repo/.venv/bin/ansible-playbook" -vvv --diff -i localhost, -c local "$here/test_ai_backup.yml" \
  -e "test_tmp=$tmp" </dev/null >"$log" 2>&1
rc=$?
set -e
tail -n 4 "$log"
# The refusal cases print expected errors (rescued); the real failure is the last task before the recap.
[ "$rc" -eq 0 ] || { echo "FAIL: playbook rc=$rc at $(grep '^TASK \[' "$log" | tail -n 1)"; grep -E '^\[ERROR\]' "$log" | tail -n 1; exit 1; }
key="$tmp/etc/ai-backup/pull_ed25519"
[ -s "$key" ] || { echo "FAIL: the generated key is missing"; exit 1; }
leaks=0
while IFS= read -r form; do
  [ "${#form}" -ge 16 ] || continue
  if grep -qF -- "$form" "$log"; then echo "FAIL: the private key appears in the output (as ${form:0:8}...)"; leaks=1; fi
done < <(python3 - "$key" <<'PY'
import base64, sys
data = open(sys.argv[1], "rb").read()
for line in data.decode().splitlines():
    if not line.startswith("-----"):
        print(line)
for i in range(3):
    c = data[i:]
    c = c[: len(c) // 3 * 3]
    print(base64.b64encode(c).decode()[:200])
PY
)
[ "$leaks" -eq 0 ] || exit 1
echo "PASS: ai_backup offline test ($(grep -c '^TASK' "$log") tasks); the generated private key (text or base64) is not in the -vvv --diff output ($(wc -l < "$log") lines)"
