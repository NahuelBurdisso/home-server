#!/bin/bash
# Restore drill: proves the backups can really be restored, every Sunday at 5:00
# (com.server.restore-drill), so the 20 h summary can say so. For each restic repository
# (the local one and, once it exists, the offsite one), from its latest nightly snapshot:
#   - every SQLite copy from sqlite-dumps.sh comes back and passes PRAGMA integrity_check;
#   - Finanzas and Recordatorios start from those copies in throwaway containers, answer their
#     pages, and hold about as many movements and reminders as the live apps (a day's changes apart);
#     Pulso starts from its copy and draws its pages (its history is there);
#   - a random sample of the other files (photos, configs, Jellyfin) comes back byte for byte.
# Writes n8n/feed/restore-drill.json (Sunday summary, phone widget) and alerts through ntfy only
# when something fails. Run by hand:  bash ~/server/setup/restore-drill.sh
set -uo pipefail
# --retry-lock: if the night's backup is still pruning (a big week of photos), wait for it.
RESTIC="/opt/homebrew/bin/restic --retry-lock 30m"
DOCKER=/usr/local/bin/docker
JQ=/usr/bin/jq
NTFY="http://127.0.0.1:8091/$(cat ~/server/secrets/ntfy-topic)"
OUT=~/server/n8n/feed/restore-drill.json
SAMPLE=20

# Under ~/Library/Caches, not /tmp: OrbStack mounts the home folder for the throwaway containers.
WORK=$(mktemp -d ~/Library/Caches/restore-drill.XXXXXX)
cleanup() { $DOCKER rm -f drill-finanzas drill-recordatorios drill-pulso >/dev/null 2>&1; rm -rf "$WORK"; }
trap cleanup EXIT
: >"$WORK/problems"
problem() { echo "$1" >>"$WORK/problems"; echo "PROBLEM: $1"; }

# Rows in a live app's database, asked inside its container (never opened from the Mac).
live_count() {
  $DOCKER exec "$1" python3 -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute(sys.argv[2]).fetchone()[0])' \
    "$2" "$3" 2>/dev/null || echo 0
}
# Restored and live counts agree when they differ by at most 10% (plus a few rows): the snapshot
# is from last night, and a day's imports or deletions are expected.
close_enough() { (( $1 > 0 || $2 == 0 )) && (( ($1 - $2) ** 2 <= ($2 / 10 + 5) ** 2 )); }

# Starts an app image on a restored database and checks it answers. Prints "true" or "false".
try_app() {
  local name=$1 image=$2 data=$3; shift 3
  local paths=("$@") port ok=true
  $DOCKER run -d --rm --name "drill-$name" -v "$data:/data" -p 127.0.0.1::8000 \
    -e TZ=America/Argentina/Cordoba -e FINANZAS_CLASSIFIER=none -e RECORDATORIOS_ALERTS=off -e PULSO_COLLECT=off \
    "$image" >/dev/null 2>&1 || { echo false; return; }
  port=$($DOCKER port "drill-$name" 8000 2>/dev/null | head -1 | cut -d: -f2)
  for _ in $(seq 30); do curl -fs -m 2 -o /dev/null "http://127.0.0.1:$port/health" && break; sleep 1; done
  for p in "${paths[@]}"; do
    curl -fs -m 30 -o /dev/null "http://127.0.0.1:$port$p" || ok=false
  done
  $DOCKER rm -f "drill-$name" >/dev/null 2>&1
  echo $ok
}

