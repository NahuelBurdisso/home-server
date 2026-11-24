#!/bin/bash
# Jellyfin transcodes on the M2's media engine (VideoToolbox) instead of the CPU: it was "none".
# Decodes H.264, HEVC and VP9 in hardware (what VideoToolbox can; VC-1 and MPEG-2 stay on the CPU)
# and encodes H.264 there too. Jellyfin rewrites its config when it quits, so it's edited while
# Jellyfin is closed, then reopened. Run again only after reinstalling Jellyfin:
#   bash ~/server/setup/jellyfin-videotoolbox.sh
set -euo pipefail
CONF="$HOME/Library/Application Support/jellyfin/config/encoding.xml"

cp "$CONF" "$CONF.bak-$(date +%Y%m%d%H%M)"
osascript -e 'quit app "Jellyfin"' 2>/dev/null || true
for _ in $(seq 30); do pgrep -x jellyfin >/dev/null || break; sleep 1; done
pgrep -x jellyfin >/dev/null && { echo "Jellyfin didn't quit; nothing changed" >&2; exit 1; }

/usr/bin/python3 -I - "$CONF" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p).read()
s = re.sub(r"<HardwareAccelerationType>.*?</HardwareAccelerationType>",
           "<HardwareAccelerationType>videotoolbox</HardwareAccelerationType>", s)
s = re.sub(r"<HardwareDecodingCodecs>.*?</HardwareDecodingCodecs>|<HardwareDecodingCodecs\s*/>",
           "<HardwareDecodingCodecs>\n    <string>h264</string>\n    <string>hevc</string>\n    <string>vp9</string>\n"
           "  </HardwareDecodingCodecs>", s, flags=re.S)
s = re.sub(r"<EnableHardwareEncoding>.*?</EnableHardwareEncoding>", "<EnableHardwareEncoding>true</EnableHardwareEncoding>", s)
open(p, "w").write(s)
PY
open -a Jellyfin
for _ in $(seq 60); do curl -fs -m 2 -o /dev/null http://127.0.0.1:8096/health && break; sleep 1; done
grep -E "HardwareAccelerationType" "$CONF"
