#!/bin/bash
# Quick check of the whole server: run after a reboot or power cut.
#   bash ~/server/setup/healthcheck.sh
eval "$(/opt/homebrew/bin/brew shellenv)"

ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAILS=$((FAILS+1)); }
check() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }
FAILS=0

echo "System"
up=$(sysctl -n kern.boottime | sed -E 's/^\{ sec = ([0-9]+).*/\1/')
echo "        booted $(date -r "$up" '+%d/%m %H:%M'), power: $(pmset -g batt | head -1 | cut -d"'" -f2), battery $(pmset -g batt | grep -Eo '[0-9]+%')"
source ~/server/setup/server.env
check "LAN IP $LAN_IP" "ifconfig en0 | grep -q \"inet $LAN_IP \""
check "Internet" "ping -c 1 -t 3 1.1.1.1"
check "On mains power" "pmset -g batt | grep -q \"'AC Power'\""
check "Never sleeps (SleepDisabled)" "pmset -g | grep -q 'SleepDisabled.*1'"
check "Firewall on" "/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate | grep -q enabled"
check "SSD healthy (SMART passed, no critical warning or media errors)" "/opt/homebrew/bin/smartctl -a disk0 -j | jq -e '.smart_status.passed and .nvme_smart_health_information_log.critical_warning == 0 and .nvme_smart_health_information_log.media_errors == 0'"
check "Disk under 85%" "[ \$(df -P /System/Volumes/Data | awk 'NR==2{gsub(\"%\",\"\",\$5);print \$5}') -lt 85 ]"

echo "Remote access"
check "Tailscale connected" "[ \"\$(tailscale status --json | jq -r .BackendState)\" = Running ]"
check "SSH (22)" "nc -z -G 2 127.0.0.1 22"
check "Screen Sharing (5900)" "nc -z -G 2 127.0.0.1 5900"

check "Tailscale key never expires (else remote access dies some day)" "[ \"\$(tailscale status --json | jq '.Self.KeyExpiry')\" = null ]"
check "Nothing public: no Tailscale Funnel" "[ \"\$(tailscale serve status --json | jq '(.AllowFunnel // {}) | length')\" = 0 ]"
check "tailscale serve has all 15 HTTPS ports" "[ \$(tailscale serve status --json | jq '.TCP // {} | length') -ge 15 ]"

TS_HOST=mac-server.tailnet-ejemplo.ts.net
TS_IP=$(tailscale ip -4 2>/dev/null)
# This Mac doesn't use MagicDNS itself, so point the name at its own Tailscale IP.
https_ok() { curl -fs -m 10 -o /dev/null --resolve "$TS_HOST:$1:$TS_IP" "https://$TS_HOST:$1$2"; }
check "HTTPS Homepage (443)"    "https_ok 443 /"
check "HTTPS AdGuard (3000)"    "https_ok 3000 /"
check "HTTPS Uptime Kuma (3011)" "https_ok 3011 /"
check "HTTPS Open WebUI (3012)" "https_ok 3012 /health"
# An Open WebUI update can drop these; without them the Asistente answers that it has no access to anything.
asistente_ok() {
  local t; t=$(curl -fsS -m 10 -X POST http://127.0.0.1:3012/api/v1/auths/signin -H 'Tailscale-User-Login: nahuelburdisso@github' \
    -H 'Content-Type: application/json' -d '{"email": "", "password": ""}' | jq -r .token) || return 1
  curl -fsS -m 10 -H "Authorization: Bearer $t" 'http://127.0.0.1:3012/api/v1/models/model?id=asistente' |
    jq -e '(.params.num_ctx // 0) >= 16384 and .meta.builtinTools.calendar == false and (.meta.toolIds | length) == 8 and .params.think == false'
}
check "Asistente: 8 server tools, room for them, no thinking (else run setup/open-webui-asistente.sh)" asistente_ok
check "HTTPS Finanzas (3013)"   "https_ok 3013 /health"
check "HTTPS Recordatorios (3014)" "https_ok 3014 /health"
check "HTTPS Pulso (3015)"        "https_ok 3015 /health"
check "HTTPS Lista (3016)"        "https_ok 3016 /health"
check "HTTPS Papeles (3017)"      "https_ok 3017 /health"
check "HTTPS Hoy (3018)"          "https_ok 3018 /health"
check "HTTPS Mensajes (3019)"     "https_ok 3019 /health"
check "HTTPS Mesa (3020)"         "https_ok 3020 /health"
check "HTTPS Vigía (3021)"        "https_ok 3021 /health"
check "HTTPS Pedidos (3022)"      "https_ok 3022 /health"
# Until the bot has its token, only a note: there is nothing to have lost yet.
if [[ -s ~/server/secrets/telegram-token ]]; then
  check "Telegram bot gets messages (Mensajes)" "curl -fs -m 10 http://127.0.0.1:3019/health | jq -e '.telegram == \"ok\"'"
