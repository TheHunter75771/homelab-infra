#!/usr/bin/env bash
# Offline test for role research_gate (no root, nothing applied to this machine): runs test_research_gate.yml
# with the role's filter plugins loaded, in a scratch dir that must sit on a real mount other than / (the
# switched-on preflight case uses that mount as the "library share").
#   roles/research_gate/tests/run.sh            (from anywhere; uses the repo's .venv)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
tmp="$(mktemp -d --suffix=.research-gate)"
trap '[ -n "${KEEP:-}" ] || rm -rf "$tmp"' EXIT
mnt="$(findmnt --noheadings --first-only --output TARGET --target "$tmp")"
[ "$mnt" != / ] || { echo "SKIP: $tmp is on /; set TMPDIR to a directory on another mount (a tmpfs /tmp works)"; exit 1; }
log="$tmp/run.log"
export ANSIBLE_FILTER_PLUGINS="$here/../filter_plugins"
cd "$repo"   # ansible.cfg: collections_path, roles_path
set +e
"$repo/.venv/bin/ansible-playbook" -vv --diff -i localhost, -c local "$here/test_research_gate.yml" \
  -e "test_tmp=$tmp/t" </dev/null >"$log" 2>&1
rc=$?
set -e
tail -n 4 "$log"
[ "$rc" -eq 0 ] || { echo "FAIL: playbook rc=$rc"; grep -nE 'FAILED|fatal|ERROR' "$log" | head -20; exit 1; }
echo "PASS: research_gate offline test ($(grep -c '^TASK' "$log") tasks)"
