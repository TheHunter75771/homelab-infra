#!/usr/bin/env bash
# Tests for plan-guard.sh against synthetic `tofu show -json` documents (--json mode).
# Needs bash and jq only: no tofu, no Cloudflare, no secrets.
#   bash terraform/cloudflare/tests/test_plan_guard.sh
set -euo pipefail
export LC_ALL=C

HERE=$(cd "$(dirname "$0")" && pwd)
GUARD="$HERE/../plan-guard.sh"
T=$(mktemp -d)
trap 'rm -rf -- "$T"' EXIT

# Planted in every before/after value: it must never reach the guard's output.
SENTINEL="SENTINEL-attribute-value-must-not-print"

PASS=0
FAILS=0
ok() {
  PASS=$((PASS + 1))
  printf 'ok   %s\n' "$1"
}
bad() {
  FAILS=$((FAILS + 1))
  printf 'FAIL %s\n' "$1"
}

# rc NAME: a resource change. actions is a JSON array; importing is "true" or "false".
rc() {
  local address=$1 actions=$2 importing=${3:-false} mode=${4:-managed}
  local imp=null
  if [ "$importing" = true ]; then imp="{\"id\":\"$SENTINEL\"}"; fi
  printf '{"address":"%s","mode":"%s","type":"cloudflare_dns_record","name":"x","change":{"actions":%s,"before":{"content":"%s"},"after":{"content":"%s"},"importing":%s}}' \
    "$address" "$mode" "$actions" "$SENTINEL" "$SENTINEL" "$imp"
}
doc() { # doc FILE CHANGE... : a plan document with these resource changes
  local f="$T/$1"
  shift
  local IFS=,
  printf '{"format_version":"1.2","terraform_version":"1.11.5","errored":false,"resource_changes":[%s]}\n' "$*" >"$f"
  printf '%s' "$f"
}

# run NAME WANT_RC FILE [PATTERN]...: exit code and output checks. A PATTERN (ERE) must match the
# output; a "!PATTERN" must not. $ALLOW, when set, is passed as ALLOW_DESTROY.
run() {
  local name=$1 want=$2 file=$3
  shift 3
  local out got=0
  local -a envs=(-u ALLOW_DESTROY)
  if [ -n "$ALLOW" ]; then envs+=("ALLOW_DESTROY=$ALLOW"); fi
  out=$(env "${envs[@]}" "$GUARD" --json "$file" 2>&1) || got=$?
  if [ "$got" != "$want" ]; then
    bad "$name: exit $got, want $want"
    printf '%s\n' "$out" | sed 's/^/     | /'
    return
  fi
  if grep -qF -- "$SENTINEL" <<<"$out"; then
    bad "$name: printed an attribute value"
    return
  fi
  local p
  for p in "$@"; do
    if [ "${p#!}" != "$p" ]; then
      if grep -qE -- "${p#!}" <<<"$out"; then
        bad "$name: output matches /${p#!}/"
        printf '%s\n' "$out" | sed 's/^/     | /'
        return
      fi
    elif ! grep -qE -- "$p" <<<"$out"; then
      bad "$name: output lacks /$p/"
      printf '%s\n' "$out" | sed 's/^/     | /'
      return
    fi
  done
  ok "$name"
}

ALLOW=""

# --- passing plans ---
f=$(doc empty.json)
run "no changes (empty resource_changes)" 0 "$f" '^No changes\.$' 'plan-guard: OK'

printf '{"format_version":"1.2","terraform_version":"1.11.5"}\n' >"$T/nokey.json"
run "no changes (resource_changes absent)" 0 "$T/nokey.json" '^No changes\.$'

f=$(doc noop.json "$(rc cloudflare_dns_record.a '["no-op"]')" "$(rc data.cloudflare_zone.z '["read"]' false data)")
run "no-ops and data reads only" 0 "$f" '^No changes\.$'

f=$(doc imports.json "$(rc cloudflare_dns_record.a '["no-op"]' true)" "$(rc cloudflare_dns_record.b '["no-op"]' true)" "$(rc cloudflare_zone_setting.c '["no-op"]' true)")
run "imports only" 0 "$f" '^Plan: 3 to import, 0 to add, 0 to change, 0 to destroy\.$' 'plan-guard: OK'

f=$(doc update.json "$(rc cloudflare_dns_record.a '["update"]')" "$(rc cloudflare_dns_record.b '["no-op"]' true)")
run "update (plus an import)" 0 "$f" '^Plan: 1 to import, 0 to add, 1 to change, 0 to destroy\.$' '!^  (delete|replace)'

f=$(doc create.json "$(rc cloudflare_dns_record.new '["create"]')")
run "create" 0 "$f" '^Plan: 1 to add, 0 to change, 0 to destroy\.$'

