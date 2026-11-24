#!/bin/bash
# "Preguntale al server": Open WebUI gets Finanzas, Recordatorios, Pulso, Lista, Papeles, Mesa, Vigía and Pedidos as tools (their /ia/openapi.json,
# see each app's app/asistente.py) and a model "Asistente del server" (qwen3 with those tools on and
# a prompt that makes it read the numbers instead of making them up). Everything stays on this Mac.
# Open WebUI keeps this in its database, so run again only after rebuilding it:
#   bash ~/server/setup/open-webui-asistente.sh
set -euo pipefail
WEBUI=http://127.0.0.1:3012
OWNER=nahuelburdisso@github   # the Tailscale login Open WebUI signs in by (its admin)
JQ=/usr/bin/jq

# Signs in the way tailscale serve does, with the trusted login header (only answers on this Mac).
TOKEN=$(curl -fsS -m 20 -X POST "$WEBUI/api/v1/auths/signin" -H "Tailscale-User-Login: $OWNER" \
  -H 'Content-Type: application/json' -d '{"email": "", "password": ""}' | $JQ -r .token)
api() { curl -fsS -m 30 -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' "$@"; }

# The apps answer Open WebUI's backend directly (no proxy headers), like n8n calls them.
api -X POST "$WEBUI/api/v1/configs/tool_servers" -d @- >/dev/null <<'JSON'
{"TOOL_SERVER_CONNECTIONS": [
  {"type": "openapi", "url": "http://host.docker.internal:3013/ia", "path": "openapi.json", "auth_type": "none",
   "key": "", "config": {"enable": true},
   "info": {"id": "finanzas", "name": "Finanzas", "description": "Ingresos, gastos por categoría, movimientos y gastos en efectivo"}},
  {"type": "openapi", "url": "http://host.docker.internal:3014/ia", "path": "openapi.json", "auth_type": "none",
   "key": "", "config": {"enable": true},
   "info": {"id": "recordatorios", "name": "Recordatorios", "description": "Eventos, cumpleaños y vencimientos que avisan al iPhone"}},
  {"type": "openapi", "url": "http://host.docker.internal:3015/ia", "path": "openapi.json", "auth_type": "none",
   "key": "", "config": {"enable": true},
   "info": {"id": "pulso", "name": "Pulso", "description": "Historia del servidor: internet, cortes de luz, servicios, contenedores y alertas"}},
  {"type": "openapi", "url": "http://host.docker.internal:3016/ia", "path": "openapi.json", "auth_type": "none",
   "key": "", "config": {"enable": true},
   "info": {"id": "lista", "name": "Lista", "description": "La lista de compras de la casa"}},
  {"type": "openapi", "url": "http://host.docker.internal:3017/ia", "path": "openapi.json", "auth_type": "none",
   "key": "", "config": {"enable": true},
   "info": {"id": "papeles", "name": "Papeles", "description": "Los documentos de la casa: facturas, garantías, contratos"}},
  {"type": "openapi", "url": "http://host.docker.internal:3020/ia", "path": "openapi.json", "auth_type": "none",
   "key": "", "config": {"enable": true},
   "info": {"id": "mesa", "name": "Mesa", "description": "La cartera de largo plazo en papel que decide Claude: estado, posiciones, decisiones y comparación con el S&P (solo lectura)"}},
  {"type": "openapi", "url": "http://host.docker.internal:3021/ia", "path": "openapi.json", "auth_type": "none",
   "key": "", "config": {"enable": true},
   "info": {"id": "vigia", "name": "Vigía", "description": "Lo que el server vigila afuera (precios, páginas, turnos) y lo que se guardó para leer después"}},
  {"type": "openapi", "url": "http://host.docker.internal:3022/ia", "path": "openapi.json", "auth_type": "none",
   "key": "", "config": {"enable": true},
   "info": {"id": "pedidos", "name": "Pedidos", "description": "Los pedidos a domicilio de la casa (verdulería, carnicería, súper): arma el pedido con la lista y dice precios"}}
]}
JSON
echo "Tool servers: $(api "$WEBUI/api/v1/configs/tool_servers" | $JQ -r '[.TOOL_SERVER_CONNECTIONS[].info.name] | join(", ")')"

model=$($JQ -n '{
  id: "asistente", base_model_id: "qwen3:8b", name: "Asistente del server", is_active: true,
  meta: {
    description: "Tus finanzas, recordatorios, la lista de compras, lo que vigila afuera, la cartera en papel (Mesa) y el estado del server, con la IA local. Nada sale del Mac.",
    profile_image_url: "/static/favicon.png",
    toolIds: ["server:finanzas", "server:recordatorios", "server:pulso", "server:lista", "server:papeles", "server:mesa", "server:vigia", "server:pedidos"],
    capabilities: {vision: false, citations: false, usage: false},
    # Open WebUI 0.12 adds its own tools (calendar, tasks, automations, notes...) to every chat from the
    # web page. With them the request outgrew the context of qwen3: Ollama cut the start (this prompt and the
    # server tools) and the model answered in English that it had no access. Ours do all of that here.
    builtinTools: (["automations", "calendar", "channels", "chats", "code_interpreter", "files",
      "image_generation", "knowledge", "memory", "notes", "notifications", "skills", "subagents",
      "tasks", "time", "user_input", "web_search"] | map({key: ., value: false}) | from_entries),
    suggestion_prompts: [
      {content: "¿Cuánto me quedó el mes pasado y en qué se fue la plata?"},
      {content: "¿Cuánto gasté en comida los últimos meses?"},
      {content: "¿Qué tengo esta semana?"},
      {content: "¿Cómo anduvo internet esta semana? ¿Se cortó la luz?"},
      {content: "¿Cómo va la mesa contra el S&P?"},
      {content: "¿Qué falta comprar?"},
      {content: "Armá el pedido de la verdulería con la lista"},
      {content: "¿Qué estás vigilando? ¿Qué guardé sobre bicicletas?"},
      {content: "¿Dónde está la garantía de la heladera?"},
      {content: "Recordame el viernes a las 10 llamar al contador"}
    ]
  },
  params: {
    function_calling: "native", temperature: 0.2,
    # No thinking, like every other app that calls qwen3: it only has to pick a tool and read its answer back.
    # Thinking took a question from ~25 s to 40-150 s on this M2 before the first word.
    think: false,
    # Stays loaded half an hour after a question, like Mensajes asks for: once Ollama unloads it (5 min by
    # default) the next question pays the load and the whole prompt again.
    keep_alive: "30m",
    num_ctx: 16384,  # the tools (~3k tokens) plus a few answers from them (Ollama gives 4096)
    # Nothing that changes within the day goes in this prompt (no {{CURRENT_TIME}}): the tools come after it
    # in what Ollama reads, so a new minute made it read all ~3.9k tokens again (~25 s) on every question.
    system: "Sos el asistente del servidor casero de {{USER_NAME}}, en Córdoba, Argentina. Hoy es {{CURRENT_WEEKDAY}} {{CURRENT_DATE}}.\nRespondé en español rioplatense, corto y directo.\n- Plata (ingresos, gastos, categorías, comercios, cuánto quedó, el dólar): usá las herramientas de Finanzas. Para comparar meses o ver una tendencia, gastos_por_categoria con el rango de meses.\n- Fechas, eventos, cumpleaños y vencimientos: usá las herramientas de Recordatorios.\n- El servidor (si anda todo, internet, cortes de luz, servicios caídos, contenedores, alertas): usá las herramientas de Pulso.\n- La mesa (la cartera de largo plazo en papel que decide Claude, contra el S&P 500: cómo va, qué tiene, por qué compró o vendió, si le gana a la vara): herramientas de Mesa. Solo se puede mirar; si piden pausarla o decidir una compra, decí que eso se hace desde la página de la mesa o con «pausá la mesa» por Telegram.\n- La lista de compras: herramientas de Lista. Para agregar o marcar como comprado, pasá la frase de la persona tal cual.\n- Lo que se vigila afuera (precios, páginas, turnos) y los links guardados: herramientas de Vigía.\n- Pedidos a domicilio (verdulería, carnicería, súper) y cuánto está un producto: herramientas de Pedidos. armar_pedido solo deja un borrador: pasá el link para revisarlo y mandarlo, nunca digas que ya se pidió.\n- Documentos (garantías, facturas, contratos, recetas, pólizas): buscalos con las herramientas de Papeles y pasá el link.\n- Para anotar un gasto en efectivo, llamá anotar_gasto con la frase de la persona tal cual. Para agendar algo, llamá agendar_recordatorio con la frase tal cual, con el cuándo incluido. No calcules fechas vos.\n- Usá solo los números y fechas que devuelven las herramientas: ya vienen sumados, no los recalcules ni inventes. Si falta un dato (por ejemplo faltan_datos_de), decilo."
  }
}')
if api "$WEBUI/api/v1/models/model?id=asistente" >/dev/null 2>&1; then
  api -X POST "$WEBUI/api/v1/models/model/update?id=asistente" -d "$model" >/dev/null
else
  api -X POST "$WEBUI/api/v1/models/create" -d "$model" >/dev/null
fi
echo "Model: $(api "$WEBUI/api/v1/models/model?id=asistente" | $JQ -r '.name + " (" + (.meta.toolIds | join(", ")) + ")"')"

# New chats open with the Asistente, pinned first; the plain model stays one click away.
api -X POST "$WEBUI/api/v1/configs/models" -d '{"DEFAULT_MODELS": "asistente", "DEFAULT_PINNED_MODELS": "asistente",
  "MODEL_ORDER_LIST": ["asistente", "qwen3:8b"], "DEFAULT_MODEL_METADATA": {}, "DEFAULT_MODEL_PARAMS": {}}' >/dev/null
echo "Default model: $(api "$WEBUI/api/v1/configs/models" | $JQ -r .DEFAULT_MODELS)"

# Speaking to it: the local Whisper ("small" understands Spanish far better than the default "base";
# ~460 MB, downloaded once on first use). The voice that answers is the phone's own.
cfg=$(api "$WEBUI/api/v1/audio/config" | $JQ '.stt.WHISPER_MODEL = "small"')
api -m 900 -X POST "$WEBUI/api/v1/audio/config/update" -d "$cfg" >/dev/null  # the first time it downloads the model
echo "Voice: whisper $(api "$WEBUI/api/v1/audio/config" | $JQ -r .stt.WHISPER_MODEL)"
