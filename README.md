# mac-server

Servidor casero en un MacBook Pro M2 (24 GB). Todo lo que corre, cómo se entra, qué avisa y cómo se
recupera. Cada archivo explica su parte en sus comentarios; esto es el mapa. En vivo y coloreado por
estado: **Pulso → mapa** (`https://mac-server.tailnet-ejemplo.ts.net:3015/mapa`).

> **Copia pública** del repo de mi servidor de casa. El nombre del tailnet, las IP, las MAC y los
> negocios son de ejemplo; las claves (`secrets/`, `.env`) y los datos (`*/data/`) no están acá.

## Cómo se entra

| Desde | Por dónde | Qué responde |
|---|---|---|
| iPhone / Mac con Tailscale | `tailscale serve` → `https://mac-server.tailnet-ejemplo.ts.net:<puerto>` (HTTPS real, solo el tailnet) | todo |
| Wi-Fi de casa | Caddy `:80`: `http://192.168.1.200`, o los nombres `.lan` en equipos que usen AdGuard como DNS | Homepage, Immich, Jellyfin, ntfy; el resto redirige al tailnet |
| Wi-Fi de casa | AdGuard Home `:53` | DNS sin anuncios, **solo para quien lo tenga configurado** (ver *Pendientes*) |

En la LAN escuchan 80, 2283 (Immich), 8096 (Jellyfin), 53 y, del sistema, SSH (22) y Compartir
pantalla (5900); todo lo demás está en `127.0.0.1`.
Sin login propio en Kuma, Glances, AdGuard, Dozzle, Soundbox y Open WebUI: **Tailscale es el login**.
Las apps propias (Finanzas, Recordatorios, Pulso, Lista, Papeles, Hoy, Mensajes, Mesa, Vigía, Pedidos) además verifican el usuario
de Tailscale (`*_ALLOWED_LOGINS`).

## Nahuel y Sofía

La casa es de los dos: Finanzas, Recordatorios, Lista, Papeles, Vigía, Pedidos, Hoy, Open WebUI, fotos y pelis son
compartidos. Lo técnico (Pulso, Mesa, la página de Mensajes, Kuma, AdGuard, Dozzle, Glances, n8n, SSH y las
alertas del server) es solo de Nahuel: lo corta la política del tailnet (`setup/tailscale-policy.hujson`),
no cada app. En **Finanzas** cada movimiento es de Nahuel, de Sofía o de los dos: lo del banco, la
tarjeta y Mercado Pago del titular (`FINANZAS_TITULAR`), y lo que se anota a mano, por Siri, por Telegram
o con el Asistente, de quien lo anotó. Se cambia en cada movimiento, y Mes, Gastos y Movimientos se
filtran por persona.

## Servicios

