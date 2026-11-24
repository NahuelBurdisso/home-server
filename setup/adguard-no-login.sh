#!/bin/bash
# AdGuard Home without its own login: Tailscale is the login.
# Its web page then listens on this Mac only; you open it at https://mac-server.tailnet-ejemplo.ts.net:3000
# (tailscale serve, tailnet only) or http://adguard.lan, which forwards there.
#   bash ~/server/setup/adguard-no-login.sh   (asks for the Mac password)
set -euo pipefail
CONF=/Applications/AdGuardHome/AdGuardHome.yaml

sudo cp "$CONF" "$CONF.bak-$(date +%Y%m%d%H%M)"
# Web page on localhost only (it was on every network, home Wi-Fi included).
sudo sed -i '' -E 's/^(  address: )0\.0\.0\.0:3000$/\1127.0.0.1:3000/' "$CONF"
# No users = no login.
sudo awk '
  /^users:/ { print "users: []"; skip = 1; next }
  skip && /^[^ -]/ { skip = 0 }
  !skip { print }
' "$CONF" | sudo tee "$CONF.new" >/dev/null
sudo mv "$CONF.new" "$CONF"
sudo chmod 600 "$CONF"

sudo /Applications/AdGuardHome/AdGuardHome -s restart
sleep 3
echo "web:   $(sudo grep -E '^  address:' "$CONF" | head -1)"
echo "users: $(sudo grep -E '^users:' "$CONF")"
curl -s -o /dev/null -w "local page: %{http_code} (200 = no login)\n" http://127.0.0.1:3000/
