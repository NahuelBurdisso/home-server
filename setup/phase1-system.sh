#!/bin/bash
# Phase 1 (Mac self-hosting plan): the parts that need sudo.
# Run once with:  sudo bash ~/server/setup/phase1-system.sh
# Safe to run again; every step just sets the final state.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash $0" >&2
  exit 1
fi

SERVER_USER="server"
# Fixed IP set on the Mac itself: the ISP account can't add DHCP reservations.
# .200 is outside the router's DHCP pool (192.168.1.2–.99), so nothing else gets it.
STATIC_IP="192.168.1.200"
GATEWAY="192.168.1.1"

step() { printf '\n== %s\n' "$1"; }

step "Network: fixed IP $STATIC_IP on Wi-Fi"
networksetup -setmanual Wi-Fi "$STATIC_IP" 255.255.255.0 "$GATEWAY"
networksetup -setdnsservers Wi-Fi "$GATEWAY" 1.1.1.1

step "Power: never sleep on the charger, wake on network, no Power Nap or standby"
pmset -c sleep 0 disksleep 0 displaysleep 2 womp 1 powernap 0 standby 0 tcpkeepalive 1
# Lets the Mac keep running with the lid closed; the plan still keeps it open.
pmset -a disablesleep 1

step "Firewall: on (signed apps are allowed automatically)"
/usr/libexec/ApplicationFirewall/socketfilterfw --setglobalstate on
/usr/libexec/ApplicationFirewall/socketfilterfw --setallowsigned on
/usr/libexec/ApplicationFirewall/socketfilterfw --setallowsignedapp on

step "Updates: security responses on, automatic macOS installs off"
SU=/Library/Preferences/com.apple.SoftwareUpdate
defaults write "$SU" AutomaticCheckEnabled -bool true
defaults write "$SU" AutomaticDownload -bool true
defaults write "$SU" CriticalUpdateInstall -bool true
defaults write "$SU" ConfigDataInstall -bool true
defaults write "$SU" AutomaticallyInstallMacOSUpdates -bool false

step "Remote Login (SSH)"
launchctl enable system/com.openssh.sshd
launchctl bootstrap system /System/Library/LaunchDaemons/ssh.plist 2>/dev/null || true
install -d -m 700 -o "$SERVER_USER" -g staff "/Users/$SERVER_USER/.ssh"
AUTH="/Users/$SERVER_USER/.ssh/authorized_keys"
[[ -f "$AUTH" ]] || install -m 600 -o "$SERVER_USER" -g staff /dev/null "$AUTH"
HARDEN=/etc/ssh/sshd_config.d/100-server-keys-only.conf
if grep -qE '^(ssh-|ecdsa-)' "$AUTH"; then
  # Only lock out passwords once a key is in place, so you can't lock yourself out.
  cat > "$HARDEN" <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
  launchctl kickstart -k system/com.openssh.sshd
  echo "SSH is keys-only."
else
  echo "SSH is on with passwords for now: no key in $AUTH yet."
  echo "Add your computer's public key there and run this script again to make it keys-only."
fi

step "Wi-Fi watchdog: re-joins Wi-Fi when the router can't be reached"
install -d -m 755 /usr/local/sbin
cat > /usr/local/sbin/wifi-watchdog.sh <<'EOF'
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
EOF
chmod 755 /usr/local/sbin/wifi-watchdog.sh

PLIST=/Library/LaunchDaemons/com.server.wifi-watchdog.plist
cat > "$PLIST" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.server.wifi-watchdog</string>
  <key>ProgramArguments</key>
  <array><string>/usr/local/sbin/wifi-watchdog.sh</string></array>
  <key>StartInterval</key><integer>180</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/var/log/wifi-watchdog.log</string>
  <key>StandardErrorPath</key><string>/var/log/wifi-watchdog.log</string>
</dict>
</plist>
EOF
chown root:wheel "$PLIST"
chmod 644 "$PLIST"
launchctl bootout system "$PLIST" 2>/dev/null || true
launchctl bootstrap system "$PLIST"

step "Done. Current state:"
pmset -g custom | sed -n '/AC Power/,$p'
/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate
launchctl print system/com.server.wifi-watchdog | grep -E 'state|run interval' || true
