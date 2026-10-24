#!/usr/bin/env bash
# Local test for files/homelab-pull.sh. The "server" is a temp dir filled by the real
# roles/backups/files/homelab-backup.sh, and a fake ssh runs the forced command the server's
# authorized_keys uses (rrsync -ro <dir>) when rrsync is available, so the rsync options are
# checked against the same restriction. No network, no host.
#   bash roles/backup_pull/tests/test_pull.sh
# sh -c bodies are single-quoted on purpose (they read "$1"); run_* get extra args through check().
# shellcheck disable=SC2016,SC2119,SC2120
set -euo pipefail
export LC_ALL=C

HERE=$(cd "$(dirname "$0")" && pwd)
PULL="$HERE/../files/homelab-pull.sh"
BACKUP="$HERE/../../backups/files/homelab-backup.sh"
FAKE_SQ="$HERE/../../backups/tests/fake_sqlite3.py"
T=$(mktemp -d)
trap 'chmod -R u+rwX "$T" 2>/dev/null || true; rm -rf "$T"' EXIT

if command -v sqlite3 >/dev/null; then SQ=$(command -v sqlite3); else SQ=$FAKE_SQ; fi
RRSYNC=${RRSYNC:-} # set RRSYNC=/path/to/rrsync to test a specific copy (e.g. the server's)
if [ -z "$RRSYNC" ]; then
  for c in /usr/bin/rrsync /usr/share/doc/rsync/support/rrsync; do
    if [ -f "$c" ]; then RRSYNC=$c && break; fi
  done
fi
echo "rrsync under test: ${RRSYNC:-none (fake ssh runs the command unrestricted)}"

PASS=0
FAILS=0
ok() { PASS=$((PASS + 1)); printf 'ok   %s\n' "$1"; }
bad() { FAILS=$((FAILS + 1)); printf 'FAIL %s\n' "$1"; }
check() {
  local what=$1
  shift
  if "$@"; then ok "$what"; else bad "$what"; fi
}

SRV="$T/server" # the server's backups dir (rrsync root)
DATA="$T/data"
DEST="$T/home/backups/v2"
mkdir -p "$SRV" "$DATA/app" "$T/home/.ssh"
echo key >"$T/home/.ssh/pull_key"
python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("create table t(i)"); c.execute("insert into t values (1)"); c.commit()' "$DATA/app/app.db"
echo conf >"$DATA/app/config.xml"

# Fake ssh: log the options, drop them and the host, then behave like sshd's forced command.
cat >"$T/fakessh" <<EOF
#!/bin/bash
printf '%s\n' "\$*" >>"$T/ssh.log"
[ -e "$T/ssh.down" ] && { echo "ssh: connect to host: Connection refused" >&2; exit 255; }
while [ \$# -gt 0 ]; do
  case "\$1" in -i|-o|-p|-l) shift 2 ;; -*) shift ;; *) break ;; esac
done
shift # host
if [ -n "$RRSYNC" ]; then
  export SSH_ORIGINAL_COMMAND="\$*" SSH_CONNECTION="127.0.0.1 50000 127.0.0.1 22"
  exec python3 "$RRSYNC" -ro "$SRV"
fi
cd "$SRV" && exec "\$@"
EOF
chmod +x "$T/fakessh"

make_backup() { # one real nightly run into $SRV
  sleep 1.1 # archive names have one-second resolution
  env BACKUP_ROOT="$SRV" DATA_DIR="$DATA" PUSH_DIR="$T/nopush" BOOTGATE_DIR="$T/nogate" \
    KEEP="${KEEP:-14}" BACKUP_GROUP="$(id -gn)" NOTIFY_BIN=/nonexistent SQLITE3="$SQ" \
    bash "$BACKUP" >/dev/null
}
run_pull() {
  local rc=0
  env HOME="$T/home" PULL_USER=backuppull PULL_HOST=server.invalid PULL_KEY='~/.ssh/pull_key' \
    PULL_DEST='~/backups/v2' PULL_SSH="$T/fakessh" "$@" bash "$PULL" >"$T/out.log" 2>&1 || rc=$?
  return "$rc"
}
srv_list() { find "$SRV/appdata" -maxdepth 1 -name 'appdata-*.tar' -printf '%f\n' | sort; }
dest_list() { find "$DEST/appdata" -maxdepth 1 -name 'appdata-*.tar' -printf '%f\n' 2>/dev/null | sort; }
dest_state() { (cd "$DEST" && find . -path ./.incoming -prune -o -type f -printf '%P %s %T@\n' | sort); }
snapshot() { dest_state >"$T/before"; }
unchanged() { dest_state | cmp -s - "$T/before"; }
newest_srv() { srv_list | tail -n 1; }
member_offset() { # byte offset of a member's header inside a tar
  python3 -c 'import sys,tarfile; print(next(m.offset for m in tarfile.open(sys.argv[1]) if m.name == sys.argv[2]))' "$1" "$2"
}