| Servicio | Para qué | Tailnet (`:puerto`) | LAN | Carpeta |
|---|---|---|---|---|
| **Hoy** | la mañana en una pantalla: clima, agenda, lista, plata (y lo que falta pagar del mes), papeles, server | 3018 | — | `hoy/` |
| Homepage | panel con todo | 443 | `panel.lan` | `homepage/` |
| Immich | fotos | — | `fotos.lan` (:2283) | `immich/` |
| Jellyfin | pelis y series (app nativa) | — | `pelis.lan` (:8096) | `~/Media` |
| Open WebUI | IA local + "Asistente del server" | 3012 | `ia.lan` | `open-webui/` |
| Finanzas | plata del mes, gastos, tarjeta, cuánto tenés hoy (`/saldos`), de quién es cada gasto; Mercado Pago entra solo; los pagos fijos de la casa (`/pagos`: luz, gas, agua, municipal, la cuota de la casa) con sus datos para pagarlos y si este mes ya están | 3013 | — | `finanzas/` |
| Recordatorios | avisos al iPhone, calendario; el recordatorio de cada pago del mes (lo crea Finanzas, y su «Hecho» lo da por pagado) | 3014 | — | `recordatorios/` |
| **Papeles** | documentos de la casa: PDF y fotos con OCR, buscables, avisan antes de vencer | 3017 | — | `papeles/` |
| **Lista** | lista de compras por sección del súper, con Siri y el Asistente | 3016 | — | `lista/` |
| **Mensajes** | el bot de Telegram de la casa: lista, gastos, pagos del mes, agenda, papeles y la IA por chat o audio | 3019 | — | `mensajes/` |
| **Mesa** | cartera de largo plazo en acciones y ETF de EE. UU. con plata de mentira, que decide Claude dos veces por semana y se mide contra el S&P 500 | 3020 | — | `mesa/` |
| **Vigía** | vigila páginas de afuera (precios, cambios, turnos) y guarda links para leer después, con resumen | 3021 | — | `vigia/` |
| **Pedidos** | los pedidos a domicilio de la casa (verdulería, carnicería, súper): arma el pedido con la lista y dice precios | 3022 | — | `pedidos/` |
| **Soundbox** | estudio de audio con IA local: efectos de sonido, música, canciones con letra, voces ([HamSanders/soundbox](https://github.com/HamSanders/soundbox)); nativo, para que los modelos corran en la GPU (Metal) | 3023 | — | `soundbox/`, launchd |
| **Pulso** | historia del server: métricas, internet, luz, servicios, alertas, mapa | 3015 | `pulso.lan` | `pulso/` |
| n8n | automatizaciones (workflows en `n8n/workflows`) | 5678 | `auto.lan` | `n8n/` |
| Uptime Kuma | chequeos cada minuto → ntfy | 3011 | `monitor.lan` | `uptime-kuma/` |
| **Dozzle** | logs de cada contenedor (solo lectura) | 8080 | `logs.lan` | `dozzle/` |
| Glances | CPU/RAM/disco en vivo (nativo) | 61208 | `graficos.lan` | launchd |
| ntfy | avisos al iPhone | 8443 | `alertas.lan` | `ntfy/` |
| AdGuard Home | DNS sin anuncios (nativo) | 3000 | `adguard.lan` | `setup/adguard-*.sh` |
| Ollama | modelo local qwen3:8b (nativo) | 11434 | — | launchd |
| socket-proxy | API de Docker **solo lectura**, red interna `dockerapi` | — | — | `socket-proxy/` |
| diun | avisa imágenes nuevas (al resumen del domingo) | — | — | `diun/` |

## Mensajes (`:3019`): el bot de Telegram de la casa

Un bot de Telegram (creado con @BotFather; el token va en `secrets/telegram-token`, o se pega en la
página). Le pregunta a Telegram si hay mensajes (long polling): nada se abre a internet. Cada mensaje
pasa por reglas fijas (`mensajes/app/intents.py`) y va a la app que lo entiende, con sus mismas APIs
de Siri: "comprar manteca" → Lista, "gasté 8500 en la verdulería" → Finanzas (solo efectivo),
"recordame el jueves…" → Recordatorios, una foto o PDF → Papeles (el resumen de la tarjeta → Finanzas),
"buen día", "¿cómo está el server?", "saldos", «armá el pedido» o «pedí la verdura» → Pedidos, y el menú de `/`. Los pagos del mes también: «¿qué me
falta pagar?» (o `/pagos`) contesta lo que queda de este mes, y «pagué la luz», sin monto, lo marca pagado
en Finanzas ("borrar" lo desmarca); con monto ("pagué 18000 de gas") sigue siendo un gasto en efectivo, y
lo que no es un pago de la lista ("pagué el almuerzo") sigue de largo como antes. Los audios se transcriben con Whisper
(el de Open WebUI). Lo que las reglas no reconocen lo contesta qwen3 con las herramientas de todas las
apps. "borrar" deshace lo último. Solo contesta a quien entró con un **link de invitación** (Personas)
o autorizaste ahí; el resto queda anotado y avisa por ntfy una vez. `POST /api/enviar` manda un mensaje
desde n8n o un script.

## Mesa (`:3020`): Claude invierte en papel, contra el S&P

Una cartera de largo plazo en acciones y ETF de Estados Unidos con **plata de mentira** (10.000 USD),
que decide **Claude** y se mide contra **la vara**: los mismos 10.000 USD comprados en SPY el mismo día y
dejados quietos. La pregunta: ¿Claude eligiendo le gana al índice, después de costos? Antes de pensar en
plata de verdad (que no existe acá: ni broker, ni claves). Los precios son velas diarias de Yahoo
Finance, sin cuenta.

Claude decide **los lunes y jueves a las 8:30** (antes de que abra el mercado) con una tarea programada
de la app de escritorio de Claude en el Mac, `mesa-sesion`, que corre la skill `.claude/skills/mesa` y cinco
skills copiadas de `tradermonty/claude-trading-skills` en `mesa/skills/`. La app (no Claude) lleva la
contabilidad y hace cumplir los límites: una decisión que los rompe se rechaza entera. Cada orden queda con
su tesis. El **veredicto** llega a los 12 meses, escrito de antemano en `mesa/DISENO.md` (el diseño
completo, las reglas, los costos y el esquema).

Un solo contenedor, `mesa`. Avisa por ntfy y se mira desde la web (`https://mac-server.tailnet-ejemplo.ts.net:3020`,
en la pantalla de inicio). Por Telegram: «¿cómo va la mesa?», «¿por qué compró MSFT?», «pausá la mesa»,
«reanudá la mesa» (solo a quien recibe los avisos del server). Hoy tiene su tarjeta y Siri su frase
(`/api/frase`); el Asistente de Open WebUI la consulta pero **solo lee**. `mesa.db` va al backup. Es de
Nahuel: Sofía no la ve.

## Vigía (`:3021`): el server mira el mundo de afuera

Una app de la casa (Nahuel y Sofía, como Lista) con dos mitades que comparten base (`vigia.db`) y página.
**Vigilancias**: revisa páginas cada cierto tiempo (mínimo 15 min, normalmente 1 h) y avisa cuando algo
cambia. Hay tres clases: `cambio` (el texto de la página o de una parte cambió, y dice qué se agregó y qué
se sacó), `precio` (baja de precio, el más bajo que vio o por debajo de un objetivo; las subas se anotan
pero no avisan) y `texto` (aparece o desaparece una palabra, por ejemplo "turnos"). La primera revisión
solo toma la base y no avisa, y muestra en el momento qué leyó. Tres fallos seguidos, un solo aviso.
**Guardado**: se le manda un link (o un audio largo por el bot) y guarda el texto, escribe un resumen
con el modelo local y queda buscable; los videos de YouTube se leen por sus subtítulos. Los números y
la decisión de avisar nunca las pone el modelo, solo redacta el resumen. Por Telegram: pegar un link o
«guardá <link>», «seguí el precio de <link>», «avisame si cambia <link>», «avisame cuando aparezca "turnos"
en <link>», «¿qué estoy vigilando?», y un audio de hasta 45 min se transcribe y se guarda. **No puede
leer** páginas que piden login ni las armadas con JavaScript (lo dice al agregarlas), ni videos sin
subtítulos. Nunca llega a direcciones de adentro (el Mac, la casa, el tailnet). `vigia.db` va al backup; el
Asistente de Open WebUI la consulta (`/ia`). El diseño: `vigia/DISENO.md`.

## Pedidos (`:3022`): los pedidos a domicilio

Una app de la casa (Nahuel y Sofía, como Lista y Vigía) que convierte la lista de compras en un pedido por
negocio (`pedidos.db`): para cada renglón busca el producto real del negocio, con su presentación y precio
(estimado: el negocio pesa y cobra), y deja el pedido a un toque de mandarse. **Nunca manda ni paga por sí
sola**: termina en un link de WhatsApp con el texto ya escrito, o en la página del negocio con la lista para
ir tildando. **Verdufresca** (la verdulería, por pedix.app) ya está cargada con precios reales; **Carnicería** y
**Súper** quedan por configurar en `/proveedores` (sin datos igual arma el pedido y dice qué le falta). Si no
puede leer un catálogo usa el último y dice de cuándo es. Aprende lo que la casa elige (qué producto o
presentación) y solo descarga de pedix.app. Por Telegram: «armá el pedido», «pedí la verdura», «pedí la
carne», «pedí al súper», «pedí 2 kg de papas y bananas a la verdulería», «¿cuánto está la banana?», «pedidos»
(o `/pedidos`). `pedidos.db` va al backup; el Asistente de Open WebUI la consulta (`/ia`) y Claude Code la
usa desde este Mac con el skill `.claude/skills/pedidos` (lee, arma y corrige borradores; `/api/p/<id>`).
**Guardados**: cualquier pedido se guarda para repetirlo («lo de siempre»), se edita como un borrador, y
«Usar» lo suma al borrador de ese negocio con los precios del día (`/api/guardados`, `/api/usar`); cada 3 h
relee los catálogos de esos negocios y pone los guardados al precio de hoy. El diseño:
`pedidos/DISENO.md`.

## Soundbox (`:3023`): audio con IA, en el Mac

[Soundbox](https://github.com/HamSanders/soundbox) (MIT) genera efectos de sonido, música, canciones con
letra y voces con modelos abiertos, sin cuenta ni nube. Corre **nativo** (launchd, `com.server.soundbox`),
no en contenedor: así los modelos usan la GPU (Metal). Medido acá: una frase hablada 2 s contra 24 s en
contenedor, y un efecto la mitad de tiempo. Sin login propio: Tailscale es el login, y la política del
tailnet deja el puerto solo para Nahuel. `bash setup/soundbox.sh` lo instala o lo actualiza (cambiando `TAG`);
el código queda en `soundbox/src`, y biblioteca, ajustes, modelos y su motor (audio.cpp) en `soundbox/data`.
El backup lleva la biblioteca y los sonidos; los modelos (16 GB) y el código se bajan de nuevo.

| Para | Modelo | Licencia | Tarda (Metal) |
|---|---|---|---|
| efectos de sonido | Stable Audio 3.0 SFX | Stability Community (libre bajo USD 1M de ingresos) | 5 s → 4 s |
| música instrumental | Stable Audio 3.0 Small Music, y **Medium** (más calidad) | ídem | 20 s → 8 s / 13 s |
| canciones con letra (también en español) | ACE-Step 1.5 Turbo | MIT | 30 s → 50 s |
| voz en español | Kokoro (voces guardadas «Dora» y «Alex», rápida) y VoxCPM2 (más natural) | Apache 2.0 | 10 s → 3 s / 19 s |
| voz en inglés | Chatterbox Turbo | MIT | una frase → 2 s |

El botón de **mejorar el prompt** usa el qwen3 de Ollama (también pasa a inglés un prompt en español, que es
como mejor entienden los modelos). El MP3 sale con el ffmpeg de Jellyfin (`soundbox/bin/ffmpeg` es un link);
Kokoro necesita `espeak-ng` (Homebrew). Clonar voces no quedó: CosyVoice3 falla en esta versión.

## Pulso (`:3015`, `pulso.lan`)

La historia del server, minuto a minuto (14 días) y por hora (para siempre):
**ahora** (estado, tarjetas, panel del Mac, servicios 30 días, eventos) · **gráficos** · **internet**
(latencia, cortes, velocidad cada 6 h, mapa de calor día×hora) · **servicios** (90 días, detalle por
servicio) · **contenedores** (RAM/CPU, caídas) · **eventos** (todo lo que llegó por ntfy, con búsqueda)
· **semana** (contra la anterior) · **mapa** (en vivo) · **red** (quién está en el Wi-Fi: dispositivos conocidos y desconocidos, y quién está en casa) · **guardia** (el informe semanal de los logs) · **iphone** (atajo de Siri: `/api/frase`).
También es herramienta del Asistente de Open WebUI (`/ia`) y alimenta Homepage, el widget del iPhone,
el buen día y el resumen del domingo (`/api/resumen`). El Mac le manda cada 30 min lo que el contenedor no
ve (backup, simulacro, SSD, batería, Wi-Fi, Tailscale): `setup/n8n-feed.sh` → `/api/mac`.
**Red**: `setup/red-scan.sh` (launchd, cada 2 min) mira quién está en la red y se lo manda a Pulso; cada
dispositivo se nombra y se marca de quién es (los teléfonos, para saber quién está en casa). **Guardia**: los
domingos a las 19 h junta los logs de la semana, las reglas encuentran lo raro y el modelo local lo redacta en
tres líneas, con la evidencia al lado. Diseño: `pulso/DISENO-red-guardia.md`.

## Qué avisa y cuándo (ntfy, tema del server)

- **Uptime Kuma**: un servicio caído (prioridad urgente) y cuando vuelve (normal).
- **Mesa**: los movimientos de cada decisión de Claude, el resumen de los viernes y «Claude no vino» si pasan 5 días sin una decisión. Si el contenedor se cae solo lo ve Kuma y `healthcheck.sh`: desde adentro no puede avisar.
- **Vigía**: un cambio, una baja de precio o una palabra que aparece o desaparece en lo que vigila, un resumen terminado (al chat que lo pidió, si no a ntfy) y, una sola vez, "no puedo leer X" si falla tres veces seguidas.
- **Pulso**: un dispositivo nuevo en el Wi-Fi (una vez por aparato; llegadas y salidas de personas solo si se prende en `/red`), el informe de Guardia del domingo a las 19 h; un contenedor que se cae de verdad, se queda sin memoria o deja de responder (las paradas
  planificadas no avisan); el disco que llega al 85% en menos de 45 días a su ritmo.
- **launchd**: corte y vuelta de la luz (`power-alert.sh`), disco 75/85/92% (`disk-alert.sh`), el Mac
  que reinició (`notify-boot.sh`), backup fallido (`backup.sh`), actualización mensual.
- **n8n**: tormenta, granizo, viento fuerte, lluvia intensa, calor extremo o helada en las próximas 12 h
  (cada 2 h, open-meteo); buen día a las 7:30 (clima, agenda, feriados, lista de compras, tarjeta, dólar,
  cómo pasó la noche el server), resumen del
  domingo 20 h, dólar MEP, vencimientos de tarjeta, gastos para revisar.
- **Finanzas**: el resumen de la tarjeta cuando llega el aviso de BBVA o el PDF al Gmail, el resumen del mes,
  el recordatorio de subir el PDF, presupuestos pasados, Mercado Pago o el Gmail que dejan de leerse, y un
  pago del mes que sigue sin pagar 3 días después de su día. Además, con `FINANZAS_PAGOS_AVISOS=1`
  (`finanzas/compose.yml`) le crea a cada pago su recordatorio en Recordatorios (el día antes y ese día, con
  los datos y el link para pagar); tocar «Hecho» ahí lo cuenta como pagado, y un pago que ya figura (un
  movimiento que lo paga, o marcado a mano) cierra su recordatorio solo. Hoy y el buen día dicen lo que falta
  cuando vence en 3 días o ya venció.
- **healthchecks.io** (afuera): mail si el Mac deja de latir 5 min o si falta el backup de la noche.

Todo lo que pasa por ntfy queda guardado para siempre en **Pulso → eventos** (ntfy lo borra a las 72 h).

## En el iPhone (con Tailscale prendido)

Cada app tiene su página "iPhone" con los pasos exactos; esto es el índice.

| Para | Cómo | Dónde están los pasos |
|---|---|---|
| Empezar el día | **Hoy** en la pantalla de inicio (Safari → Compartir → Agregar a inicio) | `:3018` |
| "Oye Siri, buen día" | atajo a Hoy `/api/frase` (clima, agenda, lista, tarjeta, pagos que vencen, server) | el pie de Hoy |
| "Oye Siri, recordame…" / "¿qué tengo?" | atajos a `/api/rapido` y `/api/proximos` | Recordatorios → iPhone |
| "Oye Siri, agregá a la lista" / "¿qué falta?" | atajos a `/api/agregar` y `/api/lista` | Lista → iPhone |
| "Oye Siri, gasté 8500 en la verdulería" | atajo a `/api/gasto` | Finanzas → Atajo |
| Todo lo anterior por Telegram, escrito o en audio | chat con el bot | Mensajes → Ayuda |
| Subir el resumen de la tarjeta | atajo desde Compartir | Finanzas → Atajo |
| Guardar una factura, garantía o receta | atajo "Guardar en Papeles" desde Compartir | Papeles → iPhone |
| "Oye Siri, ¿cómo está el server?" | atajo a `/api/frase` | Pulso → iphone |
| Mandarle un link a Vigía para guardarlo, o vigilar un precio | **Vigía** (Safari → Compartir → Agregar a inicio), o pegarlo en el bot | `:3021` |
| Armar el pedido de la verdulería con lo de la lista | **Pedidos** (Safari → Compartir → Agregar a inicio), o «armá el pedido» en el bot | `:3022` |
| La cartera de Mesa en la pantalla de inicio | **Mesa** (Safari → Compartir → Agregar a inicio) | `:3020` |
| "Oye Siri, ¿cómo va la mesa?" | atajo a `/api/frase` de Mesa | Mesa → Ajustes |
| Preguntarle todo a la IA local, también por voz | Open WebUI, modelo "Asistente del server" | `:3012` |
| Widget en la pantalla de inicio | Scriptable con `setup/scriptable/mac-server.js` (al tocarlo abre Hoy) | el comentario del script |
| Avisos | app ntfy suscrita a `https://mac-server.tailnet-ejemplo.ts.net:8443` | — |

## Trabajos programados (launchd, `setup/launchd/`)

| Job | Cuándo | Qué |
|---|---|---|
| backup | 3:30 | dumps SQLite + restic a `~/Backups` (o el disco USB *ServerBackup*), poda y chequeo |
| restore-drill | domingo 5:00 | restaura el backup a una carpeta temporal y prueba que sirva |
| monthly-update | día 1, 4:30 | actualiza contenedores y Homebrew, corre el healthcheck |
| daily-check | 10:15 | `healthcheck.sh` completo; ntfy solo si algo falla |
| n8n-feed | cada 30 min | estado del Mac → n8n, el widget del iPhone y Pulso |
| heartbeat | cada 5 min | ping a healthchecks.io |
| power-alert | cada minuto | luz / batería |
| disk-alert | cada hora | disco |
| red-scan | cada 2 min | quién está en el Wi-Fi → Pulso (red) |
| wifi-watchdog | cada 3 min | reconecta el Wi-Fi (LaunchDaemon) |
| glances, ollama, soundbox | siempre | |

⚠️ Los horarios son en la zona del Mac, que hoy está en **America/Los_Angeles** (los contenedores usan
Córdoba): el backup de "3:30" corre a las 7:30/8:30 de Argentina. Ver *Pendientes*.

## Scripts útiles

```bash
bash ~/server/setup/healthcheck.sh       # chequeo completo (después de un corte o una actualización)
bash ~/server/setup/backup.sh            # backup ahora
bash ~/server/setup/restore-drill.sh     # simulacro de restauración ahora
bash ~/server/setup/tailscale-serve.sh   # rearma los puertos HTTPS del tailnet
bash ~/server/setup/adguard-dns.sh       # upstreams del DNS con failover
bash ~/server/setup/jellyfin-videotoolbox.sh  # Jellyfin transcodifica con el chip (no la CPU)
bash ~/server/setup/open-webui-asistente.sh   # herramientas del Asistente (Finanzas, Recordatorios, Pulso, Lista, Papeles, Mesa, Vigía, Pedidos)
bash ~/server/setup/acceso-sofia.sh <login>   # Sofía a las apps de la casa (y la política del tailnet)
```

Tests de las apps propias: `docker compose run --rm <app> python -m pytest -q` en `finanzas/`,
`recordatorios/`, `pulso/`, `lista/`, `papeles/`, `hoy/`, `mensajes/`, `mesa/`, `vigia/` o `pedidos/`.

## Reglas que conviene no olvidar

- Finanzas baja sola los movimientos de Mercado Pago (cada mañana, los del día anterior) con el Access
  Token de producción de `secrets/mercadopago-token`. Si lo regenerás en Mercado Pago Developers,
  cambiá el archivo y `docker compose restart finanzas`; si deja de andar, avisa por ntfy a los 2 días.
- El aviso de BBVA "Ya podés descargar tu resumen" llega a Hotmail **sin PDF**: una regla de Hotmail lo
  reenvía a Gmail y **Finanzas lee ese Gmail sola, cada 10 minutos** (`app/correo.py`; la dirección y una
  contraseña de aplicación de Google, una por línea, en `secrets/gmail-imap`). Quedan el vencimiento y el
  saldo (Saldos, Recordatorios) y avisa al celular; si te mandás el PDF a ese Gmail, lo carga también. Cada
  vuelta es una conexión nueva que sigue desde el último mail leído: un corte de internet o de DNS no la
  mata, y al volver recupera lo que llegó. Abre el buzón en solo lectura. Si no puede leer por 6 h, o llega
  un mail de BBVA sobre el resumen que no entiende, avisa. Estado: `curl localhost:3013/api/correo`; una
  vuelta ya: `curl -X POST localhost:3013/api/correo`. Antes lo hacía n8n (`finMailTarjeta01`, retirado):
  su trigger IMAP quedaba muerto para siempre con cada corte de más de un minuto (22 y 26/10).

- No abrir con `sqlite3` desde el Mac la base de un contenedor en marcha (WAL sobre el montaje de
  OrbStack): usar la API de la app, `docker exec` o una copia.
- Los workflows de n8n se editan en el JSON **y se importan** (`n8n import:workflow`, luego
  `publish:workflow` y reiniciar n8n): el export nocturno pisa el JSON con lo que hay en la base.
- `socket-proxy` crea la red `dockerapi`: si se baja con `docker compose down`, levantarlo antes que
  Homepage, Pulso, Dozzle y Kuma.

## Pendientes (necesitan una decisión o una cuenta)

- **Zona horaria del Mac**: `sudo systemsetup -settimezone America/Argentina/Cordoba`.
- **Copia fuera de casa**: `secrets/restic-offsite.env` (Backblaze B2, Cloudflare R2 o un Storage Box;
  ver `setup/restic-offsite.env.example`). Hoy el backup vive en el mismo disco que los originales.
- **AdGuard no le llega a la casa**: el router reparte su propio DNS, así que hoy solo lo usan los
  dispositivos con Tailscale (el iPhone). Para que la TV y el resto tengan bloqueo de anuncios y los
  nombres `.lan`: en el router, DNS = `192.168.1.200`; si el Nokia no deja, apagar su DHCP y prender el
  de AdGuard (Settings → DHCP), o poner el DNS a mano en cada equipo.
- **Sofía**: invitarla al tailnet (Users → Invite users) con el mail de la cuenta con la que va a entrar,
  correr `bash ~/server/setup/acceso-sofia.sh <ese login>` y pegar la política que imprime **antes de que
  acepte** (hoy cualquier miembro llega a todo el Mac). El script dice el resto (Open WebUI, Telegram, ntfy).
- **Kuma, monitor de Mesa**: Kuma (`:3011`) → Add New Monitor → HTTP(s), nombre `Mesa`, URL `http://host.docker.internal:3020/health`, intervalo 60 s, notificación ntfy como los otros (Pulso lo muestra en el mapa por ese nombre).
- **Kuma, monitor de Vigía**: Kuma (`:3011`) → Add New Monitor → HTTP(s), nombre `Vigía`, URL `http://host.docker.internal:3021/health`, intervalo 60 s, notificación ntfy como los otros (Pulso lo muestra en el mapa por ese nombre).
- **Kuma, monitor de Pedidos**: Kuma (`:3011`) → Add New Monitor → HTTP(s), nombre `Pedidos`, URL `http://host.docker.internal:3022/health`, intervalo 60 s, notificación ntfy como los otros (Pulso lo muestra en el mapa por ese nombre).
- **Dispositivos de la casa**: nombrar los dispositivos de la casa en Pulso → red (y marcar los teléfonos de cada uno).
- **Bot de Telegram**: crearlo con @BotFather y pegar el token en Mensajes (`:3019`).
- **Widgets de Immich y Jellyfin en Homepage**: necesitan una API key de cada uno.
