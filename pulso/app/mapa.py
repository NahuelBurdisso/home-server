"""The server as a map: who comes in, through where, to which service, and what each one leans on,
colored by what Pulso measured in the last minutes. Drawn as SVG here, like the graphs.

Keep it in step with the compose files, setup/tailscale-serve.sh and caddy/Caddyfile."""
from markupsafe import Markup, escape

# id: (column, label, detail, status key). A status key is a Kuma monitor (svc.<name>.up),
# a container (ctr.<name>.mem: present = running) or None (not measured: drawn neutral).
NODES = {
    # who
    "tailnet": (0, "Tus dispositivos", "iPhone · Mac (Tailscale)", None),
    "lan": (0, "Wi-Fi de casa", "TV, invitados, la casa", None),
    "afuera": (0, "Internet", "1.1.1.1 · 8.8.8.8", "inet"),
    # through where
    "serve": (1, "tailscale serve", "HTTPS, solo el tailnet", "mac.tailscale"),
    "caddy": (1, "Caddy", ":80 · nombres .lan", "svc.Caddy (nombres cortos).up"),
    "dns": (1, "AdGuard Home", ":53 DNS sin anuncios", "svc.AdGuard Home (DNS).up"),
    # services
    "homepage": (2, "Homepage", "panel", "svc.Homepage.up"),
    "hoy": (2, "Hoy", "la mañana, de todas", "svc.Hoy.up"),
    "immich": (2, "Immich", "fotos", "svc.Immich.up"),
    "jellyfin": (2, "Jellyfin", "pelis y series", "svc.Jellyfin.up"),
    "webui": (2, "Open WebUI", "IA local", "svc.Open WebUI.up"),
    "finanzas": (2, "Finanzas", "plata", "svc.Finanzas.up"),
    "recordatorios": (2, "Recordatorios", "avisos y calendario", "svc.Recordatorios.up"),
    "lista": (2, "Lista", "compras, con Siri", "svc.Lista.up"),
    "papeles": (2, "Papeles", "documentos, con OCR", "svc.Papeles.up"),
    "mensajes": (2, "Mensajes", "bot de Telegram", "svc.Mensajes.up"),
    "mesa": (2, "Mesa", "inversión en papel", "svc.Mesa.up"),
    "vigia": (2, "Vigía", "vigila páginas y guarda links", "svc.Vigía.up"),
    "pedidos": (2, "Pedidos", "pedidos a domicilio", "svc.Pedidos.up"),
    "n8n": (2, "n8n", "automatizaciones", "svc.n8n.up"),
    "kuma": (2, "Uptime Kuma", "chequeos", "ctr.uptime-kuma.mem"),
    "pulso": (2, "Pulso", "esta historia", "svc.Pulso.up"),
    "dozzle": (2, "Dozzle", "logs", "svc.Dozzle.up"),
    "glances": (2, "Glances", "CPU, RAM, disco", "svc.Glances.up"),
    "ntfy": (2, "ntfy", "los avisos de todos", "svc.ntfy.up"),
    # what they lean on
    "ollama": (3, "Ollama", "qwen3 en el Mac", "svc.Ollama.up"),
    "immichdb": (3, "Postgres · Redis · ML", "lo de Immich", "ctr.immich_postgres.mem"),
    "proxy": (3, "socket-proxy", "Docker solo lectura", "svc.socket-proxy.up"),
    "telegram": (3, "Telegram", "trae los mensajes del bot", None),
    "yahoo": (3, "Yahoo Finance", "precios de la mesa", None),
    "push": (3, "ntfy.sh", "despierta al iPhone", None),
    "backup": (3, "restic", "backup nocturno · ~/Backups", "mac.backup"),
    "hc": (3, "healthchecks.io", "avisa si el Mac calla", None),
}

