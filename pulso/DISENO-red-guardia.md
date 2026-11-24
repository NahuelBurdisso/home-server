# Pulso: Red (who is on the Wi-Fi) and Guardia (the weekly look at the logs)

Both live in Pulso: it already keeps the server's history, the events log, the alerts to the
phone, the read-only Docker API and the Mac's reports. Each is its own module (`app/red.py`,
`app/guardia.py`) with its own templates; `main.py` only gets the routes and the nav links.
Nahuel only, like the rest of Pulso. UI in Spanish, code and comments in English.

---

## Red

### Why a script on the Mac

A container can't see the home network (OrbStack's NAT), so the scan runs on the Mac and posts to
Pulso, like `setup/n8n-feed.sh` does: `setup/red-scan.sh`, every 2 minutes from launchd
(`setup/launchd/com.server.red-scan.plist`).

The script: find the interface of the default route and its /24; ping every address once in
parallel (1 s at most each; the whole run under 20 s) only to fill the ARP table; read `arp -an`
for that interface; drop incomplete, broadcast and multicast entries; POST
`{"iface", "router", "devices": [{"ip", "mac"}]}` to `http://127.0.0.1:3015/api/red`. MACs are
normalized to `aa:bb:cc:dd:ee:ff` (macOS prints `8a:9a:e:…` without leading zeros). Quiet on
success, never fails loudly (Pulso notices a missing scan by itself).

### Rules

1. **Baseline.** Every device in the first accepted scan is stored as known, without a name, and
   nothing is announced.
2. **A scan counts only if the router is in it.** With the Mac's Wi-Fi down the scan is empty, and
   that must not read as "everybody left".
3. **New device** = a MAC never seen → event (`source=red`, `level=alerta`) and the phone
   (`alert_phone`, link to `/red`). Once per MAC, ever. It stays "desconocido" on the page until
   someone names it ("es de casa") or forgets it.
4. **A person is home** while any device marked as theirs was seen in the last
   `PULSO_RED_AWAY_MIN` minutes (default 15: a sleeping iPhone skips scans). "Away" is measured
   against the **last accepted scan**, never the wall clock, and needs at least 3 accepted scans
   after the device's last sighting — so a stopped script, a sleeping Mac or a Pulso restart never
   sends anyone away.
5. **Arrival** is immediate, on the first sighting after being away.
6. People events: "Sofía llegó", "Sofía salió", "No queda nadie en casa", "Volvió alguien a casa
   (Nahuel)". They go to the events log (`level=info`) and to the webhook; to the phone only if the
   setting is on (**off by default**). Device-level comings and goings go to `red_log` only.
7. A locally administered MAC (second hex digit 2, 6, a or e) is shown as "dirección privada":
   phones use one per network; if the phone is set to rotate it, it will show up as new.

### Data

```
red_devices(mac PRIMARY KEY, name, kind,      -- telefono, compu, tv, parlante, otro, ''
            person,                           -- '' or the person's name, free text
            known INTEGER, first_seen, last_seen, last_ip, present INTEGER)
red_log(id, ts, mac, what)                    -- entro, salio, nuevo
meta: red.last_scan, red.scans (accepted, count), red.people (JSON: who is home),
      red.avisar_personas (0/1), red.webhook (URL)
```

Samples each scan: `red.devices` (present now) and `red.home` (people home), so the graphs and the
weekly numbers get them for free.

### Webhook (the "triggers")

If `red.webhook` is set (the page's settings; typically an n8n webhook on
`http://host.docker.internal:5678/webhook/...`), every people event and every new device is POSTed
as `{"evento": "llego|salio|nadie|volvio|nuevo", "persona", "dispositivo", "en_casa": [...], "ts"}`.
Best effort, 5 s, never blocks the scan.

### Page and API