else
  echo "  NOTE  Telegram bot has no token yet: Mensajes (3019) → paste the one from @BotFather"
fi
check "HTTPS Dozzle (8080)"       "https_ok 8080 /healthcheck"
check "HTTPS Recordatorios calendar (443)" "https_ok 443 /recordatorios/calendario.ics"
check "HTTPS n8n (5678)"        "https_ok 5678 /healthz"
check "HTTPS ntfy (8443)"       "https_ok 8443 /v1/health"
check "HTTPS Ollama (11434)"    "https_ok 11434 /api/version"
check "HTTPS iPhone widget (443)" "https_ok 443 /widget.json"
check "HTTPS Glances (61208)"   "https_ok 61208 /api/4/status"
check "HTTPS Soundbox (3023)"   "https_ok 3023 /api/health"

echo "LAN exposure (only 80, 2283 and 8096 should answer on $LAN_IP)"
for p in 2375 3000 3010 3011 3012 3013 3014 3015 3016 3017 3018 3019 3020 3021 3022 3023 5678 8080 8091 8092 11434 11435 61208; do
  check "port $p closed on LAN" "! nc -z -G 2 $LAN_IP $p"
done
# No login on these: Tailscale is the login, so their short names only forward to the tailnet.
for name in graficos pulso logs auto; do
  check "$name.lan goes to Tailscale" "curl -s -o /dev/null -w '%{redirect_url}' -H 'Host: $name.lan' http://127.0.0.1/ | grep -q ts.net"
done

echo "Backups"
check "Last snapshot under 36h old" "(source ~/server/secrets/restic.env; t=\$(restic snapshots --latest 1 --json | jq -r '.[-1].time' | cut -c1-19); [ \$(( \$(date +%s) - \$(date -j -f '%Y-%m-%dT%H:%M:%S' \"\$t\" +%s) )) -lt 129600 ])"
# Every database in sqlite-dumps.sh's DBS (12) plus Jellyfin's: one stale copy is one too many.
check "SQLite copies under 36h old (sqlite-dumps.sh)" "[ \$(find ~/server/sqlite-dumps -name '*.db' -mmin -2160 | wc -l) -ge 13 ]"
check "Restore drill passed in the last 8 days (restore-drill.sh)" "[ -n \"\$(find ~/server/n8n/feed/restore-drill.json -mtime -8)\" ] && [ \"\$(jq -r .ok ~/server/n8n/feed/restore-drill.json)\" = true ]"
[[ -f ~/server/secrets/restic-offsite.env ]] || echo "  NOTE  no offsite copy yet (setup/restic-offsite.env.example)"

echo "Outside alerts (healthchecks.io)"
source ~/server/secrets/healthchecks.env 2>/dev/null
if [[ -n ${HC_HEARTBEAT_URL:-} ]]; then
  check "hc-ping.com reachable" "curl -fs -m 10 -o /dev/null https://hc-ping.com/"
else
  echo "  NOTE  no HC_HEARTBEAT_URL yet: nobody hears it if the Mac dies (heartbeat.sh)"
fi
[[ -n ${HC_BACKUP_URL:-} ]] || echo "  NOTE  no HC_BACKUP_URL yet: a missed night's backup goes unnoticed"

echo "Native services"
check "AdGuard DNS blocks ads" "[ \"\$(dig +short +time=2 +tries=1 @127.0.0.1 doubleclick.net)\" = 0.0.0.0 ]"
check "AdGuard: 2 upstreams and a fallback (adguard-dns.sh)" "[ \"\$(curl -fs -m 5 http://127.0.0.1:3000/control/dns_info | jq '(.upstream_dns | length) >= 2 and (.fallback_dns | length) >= 1')\" = true ]"
check "AdGuard web (3000)"   "curl -fs -m 5 -o /dev/null http://127.0.0.1:3000/"
check "Jellyfin transcodes in hardware (jellyfin-videotoolbox.sh)" "grep -q '<HardwareAccelerationType>videotoolbox<' \"\$HOME/Library/Application Support/jellyfin/config/encoding.xml\""
check "Papeles reads photos (Tesseract with Spanish)" "docker exec papeles tesseract --list-langs 2>&1 | grep -qx spa"
check "Jellyfin watches its folders" "! grep -q '<EnableRealtimeMonitor>false<' \"\$HOME/Library/Application Support/jellyfin/root/default\"/*/options.xml"
check "Jellyfin (8096)"      "curl -fs -m 5 -o /dev/null http://127.0.0.1:8096/health"
check "Ollama (11434)"       "curl -fs -m 5 -o /dev/null http://127.0.0.1:11434/api/version"
check "Glances (61208)"      "curl -fs -m 5 -o /dev/null http://127.0.0.1:61208/api/4/status"
check "Soundbox (3023), on the GPU" "curl -fs -m 5 http://127.0.0.1:3023/api/health | jq -e '.device == \"metal\"'"

