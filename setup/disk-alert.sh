#!/bin/bash
# Alerts through the local ntfy as the internal disk fills: 75% is a heads-up, 85% is the line
# backup.sh won't cross with a local repository, 92% means services are about to fail. Each
# level alerts when reached and again once a day while it lasts, not every hour. Runs hourly.
set -uo pipefail
STATE=~/Library/Caches/disk-alert.level   # "<level> <epoch of its last alert>"

used=$(df -P /System/Volumes/Data | awk 'NR==2 {gsub("%","",$5); print $5}')
if   (( used >= 92 )); then level=3 prio=urgent title="Disk almost full"
elif (( used >= 85 )); then level=2 prio=high   title="Disk almost full"
elif (( used >= 75 )); then level=1 prio=default title="Disk filling up"
else level=0
fi

read -r last when 2>/dev/null <"$STATE" || { last=0 when=0; }
now=$(date +%s)
if (( level == 0 || (level <= last && now - when < 86400) )); then
  # Remember a drop, so climbing back up alerts again.
  (( level < last )) && echo "$level $when" >"$STATE"
  exit 0
fi

free=$(df -Pk /System/Volumes/Data | awk 'NR==2 {printf "%d", $4 / 1000000}')
# What usually grows: photos, the local backup (a second copy of them), movies, AI models.
biggest=$(du -sh ~/server/immich/library ~/Backups ~/Media ~/.ollama 2>/dev/null | sort -hr \
  | awk '{sub(ENVIRON["HOME"] "/", "~/", $2); printf "%s%s %s", sep, $2, $1; sep=", "}')
curl -fsS -m 10 -H "Title: $title" -H "Priority: $prio" \
  -d "The Mac's disk is ${used}% full (${free} GB free). Biggest: ${biggest}. A USB disk named ServerBackup moves the backup off it." \
  "http://127.0.0.1:8091/$(cat ~/server/secrets/ntfy-topic)" >/dev/null && echo "$level $now" >"$STATE"
