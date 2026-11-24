#!/bin/bash
# Phase 6: nightly restic backup of ~/server (Immich photos + its daily DB dumps, the other
# SQLite databases as consistent copies from sqlite-dumps.sh) and Jellyfin's settings to
# ~/Backups (or a USB disk named "ServerBackup" when connected). If
# secrets/restic-offsite.env exists (B2, R2, a Storage Box...), the same backup also goes there,
# which is what protects against losing the Mac itself. Alerts go to the local ntfy.
set -euo pipefail
RESTIC=/opt/homebrew/bin/restic
NTFY="http://127.0.0.1:8091/$(cat ~/server/secrets/ntfy-topic)"

alert() { curl -fsS -m 10 -H "Title: Backup" -H "Priority: $1" -d "$2" "$NTFY" >/dev/null || true; }
# healthchecks.io (see heartbeat.sh) also emails when a night's backup never finishes,
# which ntfy can't tell: the Mac may be off. Pings: /start, then success or /fail.
HC_BACKUP_URL=$(source ~/server/secrets/healthchecks.env 2>/dev/null; echo "${HC_BACKUP_URL:-}")
hc() {
  [[ -n $HC_BACKUP_URL ]] || return 0
  curl -fsS -m 10 --retry 3 --data-raw "${2:-}" "$HC_BACKUP_URL$1" >/dev/null || true
}
trap 'alert high "Backup failed at line $LINENO. See ~/Library/Logs/backup.log"; hc /fail "Failed at line $LINENO"' ERR
hc /start

JELLYFIN="$HOME/Library/Application Support/jellyfin"
PATHS=(~/server "$JELLYFIN")
EXCLUDES=(
  --exclude ~/server/immich/postgres
  --exclude ~/server/immich/library/thumbs
  --exclude ~/server/immich/library/encoded-video
  --exclude '*/data/cache'
  --exclude ~/server/soundbox/data/models
  --exclude ~/server/soundbox/data/bin
  --exclude ~/server/soundbox/src
  --exclude "$JELLYFIN/cache"
  --exclude "$JELLYFIN/log"
  --exclude "$JELLYFIN/transcodes"
  --exclude-file ~/server/sqlite-dumps/excludes.txt
)
# Free space a backup must leave on a local repository's disk: the same 15% disk-alert.sh warns at.
RESERVE_PCT=15

# A repository on a local disk (the internal SSD today, next to the photos it copies) must not
# fill it: Immich's Postgres and the other containers stop working on a full disk. A dry run
# tells how much tonight's backup would add; if that leaves less than the reserve, it's skipped.
has_room() (
  source "$1"
  [[ $RESTIC_REPOSITORY == /* ]] || return 0
  $RESTIC snapshots >/dev/null 2>&1 || return 0   # first backup: nothing to measure against yet
  need=$($RESTIC backup "${PATHS[@]}" "${EXCLUDES[@]}" --dry-run --json 2>/dev/null \
    | /usr/bin/jq -s 'map(select(.message_type == "summary"))[0].data_added_packed // 0')
  read -r total free < <(df -Pk "$RESTIC_REPOSITORY" | awk 'NR==2 {print $2 * 1024, $4 * 1024}')
  if (( free - ${need:-0} < total * RESERVE_PCT / 100 )); then
    gb() { echo "$(( $1 / 1000000000 )) GB"; }
    echo "SKIPPED $RESTIC_REPOSITORY: needs $(gb "${need:-0}"), $(gb "$free") free, keeps $RESERVE_PCT% free"
    alert high "Skipped the backup to $RESTIC_REPOSITORY: it needs $(gb "${need:-0}") and would leave less than $RESERVE_PCT% of that disk free ($(gb "$free") free now). Free space, or plug in the ServerBackup disk."
    return 1
  fi
)

# Runs one backup in a subshell, so each repository keeps its own RESTIC_* variables.
# Exits 3 when the snapshot was saved but some files couldn't be read (restic's own code 3):
# the rest of the night (forget, prune, check, the other repository) still runs.
backup_to() (
  source "$1"
  echo "== $(date '+%F %T') $RESTIC_REPOSITORY"
  $RESTIC snapshots >/dev/null 2>&1 || $RESTIC init
  rc=0
  $RESTIC backup "${PATHS[@]}" "${EXCLUDES[@]}" --tag nightly --cleanup-cache || rc=$?
  (( rc == 0 || rc == 3 )) || exit "$rc"
  # Called as `backup_to … || rc=$?`, so set -e is off in here: each step says its own failure.
  # --retry-lock: the restore drill or a run by hand may hold the lock when the Mac wakes and
  # launchd runs the missed jobs together.
  $RESTIC forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune --retry-lock 30m || exit 1
  $RESTIC check --read-data-subset=1% --retry-lock 30m || exit 1
  exit "$rc"
)

~/server/setup/n8n-export.sh || echo "n8n export failed"
~/server/setup/trim-logs.sh || echo "log trim failed"

# The live SQLite files are left out (excludes.txt): restic takes these copies instead.
# A failed copy doesn't stop the backup (the last good one is still there), but it's reported.
dump_failed=$(~/server/setup/sqlite-dumps.sh) || {
  echo "SQLite copy failed: $dump_failed"
  alert high "SQLite copy failed for: $dump_failed. The backup has their previous copy."
}

# One repository failing doesn't stop the other (the offsite copy matters most when the local fails).
skipped=() failed=() partial=()
for env in ~/server/secrets/restic.env ~/server/secrets/restic-offsite.env; do
  name=$(basename "$env" .env)
  if [[ ! -f $env ]]; then
    echo "No offsite copy: create ~/server/secrets/restic-offsite.env (see setup/restic-offsite.env.example)"
  elif has_room "$env"; then
    rc=0
    backup_to "$env" || rc=$?
    if (( rc == 3 )); then partial+=("$name"); elif (( rc )); then failed+=("$name"); fi
  else
    skipped+=("$name")
  fi
done
if (( ${#partial[@]} )); then
  echo "Some files couldn't be read (saved without them): ${partial[*]}"
  alert default "Backup saved, but some files couldn't be read (${partial[*]}). See ~/Library/Logs/backup.log"
fi
if (( ${#failed[@]} )); then
  echo "$(date '+%F %T') backup FAILED for ${failed[*]}"
  alert high "Backup failed for ${failed[*]}. See ~/Library/Logs/backup.log"
  hc /fail "Failed: ${failed[*]}"
elif (( ${#skipped[@]} )); then
  echo "$(date '+%F %T') backup INCOMPLETE: no room for ${skipped[*]}"
  hc /fail "Skipped for lack of space: ${skipped[*]}"
elif [[ -n $dump_failed ]]; then
  echo "$(date '+%F %T') backup OK"
  hc /fail "SQLite copy failed for: $dump_failed"
else
  echo "$(date '+%F %T') backup OK"
  hc ""
fi
