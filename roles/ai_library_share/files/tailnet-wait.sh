#!/bin/sh
# Wait until this host holds every given address (tailscaled assigns them), for at most TIMEOUT
# seconds (docs/CONTRACTS.md §12.17: smbd binds only the tailnet IPs; the ai-box mount needs the
# tailnet up). Run by ai-library-tailnet-wait.service.
# Usage: sh tailnet-wait.sh TIMEOUT ADDRESS...
set -u
timeout="$1"
shift
left="$timeout"
while :; do
  missing=""
  for addr in "$@"; do
    ip -o addr show to "$addr" 2>/dev/null | grep -q . || missing="$missing $addr"
  done
  [ -z "$missing" ] && exit 0
  if [ "$left" -le 0 ]; then
    echo "ai-library: not up after ${timeout}s:$missing" >&2
    exit 1
  fi
  sleep 1
  left=$((left - 1))
done
