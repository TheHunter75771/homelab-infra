#!/usr/bin/env bash
# Local test for files/homelab-backup.sh: runs the real script against a temp tree through
# its environment overrides (no root, no host). Uses the real sqlite3 CLI when installed,
# otherwise tests/fake_sqlite3.py (same SQLite online-backup API).
#   bash roles/backups/tests/test_backup.sh
# sh -c bodies are single-quoted on purpose (they read "$1"); run_* get extra args through check().
# shellcheck disable=SC2016,SC2119,SC2120
set -euo pipefail
export LC_ALL=C

HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPT="$HERE/../files/homelab-backup.sh"
T=$(mktemp -d)
WAL_PID=""
APP_PID=""
cleanup() {
  if [ -n "$WAL_PID" ]; then kill "$WAL_PID" 2>/dev/null || true; fi
  if [ -n "$APP_PID" ]; then kill "$APP_PID" 2>/dev/null || true; fi
  chmod -R u+rwX "$T" 2>/dev/null || true
  rm -rf "$T"
}
trap cleanup EXIT

if command -v sqlite3 >/dev/null; then SQ=$(command -v sqlite3); else SQ="$HERE/fake_sqlite3.py"; fi
echo "sqlite3 under test: $SQ"
GROUP=$(id -gn)

PASS=0
FAILS=0
ok() { PASS=$((PASS + 1)); printf 'ok   %s\n' "$1"; }
bad() { FAILS=$((FAILS + 1)); printf 'FAIL %s\n' "$1"; }
check() {
  local what=$1
  shift
  if "$@"; then ok "$what"; else bad "$what"; fi
}

B="$T/srv/backups"
D="$T/srv/data"
S="$T/status" # STATUS_DIR; outside srv so it survives setup() (tests remove it explicitly)

setup() {
  chmod -R u+rwX "$T/srv" 2>/dev/null || true
  rm -rf "$T/srv" "$T/x" "$T/restore"
  mkdir -p "$B" "$D" "$T/srv/push" "$T/srv/bootgate" "$T/bin" "$T/srv/dashboard/uptime" "$T/srv/notify"
  mkdir -p "$D/sonarr/logs" "$D/sonarr/MediaCover/1" "$D/sonarr/Backups" "$D/jellyfin/cache/img" \
    "$D/jellyfin/data/metadata/x" "$D/jellyfin/data/transcodes" "$D/jellyfin/log" "$D/jellyfin/data/data" \
    "$D/jellyseerr/db" "$D/adguard/work/data" "$D/radarr"
  chmod 0700 "$D/sonarr/Backups"
  echo '<Config/>' >"$D/sonarr/config.xml"
  echo log >"$D/sonarr/logs/sonarr.txt"
  echo img >"$D/sonarr/MediaCover/1/poster.jpg"
  echo c >"$D/jellyfin/cache/img/a"
  echo m >"$D/jellyfin/data/metadata/x/m"
  echo t >"$D/jellyfin/data/transcodes/t.ts"
  echo l >"$D/jellyfin/log/l.log"
  mkdir -p "$D/jellyfin/data/trickplay/abc"
  echo tp >"$D/jellyfin/data/trickplay/abc/1.jpg"
  head -c 4096 /dev/urandom >"$D/adguard/work/data/stats.db" # a *.db that is not SQLite
  echo hi >"$D/radarr/my file.txt"
  python3 - "$D" <<'PY'
import os, sqlite3, sys
d = sys.argv[1]
for rel, n in [("sonarr/sonarr.db", 500), ("jellyfin/data/data/jellyfin.db", 2000),
               ("jellyseerr/db/db.sqlite3", 20), ("radarr/my db.db", 5), ("sonarr/logs.db", 3)]:
    c = sqlite3.connect(os.path.join(d, rel))
    c.execute("create table t(i integer, s text)")
    c.executemany("insert into t values (?, ?)", [(i, "x" * 50) for i in range(n)])
    c.commit()
    c.close()
PY
  echo '{"publicKey":"p","privateKey":"k","subject":"mailto:admin@localhost"}' >"$T/srv/push/vapid.json"
  echo '[]' >"$T/srv/push/subscriptions.json"
  echo ok >"$T/srv/bootgate/status"
  chmod 0750 "$T/srv/dashboard"
  echo '{"jellyfinApiKey":"k"}' >"$T/srv/dashboard/settings.json"
  chmod 0600 "$T/srv/dashboard/settings.json"
  printf '{"ts":1,"free":10}\n{"ts":2,"free":9}\n{"ts":3,"fr' >"$T/srv/dashboard/samples.jsonl" # an append in flight
  echo '{"days":{}}' >"$T/srv/dashboard/uptime/2026-10.json"
  echo half >"$T/srv/dashboard/.settings.json.123.abcdef.tmp"
  echo half >"$T/srv/dashboard/uptime/.2026-10.json.123.abcdef.tmp"
  printf '{"ts":"a","title":"t1"}\n{"ts":"b","title":"t2"}\n' >"$T/srv/notify/history.jsonl"
  chmod 0644 "$T/srv/notify/history.jsonl"
  echo '{}' >"$T/srv/notify/dedupe.json"
  chmod 0600 "$T/srv/notify/dedupe.json"
  : >"$T/srv/notify/.lock"
  printf '#!/bin/sh\nprintf "%%s\\n" "$*" >>"%s/notify.log"\n' "$T" >"$T/notify"
  chmod +x "$T/notify"
  : >"$T/notify.log"
}

