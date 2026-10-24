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
cleanup() {
  if [ -n "$WAL_PID" ]; then kill "$WAL_PID" 2>/dev/null || true; fi
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

setup() {
  chmod -R u+rwX "$T/srv" 2>/dev/null || true
  rm -rf "$T/srv" "$T/x"
  mkdir -p "$B" "$D" "$T/srv/push" "$T/srv/bootgate" "$T/bin"
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
  printf '#!/bin/sh\nprintf "%%s\\n" "$*" >>"%s/notify.log"\n' "$T" >"$T/notify"
  chmod +x "$T/notify"
  : >"$T/notify.log"
}

run_backup() { # extra VAR=value pairs may be passed
  local rc=0
  env BACKUP_ROOT="$B" DATA_DIR="$D" PUSH_DIR="$T/srv/push" BOOTGATE_DIR="$T/srv/bootgate" \
    KEEP=14 BACKUP_GROUP="$GROUP" NOTIFY_BIN="$T/notify" SQLITE3="$SQ" "$@" \
    bash "$SCRIPT" >"$T/out.log" 2>&1 || rc=$?
  return "$rc"
}

archives() { find "$B/appdata" -maxdepth 1 -name 'appdata-*.tar' -printf '%f\n' | sort; }
newest() { archives | tail -n 1; }
count_rows() { python3 -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("select count(*) from t").fetchone()[0])' "$1"; }
no_workdirs() { [ -z "$(find "$B" -maxdepth 1 -name '.work.*')" ]; }

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
  sqlite/jellyseerr/db/db.sqlite3 "sqlite/radarr/my db.db" files.tar.gz push.tar.gz bootgate.tar.gz; do
  check "archive has $m" grep -qxF "$m" "$T/members"
done
check "non-SQLite *.db is not in sqlite/" sh -c '! grep -q "^sqlite/adguard/.*stats.db" "$1"' _ "$T/members"
mkdir -p "$T/x" && tar -xf "$A" -C "$T/x"
check "every member matches MANIFEST.sha256" sh -c 'cd "$1" && sha256sum --quiet --strict -c MANIFEST.sha256' _ "$T/x"
check "manifest covers all 8 files" [ "$(wc -l <"$T/x/MANIFEST.sha256")" = 8 ]
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
python3 - "$D/jellyfin/data/data/jellyfin.db" <<'PY'
import sys
with open(sys.argv[1], "r+b") as f:
    f.seek(4096 + 8)          # inside page 2 (the table's b-tree page)
    f.write(b"\xff" * 1500)
PY
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
fi

# --- 7. EXCLUDE_DIRS can be overridden -----------------------------------------------------------
setup
check "EXCLUDE_DIRS='' backs up caches too" run_backup EXCLUDE_DIRS=
mkdir -p "$T/x" && tar -xf "$B/appdata/$(newest)" -C "$T/x" files.tar.gz
check "EXCLUDE_DIRS='': cache present" sh -c 'tar -tzf "$1" | grep -qx ./jellyfin/cache/img/a' _ "$T/x/files.tar.gz"

echo
echo "backup tests: $PASS passed, $FAILS failed"
[ "$FAILS" -eq 0 ]
