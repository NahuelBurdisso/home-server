#!/bin/bash
# Second admin step: system services that must run before anyone logs in.
# Run as the server user (NOT with sudo):  bash ~/server/setup/admin-services.sh
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
  echo "Run without sudo: bash $0" >&2
  exit 1
fi
eval "$(/opt/homebrew/bin/brew shellenv)"

step() { printf '\n== %s\n' "$1"; }

step "Admin password (asked once)"
sudo -v

step "AdGuard Home (official build, installed as a system service)"
AGH_DIR=/Applications/AdGuardHome
if [[ ! -x "$AGH_DIR/AdGuardHome" ]]; then
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/agh.zip" https://static.adguard.com/adguardhome/release/AdGuardHome_darwin_arm64.zip
  unzip -q "$tmp/agh.zip" -d "$tmp"
  sudo mv "$tmp/AdGuardHome" "$AGH_DIR"
  rm -rf "$tmp"
fi
sudo "$AGH_DIR/AdGuardHome" -s install || sudo "$AGH_DIR/AdGuardHome" -s start

step "Tailscale as a system service (runs before login)"
sudo brew services start tailscale

step "Firewall: let the server programs accept connections"
FW=/usr/libexec/ApplicationFirewall/socketfilterfw
for bin in \
  "$AGH_DIR/AdGuardHome" \
  "$(readlink -f /opt/homebrew/opt/tailscale/bin/tailscaled)" \
  "$(readlink -f /opt/homebrew/opt/ollama/bin/ollama)" \
  /Applications/Jellyfin.app \
  /Applications/OrbStack.app; do
  if [[ -e "$bin" ]]; then
    sudo "$FW" --add "$bin" >/dev/null
    sudo "$FW" --unblockapp "$bin" >/dev/null
    echo "allowed: $bin"
  fi
done

step "Done. Tell Claude: listo."