run_backup() { # extra VAR=value pairs may be passed
  local rc=0
  env BACKUP_ROOT="$B" DATA_DIR="$D" PUSH_DIR="$T/srv/push" BOOTGATE_DIR="$T/srv/bootgate" \
    DASHBOARD_DIR="$T/srv/dashboard" NOTIFY_STATE_DIR="$T/srv/notify" \
    KEEP=14 BACKUP_GROUP="$GROUP" NOTIFY_BIN="$T/notify" SQLITE3="$SQ" STATUS_DIR="$S" "$@" \
    bash "$SCRIPT" >"$T/out.log" 2>&1 || rc=$?
  return "$rc"
}

archives() { find "$B/appdata" -maxdepth 1 -name 'appdata-*.tar' -printf '%f\n' | sort; }
newest() { archives | tail -n 1; }
count_rows() { python3 -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("select count(*) from t").fetchone()[0])' "$1"; }
no_workdirs() { [ -z "$(find "$B" -maxdepth 1 -name '.work.*')" ]; }
st() { jq -r "$1" "$S/status.json"; }
corrupt_jellyfin_db() {
  python3 - "$D/jellyfin/data/data/jellyfin.db" <<'PY'
import sys
with open(sys.argv[1], "r+b") as f:
    f.seek(4096 + 8)          # inside page 2 (the table's b-tree page)
    f.write(b"\xff" * 1500)
PY
}
ISO_RE='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'

# --- 1. Happy path, with uncheckpointed WAL data in a live database -------------------------
setup
python3 - "$D/sonarr/sonarr.db" "$T/wal.ready" <<'PY' &
import os, sqlite3, sys, time
c = sqlite3.connect(sys.argv[1])
c.execute("pragma journal_mode=wal")
c.execute("pragma wal_autocheckpoint=0")
c.executemany("insert into t values (?, ?)", [(i, "wal") for i in range(100)])
c.commit()
open(sys.argv[2], "w").close()
time.sleep(60)  # hold the connection so the 100 rows stay in -wal only
PY
WAL_PID=$!
for _ in $(seq 50); do [ -e "$T/wal.ready" ] && break; sleep 0.1; done
cp "$D/sonarr/sonarr.db" "$T/raw-copy.db"
check "control: a raw copy of the live db misses the WAL rows (500, not 600)" [ "$(count_rows "$T/raw-copy.db")" = 500 ]
check "backup exits 0" run_backup
kill "$WAL_PID" 2>/dev/null || true
wait "$WAL_PID" 2>/dev/null || true
WAL_PID=""
A="$B/appdata/$(newest)"
check "exactly one archive, named appdata-<UTC stamp>.tar" [ "$(archives | grep -cE '^appdata-[0-9]{8}T[0-9]{6}Z\.tar$')" = 1 ]
check "archive mode 0640, group $GROUP" [ "$(stat -c '%a %G' "$A")" = "640 $GROUP" ]
check "LAST_OK holds an ISO-8601 UTC timestamp" grep -qxE '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z' "$B/LAST_OK"
check "LAST_OK mode 0640" [ "$(stat -c '%a' "$B/LAST_OK")" = 640 ]
check "no work dir left behind" no_workdirs
check "no failure push on success" [ ! -s "$T/notify.log" ]
tar -tf "$A" >"$T/members"
check "MANIFEST.sha256 is the last member" [ "$(tail -n 1 "$T/members")" = MANIFEST.sha256 ]
for m in sqlite/sonarr/sonarr.db sqlite/sonarr/logs.db sqlite/jellyfin/data/data/jellyfin.db \
  sqlite/jellyseerr/db/db.sqlite3 "sqlite/radarr/my db.db" files.tar.gz push.tar.gz bootgate.tar.gz \
  dashboard.tar.gz notify.tar.gz; do
  check "archive has $m" grep -qxF "$m" "$T/members"
