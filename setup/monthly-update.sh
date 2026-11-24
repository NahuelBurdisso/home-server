#!/bin/bash
# Monthly maintenance: update containers (within their pinned versions) and Homebrew
# apps, clean up old images, run the health check, and report the result via ntfy.
# Runs on the 1st of each month at 4:30 (after the nightly backup). Run by hand with:
#   bash ~/server/setup/monthly-update.sh
set -uo pipefail
eval "$(/opt/homebrew/bin/brew shellenv)"
export PATH="/usr/local/bin:$PATH"   # docker (OrbStack)

NTFY="http://127.0.0.1:8091/$(cat ~/server/secrets/ntfy-topic)"
LOG=~/Library/Logs/monthly-update.log
exec > >(tee -a "$LOG") 2>&1
echo "== $(date '+%F %T') monthly update"

updated=()
failed_tests=()

# Commit any config change first, so this update can be rolled back.
git -C ~/server add -A && git -C ~/server commit -qm "Before monthly update $(date +%F)" || true

for dir in ~/server/*/; do
  [[ -f "$dir/compose.yml" ]] || continue
  name=$(basename "$dir")
  before=$(cd "$dir" && docker compose images -q 2>/dev/null | sort)
  # Built-here images (finanzas, recordatorios, pulso) are rebuilt on a fresh base image instead of
  # pulled, and their tests run on it before it replaces the running app: a broken rebuild leaves the
  # old container running (until it restarts) and says so.
  if ! (cd "$dir" && docker compose pull -q --ignore-buildable && docker compose build -q --pull); then
    echo "WARN: $name failed"; continue
  fi
  if [[ -f "$dir/Dockerfile" && -d "$dir/tests" ]] && ! (cd "$dir" && docker compose run --rm -T \
       -e PULSO_COLLECT=off -e RECORDATORIOS_ALERTS=off -e FINANZAS_NOTIFY=0 "$name" python -m pytest -q >/dev/null 2>&1); then
    echo "WARN: $name tests failed on the new image; not restarted"
    failed_tests+=("$name"); continue
  fi
  (cd "$dir" && docker compose up -d --remove-orphans) || echo "WARN: $name failed"
  after=$(cd "$dir" && docker compose images -q 2>/dev/null | sort)
  [[ "$before" != "$after" ]] && updated+=("$name")
done
docker image prune -f >/dev/null
# Each rebuild of finanzas/recordatorios leaves layers in the build cache; a month's worth is enough.
docker builder prune -f --filter until=720h >/dev/null
# n8n compacts its database on start (DB_SQLITE_VACUUM_ON_STARTUP), even in a month with no new image.
docker restart n8n >/dev/null || echo "WARN: n8n restart failed"

# Homebrew: CLI tools and apps. Tailscale's daemon runs as root, so it's left for a manual upgrade.
brew update -q
outdated=$(brew outdated --quiet | grep -vx tailscale || true)
if [[ -n "$outdated" ]]; then
  brew upgrade $outdated && updated+=($outdated)
  launchctl kickstart -k "gui/$(id -u)/com.server.ollama" 2>/dev/null || true
  launchctl kickstart -k "gui/$(id -u)/com.server.glances" 2>/dev/null || true
  launchctl kickstart -k "gui/$(id -u)/com.server.soundbox" 2>/dev/null || true
  # A cask upgrade swaps the .app but the old Jellyfin keeps running until restarted.
  if grep -qx jellyfin <<<"$outdated"; then
    osascript -e 'quit app "Jellyfin"' 2>/dev/null || pkill -x jellyfin || true
    sleep 10
    open -a Jellyfin
  fi
fi

sleep 60
if bash ~/server/setup/healthcheck.sh > /tmp/healthcheck.out 2>&1; then
  status="all 30+ checks OK"; prio=default
else
  status="$(grep -c FAIL /tmp/healthcheck.out) check(s) FAILED: $(grep FAIL /tmp/healthcheck.out | sed -E 's/.*FAIL[^ ]* *//' | tr '\n' ',' )"; prio=high
fi

msg="Updated: ${updated[*]:-nothing}. Health: $status"
(( ${#failed_tests[@]} )) && { msg="$msg. Tests FAILED on the new image of: ${failed_tests[*]} (still running the old one: look before restarting them)."; prio=high; }
[[ -n "$(brew outdated --quiet | grep -x tailscale)" ]] && msg="$msg. Tailscale has an update: run 'sudo brew upgrade tailscale'."
echo "$msg"
curl -fsS -m 10 -H "Title: Monthly update" -H "Priority: $prio" -d "$msg" "$NTFY" >/dev/null || true