EDGES = [
    ("tailnet", "serve"), ("lan", "caddy"), ("lan", "dns"), ("dns", "afuera"),
    ("serve", "homepage"), ("serve", "hoy"), ("serve", "webui"), ("serve", "finanzas"), ("serve", "recordatorios"), ("serve", "lista"), ("serve", "papeles"),
    ("serve", "mensajes"), ("serve", "mesa"), ("serve", "vigia"), ("serve", "pedidos"),
    ("serve", "n8n"), ("serve", "kuma"), ("serve", "pulso"), ("serve", "dozzle"), ("serve", "glances"),
    ("serve", "ntfy"), ("caddy", "homepage"), ("caddy", "immich"), ("caddy", "jellyfin"),
    ("caddy", "ntfy"),
    ("webui", "ollama"), ("finanzas", "ollama"), ("recordatorios", "ollama"), ("mensajes", "ollama"), ("vigia", "ollama"), ("mensajes", "telegram"), ("mesa", "yahoo"), ("immich", "immichdb"),
    ("pulso", "proxy"), ("dozzle", "proxy"), ("homepage", "proxy"), ("ntfy", "push"),
    ("immich", "backup"), ("finanzas", "backup"), ("recordatorios", "backup"), ("lista", "backup"), ("papeles", "backup"), ("mensajes", "backup"), ("mesa", "backup"), ("vigia", "backup"), ("pedidos", "backup"), ("n8n", "backup"),
]  # only between columns: the services talking to each other (n8n, ntfy) would hide the rest
TITLES = ["quién entra", "por dónde", "servicios", "de qué dependen"]
W, COL_X, BOX_W, BOX_H, GAP, TOP = 1000, [10, 250, 500, 770], 210, 38, 10, 34


def state(key: str | None, cur: dict, mac: dict | None = None) -> str:
    if key is None:
        return "na"
    if key.startswith("mac."):  # from the Mac's own report (main.mac_status), every 30 min
        if not mac:
            return "na"
        if key == "mac.tailscale":
            return "up" if mac["tailscale"] == "Running" else "down"
        return "up" if mac["backup_age_h"] is not None and mac["backup_age_h"] <= 36 else "down"
    if key == "inet":
        if "inet.cf" not in cur:
            return "na"
        return "up" if cur.get("inet.cf") is not None or cur.get("inet.google") is not None else "down"
    if key.startswith("ctr."):
        if "sys.ctr_running" not in cur:  # Docker's API wasn't answering: unknown, not stopped
            return "na"
        return "up" if key in cur else "down"
    if key not in cur:
        return "na"
    return "up" if cur[key] == 1 else "down" if cur[key] == 0 else "na"


def draw(cur: dict, mac: dict | None = None) -> Markup:
    cols: dict[int, list[str]] = {}
    for nid, (col, *_ ) in NODES.items():
        cols.setdefault(col, []).append(nid)
    tallest = max(len(v) for v in cols.values())
    height = TOP + tallest * (BOX_H + GAP)
    pos = {}
    for col, ids in cols.items():
        block = len(ids) * (BOX_H + GAP) - GAP
        y0 = TOP + (height - TOP - block) / 2  # each column centered
        for i, nid in enumerate(ids):
            pos[nid] = (COL_X[col], y0 + i * (BOX_H + GAP))
    states = {nid: state(n[3], cur, mac) for nid, n in NODES.items()}
    out = [f'<svg class="map" viewBox="0 0 {W} {height + 6}" role="img" aria-label="Mapa del servidor">']
    for col, title in enumerate(TITLES):
        out.append(f'<text class="coltitle" x="{COL_X[col]}" y="16">❯ {escape(title)}</text>')
    for a, b in EDGES:
        (ax, ay), (bx, by) = pos[a], pos[b]
        if NODES[a][0] < NODES[b][0]:
            x1, y1, x2, y2 = ax + BOX_W, ay + BOX_H / 2, bx, by + BOX_H / 2
        elif NODES[a][0] > NODES[b][0]:  # AdGuard → internet goes back left
            x1, y1, x2, y2 = ax, ay + BOX_H / 2, bx + BOX_W, by + BOX_H / 2
        else:
            continue
        mid = (x1 + x2) / 2
        bad = "down" in (states[a], states[b])
        out.append(f'<path class="edge{" bad" if bad else ""}" data-a="{a}" data-b="{b}" '
                   f'd="M{x1:.0f},{y1:.0f} C{mid:.0f},{y1:.0f} {mid:.0f},{y2:.0f} {x2:.0f},{y2:.0f}"/>')
    for nid, (col, label, detail, _) in NODES.items():
        x, y = pos[nid]
        st = states[nid]
        tip = {"up": "anda", "down": "CAÍDO", "na": "sin medir"}[st]
        out.append(f'<g class="node {st}" data-id="{nid}"><title>{escape(label)}: {tip}</title>'
                   f'<rect x="{x}" y="{y:.0f}" width="{BOX_W}" height="{BOX_H}" rx="3"/>'
                   f'<circle cx="{x + 14}" cy="{y + BOX_H / 2:.0f}" r="4"/>'
                   f'<text x="{x + 26}" y="{y + 16:.0f}" class="label">{escape(label)}</text>'
                   f'<text x="{x + 26}" y="{y + 30:.0f}" class="detail">{escape(detail)}</text></g>')
    out.append("</svg>")
    return Markup("".join(out))