done
check "non-SQLite *.db is not in sqlite/" sh -c '! grep -q "^sqlite/adguard/.*stats.db" "$1"' _ "$T/members"
mkdir -p "$T/x" && tar -xf "$A" -C "$T/x"
check "every member matches MANIFEST.sha256" sh -c 'cd "$1" && sha256sum --quiet --strict -c MANIFEST.sha256' _ "$T/x"
check "manifest covers all 10 files" [ "$(wc -l <"$T/x/MANIFEST.sha256")" = 10 ]
check "sqlite copy of the live db includes the WAL rows (600)" [ "$(count_rows "$T/x/sqlite/sonarr/sonarr.db")" = 600 ]
check "sqlite copy passes integrity_check" [ "$("$SQ" "$T/x/sqlite/jellyfin/data/data/jellyfin.db" 'PRAGMA integrity_check;')" = ok ]
check "db name with a space survives" [ "$(count_rows "$T/x/sqlite/radarr/my db.db")" = 5 ]
tar -tzvf "$T/x/files.tar.gz" >"$T/files.v"
tar -tzf "$T/x/files.tar.gz" >"$T/files"
for f in ./sonarr/config.xml ./adguard/work/data/stats.db "./radarr/my file.txt" ./sonarr/Backups/; do
  check "files.tar.gz has $f" grep -qxF "$f" "$T/files"
done
check "files.tar.gz keeps directory modes (Backups 0700)" grep -qE '^drwx------ .* \./sonarr/Backups/$' "$T/files.v"
check "files.tar.gz excludes cache/metadata/trickplay/transcodes/log(s)/MediaCover" \
  sh -c '! grep -E "/(cache|metadata|trickplay|transcodes|log|logs|MediaCover)(/|$)" "$1"' _ "$T/files"
check "files.tar.gz excludes the live SQLite dbs" sh -c '! grep -E "(sonarr|logs|jellyfin|my db)\.db$|db\.sqlite3$" "$1"' _ "$T/files"
check "files.tar.gz excludes -wal/-shm sidecars" sh -c '! grep -E -- "-(wal|shm)$" "$1"' _ "$T/files"
check "push.tar.gz has vapid.json + subscriptions.json" \
  sh -c 'tar -tzf "$1" | grep -qx ./vapid.json && tar -tzf "$1" | grep -qx ./subscriptions.json' _ "$T/x/push.tar.gz"
check "bootgate.tar.gz has status" sh -c 'tar -tzf "$1" | grep -qx ./status' _ "$T/x/bootgate.tar.gz"

# --- 2. Rotation keeps the newest KEEP ---------------------------------------------------------
setup
mkdir -p "$B/appdata"
for i in 0 1 2 3 4; do echo old >"$B/appdata/appdata-20200101T00000${i}Z.tar"; done
check "backup with KEEP=3 exits 0" run_backup KEEP=3
check "KEEP=3 leaves 3 archives" [ "$(archives | wc -l)" = 3 ]
archives >"$T/left"
check "the two newest old archives survive" sh -c 'grep -qx appdata-20200101T000003Z.tar "$1" && grep -qx appdata-20200101T000004Z.tar "$1"' _ "$T/left"
check "the oldest are gone" sh -c '! grep -q "T00000[012]Z" "$1"' _ "$T/left"

# --- 3. A corrupt database fails the run, pushes, and changes nothing ---------------------------
setup
mkdir -p "$B/appdata"
echo good >"$B/appdata/appdata-20200101T000000Z.tar"
echo 2020-01-01T00:00:00Z >"$B/LAST_OK"
corrupt_jellyfin_db
if run_backup; then bad "corrupt db: backup should fail"; else ok "corrupt db: backup exits non-zero"; fi
check "corrupt db: failure pushed with --tag backup" grep -q -- '--tag backup --title Backup failed --body .*jellyfin' "$T/notify.log"
check "corrupt db: no new archive" [ "$(archives)" = appdata-20200101T000000Z.tar ]
check "corrupt db: LAST_OK untouched" grep -qx 2020-01-01T00:00:00Z "$B/LAST_OK"
check "corrupt db: no work dir left behind" no_workdirs

