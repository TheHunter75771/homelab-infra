#!/usr/bin/env bash
# Offline test for role voice_profile (no root, nothing applied to this machine). Runs test_voice_profile.yml
# with -vvv --diff, the role's filter plugins loaded and a fake systemd-run and systemctl (tests/bin) first on
# PATH, in a scratch dir that must sit on a real mount other than / (the preflight uses that mount as the
# "library share"). The role holds no secrets, so there is no canary check.
#   roles/voice_profile/tests/run.sh            (from anywhere; the repo's .venv if present, else PATH)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
tmp="$(mktemp -d --suffix=.voice-profile)"
trap '[ -n "${KEEP:-}" ] || rm -rf "$tmp"' EXIT
mnt="$(findmnt --noheadings --first-only --output TARGET --target "$tmp")"
[ "$mnt" != / ] || { echo "SKIP: $tmp is on /; set TMPDIR to a directory on another mount (a tmpfs /tmp works)"; exit 1; }
log="$tmp/run.log"
export ANSIBLE_FILTER_PLUGINS="$here/../filter_plugins"
export VP_TEST_RUN_ARGS="$tmp/systemd-run.args"
export VP_TEST_ACTIVE="$tmp/systemctl.active"
export VP_TEST_UNIT="$tmp/systemctl.unit"
export PATH="$here/bin:$PATH"
cd "$repo"   # ansible.cfg: collections_path, roles_path
# The repo's .venv when there is one (a workstation), else ansible-playbook on PATH (CI has no .venv).
playbook="$repo/.venv/bin/ansible-playbook"
[ -x "$playbook" ] || playbook="$(command -v ansible-playbook)" || { echo "FAIL: no ansible-playbook (.venv or PATH)"; exit 1; }
set +e
"$playbook" -vvv --diff -i localhost, -c local "$here/test_voice_profile.yml" \
  -e "test_tmp=$tmp/t" </dev/null >"$log" 2>&1
rc=$?
set -e
tail -n 4 "$log"
[ "$rc" -eq 0 ] || { echo "FAIL: playbook rc=$rc (KEEP=1 keeps $log)"; grep -nE 'FAILED|fatal|ERROR' "$log" | head -20; exit 1; }
grep -q 'TASK \[Verify | voice-profile status as vprof in the unit.s sandbox\]' "$log" || { echo "FAIL: verify.yml did not run"; exit 1; }
echo "PASS: voice_profile offline test ($(grep -c '^TASK' "$log") tasks, $(wc -l < "$log") lines of output)"
