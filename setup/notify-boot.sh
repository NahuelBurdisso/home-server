#!/bin/bash
# Sends "Mac server is back" to the local ntfy after a reboot (e.g. a power cut).
# Waits for the ntfy container, since OrbStack starts a bit after login.
T=$(cat ~/server/secrets/ntfy-topic)
# Tries for 10 minutes until ntfy takes the message itself (answering its health check isn't enough).
up=$(sysctl -n kern.boottime | sed -E 's/^\{ sec = ([0-9]+).*/\1/')
for i in $(seq 1 60); do
  if curl -fsS -m 10 -H "Title: Mac server" -H "Tags: electric_plug" \
      -d "Started again at $(date -r "$up" '+%H:%M %d/%m'). Battery: $(pmset -g batt | grep -Eo '[0-9]+%')" \
      "http://127.0.0.1:8091/$T" >/dev/null 2>&1; then
    exit 0
  fi
  sleep 10
done
exit 1
