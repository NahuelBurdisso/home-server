#!/bin/bash
# Pings the router; if three pings fail, turns Wi-Fi off and on.
IFACE=en0
GW=$(route -n get default 2>/dev/null | awk '/gateway/ {print $2}')
GW=${GW:-192.168.1.1}
if ! ping -c 3 -t 10 "$GW" >/dev/null 2>&1; then
  echo "$(date '+%F %T') router $GW unreachable, cycling Wi-Fi"
  networksetup -setairportpower "$IFACE" off
  sleep 5
  networksetup -setairportpower "$IFACE" on
fi