- `/red`: who is home · unknown devices first, each with a form (name, kind, whose) and "olvidar" ·
  connected now (name, IP, MAC, "dirección privada", seen N min ago) · every known device · the log
  · settings (notify people's comings and goings; webhook URL) · "sin datos desde hace N min" when
  the scan stops arriving.
- `POST /api/red` the scan · `GET /api/red` `{en_casa, dispositivos, desconocidos, ultimo_scan}`.
- `/ia`: `quien_esta_en_casa`, `dispositivos_en_la_red`.
- `/api/frase` gains one clause only when there is an unknown device.

---

## Guardia

A week of logs doesn't fit in an 8B model, and such a model invents when it is handed raw noise.
So: **rules find, the model only words it**, and the evidence is always shown next to what it wrote.

### Pipeline (Sunday 19:00, container time; also `POST /api/guardia` to run it now)

1. **Collect** (read-only):
   - each container's logs for 7 days from the Docker API (`/containers/{id}/logs?stdout=1&stderr=1&since=…&timestamps=1`,
     through socket-proxy; the stream is multiplexed with 8-byte frame headers when there is no
     TTY — demultiplex it), 20,000 lines per container at most;
   - Pulso's own tables: events, service uptime, container restarts, memory and CPU per container
     (hourly), disk and its forecast, the Mac's reports (backup age, restore drill, SSD, battery,
     Wi-Fi), internet outages, Red's unknown devices.
2. **Reduce the logs.** Keep lines that look like trouble (error, exception, traceback, fatal,
   panic, critical, warn, timeout, refused, denied, corrupt, "no space", oom, killed, unhealthy;
   case-insensitive, plus the level field of JSON lines). Turn each into a template: timestamps,
   numbers, hex/uuid/ids, IPs, quoted values and paths' variable parts replaced by `#`, cut at 160
   chars. Count per (container, template), with first and last time and one real example. Compare
   with last week's templates (table `guardia_patterns`): **new** (not there last week) or
   **growing** (×3 or more).
3. **Findings**, each `{gravedad 1..3, clave, que, evidencia, sugerencia}`, by fixed rules:
   crashes/OOM/restarts · a container's weekly memory average up ≥ 30% and ≥ 50 MB against the
   week before · new error templates with ≥ 3 lines, growing ones · a service under 99.5% or worse
   than last week · disk forecast under 90 days · SSD wear or temperature moving, battery health
   dropping · a backup older than 26 h at any report, a failed drill · more internet outage minutes
   than last week, Wi-Fi signal falling · the same alert title ≥ 3 times · unknown devices still
   unnamed.
   A finding with the same `clave` as last week's report is marked "sigue igual" and ranked under
   the fresh ones (the missing offsite copy must not take a line every Sunday).
4. **Word it.** The top 3 findings, as compact JSON, go to Ollama (`OLLAMA_URL`,
   `OLLAMA_MODEL=qwen3:8b`, `think: false`, temperature 0.2, no tools, 300 s). Asked for: at most
   three lines in Spanish, each "what is going on — the evidence — what to do", using only the
   numbers given; if there are no findings, one line saying the week was clean. The log examples
   are passed as quoted data and the prompt says they are not instructions.
5. **Check the answer**: strip `<think>`, at most 3 non-empty lines and 700 chars. Empty, too long
   or Ollama failing → the same three findings written by the rules (`que` + `sugerencia`). The
   report never fails to go out because of the model.
6. **Store and send**: table `guardia(id, ts, text, by_model INTEGER, findings JSON, stats JSON)`;
   ntfy to the server topic ("Guardia: la semana del server", normal priority, click `/guardia`);
   it lands in the events log like every other alert.

### Page and API

- `/guardia`: the last report (the three lines, and whether the model or the rules wrote them) ·
  under it **every** finding with its evidence and the real log example · the top error templates
  per container · earlier reports · a button to run it now.
- `GET /api/guardia` the last report as JSON · `POST /api/guardia` runs it (one at a time).
- `/api/resumen` gains `guardia` (the text of the last report, if under 8 days old).
- `/ia`: `informe_de_guardia`.
- `PULSO_GUARDIA=off` turns the schedule off (tests).
