#!/bin/bash
# Everything in the self-hosting plan that needs an admin password, in one go.
# Run as the server user (NOT with sudo):  bash ~/server/setup/bootstrap-admin.sh
# It asks for the password once. Safe to run again.
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
  echo "Run without sudo: bash $0 (Homebrew refuses to run as root)" >&2
  exit 1
fi

step() { printf '\n== %s\n' "$1"; }

step "Admin password (asked once)"
sudo -v
# Keep the sudo timestamp fresh until this script ends.
while true; do sudo -n true; sleep 50; kill -0 "$$" 2>/dev/null || exit; done 2>/dev/null &

step "Homebrew"
if [[ ! -x /opt/homebrew/bin/brew ]]; then
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
eval "$(/opt/homebrew/bin/brew shellenv)"
grep -q 'brew shellenv' ~/.zprofile 2>/dev/null || echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
brew update

# AdGuard Home is NOT installed from Homebrew: admin-services.sh installs the official build.
step "Command-line tools: Tailscale, Ollama, restic, Glances"
brew install tailscale ollama restic jq glances smartmontools

step "Apps: OrbStack, Jellyfin, AlDente"
for cask in orbstack jellyfin aldente; do
  brew list --cask "$cask" >/dev/null 2>&1 || brew install --cask "$cask"
done

step "Tailscale as a system service (runs before login)"
sudo brew services start tailscale

step "Firewall: let the server programs accept connections"
FW=/usr/libexec/ApplicationFirewall/socketfilterfw
for bin in \
  /opt/homebrew/opt/tailscale/bin/tailscaled \
  /opt/homebrew/opt/ollama/bin/ollama \
  /Applications/Jellyfin.app \
  /Applications/OrbStack.app; do
  real=$(readlink -f "$bin" 2>/dev/null || echo "$bin")
  if [[ -e "$real" ]]; then
    sudo "$FW" --add "$real" >/dev/null
    sudo "$FW" --unblockapp "$real" >/dev/null
    echo "allowed: $real"
  fi
done

step "Done"
echo "Next: bash ~/server/setup/admin-services.sh (AdGuard Home). Then tell Claude: listo."
