#!/bin/bash
# Sofía into the house's apps. Her login is the email you invite her with (Users → Invite users), if
# she signs in with that same account; else, the one the admin console shows for her after she joined.
# Run it and paste the policy BEFORE she accepts: the old one let any member reach the whole Mac.
# Adds it to Finanzas, Recordatorios, Lista, Papeles, Vigía and Hoy (*_ALLOWED_LOGINS), tells Finanzas that
# what she writes down is hers (FINANZAS_PERSONAS), recreates those containers and prints the tailnet
# policy to paste (setup/tailscale-policy.hujson: she reaches only those apps). Running it again
# changes nothing.
#   bash ~/server/setup/acceso-sofia.sh sofia@gmail.com
set -euo pipefail
LOGIN=${1:?"Uso: acceso-sofia.sh <login de Tailscale de Sofía>"}
[[ $LOGIN =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$ ]] || { echo "No parece un login de Tailscale: $LOGIN" >&2; exit 1; }
cd "$(dirname "$0")/.."

add() {  # add <file> <variable> <value>: appends ",value" to that line unless it's already there
  local line; line=$(grep -E "^ *$2: " "$1")
  if ! grep -qiF -- "$3" <<<"$line"; then
    sed -i '' -E "s|^( *$2: .*)$|\1,$3|" "$1"
  fi
}

for app in finanzas recordatorios lista papeles vigia pedidos hoy; do
  add "$app/compose.yml" "$(tr '[:lower:]' '[:upper:]' <<<"$app")_ALLOWED_LOGINS" "$LOGIN"
done
add finanzas/compose.yml FINANZAS_PERSONAS "$LOGIN=sofia"
grep -hE "_ALLOWED_LOGINS|FINANZAS_PERSONAS" {finanzas,recordatorios,lista,papeles,vigia,pedidos,hoy}/compose.yml | sed 's/^ */  /'

for app in finanzas recordatorios lista papeles vigia pedidos hoy; do
  docker compose -f "$app/compose.yml" up -d --wait >/dev/null
done
echo "Apps reiniciadas."

cat <<EOF

Falta, a mano:
1. Pegá esta política en https://login.tailscale.com/admin/acls/file (reemplaza la que hay), antes de que
   Sofía acepte la invitación:

$(sed "s/SOFIA_LOGIN/$LOGIN/g" setup/tailscale-policy.hujson)

2. La primera vez que Sofía abra Open WebUI (:3012) queda pendiente: aprobala en Panel de admin → Usuarios.
3. Telegram: en Mensajes (:3019) → Personas, creá una invitación y mandásela.
4. Avisos de Recordatorios: en su app ntfy, servidor https://mac-server.tailnet-ejemplo.ts.net:8443 y el tema
   de secrets/ntfy-topic-recordatorios (el de secrets/ntfy-topic es el del server: ese no).
EOF
