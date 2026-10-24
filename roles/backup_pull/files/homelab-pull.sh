#!/usr/bin/env bash
# homelab-pull: copy the media server's nightly backup archives to this workstation.
# Installed by the Ansible role `backup_pull` (docs/CONTRACTS.md §8), run by the user timer
# homelab-pull.timer.
#
# The server side only allows a read-only rsync of its backups dir (rrsync -ro), so the
# remote path is empty: "<user>@<host>:" is the root of that dir.
#
# Never lets a bad transfer replace good data:
#   1. rsync -a --delete-after mirrors the server into $PULL_DEST/.incoming (staging).
#   2. Every archive that is new or changed there is checked: `tar -tf` must succeed and its
#      MANIFEST.sha256 (written last by homelab-backup) must be present and match every
#      member. A copy cut short at a member boundary still lists cleanly with tar -tf; the
#      missing manifest is what catches it.
#   3. Only then is staging promoted into $PULL_DEST (hard links, so no second copy on disk).
#      An archive that fails is deleted from staging (re-fetched next run) and kept out of
#      the promotion, so whatever $PULL_DEST already holds stays as it was.
#   4. $PULL_DEST/.last_pull gets the time (ISO 8601, UTC) once the newest archive verified;
#      the server's LAST_OK is only carried over then, too.
# Exits non-zero (a failed unit in `systemctl --user --failed`) when anything was rejected,
# the transfer failed, or the server's LAST_OK is older than $PULL_MAX_AGE_HOURS.
set -euo pipefail
umask 077
export LC_ALL=C

