#!/bin/bash
# The full health check (healthcheck.sh) once a day, and an ntfy alert only when something fails.
# Uptime Kuma watches the web pages every minute, but not what this checks: ports open to the
# Wi-Fi, tailscale serve's HTTPS, backups and their restore drill, launchd jobs, n8n workflows
# that got unpublished, AdGuard's upstreams... Those can break quietly; now they're found within
# a day. Runs at 10:15 (com.server.daily-check). By hand:  bash ~/server/setup/daily-check.sh
set -uo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
NTFY="http://127.0.0.1:8091/$(cat ~/server/secrets/ntfy-topic)"
OUT=$(mktemp)
trap 'rm -f "$OUT"' EXIT

# At most 15 minutes: a wedged OrbStack makes `docker info`/`docker exec` hang forever, and then
# launchd would never start another day's check (power-alert.sh guards docker the same way).
/usr/bin/perl -e 'alarm 900; exec @ARGV' bash ~/server/setup/healthcheck.sh >"$OUT" 2>&1
if (( $? == 142 )); then  # 128 + SIGALRM
  echo "$(date '+%F %T') daily check: timed out"
  curl -fsS -m 10 -H "Title: Chequeo diario colgado" -H "Priority: 4" -H "Tags: warning" \
    -d "healthcheck.sh no terminó en 15 minutos (¿OrbStack trabado?). Probá: docker info" "$NTFY" >/dev/null || echo "ntfy failed"
fi
fails=$(grep -c 'FAIL' "$OUT")
echo "$(date '+%F %T') daily check: $fails failed"
if (( fails )); then
  list=$(grep 'FAIL' "$OUT" | sed -E 's/.*FAIL[^ ]* *//' | head -12)
  curl -fsS -m 10 -H "Title: Chequeo diario: $fails falla$( ((fails > 1)) && echo s)" -H "Priority: 4" -H "Tags: warning" \
    -d "$list
Detalle: bash ~/server/setup/healthcheck.sh" "$NTFY" >/dev/null || echo "ntfy failed"
fi
