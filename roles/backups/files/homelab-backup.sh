#!/usr/bin/env bash
# homelab-backup: nightly, integrity-checked archive of the media server's app data.
# Installed by the Ansible role `backups` (docs/CONTRACTS.md §8), run by homelab-backup.timer.
#
# Writes $BACKUP_ROOT/appdata/appdata-<UTC stamp>.tar (root:$BACKUP_GROUP 0640) holding:
#   sqlite/<path>     a `sqlite3 .backup` copy of every SQLite database under $DATA_DIR
#                     (*.db, *.sqlite, *.sqlite3 that carry the SQLite header), each one
#                     passing PRAGMA integrity_check. Owner and mode follow the original.
#   files.tar.gz      everything else under $DATA_DIR (directories included, owners kept),
#                     minus directories named in $EXCLUDE_DIRS (caches, metadata,
#                     transcodes, logs) and minus the live databases and their -wal/-shm.
#   push.tar.gz       $PUSH_DIR (VAPID keys + subscriptions), if present
#   bootgate.tar.gz   $BOOTGATE_DIR (boot gate state + history), if present
#   MANIFEST.sha256   sha256 of every member above. Always the LAST member, so a copy that
#                     was cut short is detectable (homelab-pull checks it).
# Keeps the newest $KEEP archives, writes $BACKUP_ROOT/LAST_OK (ISO 8601, UTC) on success,
# and on any failure pushes `homelab-notify --tag backup` with the reason.
#
# Every run, success or failure, atomically writes $STATUS_DIR/status.json (0644, read by the
# dashboard, docs/CONTRACTS.md §11.5):
#   {"ok": bool, "last_ok": ISO|null, "last_run": ISO, "archive": "<file name>"|null,
#    "size_bytes": int|null, "error": str|null}
# On failure last_ok/archive/size_bytes carry over from the previous success. "error" is a
# short path-free summary; the full detail goes to the journal only.
#
# Restore (as root, with the app stopped):
#   mkdir /tmp/r && tar -xpf appdata-<stamp>.tar -C /tmp/r
#   tar -xpzf /tmp/r/files.tar.gz -C "$DATA_DIR"
#   copy each /tmp/r/sqlite/<path> to $DATA_DIR/<path> (cp -p), then delete <path>-wal/-shm
#   tar -xpzf /tmp/r/push.tar.gz -C "$PUSH_DIR"
#
# Every setting can be overridden from the environment (the systemd unit sets them; the
# role's tests point them at a temp dir).
set -Eeuo pipefail
umask 077
export LC_ALL=C

BACKUP_ROOT=${BACKUP_ROOT:-/srv/homelab/backups}
DATA_DIR=${DATA_DIR:-/srv/homelab/data}
PUSH_DIR=${PUSH_DIR:-/srv/homelab/push}
BOOTGATE_DIR=${BOOTGATE_DIR:-/var/lib/bootgate}
KEEP=${KEEP:-14}
BACKUP_GROUP=${BACKUP_GROUP:-backuppull}
NOTIFY_BIN=${NOTIFY_BIN:-/usr/local/bin/homelab-notify}
SQLITE3=${SQLITE3:-sqlite3}
EXCLUDE_DIRS=${EXCLUDE_DIRS-cache Cache .cache metadata MediaCover trickplay keyframes transcodes log logs}
STATUS_DIR=${STATUS_DIR:-/var/lib/homelab-backup}

# tar messages that only mean a live app changed or deleted a file while we read it.
VANISH_RE='No such file or directory|file changed as we read it|File removed before we read it|Exiting with failure status due to previous errors'
SQLITE_MAGIC=53514c69746520666f726d6174203300 # "SQLite format 3\0"

WORK=""
OUT=""
pub="unexpected error"    # path-free summary: status.json "error" and the push body
reason="unexpected error" # full detail: journal only
RUN_ISO=""
CUR_LAST_OK="" # set once this run has published its archive
CUR_ARCHIVE=""
CUR_SIZE=""
ISO_RE='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'

log() { printf '%s\n' "$*"; }

# fail SUMMARY [DETAIL]: SUMMARY must hold no paths or secrets; DETAIL is journal-only.
fail() {
  pub=$1
  reason=$1${2:+: $2}
  exit 1
}

