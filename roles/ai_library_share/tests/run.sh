#!/usr/bin/env bash
# Offline tests for role ai_library_share (no root, nothing applied to this machine):
#  1. Static: every task that touches the ailib password is no_log (check_no_log.py; covers the
#     tasks that only run on the real hosts).
#  2. The backup script's unit tests (test_backup.py, under `unshare -rm` with tmpfs drives): rotation,
#     a crash at every rotation step, --no-links/--no-specials, rsync 23 and a snapshot without the
#     sentinel refused, and every refusal (unmounted drives, exact <mount>/ai-library paths, '..',
#     symlinks, wrong fs, not a library, no sentinel, overlap, lock) with other backups intact.
#  3. test_ai_library_share.yml with -vvv --diff, then proves no password reached the output: neither
#     the synthetic canary the test plants as an existing password nor the one the role generated, as
#     text or base64 (slurp returns base64 of the file: password + LF), nor the credentials file's
#     content in base64.
#   roles/ai_library_share/tests/run.sh            (from anywhere; uses the repo's .venv)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
"$repo/.venv/bin/python" "$here/check_no_log.py" "$here"/../tasks/*.yml
command -v testparm >/dev/null || { echo "FAIL: testparm is missing (dnf install samba-common-tools)"; exit 1; }
rt="/run/user/$(id -u)"
findmnt -n --mountpoint "$rt" >/dev/null || { echo "FAIL: $rt must be a mount (it stands in for the share drive)"; exit 1; }
findmnt -n --mountpoint /tmp >/dev/null || { echo "FAIL: /tmp must be a mount (it stands in for the backup drive)"; exit 1; }
unshare -rm true || { echo "FAIL: unshare -rm (unprivileged user namespaces) is needed for the backup tests"; exit 1; }
# The backup tests mount tmpfs drives, so they run in a private user + mount namespace.
( cd "$here" && unshare -rm python3 -m unittest test_backup 2>&1 ) | tail -n 3
tmp="$(mktemp -d /tmp/ai-library-share.XXXXXX)"
trt="$rt/$(basename "$tmp")"   # same name: both test folders are exactly <mount>/<name>
mkdir -m 0700 "$trt"
trap '[ -n "${KEEP:-}" ] || rm -rf "$tmp" "$trt" "$trt-linked" "$trt-victim"' EXIT
canary="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
( umask 077; printf '%s\n' "$canary" > "$tmp/canary-password" )
log="$tmp/run.log"
cd "$repo"   # ansible.cfg: collections_path, roles_path
set +e
"$repo/.venv/bin/ansible-playbook" -vvv --diff -i localhost, -c local "$here/test_ai_library_share.yml" \
  -e "test_tmp=$tmp" -e "test_rt=$trt" -e "test_canary=$canary" </dev/null >"$log" 2>&1
rc=$?
set -e
tail -n 4 "$log"
[ "$rc" -eq 0 ] || { echo "FAIL: playbook rc=$rc"; grep -nE 'FAILED|fatal|ERROR' "$log" | head -20; exit 1; }
gen="$(tr -d '\n' < "$tmp/generated-password")"
[ "${#gen}" -eq 48 ] || { echo "FAIL: no generated password was kept for the check"; exit 1; }
leaks=0
for name in canary generated; do
  if [ "$name" = canary ]; then pw="$canary"; else pw="$gen"; fi
  for form in "$pw" "$(printf '%s' "$pw" | base64 -w0)" "$(printf '%s\n' "$pw" | base64 -w0)" \
              "$(printf '%s\n%s\n' "$pw" "$pw" | base64 -w0)" \
              "$(printf 'username=ailib\npassword=%s\n' "$pw" | base64 -w0)"; do
    if grep -qF -- "$form" "$log"; then echo "FAIL: the $name password appears in the output (as ${form:0:6}...)"; leaks=1; fi
  done
done
[ "$leaks" -eq 0 ] || exit 1
echo "PASS: playbook ok; neither the canary nor the generated password (text or base64) is in the -vvv --diff output ($(wc -l < "$log") lines)"
