#!/usr/bin/env bash
# homelab-calendar-recovered (docs/CONTRACTS.md §13.1, amended): ExecStart= of
# homelab-calendar-recovered.service, which every start of homelab-calendar.service queues.
# homelab-calendar-alert.service leaves a marker when the calendar gave up. On the next start this
# pushes "Calendar back online" (which closes the alert on the dashboard) and removes the marker, but only
# once Radicale really serves: an unauthenticated PROPFIND / answers 401, and the same service
# invocation is still active after a settle period. A server that binds and then dies gets no
# "back online", and the marker stays for the next start. No marker: does nothing.
# Always exits 0: a failed push never fails the unit. Settings come from the unit's environment;
# the defaults match the role's.
set -u

MARKER=${CALENDAR_ALERT_MARKER:-/var/lib/homelab-calendar-alert/down}
NOTIFY_BIN=${CALENDAR_NOTIFY_BIN:-/usr/local/bin/homelab-notify}
SERVICE=${CALENDAR_SERVICE:-homelab-calendar.service}
HOST=${CALENDAR_HOST:-127.0.0.1}
PORT=${CALENDAR_PORT:-5232}
WAIT_SECONDS=${CALENDAR_WAIT_SECONDS:-30}
SETTLE_SECONDS=${CALENDAR_SETTLE_SECONDS:-5}
# Must end in a recovery word, or the dashboard never closes the "Calendar is down" alert.
TITLE=${CALENDAR_RECOVERED_TITLE:-Calendar back online}

[ -e "$MARKER" ] || exit 0

# Radicale answers, as Radicale: an unauthenticated PROPFIND / is refused with 401. Plain bash over
# /dev/tcp (no curl needed); HTTP/1.0, so the server closes the connection after the reply.
answers() {
  (
    exec 3<>"/dev/tcp/$HOST/$PORT" || exit 1
    printf 'PROPFIND / HTTP/1.0\r\nHost: %s:%s\r\nDepth: 0\r\nContent-Length: 0\r\n\r\n' "$HOST" "$PORT" >&3
    IFS= read -r -t 5 status <&3 || exit 1
    case $status in
      "HTTP/1."[01]" 401"*) exit 0 ;;
      *) exit 1 ;;
    esac
  ) 2>/dev/null
}

invocation() { systemctl show --property=InvocationID --value "$SERVICE" 2>/dev/null; }

keep_open() {
  echo "$1; keeping the alert open"
  exit 0
}

waited=0
until answers; do
  [ "$waited" -lt "$WAIT_SECONDS" ] || keep_open "calendar not answering on $HOST:$PORT after ${WAIT_SECONDS}s"
  sleep 1
  waited=$((waited + 1))
done

# The instance that answered must still be the one running after the settle period.
before=$(invocation)
[ -n "$before" ] || keep_open "no invocation of $SERVICE"
sleep "$SETTLE_SECONDS"
systemctl is-active --quiet "$SERVICE" || keep_open "$SERVICE stopped within ${SETTLE_SECONDS}s"
[ "$(invocation)" = "$before" ] || keep_open "$SERVICE restarted within ${SETTLE_SECONDS}s"

"$NOTIFY_BIN" --tag calendar --title "$TITLE" \
  --body "homelab-calendar.service is running again." </dev/null
rc=$?
# 0 = delivered (or a suppressed duplicate), 2 = no subscribed devices (CONTRACTS §4).
if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
  echo "homelab-notify exited $rc; removing the marker anyway (one push per recovery)"
fi
rm -f -- "$MARKER"
exit 0
