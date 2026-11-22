#!/usr/bin/env bash
# Offline test for role unit_hardening (no root, no systemd, nothing applied to this machine):
# 1. test_filters.py: the filter plugin (systemctl cat parsing, exec paths) and the "uses tailscaled" regex
#    against real-world positives and negatives; test_scripts.py: ns_probe.py and scan_exec.py run as the role
#    runs them, against scratch trees (hidden/stale/unreadable namespaces, deep helpers, symlinks, limits);
# 2. make_fixture.py builds a synthetic host under a scratch dir (unit files, apps, a real unix socket, a fake
#    /proc), and test_unit_hardening.yml runs the role against it with the fake systemctl (tests/bin/systemctl);
# 3. every check the playbook must reach did run;
# 4. test_check_mode.yml with --check on a fresh fixture: the preflight runs, nothing is written, reloaded or
#    restarted, and the verify is skipped.
#   roles/unit_hardening/tests/run.sh          (from anywhere; uses the repo's .venv)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
"$repo/.venv/bin/python" -B "$here/test_filters.py"
"$repo/.venv/bin/python" -B "$here/test_scripts.py"
tmp="$(mktemp -d --suffix=.unit-hardening)"
ps_pid=""
cleanup() {
  # Only the one server this script started, by its exact PID.
  [ -z "$ps_pid" ] || kill "$ps_pid" 2>/dev/null || true
  [ -n "${KEEP:-}" ] || { chmod -R u+rwX "$tmp" 2>/dev/null; rm -rf "$tmp"; }
}
trap cleanup EXIT
python3 "$here/make_fixture.py" "$tmp"
# Ollama's /api/ps for the Q6-4 busy check: <tmp>/www/api/ps on a free loopback port.
ps_port="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
python3 -m http.server --bind 127.0.0.1 --directory "$tmp/www" "$ps_port" >/dev/null 2>&1 &
ps_pid=$!
for _ in $(seq 50); do
  python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$ps_port/api/ps', timeout=1)" 2>/dev/null && break
  sleep 0.1
done
export UH_TEST_STATE="$tmp/state.json" UH_TEST_LOG="$tmp/systemctl.log"
log="$tmp/run.log"
cd "$repo"   # ansible.cfg: collections_path, roles_path
set +e
"$repo/.venv/bin/ansible-playbook" -v --diff -i "$here/inventory.yml" "$here/test_unit_hardening.yml" \
  -e "test_tmp=$tmp" -e "test_ps_port=$ps_port" </dev/null >"$log" 2>&1
rc=$?
set -e
tail -n 4 "$log"
[ "$rc" -eq 0 ] || { echo "FAIL: playbook rc=$rc"; grep -nE 'FAILED|fatal|ERROR' "$log" | grep -v '\.\.\.ignoring' | tail -20; exit 1; }
for task in 'Inputs refuses bad inputs' 'Preflight refuses units it can.t vouch for' \
            'Exec paths, scan coverage, and the unit-file scan.s view' 'The renders equal tests/expected/ literally' \
            'Converge 1 called daemon-reload once, then try-restart for exactly the restart-true units' \
            'Converge 2 changed nothing' 'Converge 3 restarted only svc-b, without a daemon-reload' \
            'Off.service lost its drop-in; one daemon-reload, no restart' \
            'A drop-in left on an undeclared unit fails the verify' 'A unit that dies once hardened fails the verify' \
            'Verify assertions on canned readings' 'The sweep fails the verify on it' \
            'A busy .or silent. Ollama is not restarted' 'Only Ollama was restarted' \
            'It passed without asking /api/ps or restarting' 'An idle Ollama was asked, then restarted without the override' \
            'The directory above the bus counts as the bus'; do
  grep -qE "TASK \[$task" "$log" || { echo "FAIL: task did not run: $task"; exit 1; }
done
# Check mode, on a fresh fixture: nothing written, nothing reloaded or restarted.
rm -rf "${tmp:?}/host" "${tmp:?}/proc" "${tmp:?}/opt" "${tmp:?}/etc" "${tmp:?}/data" "${tmp:?}/state.json" "${tmp:?}/systemctl.log"
python3 "$here/make_fixture.py" "$tmp" >/dev/null
set +e
"$repo/.venv/bin/ansible-playbook" --check --diff -i "$here/inventory.yml" "$here/test_check_mode.yml" \
  -e "test_tmp=$tmp" </dev/null >"$tmp/check.log" 2>&1
rc=$?
set -e
[ "$rc" -eq 0 ] || { echo "FAIL: check-mode playbook rc=$rc"; grep -nE 'FAILED|fatal|ERROR' "$tmp/check.log" | tail -10; exit 1; }
w="$(find "$tmp/host/etc/systemd" "$tmp/host/etc/tmpfiles.d" -type f)"
[ -z "$w" ] || { echo "FAIL: check mode wrote files: $w"; exit 1; }
! grep -qE '^(daemon-reload|try-restart)' "$tmp/systemctl.log" || { echo "FAIL: check mode reloaded or restarted"; exit 1; }
grep -qE 'TASK \[unit_hardening : Preflight \| nothing the units run uses' "$tmp/check.log" || { echo "FAIL: check mode skipped the preflight"; exit 1; }
! grep -qE 'TASK \[unit_hardening : Verify \| first read' "$tmp/check.log" || { echo "FAIL: check mode ran the verify"; exit 1; }
n_ref=$(grep -cE 'TASK \[Refused by the expected check for ' "$log" || true)
n_ver=$(grep -cE 'TASK \[Verify case ended as expected for ' "$log" || true)
echo "PASS: unit_hardening offline test ($(grep -c '^TASK' "$log") tasks; $n_ref refusal cases, $n_ver verify cases; check mode clean)"
