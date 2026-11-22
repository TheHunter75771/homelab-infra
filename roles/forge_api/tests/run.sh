#!/usr/bin/env bash
# Offline test for role forge_api (no root, nothing applied to this machine). Runs test_forge_api.yml with -vvv
# --diff, the role's filter plugins loaded and fakes for every host command the verify runs (tests/bin: systemctl,
# systemd-run, sysctl, ip, ss, curl, nsenter, docker, ufw, journalctl) first on PATH, in a scratch dir that must sit on a
# real mount other than / (the switched-on preflight uses that mount as the "library share"). Afterwards the
# play's whole output is searched for every token the run generated: none may appear (the token is no_log and
# never passes through Ansible).
#   roles/forge_api/tests/run.sh            (from anywhere; the repo's .venv if present, else PATH)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
tmp="$(mktemp -d --suffix=.forge-api)"
trap '[ -n "${KEEP:-}" ] || rm -rf "$tmp"' EXIT
mnt="$(findmnt --noheadings --first-only --output TARGET --target "$tmp")"
[ "$mnt" != / ] || { echo "SKIP: $tmp is on /; set TMPDIR to a directory on another mount (a tmpfs /tmp works)"; exit 1; }
log="$tmp/run.log"
mkdir -p "$tmp/fakes"
export ANSIBLE_FILTER_PLUGINS="$here/../filter_plugins"
export FA_TEST_DIR="$tmp/fakes"
export FA_TEST_TOKEN="$tmp/t/etc/forge-api/token"
export FA_TEST_UNITS="$tmp/t/systemd"   # the test's forge_api_unit_dir: the fake systemctl reads the units there
export PATH="$here/bin:$PATH"
cd "$repo"   # ansible.cfg: collections_path, roles_path
# The repo's .venv when there is one (a workstation), else ansible-playbook on PATH (CI has no .venv).
playbook="$repo/.venv/bin/ansible-playbook"
[ -x "$playbook" ] || playbook="$(command -v ansible-playbook)" || { echo "FAIL: no ansible-playbook (.venv or PATH)"; exit 1; }
set +e
"$playbook" -vvv --diff -i localhost, -c local "$here/test_forge_api.yml" \
  -e "test_tmp=$tmp/t" </dev/null >"$log" 2>&1
rc=$?
set -e
tail -n 4 "$log"
[ "$rc" -eq 0 ] || { echo "FAIL: playbook rc=$rc (KEEP=1 keeps $log)"; grep -nE 'FAILED|fatal|ERROR' "$log" | head -20; exit 1; }
for t in "Verify | the token is not in the journal, which holds the service's own lines" \
         "Verify | Open WebUI's trust settings hold (forge_api_verify_owui_trust)"; do
  grep -qF "TASK [$t]" "$log" || { echo "FAIL: verify.yml did not run ($t)"; exit 1; }
done
# The trust checker on its own (Python unit test, stdlib only).
python3 -I -B "$here/test_owui_trust_check.py" >"$tmp/owui.log" 2>&1 || { cat "$tmp/owui.log"; echo "FAIL: test_owui_trust_check.py"; exit 1; }
# Every token file the run made (the generated one, the kept ones): none may appear anywhere in the output.
n=0
while IFS= read -r -d '' f; do
  t="$(head -n1 "$f")"
  [ "${#t}" -ge 32 ] || continue
  n=$((n + 1))
  if grep -qF -- "$t" "$log"; then echo "FAIL: a token from $f appears in the play's output"; exit 1; fi
done < <(find "$tmp/t" -name 'token*' -type f -print0)
[ "$n" -ge 2 ] || { echo "FAIL: expected at least 2 token files to search for, found $n"; exit 1; }
echo "PASS: forge_api offline test ($(grep -c '^TASK' "$log") tasks, $(wc -l < "$log") lines of output; $n tokens absent from it)"
