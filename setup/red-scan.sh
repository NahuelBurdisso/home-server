#!/bin/bash
# Who is on the home Wi-Fi, for Pulso's /red. Pulso runs in a container and can't see the home
# network (OrbStack's NAT), so the Mac looks: ping every address of the default route's /24 once,
# in parallel (only to fill the ARP table: a sleeping iPhone answers ARP, not always the ping),
# read the ARP table for that interface and post the devices to Pulso. Every 2 minutes from
# launchd (setup/launchd/com.server.red-scan.plist).
# Quiet on success and never fails loudly: Pulso notices a missing scan by itself.
# `red-scan.sh --print` prints the JSON instead of posting it.
set -uo pipefail
PULSO=http://127.0.0.1:3015/api/red

route=$(/sbin/route -n get default 2>/dev/null) || exit 0
iface=$(awk '/interface:/ {print $2}' <<<"$route")
router=$(awk '/gateway:/ {print $2}' <<<"$route")
ip=$(/usr/sbin/ipconfig getifaddr "$iface" 2>/dev/null) || exit 0
[[ -n $iface && $router =~ ^[0-9.]+$ && $ip =~ ^[0-9.]+$ ]] || exit 0
net=${ip%.*}  # the house is a /24 (192.168.1.0/24)

# One ping to each address, 1 s at most each: fping takes ~1.5 s for the whole /24; without it,
# 64 pings at a time take ~4 s.
if /usr/bin/fping -v >/dev/null 2>&1; then
  /usr/bin/fping -q -r0 -t 1000 -i 1 -g "$net.0/24" >/dev/null 2>&1
else
  seq 1 254 | xargs -P 64 -I{} /sbin/ping -c1 -t1 -q "$net.{}" >/dev/null 2>&1
fi

# `arp -anl` says how fresh each entry is: `192.168.1.17  8a:9a:e:aa:bb:cc  1m37s  1m36s  en0  1`
# (macOS drops the leading zeros). An entry stays listed as "expired" for many minutes after its
# device stopped answering, so only the live ones count: without this a phone that left would
# look home for 20 more minutes.
json=$(/usr/sbin/arp -anl -i "$iface" 2>/dev/null | IFACE="$iface" ROUTER="$router" NET="$net." /usr/bin/python3 -I -c '
import json, os, sys
devices = {}
for line in sys.stdin:
    f = line.split()
    if len(f) < 5 or not f[0].startswith(os.environ["NET"]) or f[4] != os.environ["IFACE"]:
        continue
    ip, parts, expire = f[0], f[1].lower().split(":"), f[2]
    if len(parts) != 6 or expire == "expired":  # "(incomplete)" has no six parts
        continue
    try:
        first = int(parts[0], 16)
    except ValueError:
        continue
    mac = ":".join(p.zfill(2) for p in parts)
    if mac == "ff:ff:ff:ff:ff:ff" or first & 1 or ip.endswith(".255"):  # broadcast, multicast
        continue
    devices[mac] = ip
print(json.dumps({"iface": os.environ["IFACE"], "router": os.environ["ROUTER"],
                  "devices": [{"ip": ip, "mac": mac} for mac, ip in sorted(devices.items(), key=lambda d: tuple(map(int, d[1].split("."))))]}))
') || exit 0

if [[ ${1:-} == --print ]]; then
  echo "$json"
  exit 0
fi
curl -fsS -m 5 -X POST -H 'Content-Type: application/json' --data-binary "$json" "$PULSO" >/dev/null 2>&1 || true
