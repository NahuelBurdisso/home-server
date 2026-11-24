#!/bin/bash
# Consistent copies of every SQLite database to ~/server/sqlite-dumps, for backup.sh. Copying a
# live WAL database file by file (what restic does) can catch the .db and its -wal at different
# moments, so the restore may not open. VACUUM INTO writes one self-contained snapshot instead.
# Run inside each container: the Mac can't open their databases safely (OrbStack's mount breaks
# WAL's shared memory). Each copy is checked before it replaces the previous one, so a failure
# keeps the last good copy. Writes excludes.txt (the live files, for restic) and prints the
# names that failed; exits 1 if any did.
# To restore one: restic restore it, stop the container, delete its database and the -wal/-shm
# next to it, copy the dump in under the database's name, start the container.
set -uo pipefail
DOCKER=/usr/local/bin/docker
OUT=~/server/sqlite-dumps
mkdir -p "$OUT"

# name                container      data dir inside             data dir on the Mac         database         tool
DBS=(
  "finanzas           finanzas       /data                       finanzas/data               finanzas.db      python"
  "recordatorios      recordatorios  /data                       recordatorios/data          recordatorios.db python"
  "pulso              pulso          /data                       pulso/data                  pulso.db         python"
  "lista              lista          /data                       lista/data                  lista.db         python"
  "papeles            papeles        /data                       papeles/data                papeles.db       python"
  "mensajes           mensajes       /data                       mensajes/data               mensajes.db      python"
  "vigia              vigia          /data                       vigia/data                  vigia.db         python"
  "pedidos            pedidos        /data                       pedidos/data                pedidos.db       python"
  "mesa               mesa           /data                       mesa/data                   mesa.db          python"
  "open-webui         open-webui     /app/backend/data           open-webui/data             webui.db         python"
  "open-webui-vectors open-webui     /app/backend/data/vector_db open-webui/data/vector_db   chroma.sqlite3   python"
  "n8n                n8n            /home/node/.n8n             n8n/data                    database.sqlite  node"
  "uptime-kuma        uptime-kuma    /app/data                   uptime-kuma/data            kuma.db          cli"
)
JELLYFIN_DB="$HOME/Library/Application Support/jellyfin/data/jellyfin.db"
TMP=.sqlite-dump.tmp

# Checks a fresh copy without letting SQLite create -wal/-shm next to it, then moves it into place.
keep() {
  [[ $(/usr/bin/sqlite3 "file:$1?immutable=1" "PRAGMA quick_check" 2>&1) == ok ]] && mv -f "$1" "$OUT/$2.db"
}

failed=()
: >"$OUT/excludes.txt"
for row in "${DBS[@]}"; do
  read -r name container inside host db tool <<<"$row"
  # The live file and its companions only: "$db*" also matched rollback copies
  # (like the old Mesa's mesa.db.antes-fase5…), which then never reached the backup.
  for suffix in "" -wal -shm -journal; do echo "$HOME/server/$host/$db$suffix" >>"$OUT/excludes.txt"; done
  rm -f ~/server/"$host/$TMP"
  case $tool in
    python) cmd=(python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("VACUUM INTO ?", (sys.argv[2],)); c.close()'
                 "$inside/$db" "$inside/$TMP") ;;
    node)   cmd=(node -e 'const d=new (require("node:sqlite").DatabaseSync)(process.argv[1]); d.prepare("VACUUM INTO ?").run(process.argv[2]); d.close()'
                 "$inside/$db" "$inside/$TMP") ;;
    cli)    cmd=(sqlite3 "$inside/$db" "VACUUM INTO '$inside/$TMP'") ;;
  esac
  if ! { $DOCKER exec "$container" "${cmd[@]}" >/dev/null 2>&1 && keep ~/server/"$host/$TMP" "$name"; }; then
    failed+=("$name")
  fi
  rm -f ~/server/"$host/$TMP"
done

# Jellyfin runs on the Mac itself, so its database is copied from here.
echo "$JELLYFIN_DB*" >>"$OUT/excludes.txt"
rm -f "$OUT/$TMP"
/usr/bin/sqlite3 "$JELLYFIN_DB" "VACUUM INTO '$OUT/$TMP'" 2>/dev/null && keep "$OUT/$TMP" jellyfin || failed+=(jellyfin)
rm -f "$OUT/$TMP"

# So does Soundbox (launchd, not a container): its library of generated sounds.
SOUNDBOX_DB="$HOME/server/soundbox/data/soundbox.db"
for suffix in "" -wal -shm -journal; do echo "$SOUNDBOX_DB$suffix" >>"$OUT/excludes.txt"; done
/usr/bin/sqlite3 "$SOUNDBOX_DB" "VACUUM INTO '$OUT/$TMP'" 2>/dev/null && keep "$OUT/$TMP" soundbox || failed+=(soundbox)
rm -f "$OUT/$TMP"

if (( ${#failed[@]} )); then echo "${failed[*]}"; exit 1; fi