# write_status OK LAST_OK LAST_RUN ARCHIVE SIZE ERROR (empty string = null), atomically.
write_status() {
  local tmp
  [ -d "$STATUS_DIR" ] || install -d -m 0755 "$STATUS_DIR" || return 1
  tmp=$(mktemp "$STATUS_DIR/.status.XXXXXX") || return 1
  if jq -n --argjson ok "$1" --arg last_ok "$2" --arg last_run "$3" --arg archive "$4" \
    --arg size "$5" --arg error "$6" '
      def nul: if . == "" then null else . end;
      {ok: $ok, last_ok: ($last_ok | nul), last_run: $last_run, archive: ($archive | nul),
       size_bytes: ($size | nul | if . == null then null else tonumber end), error: ($error | nul)}' \
    >"$tmp" && chmod 0644 "$tmp" && mv -f -- "$tmp" "$STATUS_DIR/status.json"; then
    return 0
  fi
  rm -f -- "$tmp"
  return 1
}

# prev_field NAME REGEX: a field of the previous status.json, printed only if it matches REGEX
# (so a damaged or hand-edited file can't smuggle a path into the new one).
prev_field() {
  local v
  v=$(jq -r --arg k "$1" '.[$k] // empty | tostring' "$STATUS_DIR/status.json" 2>/dev/null) || return 0
  if [[ $v =~ $2 ]]; then printf '%s' "$v"; fi
}

# Failure status: this run's archive if it got as far as publishing one, else the last success.
record_failure() {
  local last_ok=$CUR_LAST_OK archive=$CUR_ARCHIVE size=$CUR_SIZE
  if [ -z "$archive" ]; then
    last_ok=$(prev_field last_ok "$ISO_RE")
    archive=$(prev_field archive '^appdata-[0-9]{8}T[0-9]{6}Z\.tar$')
    size=$(prev_field size_bytes '^[0-9]+$')
  fi
  write_status false "$last_ok" "${RUN_ISO:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}" "$archive" "$size" "${pub:0:300}"
}

notify_failure() {
  local rc=0
  if [ ! -x "$NOTIFY_BIN" ]; then
    log "notifier $NOTIFY_BIN is missing; failure not pushed" >&2
    return 0
  fi
  "$NOTIFY_BIN" --tag backup --title "Backup failed" --body "$1" || rc=$?
  if [ "$rc" -ne 0 ]; then
    log "homelab-notify exited $rc" >&2
  fi
}

on_exit() {
  local rc=$?
  if [ -n "$WORK" ]; then
    rm -rf -- "$WORK" || true
  fi
  if [ "$rc" -ne 0 ]; then
    log "backup FAILED: $reason" >&2
    record_failure || log "could not write $STATUS_DIR/status.json" >&2
    notify_failure "${pub:0:300}"
  fi
  exit "$rc"
}

trap on_exit EXIT
trap 'pub="unexpected error (line $LINENO)"; reason="failed at line $LINENO: $BASH_COMMAND"' ERR
trap 'pub="interrupted by a signal"; reason=$pub; exit 143' TERM INT HUP

is_db_name() {
  case $1 in
    *.db | *.sqlite | *.sqlite3) return 0 ;;
    *) return 1 ;;
  esac
}

is_sqlite() {
  local magic
  magic=$(head -c 16 -- "$1" 2>/dev/null | od -An -tx1 | tr -d ' \n') || return 1
  [ "$magic" = "$SQLITE_MAGIC" ]
}

# run_tar LABEL TAR-ARGS...: tolerates only "a live app changed/removed a file" complaints.
run_tar() {
  local what=$1 rc=0
  shift
  tar "$@" 2>"$WORK/tar.err" || rc=$?
  [ "$rc" -eq 0 ] && return 0
  grep -v -E "$VANISH_RE" "$WORK/tar.err" >"$WORK/tar.bad" || true
  if [ "$rc" -gt 2 ] || [ -s "$WORK/tar.bad" ]; then
    fail "tar of $what failed (exit $rc)" "$(head -c 200 "$WORK/tar.err")"
  fi
  log "note: $what: $(grep -c 'No such file\|changed as we read\|removed before' "$WORK/tar.err") file(s) changed or vanished while archiving (normal for live apps)"
}