# --- 4. Missing data dir fails and pushes -------------------------------------------------------
setup
rm -rf "$D"
if run_backup; then bad "missing data dir: should fail"; else ok "missing data dir: exits non-zero"; fi
check "missing data dir: failure pushed" grep -q 'does not exist' "$T/notify.log"

# --- 5. Missing push dir is a warning, not a failure ---------------------------------------------
setup
rm -rf "$T/srv/push"
check "missing push dir: backup still exits 0" run_backup
check "missing push dir: no push.tar.gz member" sh -c '! tar -tf "$1" | grep -qx push.tar.gz' _ "$B/appdata/$(newest)"
check "missing push dir: warning logged" grep -q 'skipped' "$T/out.log"

# --- 6. A file vanishing mid-run is tolerated; an unreadable file is not ----------------------
setup
echo bye >"$D/sonarr/vanish.txt"
real_tar=$(command -v tar)
cat >"$T/bin/tar" <<EOF
#!/bin/sh
case " \$* " in *" -T "*) rm -f "$D/sonarr/vanish.txt" ;; esac
exec "$real_tar" "\$@"
EOF
chmod +x "$T/bin/tar"
check "vanished file: backup exits 0" run_backup PATH="$T/bin:$PATH"
check "vanished file: noted in the log" grep -q 'vanished while archiving' "$T/out.log"
setup
echo secret >"$D/radarr/unreadable.txt"
chmod 000 "$D/radarr/unreadable.txt"
if [ "$(id -u)" = 0 ]; then
  ok "unreadable file: skipped (running as root)"
else
  if run_backup; then bad "unreadable file: should fail"; else ok "unreadable file: exits non-zero"; fi
  check "unreadable file: failure pushed" grep -q 'tar of' "$T/notify.log"
  check "unreadable file: status error is path-free" [ "$(st .error)" = "tar of data files failed (exit 2)" ]
fi

# --- 7. EXCLUDE_DIRS can be overridden -----------------------------------------------------------
setup
check "EXCLUDE_DIRS='' backs up caches too" run_backup EXCLUDE_DIRS=
mkdir -p "$T/x" && tar -xf "$B/appdata/$(newest)" -C "$T/x" files.tar.gz
check "EXCLUDE_DIRS='': cache present" sh -c 'tar -tzf "$1" | grep -qx ./jellyfin/cache/img/a' _ "$T/x/files.tar.gz"

# --- 8. status.json (dashboard, CONTRACTS §11.5) --------------------------------------------------
# 8a. First run ever, and it fails: nothing to carry over.
setup
rm -rf "$S" "$D"
if run_backup; then bad "status first run: should fail"; else ok "status first run: exits non-zero"; fi
check "status first run: dir created 0755, file 0644" [ "$(stat -c %a "$S") $(stat -c %a "$S/status.json")" = "755 644" ]
check "status first run: ok false, nothing carried" \
  [ "$(jq -c '[.ok, .last_ok, .archive, .size_bytes, .error]' "$S/status.json")" = '[false,null,null,null,"data dir does not exist"]' ]
check "status first run: last_run is ISO-8601 UTC" grep -qE "$ISO_RE" <(st .last_run)

# 8b. Success.
setup
check "status success: exits 0" run_backup
check "status success: exactly the six keys" \
  [ "$(jq -c 'keys' "$S/status.json")" = '["archive","error","last_ok","last_run","ok","size_bytes"]' ]
check "status success: ok true, error null" [ "$(jq -c '[.ok, .error]' "$S/status.json")" = '[true,null]' ]
check "status success: archive is the bare file name" [ "$(st .archive)" = "$(newest)" ]
check "status success: size_bytes is the archive size (a number)" \
  [ "$(jq -c '.size_bytes' "$S/status.json")" = "$(stat -c %s "$B/appdata/$(newest)")" ]
check "status success: last_ok equals LAST_OK" [ "$(st .last_ok)" = "$(cat "$B/LAST_OK")" ]
check "status success: last_run equals last_ok" [ "$(st .last_run)" = "$(st .last_ok)" ]
check "status success: file 0644" [ "$(stat -c %a "$S/status.json")" = 644 ]
check "status success: no temp files left" [ -z "$(find "$S" -name '.status.*')" ]
cp "$S/status.json" "$T/status.ok"