: "${PULL_USER:?set PULL_USER}" "${PULL_HOST:?set PULL_HOST}" "${PULL_KEY:?set PULL_KEY}" "${PULL_DEST:?set PULL_DEST}"
PULL_KEY=${PULL_KEY/#\~/$HOME}
PULL_DEST=${PULL_DEST/#\~/$HOME}
PULL_DEST=${PULL_DEST%/}
PULL_SSH=${PULL_SSH:-ssh}
PULL_MAX_AGE_HOURS=${PULL_MAX_AGE_HOURS:-48}
PULL_SOURCE=${PULL_SOURCE:-"$PULL_USER@$PULL_HOST:"}

STAGE="$PULL_DEST/.incoming"
TMP=""

log() { printf '%s\n' "$*"; }
die() {
  log "pull FAILED: $*" >&2
  exit 1
}
cleanup() { if [ -n "$TMP" ]; then rm -rf -- "$TMP"; fi; }
trap cleanup EXIT

# tar -tf, then every member against MANIFEST.sha256 in one streaming pass (no extraction).
verify_archive() {
  local a=$1
  if ! tar -tf "$a" >/dev/null 2>"$TMP/tar.err"; then
    log "  ${a##*/}: tar -tf failed: $(head -c 200 "$TMP/tar.err")" >&2
    return 1
  fi
  python3 - "$a" <<'PY'
import hashlib, sys, tarfile

def unescape(name):
    # sha256sum escapes "\" and newlines in names (and then prefixes the line with "\").
    out, i = [], 0
    while i < len(name):
        if name[i] == "\\" and i + 1 < len(name):
            out.append({"n": "\n", "r": "\r", "\\": "\\"}.get(name[i + 1], name[i + 1]))
            i += 2
        else:
            out.append(name[i])
            i += 1
    return "".join(out)

path = sys.argv[1]
label = path.rsplit("/", 1)[-1]
try:
    got, manifest = {}, None
    with tarfile.open(path, mode="r:") as tf:
        for m in tf:
            if not m.isfile():
                continue
            f = tf.extractfile(m)
            if m.name == "MANIFEST.sha256":
                manifest = f.read().decode("utf-8")
                continue
            h = hashlib.sha256()
            while chunk := f.read(1 << 20):
                h.update(chunk)
            got[m.name] = h.hexdigest()
    if manifest is None:
        raise ValueError("no MANIFEST.sha256 (archive cut short?)")
    want = {}
    for line in manifest.splitlines():
        if not line:
            continue
        escaped = line.startswith("\\")
        if escaped:
            line = line[1:]
        digest, sep, name = line.partition(" ")
        if not sep or len(digest) != 64:
            raise ValueError(f"malformed manifest line: {line[:80]!r}")
        name = name[1:] if name[:1] in (" ", "*") else name
        want[unescape(name) if escaped else name] = digest
    missing = sorted(set(want) - set(got))
    extra = sorted(set(got) - set(want))
    wrong = sorted(n for n in want if n in got and got[n] != want[n])
    if missing or extra or wrong:
        raise ValueError(f"manifest mismatch: missing={missing[:3]} unlisted={extra[:3]} bad-hash={wrong[:3]}")
    print(f"  {label}: verified {len(want)} members")
except (tarfile.TarError, OSError, EOFError, ValueError, UnicodeDecodeError) as e:
    print(f"  {label}: REJECTED: {e}", file=sys.stderr)
    sys.exit(1)
PY
}

main() {
  local rc=0 a name newest newest_ok=0 rejected=0 last_ok ok_epoch age_h
  local -a archives=() kept=()

  [[ $PULL_MAX_AGE_HOURS =~ ^[0-9]+$ ]] || die "PULL_MAX_AGE_HOURS must be a whole number"
  mkdir -p -- "$STAGE"
  TMP=$(mktemp -d "$PULL_DEST/.tmp.XXXXXX")
  : >"$TMP/rejected"

  # 1. Mirror into staging. IdentitiesOnly: offer only the pull key (the server allows 3 tries).
  #    /.work.* is the server's in-progress dir; exit 24 = a file vanished mid-run (rotation).
  #    --chmod: the copies hold secrets (VAPID key, app configs), so they stay private here.
  rsync -a --delete-after --chmod=go-rwx --exclude='/.work.*' \
    -e "$PULL_SSH -i '$PULL_KEY' -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=20" \
    "$PULL_SOURCE" "$STAGE/" || rc=$?
  case $rc in
    0) ;;
    24) log "note: a file vanished on the server during the transfer (rotation); continuing" ;;
    *) die "rsync exited $rc; $PULL_DEST left as it was" ;;
  esac

  # 2. Verify what is new or changed since the last promotion (same inode = already verified).
  shopt -s nullglob
  archives=("$STAGE"/appdata/appdata-*.tar)
  shopt -u nullglob
  [ "${#archives[@]}" -gt 0 ] || die "no archives on the server yet; $PULL_DEST left as it was"
  newest=${archives[-1]##*/}
  for a in "${archives[@]}"; do
    name=${a##*/}
    if [ -e "$PULL_DEST/appdata/$name" ] && [ "$a" -ef "$PULL_DEST/appdata/$name" ]; then
      [ "$name" != "$newest" ] || newest_ok=1
      continue
    fi
    if verify_archive "$a"; then
      [ "$name" != "$newest" ] || newest_ok=1
    else
      rejected=$((rejected + 1))
      rm -f -- "$a"
      printf '/appdata/%s\n' "$name" >>"$TMP/rejected"
    fi
  done

  # 3. Promote verified files. Excluded paths are neither copied nor deleted in $PULL_DEST.
  #    LAST_OK only moves together with a verified newest archive.
  [ "$newest_ok" -eq 1 ] || printf '/LAST_OK\n' >>"$TMP/rejected"
  rsync -a --delete-after --link-dest="$STAGE" \
    --exclude=/.incoming/ --exclude='/.tmp.*' --exclude=/.last_pull --exclude='/appdata/.*' \
    --exclude-from="$TMP/rejected" \
    "$STAGE/" "$PULL_DEST/"

  # 4. Record the pull, then judge the server's own freshness.
  if [ "$newest_ok" -eq 1 ]; then
    date -u +%Y-%m-%dT%H:%M:%SZ >"$TMP/last_pull"
    mv -f -- "$TMP/last_pull" "$PULL_DEST/.last_pull"
  fi
  [ "$rejected" -eq 0 ] || die "$rejected archive(s) failed verification and were not kept (newest: $newest)"

  last_ok=$(head -n 1 "$PULL_DEST/LAST_OK" 2>/dev/null || true)
  [ -n "$last_ok" ] || die "the server has no LAST_OK yet"
  ok_epoch=$(date -u -d "$last_ok" +%s 2>/dev/null) || die "the server's LAST_OK is not a timestamp: ${last_ok:0:40}"
  age_h=$((($(date -u +%s) - ok_epoch) / 3600))
  [ "$age_h" -le "$PULL_MAX_AGE_HOURS" ] ||
    die "the server's last good backup ($last_ok) is ${age_h}h old: is homelab-backup.timer running?"

  shopt -s nullglob
  kept=("$PULL_DEST"/appdata/appdata-*.tar)
  shopt -u nullglob
  log "pull ok: newest $newest, ${#kept[@]} archive(s) kept, server LAST_OK $last_ok"
}

main "$@"
