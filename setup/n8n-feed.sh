#!/bin/bash
# The Mac's status for n8n's weekly summary and Pulso: the desktop widget's JSON plus Uptime Kuma's
# outages of the last 7 days, the last restore drill (restore-drill.sh), the Wi-Fi link and pending
# macOS updates. n8n runs in Docker and can't look at the Mac, so it reads this file
# (n8n/feed, mounted read-only). Every 30 minutes from launchd.
set -euo pipefail
FEED=~/server/n8n/feed
mkdir -p "$FEED"
# Queries a copy: opening Kuma's live SQLite (WAL) from the Mac, across OrbStack's bind mount,
# corrupts the container's shared memory and gives it "disk I/O error"s.
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# Without Kuma's database (being replaced, moved) the rest of the feed still goes out.
cp ~/server/uptime-kuma/data/kuma.db "$tmp/kuma.db" 2>/dev/null || : >"$tmp/kuma.db"
cp ~/server/uptime-kuma/data/kuma.db-wal "$tmp/kuma.db-wal" 2>/dev/null || true
outages=$(/usr/bin/sqlite3 -json "$tmp/kuma.db" \
  "SELECT m.name AS servicio, COUNT(*) AS caidas FROM heartbeat h JOIN monitor m ON m.id = h.monitor_id
   WHERE h.status = 0 AND h.important = 1 AND h.time > datetime('now', '-7 days')
   GROUP BY m.name ORDER BY caidas DESC" || true)
# The Wi-Fi link (this Mac's only way out): signal, noise and rate. system_profiler takes ~3 s.
wifi=$(/usr/sbin/system_profiler SPAirPortDataType -json 2>/dev/null | /usr/bin/python3 -I -c '
import json, re, sys
try:
    ifaces = json.load(sys.stdin)["SPAirPortDataType"][0]["spairport_airport_interfaces"]
except (ValueError, KeyError, IndexError):
    sys.exit()
for i in ifaces:
    cur = i.get("spairport_current_network_information") or {}
    m = re.match(r"(-?\d+) dBm / (-?\d+) dBm", cur.get("spairport_signal_noise", ""))
    if m:
        print(json.dumps({"signal_dbm": int(m[1]), "noise_dbm": int(m[2]), "rate_mbps": cur.get("spairport_network_rate"),
                          "channel": cur.get("spairport_network_channel"), "phy": cur.get("spairport_network_phymode")}))
        break
' || true)
# The battery is the server's UPS: its health (capacity left of new, cycles) says how long it lasts.
battery=$(/usr/sbin/system_profiler SPPowerDataType -json 2>/dev/null | /usr/bin/python3 -I -c '
import json, sys
try:
    items = json.load(sys.stdin)["SPPowerDataType"]
except (ValueError, KeyError):
    sys.exit()
for i in items:
    h = i.get("sppower_battery_health_info")
    if h:
        print(json.dumps({"cycles": h.get("sppower_battery_cycle_count"), "condition": h.get("sppower_battery_health"),
                          "max_capacity_pct": int(str(h.get("sppower_battery_health_maximum_capacity", "0")).rstrip("%") or 0)}))
' || true)
# The internal SSD (the only disk: the data and the local backup both live on it): SMART health,
# wear and what has been written to it.
ssd=$(/opt/homebrew/bin/smartctl -a disk0 -j 2>/dev/null | /usr/bin/python3 -I -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit()
log = d.get("nvme_smart_health_information_log") or {}
if log:
    print(json.dumps({"ok": (d.get("smart_status") or {}).get("passed"), "used_pct": log.get("percentage_used"),
                      "spare_pct": log.get("available_spare"), "critical": log.get("critical_warning"),
                      "media_errors": log.get("media_errors"), "temp_c": (d.get("temperature") or {}).get("current"),
                      "written_tb": round(log.get("data_units_written", 0) * 512000 / 1e12, 2)}))
' || true)
# macOS updates waiting (Software Update's own count from its last background check).
updates=$(defaults read /Library/Preferences/com.apple.SoftwareUpdate LastRecommendedUpdatesAvailable 2>/dev/null || echo 0)
/usr/bin/python3 -I ~/server/setup/desktop-status.py | OUTAGES="${outages:-[]}" WIFI="$wifi" BATTERY="$battery" SSD="$ssd" UPDATES="$updates" /usr/bin/python3 -I -c '
import json, os, sys
status = json.load(sys.stdin)
status.pop("services", None)
status["caidas_7d"] = json.loads(os.environ["OUTAGES"] or "[]")
try:
    with open(os.path.expanduser("~/server/n8n/feed/restore-drill.json")) as f:
        status["simulacro"] = json.load(f)
except (OSError, ValueError):
    status["simulacro"] = None
status["wifi"] = json.loads(os.environ.get("WIFI") or "null")
status["macos_updates"] = int(os.environ.get("UPDATES") or 0)
status["bateria"] = json.loads(os.environ.get("BATTERY") or "null")
status["ssd"] = json.loads(os.environ.get("SSD") or "null")
json.dump(status, sys.stdout, ensure_ascii=False)
' > "$FEED/estado.json.tmp"
mv "$FEED/estado.json.tmp" "$FEED/estado.json"
# Pulso keeps it too (backup age, restore drill, Tailscale, Wi-Fi): its container can't see the Mac.
curl -fsS -m 10 -X POST -H 'Content-Type: application/json' --data-binary @"$FEED/estado.json" \
  http://127.0.0.1:3015/api/mac >/dev/null || echo "Pulso didn't take the Mac's status"
# The iPhone widget's file, from the same status (setup/widget-data.py).
/usr/bin/python3 -I ~/server/setup/widget-data.py "$FEED/estado.json" "$FEED/widget.json"