# 8c. A failure carries last_ok/archive/size_bytes over from that success.
sleep 1.1
corrupt_jellyfin_db
if run_backup; then bad "status failure: should fail"; else ok "status failure: exits non-zero"; fi
check "status failure: ok false" [ "$(st .ok)" = false ]
for k in last_ok archive size_bytes; do
  check "status failure: $k carried over" [ "$(st ".$k")" = "$(jq -r ".$k" "$T/status.ok")" ]
done
check "status failure: last_run is this run" [ "$(st .last_run)" != "$(jq -r .last_run "$T/status.ok")" ]
check "status failure: error is a path-free summary" [ "$(st .error)" = "integrity check failed for jellyfin.db (jellyfin)" ]
check "status failure: push body is the same summary" \
  grep -qxF -- "--tag backup --title Backup failed --body integrity check failed for jellyfin.db (jellyfin)" "$T/notify.log"

# 8d. A second failure in a row still carries the last success.
rm -rf "$D"
if run_backup; then bad "status 2nd failure: should fail"; else ok "status 2nd failure: exits non-zero"; fi
check "status 2nd failure: last_ok still the last success" [ "$(st .last_ok)" = "$(jq -r .last_ok "$T/status.ok")" ]
check "status 2nd failure: archive still the last success" [ "$(st .archive)" = "$(jq -r .archive "$T/status.ok")" ]
check "status 2nd failure: new error" [ "$(st .error)" = "data dir does not exist" ]

# 8e. Recovery clears the error.
setup
check "status recovery: exits 0" run_backup
check "status recovery: ok true, error null" [ "$(jq -c '[.ok, .error]' "$S/status.json")" = '[true,null]' ]
check "status recovery: archive moved on" [ "$(st .archive)" != "$(jq -r .archive "$T/status.ok")" ]

# 8f. A damaged previous status.json can't smuggle a path or junk into the new one.
setup
printf '{"last_ok":"2026-01-01T00:00:00Z","archive":"../../etc/shadow","size_bytes":"12; rm"}\n' >"$S/status.json"
rm -rf "$D"
run_backup || true
check "status damaged: valid last_ok kept, bad archive/size dropped" \
  [ "$(jq -c '[.last_ok, .archive, .size_bytes]' "$S/status.json")" = '["2026-01-01T00:00:00Z",null,null]' ]
echo 'not json' >"$S/status.json"
run_backup || true
check "status unreadable previous: starts from nulls" [ "$(jq -c '[.ok, .last_ok, .archive]' "$S/status.json")" = '[false,null,null]' ]

# 8g. Not being able to write status.json fails the run loudly (the dashboard would be stale).
if [ "$(id -u)" != 0 ]; then
  setup
  chmod 0555 "$S"
  if run_backup; then bad "status unwritable: should fail"; else ok "status unwritable: exits non-zero"; fi
  check "status unwritable: failure pushed" grep -q 'could not write the status file' "$T/notify.log"
  chmod 0755 "$S"
fi

# --- 9. Dashboard state and alert history (outside DATA_DIR) -------------------------------------
setup
check "dashboard: backup exits 0" run_backup
mkdir -p "$T/x" && tar -xf "$B/appdata/$(newest)" -C "$T/x"
tar --numeric-owner -tvzf "$T/x/dashboard.tar.gz" >"$T/dash.v"
tar -tzf "$T/x/dashboard.tar.gz" | sort | tr '\n' ' ' >"$T/dash.l"
check "dashboard.tar.gz holds the dir, settings, samples and uptime, nothing else" \
  [ "$(cat "$T/dash.l")" = "./ ./samples.jsonl ./settings.json ./uptime/ ./uptime/2026-10.json " ]
check "settings.json kept 0600 with its numeric owner" \
  grep -qE "^-rw------- $(id -u)/$(id -g) .* \./settings\.json$" "$T/dash.v"
check "dashboard dir kept 0750" grep -qE '^drwxr-x--- .* \./$' "$T/dash.v"
mkdir -p "$T/x/dash" && tar -xzf "$T/x/dashboard.tar.gz" -C "$T/x/dash" ./samples.jsonl
check "a half-written last sample line is dropped from the copy" \
  [ "$(cat "$T/x/dash/samples.jsonl")" = "$(printf '{"ts":1,"free":10}\n{"ts":2,"free":9}')" ]
