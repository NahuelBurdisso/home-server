#!/bin/bash
# Dead man's switch: tells healthchecks.io the Mac is alive every 5 minutes (com.server.heartbeat).
# Every other alert goes out through ntfy on this same Mac, so a frozen Mac, a dead router or a
# stopped ntfy would be silent. When these pings stop (or say /fail), healthchecks.io emails you.
# Also pings /fail when the alert path itself is broken: Docker, ntfy or Tailscale down.
# Needs HC_HEARTBEAT_URL in secrets/healthchecks.env (the check's ping URL); without it, does nothing.
source ~/server/secrets/healthchecks.env 2>/dev/null
URL=${HC_HEARTBEAT_URL:-}
[[ -n $URL ]] || exit 0

broken=()
/usr/local/bin/docker info >/dev/null 2>&1 || broken+=("Docker (OrbStack)")
curl -fs -m 5 -o /dev/null http://127.0.0.1:8091/v1/health || broken+=("ntfy")
[[ $(/opt/homebrew/bin/tailscale status --json 2>/dev/null | /usr/bin/jq -r .BackendState) == Running ]] \
  || broken+=("Tailscale")

if (( ${#broken[@]} )); then
  curl -fsS -m 10 --retry 3 --data-raw "Down: ${broken[*]}. Phone alerts may not arrive." "$URL/fail" >/dev/null
else
  curl -fsS -m 10 --retry 3 "$URL" >/dev/null
fi