# Consistent online copy of one SQLite database, integrity-checked, into $OUT/sqlite/<rel>.
backup_db() {
  local src=$1 rel=$2 tmp="$WORK/db.tmp" dst="$OUT/sqlite/$2" res label="${2##*/}"
  [[ $rel != */* ]] || label="$label (${rel%%/*})" # file name + app, no path
  rm -f -- "$tmp" "$tmp-wal" "$tmp-shm" "$tmp-journal"
  # -init /dev/null: a root ~/.sqliterc (e.g. ".headers on") must not change the output we parse.
  if ! "$SQLITE3" -init /dev/null -bail -cmd ".timeout 30000" "$src" ".backup '$tmp'" </dev/null >"$WORK/sqlite.err" 2>&1; then
    fail "sqlite backup of $label failed" "$rel: $(head -c 200 "$WORK/sqlite.err")"
  fi
  res=$("$SQLITE3" -init /dev/null -bail "$tmp" "PRAGMA integrity_check;" </dev/null 2>&1) || true
  if [ "$res" != ok ]; then
    fail "integrity check failed for $label" "$rel: ${res:0:200}"
  fi
  mkdir -p -- "$(dirname -- "$dst")"
  chown --reference="$src" -- "$tmp"
  chmod --reference="$src" -- "$tmp"
  mv -- "$tmp" "$dst"
}

archive_dir() { # NAME LABEL DIR
  local name=$1 label=$2 dir=$3
  if [ ! -d "$dir" ]; then
    log "warning: $dir does not exist; $name.tar.gz skipped"
    return 0
  fi
  run_tar "$label" --numeric-owner -czf "$OUT/$name.tar.gz" -C "$dir" .
}

rotate() {
  local -a all=()
  local n i
  shopt -s nullglob
  all=("$BACKUP_ROOT"/appdata/appdata-*.tar)
  shopt -u nullglob
  n=${#all[@]}
  for ((i = 0; i < n - KEEP; i++)); do
    rm -f -- "${all[i]}"
    log "rotated out ${all[i]##*/}"
  done
}

main() {
  local epoch stamp iso rel base p rc n_db=0 n_files final
  local -a excl=() prune=() members=()
  local -A is_db=()

  epoch=$(date -u +%s)
  stamp=$(date -u -d "@$epoch" +%Y%m%dT%H%M%SZ)
  iso=$(date -u -d "@$epoch" +%Y-%m-%dT%H:%M:%SZ)
  RUN_ISO=$iso

  [[ $KEEP =~ ^[1-9][0-9]*$ ]] || fail "KEEP must be a positive integer" "got '$KEEP'"
  command -v jq >/dev/null || fail "jq is not installed"
  command -v "$SQLITE3" >/dev/null || fail "sqlite3 is not installed" "$SQLITE3"
  [ -d "$DATA_DIR" ] || fail "data dir does not exist" "$DATA_DIR"
  [ -d "$BACKUP_ROOT" ] || fail "backup dir does not exist" "$BACKUP_ROOT"
  getent group "$BACKUP_GROUP" >/dev/null || fail "group $BACKUP_GROUP does not exist"
  [ -d "$BACKUP_ROOT/appdata" ] || install -d -m 0750 -g "$BACKUP_GROUP" "$BACKUP_ROOT/appdata"

  # Leftovers of a run that was killed outright (the EXIT trap normally removes them).
  find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -name '.work.*' -mmin +720 -exec rm -rf -- {} +

  WORK=$(mktemp -d "$BACKUP_ROOT/.work.XXXXXX")
  [[ $WORK != *"'"* ]] || fail "work dir path contains a quote" "$WORK"
  OUT="$WORK/archive"
  mkdir -p "$OUT/sqlite"

  # 1. Inventory of $DATA_DIR, pruning excluded directory names anywhere in the tree.
  read -r -a excl <<<"$EXCLUDE_DIRS"
  if [ "${#excl[@]}" -gt 0 ]; then
    prune=('(' -type d '(' -name "${excl[0]}")
    for p in "${excl[@]:1}"; do prune+=(-o -name "$p"); done
    prune+=(')' -prune ')' -o)
  fi
  rc=0
  (cd "$DATA_DIR" && find . -mindepth 1 "${prune[@]}" '(' -type d -o -type f -o -type l ')' -print0) \
    >"$WORK/all.list" 2>"$WORK/find.err" || rc=$?
  if [ "$rc" -ne 0 ]; then
    grep -v 'No such file or directory' "$WORK/find.err" >"$WORK/find.bad" || true
    [ ! -s "$WORK/find.bad" ] || fail "scan of the data dir failed" "$(head -c 200 "$WORK/find.bad")"
  fi

  # 2. SQLite databases: consistent copies via the online backup API.
  while IFS= read -r -d '' p <&3; do
    rel=${p#./}
    if [ -f "$DATA_DIR/$rel" ] && [ ! -L "$DATA_DIR/$rel" ] && is_db_name "$rel" && is_sqlite "$DATA_DIR/$rel"; then
      backup_db "$DATA_DIR/$rel" "$rel"
      is_db[$rel]=1
      n_db=$((n_db + 1))
    fi
  done 3<"$WORK/all.list"

  # 3. Everything else, without the databases just copied and their live sidecars.
  : >"$WORK/files.list"
  while IFS= read -r -d '' p <&3; do
    rel=${p#./}
    [ -z "${is_db[$rel]:-}" ] || continue
    case $rel in
      *-wal | *-shm | *-journal)
        base=${rel%-*}
        [ -z "${is_db[$base]:-}" ] || continue
        ;;
    esac
    printf '%s\0' "$p" >>"$WORK/files.list"
  done 3<"$WORK/all.list"
  n_files=$(tr -cd '\0' <"$WORK/files.list" | wc -c)
  run_tar "data files" --numeric-owner -czf "$OUT/files.tar.gz" -C "$DATA_DIR" --null --no-recursion -T "$WORK/files.list"

  # 4. Push store and boot gate state.
  archive_dir push "push store" "$PUSH_DIR"
  archive_dir bootgate "boot gate state" "$BOOTGATE_DIR"

  # 5. Manifest (hashes of every member), then the outer tar with the manifest last.
  (cd "$OUT" && find . -type f -printf '%P\0' | sort -z | xargs -0 -r sha256sum --) >"$WORK/MANIFEST.sha256"
  mv -- "$WORK/MANIFEST.sha256" "$OUT/MANIFEST.sha256"
  members=(sqlite)
  for p in files.tar.gz push.tar.gz bootgate.tar.gz; do
    if [ -f "$OUT/$p" ]; then members+=("$p"); fi
  done
  members+=(MANIFEST.sha256)
  tar --numeric-owner -cf "$WORK/out.tar" -C "$OUT" -- "${members[@]}"
  tar -tf "$WORK/out.tar" >"$WORK/out.list" || fail "the new archive does not list cleanly"
  [ "$(tail -n 1 "$WORK/out.list")" = MANIFEST.sha256 ] || fail "MANIFEST.sha256 is not the last member"

  # 6. Publish atomically (same filesystem), stamp LAST_OK, rotate.
  final="$BACKUP_ROOT/appdata/appdata-$stamp.tar"
  chgrp -- "$BACKUP_GROUP" "$WORK/out.tar"
  chmod 0640 "$WORK/out.tar"
  mv -f -- "$WORK/out.tar" "$final"
  printf '%s\n' "$iso" >"$WORK/LAST_OK"
  chgrp -- "$BACKUP_GROUP" "$WORK/LAST_OK"
  chmod 0640 "$WORK/LAST_OK"
  mv -f -- "$WORK/LAST_OK" "$BACKUP_ROOT/LAST_OK"
  CUR_LAST_OK=$iso
  CUR_ARCHIVE=${final##*/}
  CUR_SIZE=$(stat -c %s -- "$final")
  rotate
  write_status true "$iso" "$iso" "$CUR_ARCHIVE" "$CUR_SIZE" "" || fail "could not write the status file" "$STATUS_DIR"

  log "backup ok: ${final##*/} ($(du -h -- "$final" | cut -f1)), $n_db database(s), $n_files other path(s)"
}

main "$@"