# --- blocked plans ---
f=$(doc delete.json "$(rc cloudflare_dns_record.game '["delete"]')" "$(rc cloudflare_dns_record.keep '["no-op"]')")
run "delete" 1 "$f" '^Plan: 0 to add, 0 to change, 1 to destroy\.$' '^  delete +cloudflare_dns_record\.game$' \
  '!cloudflare_dns_record\.keep' 'FAIL' '!plan-guard: OK'

f=$(doc rename.json "$(rc cloudflare_dns_record.game '["delete"]')" "$(rc cloudflare_dns_record.game_relay '["create"]')")
run "rename without moved{} (delete + create)" 1 "$f" '^Plan: 1 to add, 0 to change, 1 to destroy\.$' \
  '^  delete +cloudflare_dns_record\.game$' '!game_relay'

f=$(doc replace_dc.json "$(rc cloudflare_pages_domain.apex '["delete","create"]')")
run "replace (delete, create)" 1 "$f" '^Plan: 1 to add, 0 to change, 1 to destroy\.$' '^  replace +cloudflare_pages_domain\.apex$'

f=$(doc replace_cd.json "$(rc cloudflare_pages_domain.apex '["create","delete"]')")
run "replace (create, delete)" 1 "$f" '^  replace +cloudflare_pages_domain\.apex$'

f=$(doc mixed.json "$(rc cloudflare_dns_record.a '["no-op"]' true)" "$(rc cloudflare_dns_record.b '["update"]')" \
  "$(rc cloudflare_ruleset.c '["delete","create"]')" "$(rc cloudflare_email_routing_settings.d '["delete"]')")
run "mixed: every offender listed, nothing else" 1 "$f" '^Plan: 1 to import, 1 to add, 1 to change, 2 to destroy\.$' \
  '^  replace +cloudflare_ruleset\.c$' '^  delete +cloudflare_email_routing_settings\.d$' \
  '!cloudflare_dns_record\.(a|b)'

# --- ALLOW_DESTROY ---
ALLOW=1
run "ALLOW_DESTROY=1 lets a delete through, still listed" 0 "$T/delete.json" '^  delete +cloudflare_dns_record\.game$' \
  'ALLOW_DESTROY=1' 'plan-guard: OK'
run "ALLOW_DESTROY=1 lets a replace through" 0 "$T/replace_dc.json" '^  replace +cloudflare_pages_domain\.apex$'
ALLOW=yes
run "ALLOW_DESTROY=yes is not 1: still blocked" 1 "$T/delete.json" 'FAIL'
ALLOW=0
run "ALLOW_DESTROY=0: still blocked" 1 "$T/delete.json" 'FAIL'
ALLOW=""

# --- fail closed on bad input ---
printf 'not json\n' >"$T/garbage.json"
run "malformed JSON" 2 "$T/garbage.json" 'cannot read the plan JSON'
printf '{"resource_changes":[]}\n' >"$T/noformat.json"
run "not a plan document (no format_version)" 2 "$T/noformat.json" 'cannot read the plan JSON'
printf '{"format_version":"1.2","errored":true,"resource_changes":[]}\n' >"$T/errored.json"
run "errored plan" 2 "$T/errored.json" 'errored'
printf '{"format_version":"1.2","resource_changes":[{"address":"x.y","mode":"managed","change":{}}]}\n' >"$T/noactions.json"
run "resource change without actions" 2 "$T/noactions.json" 'cannot read the plan JSON'
run "missing file" 2 "$T/does-not-exist.json" 'cannot read'

# --- arguments ---
got=0
"$GUARD" --keep "$T/x.tfplan" --json "$T/empty.json" >/dev/null 2>&1 || got=$?
if [ "$got" = 2 ] && [ ! -e "$T/x.tfplan" ]; then ok "--keep with --json is refused"; else bad "--keep with --json: exit $got"; fi
got=0
"$GUARD" --bogus >/dev/null 2>&1 || got=$?
if [ "$got" = 2 ]; then ok "unknown argument is refused"; else bad "unknown argument: exit $got"; fi
got=0
"$GUARD" --help >/dev/null 2>&1 || got=$?
if [ "$got" = 0 ]; then ok "--help"; else bad "--help: exit $got"; fi

# --- --json mode never runs tofu: a fake tofu first on PATH would leave a marker ---
mkdir -p "$T/bin"
printf '#!/bin/sh\ntouch "%s/tofu-was-called"\nexit 1\n' "$T" >"$T/bin/tofu"
chmod +x "$T/bin/tofu"
got=0
env -u TOFU PATH="$T/bin:$PATH" "$GUARD" --json "$T/imports.json" >/dev/null 2>&1 || got=$?
if [ "$got" = 0 ] && [ ! -e "$T/tofu-was-called" ]; then ok "--json mode never runs tofu"; else bad "--json mode ran tofu or failed (exit $got)"; fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAILS"
[ "$FAILS" = 0 ]
