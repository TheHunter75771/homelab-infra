#!/usr/bin/env bash
# Offline test for role searxng (no root, no Docker):
# 1. Static: every task in the role's task files that touches the secret is no_log (check_no_log.py).
# 2. Runs test_searxng.yml with -vvv --diff, with a fake `docker` (tests/bin: network listing, inspect with
#    --format, port) first on PATH, a fake SearXNG (fake_searxng.py) for verify.yml, and the role's filter
#    plugins loaded. Then proves that the secrets layout.yml generated (the real thing, read back from the
#    scratch dir) never reached the output (as text, as base64 of the value, of the value with LF, and of the
#    whole .env line: slurp's form), and neither did the canary SEARXNG_SECRET in the fake container's
#    Config.Env (as text and base64), which a full `docker inspect` would print.
#   roles/searxng/tests/run.sh            (from anywhere; uses the repo's .venv)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
"$repo/.venv/bin/python" "$here/check_no_log.py" "$here"/../tasks/*.yml
tmp="$(mktemp -d --suffix=.searxng)"
server_pid=
cleanup() {
  [ -z "$server_pid" ] || kill "$server_pid" 2>/dev/null || true
  [ -n "${KEEP:-}" ] || rm -rf "$tmp"
}
trap cleanup EXIT
log="$tmp/run.log"
export SEARXNG_TEST_NETWORKS="$tmp/networks"
: >"$SEARXNG_TEST_NETWORKS"
# A fake SearXNG for verify.yml (its own PID is killed on exit, nothing else).
python3 "$here/fake_searxng.py" "$tmp/port" &
server_pid=$!
for _ in $(seq 50); do [ -s "$tmp/port" ] && break; python3 -c 'import time; time.sleep(0.1)'; done
port="$(cat "$tmp/port")"
# The fake docker's inspect fixtures: Config.Env carries a canary SEARXNG_SECRET that the play never sees.
inspect_canary="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
mkdir -p "$tmp/inspect"
export SEARXNG_TEST_INSPECT="$tmp/inspect/inspect.json" SEARXNG_TEST_PORT="$tmp/inspect/port"
printf "%s/tcp -> 127.0.0.1:%s\n" "$port" "$port" >"$SEARXNG_TEST_PORT"
for variant in good capadd; do
  python3 - "$inspect_canary" "$variant" >"$tmp/inspect/$variant.json" <<'PY'
import json, sys
canary, variant = sys.argv[1], sys.argv[2]
print(json.dumps([{
    "Config": {"User": "977:977", "Env": ["SEARXNG_SECRET=" + canary, "PATH=/usr/bin"]},
    "HostConfig": {"ReadonlyRootfs": True, "CapDrop": ["ALL"],
                   "CapAdd": ["NET_ADMIN"] if variant == "capadd" else None,
                   "SecurityOpt": ["no-new-privileges:true"], "Privileged": False, "PidsLimit": 128,
                   "NetworkMode": "research"},
    "NetworkSettings": {"Networks": {"research": {"IPAddress": "127.0.0.1", "Gateway": "127.0.0.1"}}},
}]))
PY
done
export PATH="$here/bin:$PATH"
export ANSIBLE_FILTER_PLUGINS="$here/../filter_plugins"
cd "$repo"   # ansible.cfg: collections_path, roles_path
set +e
"$repo/.venv/bin/ansible-playbook" -vvv --diff -i localhost, -c local "$here/test_searxng.yml" \
  -e "test_tmp=$tmp/t" -e "test_networks_file=$SEARXNG_TEST_NETWORKS" \
  -e "test_inspect_dir=$tmp/inspect" -e "test_fake_port=$port" </dev/null >"$log" 2>&1
rc=$?
set -e
tail -n 4 "$log"
leaks=0
# The inspect canary (Config.Env), checked even when the play failed: verify.yml must never print the
# container's environment.
for form in "$inspect_canary" "$(printf '%s' "$inspect_canary" | base64 -w0)" "$(printf 'SEARXNG_SECRET=%s' "$inspect_canary" | base64 -w0)"; do
  if grep -qF -- "$form" "$log"; then echo "FAIL: the inspect canary (Config.Env) appears in the output (as ${form:0:6}...)"; leaks=1; fi
done
[ "$rc" -eq 0 ] || { echo "FAIL: playbook rc=$rc"; grep -nE 'FAILED|fatal|ERROR' "$log" | head -20; exit 1; }
mapfile -t envs < <(find "$tmp/t" -name .env -type f)
[ "${#envs[@]}" -ge 1 ] || { echo "FAIL: no generated .env found"; exit 1; }
for f in "${envs[@]}"; do
  line="$(cat "$f")"
  value="${line#SEARXNG_SECRET=}"
  [ "${#value}" -eq 64 ] || { echo "FAIL: $f is not SEARXNG_SECRET=<64 hex>"; exit 1; }
  for form in "$value" "$(printf '%s' "$value" | base64 -w0)" "$(printf '%s\n' "$value" | base64 -w0)" \
      "$(printf '%s\n' "$line" | base64 -w0)" "$(printf '%s' "$line" | base64 -w0)"; do
    if grep -qF -- "$form" "$log"; then echo "FAIL: the secret appears in the output (as ${form:0:6}...)"; leaks=1; fi
  done
done
grep -q 'TASK \[Verify | passes on the good container\]' "$log" || { echo "FAIL: verify.yml did not run"; exit 1; }
[ "$leaks" -eq 0 ] || exit 1
echo "PASS: playbook ok; ${#envs[@]} generated secret(s) and the inspect canary (text or base64) not in the -vvv --diff output ($(wc -l < "$log") lines)"