# --- 0. No archives on the server yet ------------------------------------------------------------
mkdir -p "$SRV/appdata"
echo 2026-01-01T00:00:00Z >"$SRV/LAST_OK"
if run_pull; then bad "empty server: should fail"; else ok "empty server: exits non-zero"; fi
check "empty server: no .last_pull" [ ! -e "$DEST/.last_pull" ]
rm -f "$SRV/LAST_OK"

# --- 1. First pull --------------------------------------------------------------------------------
make_backup
make_backup
check "first pull exits 0" run_pull
check "first pull: same archives as the server" [ "$(dest_list)" = "$(srv_list)" ]
check "first pull: LAST_OK copied" cmp -s "$SRV/LAST_OK" "$DEST/LAST_OK"
check "first pull: .last_pull is ISO-8601 UTC" grep -qxE '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z' "$DEST/.last_pull"
n=$(newest_srv)
check "promoted copy is a hard link to staging (no second copy on disk)" [ "$DEST/appdata/$n" -ef "$DEST/.incoming/appdata/$n" ]
check "both archives were verified" [ "$(grep -c 'verified' "$T/out.log")" = 2 ]
check "ssh offered only the pull key, non-interactively" \
  grep -qE -- "-i $T/home/.ssh/pull_key -o BatchMode=yes -o IdentitiesOnly=yes" "$T/ssh.log"
check "no temp dir left behind" [ -z "$(find "$DEST" -maxdepth 1 -name '.tmp.*')" ]
check "dest is private (0700)" [ "$(stat -c %a "$DEST")" = 700 ]
check "pulled archives are private (0600)" [ "$(stat -c %a "$DEST/appdata/$n")" = 600 ]

# --- 2. Nothing new: nothing re-verified, .last_pull kept by --delete-after -------------------------
sleep 1.1
check "repeat pull exits 0" run_pull
check "repeat pull: unchanged archives are not re-verified" [ "$(grep -c 'verified' "$T/out.log" || true)" = 0 ]
check "repeat pull: .last_pull survives the promotion" [ -s "$DEST/.last_pull" ]

# --- 3. Rotation on the server is mirrored after a good pull --------------------------------------
KEEP=2 make_backup
KEEP=2 make_backup
check "rotation pull exits 0" run_pull
check "rotation: dest holds exactly the server's archives" [ "$(dest_list)" = "$(srv_list)" ]
check "rotation: and there are 2 of them" [ "$(dest_list | wc -l)" = 2 ]

# --- 4. A copy cut short at a member boundary (lists cleanly!) is rejected ------------------------
snapshot
cp "$DEST/.last_pull" "$T/last_pull.before"
make_backup
n=$(newest_srv)
off=$(member_offset "$SRV/appdata/$n" MANIFEST.sha256)
truncate -s "$off" "$SRV/appdata/$n"
check "control: the cut archive passes tar -tf" sh -c 'tar -tf "$1" >/dev/null' _ "$SRV/appdata/$n"
if run_pull; then bad "cut at boundary: should fail"; else ok "cut at boundary: exits non-zero"; fi
check "cut at boundary: reason is the missing manifest" grep -q 'no MANIFEST.sha256' "$T/out.log"
check "cut at boundary: dest untouched" unchanged
check "cut at boundary: .last_pull not advanced" cmp -s "$DEST/.last_pull" "$T/last_pull.before"
check "cut at boundary: bad copy dropped from staging" [ ! -e "$DEST/.incoming/appdata/$n" ]

