#!/bin/bash
# Alerts through the local ntfy when the Mac loses or gets back mains power, and again
# at 50% and 20% battery while unplugged. Runs every minute (com.server.power-alert).
# What the phone was told is saved only after ntfy accepts each message, so a failed send
# retries next minute; the protective steps below never wait for ntfy (it may be the thing down).
#
# A long power cut ends cleanly instead of with an empty battery: at 15% the containers stop
# (apps first, then Immich's Postgres and Redis, so nothing dies mid-write); ntfy and Caddy stay
# up for the alerts. At 7% the Mac shuts down, if sudo allows "/sbin/shutdown -h now" without a
# password (a sudoers rule the admin adds by hand; without it the alert says so). An Apple silicon
# MacBook turns itself on when power comes back, logs in, and this script (RunAtLoad) starts the
# containers again; if power comes back before 7%, they start again right away.
# Test without touching anything:  POWER_TEST="battery 14" DRY_RUN=1 STATE=/tmp/s bash power-alert.sh
#   (DRY_SEND_RC=1 makes every send "fail", to test that the protective steps still run)
STATE=${STATE:-"$HOME/Library/Application Support/server-power-state"}
TOLD="$STATE.told"   # what the phone knows: "<power> <lowest battery mark alerted>"
DOCKER=/usr/local/bin/docker
STOP_AT=15
SHUTDOWN_AT=7
DATABASES="immich_postgres immich_redis"
KEEP_UP="ntfy caddy"

run() { if [[ -n ${DRY_RUN:-} ]]; then echo "would run: $*"; else "$@"; fi; }
# Docker with a time limit: a hung OrbStack must not freeze this job (launchd won't start another
# one while it runs, and then nothing would shut the Mac down).
limit() { /usr/bin/perl -e 'alarm shift; exec @ARGV' "$@"; }
dk() { local secs=$1; shift; run limit "$secs" "$DOCKER" "$@"; }
send() {
  if [[ -n ${DRY_RUN:-} ]]; then echo "would send: [$1] $4"; return "${DRY_SEND_RC:-0}"; fi
  curl -fsS -m 10 -H "Title: $1" -H "Priority: $2" -H "Tags: $3" -d "$4" \
    "http://127.0.0.1:8091/$(cat ~/server/secrets/ntfy-topic)" >/dev/null
}

if [[ -n ${POWER_TEST:-} ]]; then
  read -r now pct <<<"$POWER_TEST"; left=
else
  batt=$(pmset -g batt)
  pct=$(grep -Eo '[0-9]+%' <<<"$batt" | tr -d %)
  left=$(grep -Eo '[0-9]+:[0-9]+ remaining' <<<"$batt" | cut -d' ' -f1)
  if grep -q "'AC Power'" <<<"$batt"; then now=ac; else now=battery; fi
fi

# level: the protective step reached in this outage (100 none, 15 stopped, 7 shut down).
# Third field: the containers this script stopped (comma separated), to start them again.
read -r _ level stopped 2>/dev/null <"$STATE"
level=${level:-100}
stopped=${stopped:-}
read -r told told_level 2>/dev/null <"$TOLD"
told=${told:-ac}
told_level=${told_level:-100}

save() {
  echo "$now $level $stopped" >"$STATE"
  echo "$told $told_level" >"$TOLD"
}

stop_containers() {
  local apps=() dbs=() name
  for name in $(limit 20 "$DOCKER" ps --format '{{.Names}}'); do  # only reads: runs in DRY_RUN too
    [[ " $KEEP_UP " == *" $name "* ]] && continue
    if [[ " $DATABASES " == *" $name "* ]]; then dbs+=("$name"); else apps+=("$name"); fi
  done
  (( ${#apps[@]} )) && dk 90 stop -t 60 "${apps[@]}" >/dev/null
  (( ${#dbs[@]} )) && dk 150 stop -t 120 "${dbs[@]}" >/dev/null
  local all=("${dbs[@]}" "${apps[@]}")   # databases first, so they're up before the apps
  stopped=$(IFS=,; echo "${all[*]}")
}

if [[ $now == battery ]]; then
  # Telling the phone: each message is retried next minute until ntfy takes it.
  if [[ $told != battery ]] && send "Mac server on battery" high electric_plug,warning \
      "Power is off or the charger came out. Battery ${pct}%${left:+, about $left h left}."; then
    told=battery told_level=100
  fi
  if [[ $told == battery ]]; then
    mark=
    for m in 50 20; do (( pct <= m && told_level > m )) && mark=$m; done   # the lowest one crossed: one message
    if [[ -n $mark ]] && send "Battery at ${pct}%" urgent battery \
        "Still unplugged${left:+, about $left h left}. Plug in the charger or the server will shut down."; then
      told_level=$mark
    fi
  fi
  # Protecting the data: whatever ntfy did.
  if (( pct <= STOP_AT && level > STOP_AT )); then
    stop_containers
    level=$STOP_AT
    save
    send "Server services stopped" urgent stop_sign \
      "Battery at ${pct}%: Immich, n8n, Finanzas and the rest were stopped cleanly so nothing breaks mid-write. They start again by themselves when power is back." || true
  fi
  if (( pct <= SHUTDOWN_AT && level > SHUTDOWN_AT )); then
    level=$SHUTDOWN_AT
    save
    if sudo -n -l /sbin/shutdown -h now >/dev/null 2>&1; then
      send "Mac server shutting down" urgent zzz \
        "Battery at ${pct}%. It turns on again by itself when power comes back." || true
      run sudo -n /sbin/shutdown -h now
    else
      send "Mac server can't shut itself down" urgent warning \
        "Battery at ${pct}% and this user can't shut the Mac down without a password (see setup/power-alert.sh). It will go dark when the battery runs out." || true
    fi
    exit 0
  fi
else
  level=100   # the next outage starts over
  if [[ -n $stopped ]]; then
    # After a shutdown this runs at login, maybe before OrbStack is up: what doesn't start yet is
    # kept for next minute; a container that no longer exists is dropped, not retried forever.
    retry=() started=()
    for name in ${stopped//,/ }; do
      if out=$(dk 60 start "$name" 2>&1); then
        started+=("$name")
      elif [[ $out == *"No such container"* ]]; then
        echo "$name no longer exists: not started"
      else
        retry+=("$name")
      fi
    done
    stopped=$(IFS=,; echo "${retry[*]}")
    if (( ${#started[@]} )) && [[ -z $stopped ]]; then
      send "Server services back" default white_check_mark "Power is back: started ${started[*]} again." || true
    fi
  fi
  if [[ $told == battery ]] && send "Mac server power is back" default electric_plug "Charging, battery ${pct}%."; then
    told=ac told_level=100
  fi
fi

save
