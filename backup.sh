#!/bin/bash
# Nightly backup of the proxy's SQLite database.
#
#   - Takes a consistent snapshot with `sqlite3 ... "VACUUM INTO"` -- one
#     atomic read transaction, already compacted, and it never writes to
#     the live DB or its WAL. Safe to run while the crawler is going; no
#     pause needed. Then gzips it (~30-50 MB).
#   - Always keeps a few copies locally (fallback for when the remote is
#     unreachable).
#   - Optionally pushes to a remote directory. If the remote isn't
#     reachable/mountable right now, that step is skipped silently -- a
#     missed off-box copy is not an error worth failing on.
#
# Configure via env vars (see the block below) or a `backup.env` next to
# this script. Wire it into cron, e.g.:
#
#     15 4 * * *  /path/to/xtream-filter-proxy/backup.sh
#
# The SMB push needs passwordless `sudo mount -t cifs` for the cron user
# (see README's "Backups" section).

set -u

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$SELF_DIR/backup.env" ] && . "$SELF_DIR/backup.env"

# --- config (env overrides these) ----------------------------------------
APP_DIR="${APP_DIR:-$SELF_DIR}"
DB="${DB:-$APP_DIR/data/proxy.db}"
LOCAL_DIR="${LOCAL_DIR:-$APP_DIR/data/backups}"
LOG="${LOG:-$APP_DIR/data/backup.log}"
LOCAL_KEEP="${LOCAL_KEEP:-3}"
REMOTE_KEEP="${REMOTE_KEEP:-14}"

# Remote target. Two ways:
#   1. REMOTE_DIR = an already-mounted path (NFS, sshfs, a USB disk).
#   2. SMB_SHARE + SMB_OPTS + SMB_SUBDIR = mount //host/share with cifs,
#      copy into <mount>/<SMB_SUBDIR>, unmount. SMB_HOST is pinged first.
REMOTE_DIR="${REMOTE_DIR:-}"
SMB_HOST="${SMB_HOST:-}"
SMB_SHARE="${SMB_SHARE:-}"
SMB_SUBDIR="${SMB_SUBDIR:-xtream-filter-proxy}"
SMB_OPTS="${SMB_OPTS:-guest,vers=3.0,uid=$(id -u),gid=$(id -g)}"
SMB_MNT="${SMB_MNT:-/mnt/xtream-backup}"

STAMP="$(date +%Y%m%d-%H%M%S)"
NAME="proxy-${STAMP}.db.gz"

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
mkdir -p "$(dirname "$LOG")"

# --- one at a time -------------------------------------------------------
exec 9>"$APP_DIR/data/.backup.lock"
if ! flock -n 9; then
  log "another backup is still running -- skipping"
  exit 0
fi

# --- consistent snapshot of the DB -----------------------------------
# `VACUUM INTO` writes a full, self-contained, already-compacted copy in
# one atomic read transaction. It never touches the live DB or its WAL --
# no pause, no checkpoint, nothing that could race the running app. (An
# earlier version paused the crawler and ran `wal_checkpoint(TRUNCATE)` on
# the live file; that raced concurrent writes and corrupted a page.)
mkdir -p "$LOCAL_DIR"
RAW="$(mktemp -u "${LOCAL_DIR}/.tmp.XXXXXX.db")"   # -u: sqlite creates it

if ! sqlite3 "$DB" "VACUUM INTO '$RAW';" 2>>"$LOG"; then
  log "ERROR: VACUUM INTO failed"
  rm -f "$RAW" "${RAW}-wal" "${RAW}-shm"
  exit 1
fi

# Sanity check. quick_check, not integrity_check: VACUUM INTO already
# rebuilds every page and index from a consistent read, so a full
# integrity_check (minutes on a Pi) is redundant -- quick_check catches a
# truncated / unreadable snapshot in a second or two.
if ! sqlite3 "$RAW" "PRAGMA quick_check;" 2>/dev/null | head -1 | grep -q '^ok$'; then
  log "ERROR: quick_check failed on the snapshot -- discarding"
  rm -f "$RAW"
  exit 1
fi

if ! gzip -c "$RAW" > "${LOCAL_DIR}/${NAME}"; then
  log "ERROR: gzip failed"
  rm -f "$RAW" "${LOCAL_DIR}/${NAME}"
  exit 1
fi
rm -f "$RAW"
log "local backup ok: ${NAME} ($(du -h "${LOCAL_DIR}/${NAME}" | cut -f1))"

ls -1t "${LOCAL_DIR}"/proxy-*.db.gz 2>/dev/null | tail -n +$((LOCAL_KEEP + 1)) | while read -r f; do
  rm -f "$f" && log "pruned local $(basename "$f")"
done

# --- remote copy ------------------------------------------------------
push_to() {   # $1 = target directory
  local dir="$1"
  if mkdir -p "$dir" \
     && cp "${LOCAL_DIR}/${NAME}" "${dir}/${NAME}.part" \
     && mv "${dir}/${NAME}.part" "${dir}/${NAME}"; then
    log "remote backup ok: ${dir}/${NAME}"
    ls -1t "${dir}"/proxy-*.db.gz 2>/dev/null | tail -n +$((REMOTE_KEEP + 1)) | while read -r f; do
      rm -f "$f" && log "pruned remote $(basename "$f")"
    done
    return 0
  fi
  log "ERROR: copy to ${dir} failed"
  return 1
}

if [ -n "$REMOTE_DIR" ]; then
  if [ -d "$REMOTE_DIR" ] || mountpoint -q "$(dirname "$REMOTE_DIR")" 2>/dev/null; then
    push_to "$REMOTE_DIR"
  else
    log "REMOTE_DIR ${REMOTE_DIR} not available -- skipping remote copy"
  fi
elif [ -n "$SMB_SHARE" ]; then
  if [ -n "$SMB_HOST" ] && ! ping -c1 -W2 "$SMB_HOST" >/dev/null 2>&1; then
    log "SMB host ${SMB_HOST} not responding -- skipping remote copy"
  elif ! sudo mkdir -p "$SMB_MNT" || ! sudo mount -t cifs "$SMB_SHARE" "$SMB_MNT" -o "$SMB_OPTS" 2>>"$LOG"; then
    log "SMB mount failed -- skipping remote copy"
  else
    push_to "${SMB_MNT}/${SMB_SUBDIR}"
    sync
    sudo umount "$SMB_MNT" 2>>"$LOG" || log "WARN: umount ${SMB_MNT} failed"
  fi
fi

exit 0
