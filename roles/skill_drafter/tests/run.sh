#!/usr/bin/env bash
# Offline test for role skill_drafter (no root, nothing applied to this machine). Runs test_skill_drafter.yml
# with -vvv --diff, the role's filter plugins loaded and a fake systemd-run and systemctl (tests/bin) first on
# PATH, in a scratch dir that must sit on a real mount other than / (the preflight uses that mount as the
# "library share"). The role holds no secrets, so there is no canary check.
#   roles/skill_drafter/tests/run.sh            (from anywhere; uses the repo's .venv)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
tmp="$(mktemp -d --suffix=.skill-drafter)"
trap '[ -n "${KEEP:-}" ] || rm -rf "$tmp"' EXIT
mnt="$(findmnt --noheadings --first-only --output TARGET --target "$tmp")"
[ "$mnt" != / ] || { echo "SKIP: $tmp is on /; set TMPDIR to a directory on another mount (a tmpfs /tmp works)"; exit 1; }
log="$tmp/run.log"
export ANSIBLE_FILTER_PLUGINS="$here/../filter_plugins"
export SD_TEST_RUN_ARGS="$tmp/systemd-run.args"
export SD_TEST_SYSTEMCTL="$tmp/systemctl.mode"
export PATH="$here/bin:$PATH"
cd "$repo"   # ansible.cfg: collections_path, roles_path
set +e
"$repo/.venv/bin/ansible-playbook" -vvv --diff -i localhost, -c local "$here/test_skill_drafter.yml" \
  -e "test_tmp=$tmp/t" </dev/null >"$log" 2>&1
rc=$?
set -e
tail -n 4 "$log"
[ "$rc" -eq 0 ] || { echo "FAIL: playbook rc=$rc (KEEP=1 keeps $log)"; grep -nE 'FAILED|fatal|ERROR' "$log" | head -20; exit 1; }
grep -q 'TASK \[Verify | skill-drafter status as sdraft in the unit.s sandbox\]' "$log" || { echo "FAIL: verify.yml did not run"; exit 1; }
echo "PASS: skill_drafter offline test ($(grep -c '^TASK' "$log") tasks, $(wc -l < "$log") lines of output)"
