#!/bin/bash
# HTTPS with a real certificate for the services that need it, tailnet only (no Funnel).
# Each one keeps its usual port: https://mac-server.tailnet-ejemplo.ts.net:<port>
# tailscaled keeps this config across reboots; run again only to rebuild the Mac.
#   bash ~/server/setup/tailscale-serve.sh
set -euo pipefail
eval "$(/opt/homebrew/bin/brew shellenv)"

serve() { tailscale serve --bg --https="$1" "http://127.0.0.1:$2" >/dev/null; }

serve 443   3010   # Homepage
serve 3000  3000   # AdGuard Home (no login: Tailscale is the login)
serve 3011  3011   # Uptime Kuma (no login: Tailscale is the login)
serve 3012  3012   # Open WebUI (HTTPS lets the browser use the microphone; signs you in from Tailscale)
serve 3013  3013   # Finanzas (tailnet only; the app also checks who you are)
serve 3014  3014   # Recordatorios (same; the ntfy buttons call it back from the phone)
serve 3018  3018   # Hoy, the morning on one screen (same: tailnet only, the app checks who you are)
serve 3017  3017   # Papeles, the house's documents (same: tailnet only, the app checks who you are)
serve 3016  3016   # Lista, the shopping list (same: tailnet only, the app checks who you are)
serve 3015  3015   # Pulso, the server's history (same: tailnet only, the app checks who you are)
serve 3019  3019   # Mensajes, the house's Telegram bot (same: tailnet only, the app checks who you are)
serve 3022  3022   # Pedidos, the house's delivery orders (same: tailnet only, the app checks who you are)
serve 3021  3021   # Vigía, the server watching the outside world (same: tailnet only, the app checks who you are)
serve 3020  3020   # Mesa, the long-term paper portfolio Claude decides (Nahuel only: the app checks who you are)
serve 3023  3023   # Soundbox, local AI audio: sound effects, music, voices (no login: Tailscale is the login)
# Its calendar feed again on 443, because iOS won't subscribe to a calendar on another port:
# https://mac-server.tailnet-ejemplo.ts.net/recordatorios/calendario.ics (the prefix is stripped)
tailscale serve --bg --https=443 --set-path /recordatorios http://127.0.0.1:3014 >/dev/null
# The iPhone widget's status (setup/scriptable/mac-server.js), from Caddy: tailscale serve may only
# serve a file itself when run as root.
tailscale serve --bg --https=443 --set-path /widget.json http://127.0.0.1:8092 >/dev/null
serve 5678  5678   # n8n (webhooks and OAuth need HTTPS)
serve 8443  8091   # ntfy (the phone app is subscribed here)
serve 61208 61208  # Glances (no login: Tailscale is the login)
serve 8080  8080   # Dozzle, container logs (Tailscale is the login)
serve 11434 11435  # Ollama API for apps on your devices, through Caddy (Ollama itself is localhost only)

tailscale serve status
