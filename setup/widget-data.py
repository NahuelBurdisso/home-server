#!/usr/bin/python3 -I
"""What the iPhone widget shows (setup/scriptable/mac-server.js), as one small JSON file:
the server's light (green, yellow, red, and why), battery, backups, the internet connection
(Pulso), last month's money and what's coming up. n8n-feed.sh runs it every 30 minutes after writing estado.json; tailscale
serve publishes the file, tailnet only, at https://mac-server.tailnet-ejemplo.ts.net/widget.json.
    python3 widget-data.py <estado.json> <widget.json>"""
import json
import os
import sys
import urllib.request
from datetime import datetime


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.load(resp)
    except Exception:
        return None


def hours_since(stamp):
    try:
        return round((datetime.now() - datetime.fromisoformat(stamp)).total_seconds() / 3600)
    except (TypeError, ValueError):
        return None


src, dst = sys.argv[1:3]
with open(src) as f:
    st = json.load(f)
s, c, drill = st.get("system", {}), st.get("containers", {}), st.get("simulacro") or {}
backup_h = hours_since((st.get("backup") or {}).get("last"))
drill_h = hours_since(drill.get("fecha"))

bad, warn = [], []
if c.get("down"):
    bad.append("caídos: " + ", ".join(c["down"]))
if not s.get("internet"):
    bad.append("sin internet")
if backup_h is None or backup_h > 36:
    bad.append("backup viejo")
if drill and not drill.get("ok"):
    bad.append("falló el simulacro")
if not s.get("on_ac"):
    warn.append(f"a batería {s.get('battery')}%")
if (s.get("disk_used_pct") or 0) >= 85:
    warn.append(f"disco al {s['disk_used_pct']}%")
if drill_h is None or drill_h > 8 * 24:
    warn.append("sin simulacro reciente")
if drill and not drill.get("offsite"):
    warn.append("sin copia fuera de casa")

# Pulso (pulso/) sees what this file can't: a service Uptime Kuma finds down while its container
# runs, and how the internet did over the day.
pulso = get("http://127.0.0.1:3015/api/resumen") or {}
bad += [a for a in pulso.get("avisos", []) if a.startswith("caído:")]
if not pulso:
    warn.append("Pulso no responde")

fin = get("http://127.0.0.1:3013/api/estado") or {}
rec = get("http://127.0.0.1:3014/ia/proximos?dias=7") or {}
month = fin.get("mes_anterior") or {}
due = (fin.get("vencimientos") or [None])[0]
out = {
    "hora": datetime.now().strftime("%H:%M"),
    "luz": "rojo" if bad else "amarillo" if warn else "verde",
    "avisos": bad + warn,
    "bateria": s.get("battery"), "enchufado": s.get("on_ac"), "disco_libre_gb": s.get("disk_free_gb"),
    "contenedores": f"{c.get('running', 0)}/{c.get('total', 0)}",
    "backup_hace_h": backup_h,
    "simulacro": "ok" if drill.get("ok") else "falló" if drill else None,
    "internet": {"ms": pulso.get("internet_ms"), "uptime_24h": pulso.get("internet_24h"),
                 "cortes_24h": pulso.get("internet_cortes_24h"), "min_caido_24h": pulso.get("internet_min_caido_24h")}
    if pulso else None,
    "finanzas": {
        "mes": (month.get("nombre") or "").split(" ")[0], "quedo": month.get("quedo"),
        "negativo": (month.get("quedo_centavos") or 0) < 0,
        "para_revisar": fin.get("para_revisar"), "mep": fin.get("mep"),
        "vence": f"{due['cuenta']} {due['vence'][8:10]}/{due['vence'][5:7]}" + (f" {due['total']}" if due.get("total") else "")
        if due else None,
    } if fin else None,
    "proximos": [{"cuando": r["cuando"], "titulo": r["titulo"]} for r in rec.get("recordatorios", [])[:3]],
}
with open(dst + ".tmp", "w") as f:
    json.dump(out, f, ensure_ascii=False)
os.replace(dst + ".tmp", dst)