# --- 5. A copy cut mid-member is rejected ----------------------------------------------------------
size=$(stat -c %s "$SRV/appdata/$n")
truncate -s $((size / 2)) "$SRV/appdata/$n"
if run_pull; then bad "cut mid-member: should fail"; else ok "cut mid-member: exits non-zero"; fi
check "cut mid-member: dest untouched" unchanged

# --- 6. Same-size corruption is caught by the manifest hashes ----------------------------------------
rm -f "$SRV/appdata/$n"
make_backup
n=$(newest_srv)
off=$(member_offset "$SRV/appdata/$n" files.tar.gz)
printf 'XXXXXXXX' | dd of="$SRV/appdata/$n" bs=1 seek=$((off + 512 + 20)) conv=notrunc status=none
if run_pull; then bad "bit flip: should fail"; else ok "bit flip: exits non-zero"; fi
check "bit flip: reason is a hash mismatch" grep -q 'bad-hash' "$T/out.log"
check "bit flip: dest untouched" unchanged

# --- 7. An already-pulled archive that goes bad on the server never replaces the good copy ----------
rm -f "$SRV/appdata/$n"
check "recovery pull exits 0" run_pull
old=$(dest_list | head -n 1)
sha_before=$(sha256sum <"$DEST/appdata/$old")
truncate -s 10240 "$SRV/appdata/$old"
if run_pull; then bad "bad re-sync of a kept archive: should fail"; else ok "bad re-sync of a kept archive: exits non-zero"; fi
check "kept archive: good copy still in dest, byte for byte" [ "$(sha256sum <"$DEST/appdata/$old")" = "$sha_before" ]
rm -f "$SRV/appdata/$old"

# --- 8. The server's in-progress work dir (root 0700) is skipped ----------------------------------
mkdir -p "$SRV/.work.abc123" && echo partial >"$SRV/.work.abc123/out.tar" && chmod 0700 "$SRV/.work.abc123"
chmod 0000 "$SRV/.work.abc123"
check "work dir present: pull exits 0" run_pull
check "work dir present: not in staging" [ ! -e "$DEST/.incoming/.work.abc123" ]
check "work dir present: not in dest" [ ! -e "$DEST/.work.abc123" ]
chmod 0700 "$SRV/.work.abc123" && rm -rf "$SRV/.work.abc123"

# --- 9. Stale server: archives kept, but the unit fails loudly ----------------------------------------
echo 2020-01-01T00:00:00Z >"$SRV/LAST_OK"
touch -d 2020-01-01T00:00:00Z "$SRV/LAST_OK" # same size: make sure rsync's size+mtime check sees it
if run_pull; then bad "stale LAST_OK: should fail"; else ok "stale LAST_OK: exits non-zero"; fi
check "stale LAST_OK: message names the timer" grep -q 'homelab-backup.timer' "$T/out.log"
check "stale LAST_OK: archives still promoted" [ "$(dest_list)" = "$(srv_list)" ]
echo garbage >"$SRV/LAST_OK"
touch -d 2020-01-02T00:00:00Z "$SRV/LAST_OK"
if run_pull; then bad "garbage LAST_OK: should fail"; else ok "garbage LAST_OK: exits non-zero"; fi
check "garbage LAST_OK: clear message" grep -q 'not a timestamp' "$T/out.log"
make_backup # fresh LAST_OK again

# --- 10. Transfer failure leaves dest alone ------------------------------------------------------------
snapshot
touch "$T/ssh.down"
if run_pull; then bad "ssh down: should fail"; else ok "ssh down: exits non-zero"; fi
check "ssh down: reason is rsync" grep -q 'rsync exited' "$T/out.log"
check "ssh down: dest untouched" unchanged
rm -f "$T/ssh.down"
check "back up: pull exits 0" run_pull
check "summary counts the archives kept in dest" grep -q "$(dest_list | wc -l) archive(s) kept" "$T/out.log"

echo
echo "pull tests: $PASS passed, $FAILS failed"
[ "$FAILS" -eq 0 ]
