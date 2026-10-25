#!/usr/bin/env bash
# plan-guard.sh: plan this module and refuse any plan that deletes something.
#
# prevent_destroy (on every resource) stops `tofu destroy` and forced replaces, but not the
# removal or rename of a resource block, because the guard leaves with the block. This script
# closes that gap: it fails if any resource change in the plan contains a "delete" action,
# which also covers replaces (["delete","create"] and ["create","delete"]).
#
# It never applies. Its output is the plan summary line and the addresses of offending
# resources only: plan output carries identifiers, so it is never printed.
#
# Usage:
#   ./plan-guard.sh                    plan into a private temp dir, check, discard the plan
#   ./plan-guard.sh --keep PATH        same, and save the checked plan at PATH if it passes;
#                                      then apply exactly that file: tofu apply PATH
#   ./plan-guard.sh --json FILE        check an existing `tofu show -json` document (tests)
#
# Environment:
#   ALLOW_DESTROY=1   let deletes through (they are still listed); only for a reviewed,
#                     deliberate deletion
#   TOFU              tofu binary (default: tofu)
#   CLOUDFLARE_API_TOKEN and TF_VAR_state_passphrase must be set for a real plan (README.md).
#
# Exit codes: 0 pass, 1 deletes found, 2 usage or tofu/JSON error, 3 summary mismatch.
set -euo pipefail
umask 077

usage() {
  sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "$0"
  exit "$1"
}
die() {
  printf 'plan-guard: %s\n' "$1" >&2
  exit 2
}

keep=""
json_file=""
while [ $# -gt 0 ]; do
  case $1 in
  --keep)
    [ $# -ge 2 ] && [ -n "$2" ] || die "--keep needs a path"
    keep=$2
    shift 2
    ;;
  --json)
    [ $# -ge 2 ] && [ -n "$2" ] || die "--json needs a file"
    json_file=$2
    shift 2
    ;;
  -h | --help) usage 0 ;;
  *) die "unknown argument: $1 (see --help)" ;;
  esac
done
[ -z "$keep" ] || [ -z "$json_file" ] || die "--keep and --json cannot be combined"
command -v jq >/dev/null || die "jq is required"

tmpdir=$(mktemp -d)
cleanup() { rm -rf -- "$tmpdir"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

tofu_summary=""
if [ -n "$json_file" ]; then
  [ -r "$json_file" ] || die "cannot read $json_file"
  doc=$json_file
else
  tofu=${TOFU:-tofu}
  module_dir=$(cd "$(dirname "$0")" && pwd)
  plan="$tmpdir/guard.tfplan"
  # Plan output holds identifiers: it stays in the private temp dir, never on the terminal.
  if ! "$tofu" -chdir="$module_dir" plan -input=false -no-color -out="$plan" >"$tmpdir/plan.log" 2>&1; then
    printf 'plan-guard: tofu plan failed. Error headlines (run tofu plan yourself for details):\n' >&2
    grep -E '^Error: ' "$tmpdir/plan.log" | sort -u >&2 || true
    exit 2
  fi
  tofu_summary=$(grep -E '^(Plan: |No changes\.)' "$tmpdir/plan.log" | tail -n 1 || true)
  if ! "$tofu" -chdir="$module_dir" show -json "$plan" >"$tmpdir/plan.json" 2>"$tmpdir/show.log"; then
    die "tofu show -json failed"
  fi
  doc="$tmpdir/plan.json"
fi

# One pass over the document: a summary in tofu's format, then the offending addresses.
# Only .address and .change.actions/.importing are read; attribute values never are.
report=$(jq -r '
  if (type != "object") or (.format_version == null) then error("not a tofu show -json plan document")
  elif .errored == true then error("the plan errored")
  else . end
  | [(.resource_changes // [])[] | select(.mode != "data")] as $rc
  | ([$rc[] | select(.change.importing != null)] | length) as $import
  | ([$rc[] | select(.change.actions | any(. == "create"))] | length) as $add
  | ([$rc[] | select(.change.actions | any(. == "update"))] | length) as $change
  | ([$rc[] | select(.change.actions | any(. == "delete"))] | length) as $destroy
  | ([$rc[] | select(.change.actions | any(. == "forget"))] | length) as $forget
  | (if ($import + $add + $change + $destroy + $forget) == 0 then "No changes."
     else "Plan: "
       + (if $import > 0 then "\($import) to import, " else "" end)
       + "\($add) to add, \($change) to change, \($destroy) to destroy"
       + (if $forget > 0 then ", \($forget) to forget" else "" end)
       + "."
     end),
    ($rc[] | select(.change.actions | any(. == "delete"))
      | (if (.change.actions | any(. == "create")) then "replace" else "delete" end) + "\t" + .address)
' "$doc" 2>"$tmpdir/jq.log") || die "cannot read the plan JSON ($(head -n 1 "$tmpdir/jq.log"))"

summary=$(printf '%s\n' "$report" | head -n 1)
offenders=$(printf '%s\n' "$report" | tail -n +2)

printf '%s\n' "$summary"

# A real run cross-checks the count against tofu's own summary line: a mismatch means this
# script misread the plan, so it fails closed.
if [ -z "$json_file" ]; then
  case $summary in
  "No changes.") [[ $tofu_summary == "No changes."* ]] || mismatch=1 ;;
  *) [ "$tofu_summary" = "$summary" ] || mismatch=1 ;;
  esac
  if [ "${mismatch:-0}" = 1 ]; then
    printf 'plan-guard: summary mismatch, tofu says: %s\n' "${tofu_summary:-<none>}" >&2
    exit 3
  fi
fi

if [ -n "$offenders" ]; then
  printf '%s\n' "$offenders" | while IFS=$'\t' read -r action address; do
    printf '  %-7s %s\n' "$action" "$address"
  done
  if [ "${ALLOW_DESTROY:-}" = 1 ]; then
    printf 'plan-guard: ALLOW_DESTROY=1, deletes allowed (they must be the reviewed, intended ones)\n' >&2
  else
    printf 'plan-guard: FAIL, the plan deletes or replaces the resources above (ALLOW_DESTROY=1 overrides)\n' >&2
    exit 1
  fi
fi

if [ -n "$keep" ]; then
  cp -- "$plan" "$keep"
  chmod 600 -- "$keep"
  printf 'plan-guard: checked plan saved; apply exactly it: tofu apply %s\n' "$keep" >&2
fi
printf 'plan-guard: OK\n' >&2