echo "Containers"
check "OrbStack / Docker" "docker info"
for c in caddy homepage uptime-kuma ntfy diun open-webui n8n finanzas recordatorios lista papeles hoy mensajes mesa pulso dozzle socket-proxy immich_server immich_postgres immich_machine_learning immich_redis; do
  check "$c" "[ \"\$(docker inspect -f '{{.State.Running}}' $c)\" = true ]"
done

echo "Scheduled jobs"
for j in com.server.backup com.server.notify-boot com.server.disk-alert com.server.power-alert com.server.heartbeat com.server.glances com.server.ollama com.server.soundbox com.server.n8n-feed com.server.restore-drill com.server.daily-check com.server.monthly-update; do
  check "$j" "launchctl print gui/$(id -u)/$j"
done
check "com.server.wifi-watchdog" "launchctl print system/com.server.wifi-watchdog"

echo "Automations (n8n)"
# Every workflow in n8n/workflows, by id: a count would miss one that got unpublished.
active=$(docker exec -e N8N_RUNNERS_BROKER_PORT=5690 -e N8N_PORT=5691 n8n n8n list:workflow --active=true 2>/dev/null)
# Published isn't enough: a trigger whose activation fails (no network or DNS for the 15 s n8n tries,
# 22 and 26/10) stays 'failed' while the workflow still reads as published, and n8n never retries it,
# not even on a restart. Fix: n8n unpublish:workflow, restart, publish:workflow, restart. That's why
# nothing that keeps a connection open lives in n8n any more (the card mail went to Finanzas).
# Queried on a copy of the DB.
n8ndb=$(mktemp -d)
cp ~/server/n8n/data/database.sqlite "$n8ndb/" 2>/dev/null
cp ~/server/n8n/data/database.sqlite-wal "$n8ndb/" 2>/dev/null
failed=$(/usr/bin/sqlite3 "$n8ndb/database.sqlite" "SELECT DISTINCT s.workflowId FROM workflow_publication_trigger_status s
  JOIN workflow_entity w ON w.id = s.workflowId AND w.activeVersionId = s.versionId WHERE s.status = 'failed'" 2>/dev/null) \
  || failed=QUERY-FAILED  # e.g. an n8n update renamed the table: fail loudly rather than pass
rm -rf "$n8ndb"
for f in ~/server/n8n/workflows/*.json; do
  id=$(basename "$f" .json)
  # A retired one (its meta.retired says what replaced it) must stay off, or it would run twice.
  if jq -e '.meta.retired' "$f" >/dev/null 2>&1; then
    check "workflow $id retired, not published" "! grep -q '^$id|' <<<\"\$active\""
    continue
  fi
  check "workflow $id published" "grep -q '^$id|' <<<\"\$active\""
  check "workflow $id triggers running" "! grep -qx -e '$id' -e QUERY-FAILED <<<\"\$failed\""
done
check "Finanzas read the card mail from Gmail < 1 h ago (app/correo.py)" "curl -fs -m 5 http://127.0.0.1:3013/api/correo | jq -e '.on and (.ok_age_s // 1e9) < 3600'"
check "Mac status for n8n < 1 h old" "[ -n \"\$(find ~/server/n8n/feed/estado.json -mmin -60)\" ]"
check "Pulso measured in the last 3 min" "[ \$(curl -fs -m 5 http://127.0.0.1:3015/health | jq .last_sample_age_s) -lt 180 ]"
check "Pulso heard from the Mac < 1 h ago (n8n-feed.sh)" "[ \$(curl -fs -m 5 http://127.0.0.1:3015/health | jq '.mac_report_age_s // 99999') -lt 3600 ]"
check "socket-proxy answers, refuses writes" "docker exec pulso python -c \"import urllib.request as u
u.urlopen('http://socket-proxy:2375/_ping', timeout=5)
try: u.urlopen(u.Request('http://socket-proxy:2375/containers/caddy/restart', method='POST'), timeout=5); exit(1)
except u.HTTPError as e: exit(e.code != 403)\""
check "socket-proxy unreachable from other containers" "! docker exec recordatorios python -c \"import urllib.request as u; u.urlopen('http://host.docker.internal:2375/_ping', timeout=3)\""
check "diun reaches n8n" "docker exec diun wget -q -O /dev/null --post-data='{\"status\":\"check\"}' --header='Content-Type: application/json' http://host.docker.internal:5678/webhook/diun"

echo
if [ "$FAILS" -eq 0 ]; then echo "All good."; else echo "$FAILS check(s) failed."; fi
exit "$FAILS"