# One repository: its result as one JSON object on stdout; progress goes to the log.
drill_repo() (
  exec 3>&1 1>&2
  source "$1"
  problems_before=$(wc -l <"$WORK/problems")
  label=$(basename "$1" .env | sed 's/^restic-//; s/^restic$/local/')
  started=$(date +%s)
  dir="$WORK/$label"
  mkdir -p "$dir"

  snap=$($RESTIC snapshots --tag nightly --json 2>/dev/null | $JQ -c 'max_by(.time) // empty')
  if [[ -z $snap ]]; then
    problem "$label: no snapshot to restore"
    $JQ -nc --arg r "$label" '{repo: $r, ok: false}' >&3
    return
  fi
  id=$($JQ -r .short_id <<<"$snap")
  # restic's time carries the zone offset ("...12:39:57.123-03:00"); date wants "-0300".
  snap_epoch=$(date -j -f '%Y-%m-%dT%H:%M:%S%z' \
    "$($JQ -r .time <<<"$snap" | sed -E 's/\.[0-9]+//; s/([+-][0-9]{2}):([0-9]{2})$/\1\2/')" +%s)
  snap_time=$(date -r "$snap_epoch" '+%Y-%m-%dT%H:%M:%S')
  echo "== $label: snapshot $id from $snap_time"
  # Restoring last month's snapshot fine says nothing about last night's backup.
  snap_age_h=$(( ($(date +%s) - snap_epoch) / 3600 ))
  (( snap_age_h <= 36 )) || problem "$label: the newest snapshot is $snap_age_h h old"

  # 1. The SQLite copies.
  dumps="$HOME/server/sqlite-dumps"
  $RESTIC restore "$id" --target "$dir" --include "$dumps" >/dev/null 2>&1 || problem "$label: restic restore of the SQLite copies failed"
  dbs_ok=0 dbs_total=0
  for live in "$dumps"/*.db; do
    name=$(basename "$live")
    dbs_total=$((dbs_total + 1))
    if [[ $(/usr/bin/sqlite3 "file:$dir$dumps/$name?immutable=1" "PRAGMA integrity_check" 2>&1) == ok ]]; then
      dbs_ok=$((dbs_ok + 1))
    else
      problem "$label: $name did not come back intact"
    fi
  done

  # 2. The apps, started from what came back.
  count() { /usr/bin/sqlite3 "file:$1?immutable=1" "$2" 2>/dev/null || echo 0; }
  mkdir -p "$dir/finanzas" "$dir/recordatorios"
  cp "$dir$dumps/finanzas.db" "$dir/finanzas/finanzas.db" 2>/dev/null
  cp "$dir$dumps/recordatorios.db" "$dir/recordatorios/recordatorios.db" 2>/dev/null
  fin_rest=$(count "$dir/finanzas/finanzas.db" "SELECT COUNT(*) FROM transactions")
  fin_live=$(live_count finanzas /data/finanzas.db "SELECT COUNT(*) FROM transactions")
  rec_rest=$(count "$dir/recordatorios/recordatorios.db" "SELECT COUNT(*) FROM reminders")
  rec_live=$(live_count recordatorios /data/recordatorios.db "SELECT COUNT(*) FROM reminders")
  fin_up=$(try_app finanzas finanzas:local "$dir/finanzas" /mes /api/estado /gastos)
  rec_up=$(try_app recordatorios recordatorios:local "$dir/recordatorios" / /api/proximos?dias=30 /calendario.ics)
  # Pulso: only once a snapshot has its copy (a missing one is already reported as a SQLite problem).
  pulso_up=null
  if [[ -f "$dir$dumps/pulso.db" ]]; then
    mkdir -p "$dir/pulso" && cp "$dir$dumps/pulso.db" "$dir/pulso/pulso.db"
    pulso_up=$(try_app pulso pulso:local "$dir/pulso" / "/graficos?r=7d" /api/resumen /mapa)
    [[ $pulso_up == true ]] || problem "$label: Pulso did not start from the restored copy"
  fi
  [[ $fin_up == true ]] || problem "$label: Finanzas did not start from the restored copy"
  [[ $rec_up == true ]] || problem "$label: Recordatorios did not start from the restored copy"
  close_enough "$fin_rest" "$fin_live" || problem "$label: Finanzas came back with $fin_rest movements, the live app has $fin_live"
  close_enough "$rec_rest" "$rec_live" || problem "$label: Recordatorios came back with $rec_rest reminders, the live app has $rec_live"

  # 3. A random sample of everything else that hasn't changed since the snapshot. Files that
  # change all the time (logs, git's internals, live databases) say nothing about the backup.
  $RESTIC ls "$id" --json 2>/dev/null \
    | $JQ -r 'select(.type == "file" and .size > 0) | .path' \
    | grep -vE '/sqlite-dumps/|/\.git/|\.log$|/data/|-wal$|-shm$|/n8n/feed/' \
    | grep -v '[][*?\\]' \
    | while IFS= read -r f; do
        [[ -f $f && $(stat -f %m "$f") -le $snap_epoch ]] && echo "$f"
      done | sort -R | head -n "$SAMPLE" >"$dir/sample"
  sample_total=$(wc -l <"$dir/sample" | tr -d ' ')
  sample_ok=0
  if (( sample_total )); then
    includes=()
    while IFS= read -r f; do includes+=(--include "$f"); done <"$dir/sample"
    $RESTIC restore "$id" --target "$dir/files" "${includes[@]}" >/dev/null 2>&1
    while IFS= read -r f; do
      if cmp -s "$f" "$dir/files$f"; then
        sample_ok=$((sample_ok + 1))
      else
        problem "$label: $f came back different"
      fi
    done <"$dir/sample"
  fi

  secs=$(( $(date +%s) - started ))
  echo "   SQLite $dbs_ok/$dbs_total, Finanzas $fin_rest (live $fin_live) up=$fin_up, Recordatorios $rec_rest (live $rec_live) up=$rec_up, Pulso up=$pulso_up, files $sample_ok/$sample_total, ${secs}s"
  ok=$( (( $(wc -l <"$WORK/problems") == problems_before )) && echo true || echo false)
  $JQ -nc --arg r "$label" --arg snap "$snap_time" --argjson age $(( (started - snap_epoch) / 3600 )) \
    --argjson dbs_ok "$dbs_ok" --argjson dbs "$dbs_total" \
    --argjson fin "$fin_rest" --argjson fin_live "$fin_live" --argjson fin_up "$fin_up" \
    --argjson rec "$rec_rest" --argjson rec_live "$rec_live" --argjson rec_up "$rec_up" --argjson pulso_up "$pulso_up" \
    --argjson s_ok "$sample_ok" --argjson s "$sample_total" --argjson secs "$secs" --argjson ok "$ok" \
    '{repo: $r, snapshot: $snap, snapshot_age_h: $age, sqlite: {ok: $dbs_ok, total: $dbs},
      finanzas: {movimientos: $fin, en_vivo: $fin_live, arranca: $fin_up},
      recordatorios: {recordatorios: $rec, en_vivo: $rec_live, arranca: $rec_up}, pulso: {arranca: $pulso_up},
      archivos: {ok: $s_ok, total: $s}, segundos: $secs, ok: $ok}' >&3
)

echo "== $(date '+%F %T') restore drill"
: >"$WORK/repos"
for env in ~/server/secrets/restic.env ~/server/secrets/restic-offsite.env; do
  [[ -f $env ]] || continue
  drill_repo "$env" >>"$WORK/repos"
done

# A drill that restored nothing proved nothing (no restic.env, say): that's a problem, not an ok.
[[ -s $WORK/repos ]] || problem "no repository was drilled (missing ~/server/secrets/restic.env?)"
ok=$([[ ! -s $WORK/problems ]] && echo true || echo false)
mkdir -p "$(dirname "$OUT")"
$JQ -n --slurpfile repos "$WORK/repos" --rawfile problems "$WORK/problems" --argjson ok "$ok" \
  --arg when "$(date '+%Y-%m-%dT%H:%M:%S')" --argjson offsite "$([[ -f ~/server/secrets/restic-offsite.env ]] && echo true || echo false)" \
  '{fecha: $when, ok: $ok, offsite: $offsite, repos: $repos,
    problemas: ($problems | split("\n") | map(select(length > 0)))}' >"$OUT.tmp" && mv "$OUT.tmp" "$OUT"

if [[ $ok == true ]]; then
  echo "$(date '+%F %T') restore drill OK"
else
  echo "$(date '+%F %T') restore drill FAILED"
  curl -fsS -m 10 -H "Title: Restore drill failed" -H "Priority: high" -H "Tags: rotating_light" \
    -d "The weekly test restore found problems: $(paste -sd ';' "$WORK/problems" | sed 's/;/; /g'). See ~/Library/Logs/restore-drill.log" \
    "$NTFY" >/dev/null || true
  exit 1
fi