check "the copy still ends in a newline" [ "$(tail -c 1 "$T/x/dash/samples.jsonl" | od -An -tx1 | tr -d ' ')" = 0a ]
check "the live samples.jsonl itself is untouched" grep -q '"fr$' "$T/srv/dashboard/samples.jsonl"
check "the dropped line is noted in the journal" grep -q 'dropped a half-written last line of samples.jsonl' "$T/out.log"
check "notify.tar.gz holds only history.jsonl (no dedupe state, no lock, no dir entry)" \
  [ "$(tar -tzf "$T/x/notify.tar.gz")" = ./history.jsonl ]
check "history.jsonl copied whole" sh -c 'tar -xOzf "$1" ./history.jsonl | cmp -s - "$2"' _ "$T/x/notify.tar.gz" "$T/srv/notify/history.jsonl"
check "history.jsonl mode kept (0644)" sh -c 'tar -tvzf "$1" | grep -q "^-rw-r--r-- "' _ "$T/x/notify.tar.gz"
mkdir -p "$T/restore/dash" "$T/restore/notify"
chmod 0755 "$T/restore/dash" "$T/restore/notify"
tar -xpzf "$T/x/dashboard.tar.gz" -C "$T/restore/dash"
tar -xpzf "$T/x/notify.tar.gz" -C "$T/restore/notify"
check "restore: dashboard dir back to 0750, settings.json 0600" \
  [ "$(stat -c %a "$T/restore/dash") $(stat -c %a "$T/restore/dash/settings.json")" = "750 600" ]
check "restore: notify dir mode left alone, history.jsonl 0644" \
  [ "$(stat -c %a "$T/restore/notify") $(stat -c %a "$T/restore/notify/history.jsonl")" = "755 644" ]

# Appends while the backup reads: every archived line is still whole JSON.
setup
printf '{"ts":0,"free":0}\n' >"$T/srv/dashboard/samples.jsonl"
(
  i=1
  while :; do
    printf '{"ts":%d,"free":%d,"pad":"%s"}\n' "$i" "$i" "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" >>"$T/srv/dashboard/samples.jsonl"
    printf '{"ts":%d,"title":"t"}\n' "$i" >>"$T/srv/notify/history.jsonl"
    i=$((i + 1))
  done
) &
APP_PID=$!
sleep 0.3
check "live appends: backup exits 0" run_backup
kill "$APP_PID" 2>/dev/null || true
wait "$APP_PID" 2>/dev/null || true
APP_PID=""
mkdir -p "$T/x/live" && tar -xf "$B/appdata/$(newest)" -C "$T/x"
tar -xzf "$T/x/dashboard.tar.gz" -C "$T/x/live" ./samples.jsonl
tar -xzf "$T/x/notify.tar.gz" -C "$T/x/live" ./history.jsonl
for f in samples.jsonl history.jsonl; do
  check "live appends: archived $f has lines" [ "$(wc -l <"$T/x/live/$f")" -gt 1 ]
  check "live appends: every archived $f line is whole JSON" \
    python3 -c 'import json, sys; [json.loads(line) for line in open(sys.argv[1])]' "$T/x/live/$f"
done

# Fresh box: no dashboard or notifier state yet.
setup
rm -rf "$T/srv/dashboard" "$T/srv/notify"
check "fresh box: backup exits 0 without dashboard/notify dirs" run_backup
check "fresh box: no dashboard/notify members" \
  sh -c '! tar -tf "$1" | grep -qE "^(dashboard|notify)\.tar\.gz$"' _ "$B/appdata/$(newest)"
check "fresh box: both skips logged" sh -c 'grep -q "dashboard.tar.gz skipped" "$1" && grep -q "notify.tar.gz skipped" "$1"' _ "$T/out.log"
setup
rm -f "$T/srv/notify/history.jsonl"
check "notifier dir without history: backup exits 0" run_backup
check "notifier dir without history: no notify member" sh -c '! tar -tf "$1" | grep -qx notify.tar.gz' _ "$B/appdata/$(newest)"
check "notifier dir without history: logged" grep -q 'history.jsonl does not exist; left out of notify.tar.gz' "$T/out.log"

echo
echo "backup tests: $PASS passed, $FAILS failed"
[ "$FAILS" -eq 0 ]
