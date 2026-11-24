"""Pulso: the server's history. Glances and Uptime Kuma show "now"; this keeps every minute
(CPU, memory, disk, network, each container, each service, the internet connection, the power)
and every alert, so you can see what happened last night, last week or since the disk started
filling up."""
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import asistente, charts, collect, guardia, mapa, red, store
from .charts import chart, fmt
from .db import connect, get_meta, set_meta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
HERE = Path(__file__).parent
ALLOWED = {s.strip().lower() for s in os.environ.get("PULSO_ALLOWED_LOGINS", "").split(",") if s.strip()}

# name -> (span, step) in seconds. Steps of whole hours read the hourly rows.
RANGES = {
    "1h": (3600, 60),
    "6h": (6 * 3600, 60),
    "24h": (86400, 300),
    "7d": (7 * 86400, 1800),
    "30d": (30 * 86400, 3 * 3600),
    "1a": (365 * 86400, 86400),
}
GREEN, CYAN, AMBER, VIOLET, RED = "var(--green)", "var(--cyan)", "var(--amber)", "var(--violet)", "var(--red)"
MAC_RAM = 24e9  # only for the "x of 24 GB" line


@asynccontextmanager
async def lifespan(app):
    if os.environ.get("PULSO_COLLECT", "on") != "off":
        collect.start()
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.mount("/ia", asistente.ia)  # tools for the local AI in Open WebUI (app/asistente.py)
templates = Jinja2Templates(directory=HERE / "templates")


def when(ts: int, with_day: bool = False) -> str:
    d = datetime.fromtimestamp(ts)
    today = datetime.now().date()
    if not with_day and d.date() == today:
        return d.strftime("%H:%M")
    return f"{charts.DAYS[d.weekday()]} {d.day}/{d.month} {d.strftime('%H:%M')}"


def minutes(m: int) -> str:
    if m < 60:
        return f"{m} min"
    if m < 1440:
        return f"{m // 60} h {m % 60:02d} min"
    return f"{m // 1440} d {m % 1440 // 60} h"


templates.env.globals.update(fmt=fmt, when=when, minutes=minutes, RANGES=RANGES, DAYS=charts.DAYS,
                             TZ=os.environ.get("TZ", ""))


SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
OWN_HOSTS = {"mac-server.tailnet-ejemplo.ts.net", "localhost", "127.0.0.1"}


def same_site(request: Request) -> bool:
    """A form or script on another web site can make the browser POST here (CSRF): browsers say so
    in Sec-Fetch-Site and Origin. Siri, n8n and curl send neither, and are let through."""
    site = request.headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "same-site", "none"):
        return False
    origin = request.headers.get("origin")
    if origin and origin != "null":
        return urlparse(origin).hostname in OWN_HOSTS | {request.url.hostname}
    return True


@app.middleware("http")
async def tailnet_owner_only(request: Request, call_next):
    """tailscale serve tells us who is asking (Tailscale-User-Login). Only the people in
    PULSO_ALLOWED_LOGINS get in; direct requests on this Mac (no proxy) are allowed too."""
    if request.method not in SAFE_METHODS and not same_site(request):
        return JSONResponse({"error": "Pedido desde otro sitio: rechazado."}, status_code=403)
    if request.url.path != "/health":
        login = request.headers.get("tailscale-user-login", "").lower()
        proxied = "x-forwarded-for" in request.headers
        if (login and ALLOWED and login not in ALLOWED) or (proxied and not login):
            return JSONResponse({"error": "Solo para el dueño del tailnet."}, status_code=403)
    return await call_next(request)


def render(request: Request, template: str, **ctx):
    return templates.TemplateResponse(request, template, {"page": template.split(".")[0], **ctx})


def window(r: str) -> tuple[str, int, int, int]:
    r = r if r in RANGES else "24h"
    span, step = RANGES[r]
    if step >= 86400:  # whole Córdoba days (UTC midnight is 21:00 here)
        end = store.day_start(time.time()) + 86400
    else:
        end = (int(time.time()) // step + 1) * step
    return r, end - span, end, step


def status(conn, now: float) -> dict:
    """The light: red when something is down right now, yellow for what needs a look."""
    cur = store.latest(conn)
    bad, warn = [], []
    down = sorted(k[4:-3] for k, v in cur.items() if k.startswith("svc.") and k.endswith(".up") and v == 0)
    if down:
        bad.append("caído: " + ", ".join(down))
    if "inet.cf" in cur and cur.get("inet.cf") is None and cur.get("inet.google") is None:
        bad.append("sin internet" + (" (ni el router responde)" if cur.get("lan.router") is None else ""))
    if not cur:
        bad.append("no hay datos de los últimos 5 minutos")
    if cur.get("bat.ac") == 0:
        warn.append(f"a batería ({fmt(cur.get('bat.pct'), '%')})")
    if (cur.get("sys.disk") or 0) >= 85:
        warn.append(f"disco al {fmt(cur['sys.disk'], '%')}")
    if (cur.get("sys.mem") or 0) >= 92 and (cur.get("sys.swap") or 0) >= 50:
        warn.append("poca memoria libre")
    mac = mac_info(conn)
    if mac:
        if mac["backup_age_h"] is None or mac["backup_age_h"] > 36:
            bad.append("backup viejo" if mac["backup_age_h"] else "sin backup")
        if mac["drill_ok"] is False:
            bad.append("falló el simulacro de restauración")
        elif mac["drill_age_h"] is not None and mac["drill_age_h"] > 8 * 24:  # weekly: one missed
            warn.append("el simulacro de restauración no corre hace más de una semana")
        if mac["tailscale"] != "Running":
            bad.append("Tailscale caído")
        ssd = mac.get("ssd") or {}
        if ssd and (ssd.get("ok") is False or ssd.get("critical") or ssd.get("media_errors")):
            bad.append("el SSD reporta errores (SMART)")
        elif (ssd.get("used_pct") or 0) >= 80:
            warn.append(f"SSD gastado al {ssd['used_pct']}%")
    elif get_meta(conn, "mac"):
        warn.append("el Mac no reporta hace más de 2 h (n8n-feed)")
    recent = store.events(conn, int(now) - 3600, int(now) + 60, ("alerta",), limit=5)
    if recent and not bad:
        warn.append(f"{len(recent)} alerta{'s' if len(recent) > 1 else ''} en la última hora")
    light = "rojo" if bad else "amarillo" if warn else "verde"
    return {"light": light, "notes": bad + warn, "cur": cur, "mac": mac}


def services_view(conn, now: float, days: int = 30) -> list[dict]:
    today = store.day_start(now)
    cur = store.latest(conn, "svc.")
    names = sorted({k[4:].rsplit(".", 1)[0] for k in store.keys(conn, "svc.", int(now) - days * 86400)},
                   key=str.lower)
    out = []
    for name in names:
        strip = store.uptime_days(conn, f"svc.{name}.up", days, today)
        # Weighted by measured minutes: a day with 10 measured minutes is not worth a whole day.
        minutes = sum(d["measured"] for d in strip)
        total = sum(d["up"] * d["measured"] for d in strip if d["up"] is not None) / minutes if minutes else None
        out.append({"name": name, "up": cur.get(f"svc.{name}.up"), "ms": cur.get(f"svc.{name}.ms"),
                    "strip": charts.strip(strip), "uptime": total,
                    "down_min": sum(d["down_min"] for d in strip)})
    return out


def containers_view(conn, start: int, end: int, step: int) -> list[dict]:
    cur = store.latest(conn, "ctr.")
    names = sorted({k[4:].rsplit(".", 1)[0] for k in store.keys(conn, "ctr.", start)})
    evs = store.events(conn, start, end, limit=20000, source="docker")
    out = []
    for name in names:
        mem = store.buckets(conn, f"ctr.{name}.mem", start, end, step)
        cpu = store.buckets(conn, f"ctr.{name}.cpu", start, end, step)
        mine = [e for e in evs if e["source"] == "docker" and e["title"].split(" ", 1)[0] == name]
        out.append({
            "name": name, "mem": cur.get(f"ctr.{name}.mem"), "cpu": cur.get(f"ctr.{name}.cpu"),
            "running": f"ctr.{name}.mem" in cur if "sys.ctr_running" in cur else None,  # None: not measured
            "mem_peak": max((b["max"] for b in mem if b and b["max"] is not None), default=None),
            "cpu_avg": _avg(cpu),
            "mem_chart": chart([("RAM", VIOLET, mem)], start, end, unit="B", height=34, axes=False),
            "cpu_chart": chart([("CPU", GREEN, cpu)], start, end, unit="%", height=34, axes=False),
            "crashes": sum(1 for e in mine if e["level"] == "alerta"),
            "starts": sum(1 for e in mine if e["title"].endswith("arrancó")),
        })
    out.sort(key=lambda c: (not c["running"], -(c["mem"] or 0)))
    return out


def _avg(buckets) -> float | None:
    vals = [(b["avg"], b["n"] - b["fails"]) for b in buckets if b and b["avg"] is not None]
    weight = sum(w for _, w in vals)
    return sum(v * w for v, w in vals) / weight if weight else None


STORMY = {95: "⛈️ tormenta", 96: "⛈️ tormenta con granizo", 99: "⛈️ tormenta con granizo"}


def weather_at(conn, ts: int) -> str:
    """How it was outside around a moment ('🌧️ lluvia 3 mm, ráfagas 55 km/h'), or ''."""
    near = {}
    for key in ("wx.code", "wx.rain", "wx.gust"):
        b = store.buckets(conn, key, ts - 900, ts + 900, 1800)[0]
        near[key] = b["max"] if b else None
    code, rain, gust = near["wx.code"], near["wx.rain"], near["wx.gust"]
    parts = []
    if code is not None and int(code) in STORMY:
        parts.append(STORMY[int(code)])
    elif rain:
        parts.append(f"🌧️ lluvia {rain:.0f} mm")
    if gust and gust >= 40:
        parts.append(f"ráfagas {gust:.0f} km/h")
    return ", ".join(parts)


def internet_view(conn, start: int, end: int) -> dict:
    outages = store.internet_outages(conn, max(start, end - store.RAW_DAYS * 86400), end)
    for o in outages:
        o["weather"] = weather_at(conn, o["start"])
    total_min, cf_fails = store.minutes(conn, "inet.cf", start, end)
    down_min = sum(o["minutes"] for o in outages)
    return {"outages": outages, "down_min": down_min,
            # Outages bridge a missing minute or two, so they can count a little more than was measured.
            "uptime": max(0.0, 1 - down_min / total_min) if total_min else None,
            "cf_loss": cf_fails / total_min * 100 if total_min else None}


STARTED = time.time()


@app.get("/health")
def health():
    """503 when nothing was measured in 5 minutes (the collector stopped), so Uptime Kuma and
    Docker's healthcheck notice; the first minutes after a start get the benefit of the doubt."""
    conn = connect()
    # The sampler's own probe, written every minute even offline: the weather thread and the Mac's
    # report write too, but they don't prove the sampler is alive.
    last = conn.execute("SELECT last_ts FROM series WHERE key = 'inet.cf'").fetchone()
    last = last[0] if last else None
    mac = json.loads(get_meta(conn, "mac") or "null")
    age = int(time.time() - last) if last else None
    stale = os.environ.get("PULSO_COLLECT", "on") != "off" and time.time() - STARTED > 300 and (age is None or age > 300)
    return JSONResponse({"ok": not stale, "last_sample_age_s": age,
                         "mac_report_age_s": int(time.time() - mac["received"]) if mac else None},
                        status_code=503 if stale else 200)


@app.get("/")
def now_page(request: Request):
    conn = connect()
    now = time.time()
    st = status(conn, now)
    cur = st["cur"]
    end6 = (int(now) // 60 + 1) * 60
    start6 = end6 - 6 * 3600
    end7 = (int(now) // 3600 + 1) * 3600  # whole hours: these graphs read the hourly rows
    start7 = end7 - 7 * 86400

    def b(key, start=start6, end=end6, step=60):
        return store.buckets(conn, key, start, end, step)

    inet_loss = b("inet.cf")
    day_net = internet_view(conn, end6 - 86400, end6)
    cuts = store.power_cuts(conn, end6 - store.RAW_DAYS * 86400, end6)
    forecast = store.disk_forecast(conn, int(now))
    cards = [
        {"title": "CPU", "href": "/graficos?r=6h", "value": fmt(cur.get("sys.cpu"), "%"),
         "sub": f"carga {fmt(cur.get('sys.load'), 'x')} · 8 núcleos",
         "chart": chart([("CPU", GREEN, b("sys.cpu"))], start6, end6, unit="%", ymax=100, height=44, axes=False)},
        {"title": "Memoria", "href": "/graficos?r=6h", "value": fmt(cur.get("sys.mem"), "%"),
         "sub": f"{fmt(cur.get('sys.mem_used'), 'B')} de {fmt(MAC_RAM, 'B')}"
                + (f" · swap {fmt(cur.get('sys.swap'), '%')}" if cur.get("sys.swap") else ""),
         "chart": chart([("RAM", VIOLET, b("sys.mem"))], start6, end6, unit="%", ymax=100, height=44, axes=False)},
        {"title": "Disco", "href": "/graficos?r=30d", "value": fmt(cur.get("sys.disk_free"), "B") + " libres",
         "sub": disk_sentence(forecast),
         "chart": chart([("Libre", CYAN, b("sys.disk_free", start7, end7, 3600))], start7, end7, unit="B",
                        height=44, axes=False)},
        {"title": "Internet", "href": "/internet", "value": fmt(cur.get("inet.cf"), "ms") if cur.get("inet.cf") is not None
            else ("sin conexión" if "inet.cf" in cur else "—"),
         "sub": (f"{fmt(day_net['uptime'] * 100, '%')} arriba en 24 h" if day_net["uptime"] is not None else "midiendo")
                + (f" · {len(day_net['outages'])} corte{'s' if len(day_net['outages']) != 1 else ''}" if day_net["outages"] else ""),
         "chart": chart([("Cloudflare", AMBER, inet_loss)], start6, end6, unit="ms", height=44, axes=False, loss=inet_loss)},
        {"title": "Red de casa", "href": "/graficos?r=6h", "value": "↓ " + fmt(cur.get("net.rx"), "B/s"),
         "sub": "↑ " + fmt(cur.get("net.tx"), "B/s") + " · router " + (fmt(cur.get("lan.router"), "ms") if cur.get("lan.router") is not None else "sin respuesta"),
         "chart": chart([("Bajada", CYAN, b("net.rx"))], start6, end6, unit="B/s", height=44, axes=False)},
        {"title": "Energía", "href": "/energia",
         "value": ("enchufado" if cur.get("bat.ac") == 1 else "a batería" if cur.get("bat.ac") == 0 else "—"),
         "sub": f"batería {fmt(cur.get('bat.pct'), '%')} · " + (f"{len(cuts)} corte{'s' if len(cuts) != 1 else ''} de luz en 14 días" if cuts else "sin cortes de luz en 14 días"),
         "chart": chart([("Batería", GREEN, b("bat.pct", start7, end7, 3600))], start7, end7, unit="%", ymax=100,
                        height=44, axes=False)},
    ]
    mac = st["mac"]
    if mac:
        age = mac["backup_age_h"]
        cards.append({"title": "Mac y backup", "href": "#mac",
                      "value": f"backup hace {age:.0f} h" if age is not None else "sin backup",
                      "sub": ("simulacro ok" if mac["drill_ok"] else "simulacro FALLÓ" if mac["drill_ok"] is False else "sin simulacro")
                             + " · " + ("con copia afuera" if mac["offsite"] else "sin copia fuera de casa"),
                      "chart": chart([("Agregado por noche", VIOLET, b("mac.backup_added", start7, end7, 3600))], start7, end7,
                                     unit="B", height=44, axes=False)})
    day_alerts = store.events(conn, int(now) - 86400, int(now) + 60, ("alerta",), limit=500)
    hourly = [None] * (7 * 24)
    for e in store.events(conn, start7, end7, ("alerta",), limit=5000):
        i = (e["ts"] - start7) // 3600
        if 0 <= i < len(hourly):
            hourly[i] = {"avg": (hourly[i]["avg"] if hourly[i] else 0) + 1, "max": None, "n": 1, "fails": 0}
    cards.append({"title": "Alertas", "href": "/eventos?f=alertas&d=7",
                  "value": f"{len(day_alerts)} en 24 h" if day_alerts else "ninguna en 24 h",
                  "sub": (f"la última: {day_alerts[0]['title']} ({when(day_alerts[0]['ts'])})" if day_alerts
                          else "nada fuera de lo normal"),
                  "chart": chart([("Alertas por hora", RED, hourly)], start7, end7, unit="", height=44, axes=False)})
    return render(request, "ahora.html", st=st, cards=cards, mac_rows=mac_rows(st["mac"], cur), services=services_view(conn, now),
                  containers=containers_view(conn, start6, end6, 300)[:6],
                  events=store.events(conn, 0, int(now) + 60, limit=8),
                  running=cur.get("sys.ctr_running"), total=cur.get("sys.ctr_total"))


def mac_rows(mac: dict | None, cur: dict) -> list[tuple[str, str, str]]:
    """(state, what, detail) for the Mac panel: what only the Mac sees, from its last report,
    plus whether the house uses AdGuard."""
    rows = []
    if "dns.lan_clients" in cur:
        n = int(cur["dns.lan_clients"])
        rows.append(("up" if n else "warn", "AdGuard en la Wi-Fi",
                     f"{n} equipo{'s' if n != 1 else ''} de la casa lo usa{'n' if n != 1 else ''}" if n else
                     "ningún equipo de la casa lo usa: el router reparte su DNS (ver README, Pendientes)"))
    if not mac:
        return rows
    age = mac["backup_age_h"]
    rows.append(("up" if age is not None and age <= 36 else "down", "Backup",
                 (f"hace {age:.0f} h" if age is not None else "nunca") + f" · {'disco USB' if mac['backup_where'] == 'usb' else '~/Backups, este disco'}"))
    rows.append(("up" if mac["drill_ok"] else "down" if mac["drill_ok"] is False else "unk", "Simulacro de restauración",
                 "volvió bien" if mac["drill_ok"] else "FALLÓ" if mac["drill_ok"] is False else "todavía no corrió"))
    rows.append(("up" if mac["offsite"] else "warn", "Copia fuera de casa",
                 "sí" if mac["offsite"] else "no: si se pierde el Mac, se pierde el backup"))
    ssd = mac.get("ssd") or {}
    if ssd:
        healthy = ssd.get("ok") and not ssd.get("media_errors") and not ssd.get("critical")
        rows.append(("up" if healthy else "down", "SSD",
                     f"{'sano' if healthy else 'CON ERRORES'} · {ssd.get('used_pct')}% gastado · {ssd.get('written_tb')} TB escritos · {ssd.get('temp_c')} °C"))
    bat = mac.get("battery") or {}
    if bat:
        rows.append(("up" if bat.get("condition") == "Good" else "warn", "Batería (el UPS)",
                     f"salud {bat.get('max_capacity_pct')}% · {bat.get('cycles')} ciclos · {bat.get('condition')}"))
    wifi = mac.get("wifi") or {}
    if wifi:
        sig = wifi.get("signal_dbm") or -100
        rows.append(("up" if sig >= -67 else "warn", "Wi-Fi",
                     f"{sig} dBm · {wifi.get('rate_mbps')} Mbit/s · canal {wifi.get('channel')}"))
    rows.append(("up" if mac["tailscale"] == "Running" else "down", "Tailscale",
                 f"{mac['tailscale']} · {mac['peers_online']} de {mac['peers']} dispositivos conectados"))
    rows.append(("warn" if mac["updates"] else "up", "macOS",
                 f"{mac['updates']} {'actualizaciones' if mac['updates'] > 1 else 'actualización'} esperando" if mac["updates"] else "al día"))
    return rows


def disk_sentence(f: dict | None) -> str:
    if not f:
        return ""
    if f["status"] == "midiendo":
        return "pronóstico cuando haya 2 días de datos"
    if f["status"] == "estable":
        return "estable (no crece)" if f["per_day"] >= 0 else f"crece {fmt(-f['per_day'], 'B')}/día: años de margen"
    if f["status"] == "lleno":
        return "ya pasó el 85%: liberá espacio"
    days = f["days_left"]
    when_txt = f"{days:.0f} días" if days < 60 else f"{days / 30:.0f} meses" if days < 730 else f"{days / 365:.0f} años"
    return f"crece {fmt(-f['per_day'], 'B')}/día: 85% en ~{when_txt}"


@app.get("/graficos")
def graphs_page(request: Request, r: str = "24h"):
    conn = connect()
    r, start, end, step = window(r)

    def b(key):
        return store.buckets(conn, key, start, end, step)

    cpu = b("sys.cpu")
    sections = [
        ("CPU", chart([("Promedio", GREEN, cpu)], start, end, unit="%", ymax=100), "Promedio de cada tramo; el pico se ve en la línea de 1 h o 6 h."),
        ("Pico de CPU", chart([("Pico", AMBER, cpu)], start, end, unit="%", ymax=100, field="max"), None),
        ("Memoria", chart([("RAM", VIOLET, b("sys.mem")), ("Swap", RED, b("sys.swap"))], start, end, unit="%", ymax=100), None),
        ("Carga (load 1 min)", chart([("Carga", CYAN, b("sys.load"))], start, end, unit="x"), "8 = los 8 núcleos ocupados."),
        ("Disco libre", chart([("Libre", CYAN, b("sys.disk_free"))], start, end, unit="B"), None),
        ("Red (en0)", chart([("Bajada", CYAN, b("net.rx")), ("Subida", AMBER, b("net.tx"))], start, end, unit="B/s"), None),
        ("Disco: lectura y escritura", chart([("Escritura", AMBER, b("io.write")), ("Lectura", GREEN, b("io.read"))], start, end, unit="B/s"), None),
        ("Batería", chart([("Batería", GREEN, b("bat.pct"))], start, end, unit="%", ymax=100), "Si baja, hubo un corte de luz."),
        ("Afuera: temperatura", chart([("Temperatura", AMBER, b("wx.temp"))], start, end, unit="°"),
         "Córdoba, de open-meteo cada 10 min. Para cruzar con los cortes."),
        ("Afuera: lluvia y viento", chart([("Lluvia (mm)", CYAN, b("wx.rain")), ("Ráfagas (km/h)", RED, b("wx.gust"))],
                                          start, end, unit=""), None),
        ("DNS de la casa (AdGuard): consultas en 24 h", chart([("Consultas", CYAN, b("dns.queries_24h")),
                                                               ("Bloqueadas", RED, b("dns.blocked_24h"))], start, end, unit=""),
         "Suma móvil de las últimas 24 h, como la cuenta AdGuard."),
        ("DNS: tiempo promedio de respuesta", chart([("AdGuard", AMBER, b("dns.ms"))], start, end, unit="ms"),
         "Incluye ir a Quad9/Cloudflare cuando no está en caché. Si sube, el DNS anda lento."),
        ("Contenedores corriendo", chart([("Corriendo", GREEN, b("sys.ctr_running"))], start, end, unit=""), None),
        ("Backup: lo que suma cada noche", chart([("Agregado", VIOLET, b("mac.backup_added"))], start, end, unit="B"),
         "Comprimido, después de deduplicar. Un salto grande: muchas fotos nuevas, o algo que no debería estar."),
        ("SSD: lo escrito en total", chart([("Escrito", VIOLET, b("mac.ssd_written_tb"))], start, end, unit=" TB"),
         "Lo que más gasta un SSD; cuánto lleva gastado (SMART) está en la tarjeta Mac y backup."),
        ("Señal Wi-Fi", chart([("Señal", AMBER, b("mac.wifi_signal"))], start, end, unit=" dBm"),
         "Cada 30 min. Más cerca de 0 es mejor: -30 excelente, -67 justo, -80 se corta."),
    ]
    return render(request, "graficos.html", r=r, sections=sections)


@app.get("/contenedores")
def containers_page(request: Request, r: str = "24h"):
    conn = connect()
    r, start, end, step = window(r)
    return render(request, "contenedores.html", r=r, containers=containers_view(conn, start, end, step))


@app.get("/internet")
def internet_page(request: Request, r: str = "24h"):
    conn = connect()
    r, start, end, step = window(r)
    cf, google, router = (store.buckets(conn, k, start, end, step) for k in ("inet.cf", "inet.google", "lan.router"))
    view = internet_view(conn, start, end)
    latency = chart([("Cloudflare", AMBER, cf), ("Google", CYAN, google), ("Router", GREEN, router)],
                    start, end, unit="ms", loss=cf)
    peaks = chart([("Cloudflare", AMBER, cf)], start, end, unit="ms", field="max")
    # Speed tests run every few hours: one point per 6 h (per day over a year).
    sstep = 86400 if step >= 86400 else 6 * 3600
    day = store.day_start(start)  # local 0, 6, 12, 18 h (or local midnight)
    sstart = day + (start - day) // sstep * sstep
    speed = chart([("Bajada", CYAN, store.buckets(conn, "inet.down_mbps", sstart, end, sstep)),
                   ("Subida", AMBER, store.buckets(conn, "inet.up_mbps", sstart, end, sstep))],
                  sstart, end, unit=" Mbit/s")
    downs = dict(store.recent(conn, "inet.down_mbps", 8))
    tests = [(ts, down, dict(store.recent(conn, "inet.up_mbps", 8)).get(ts)) for ts, down in downs.items()]
    # Weekday x hour over the last 30 days (hourly rows): does the provider get slow at some hours?
    hstart = (int(time.time()) // 3600 - 30 * 24) * 3600
    cells: dict[tuple[int, int], list] = {}
    for i, bk in enumerate(store.buckets(conn, "inet.cf", hstart, hstart + 30 * 86400, 3600)):
        if bk and bk["n"]:
            d = datetime.fromtimestamp(hstart + i * 3600)
            c = cells.setdefault((d.weekday(), d.hour), [0.0, 0, 0, 0])
            if bk["avg"] is not None:
                c[0] += bk["avg"] * (bk["n"] - bk["fails"])
                c[1] += bk["n"] - bk["fails"]
            c[2] += bk["fails"]
            c[3] += bk["n"]
    heat = charts.heatmap({k: (c[0] / c[1] if c[1] else None, c[2] / c[3]) for k, c in cells.items()})
    return render(request, "internet.html", r=r, view=view, latency=latency, peaks=peaks, speed=speed, tests=tests, heat=heat,
                  raw_limited=end - start > store.RAW_DAYS * 86400)


def power_view(conn, now: int) -> dict:
    """The power cuts of the last 14 days, how much battery each one took, and how long a full
    battery would last at the pace measured during them."""
    cuts = store.power_cuts(conn, now - store.RAW_DAYS * 86400, now + 60)
    drains = []
    for c in cuts:
        pcts = [b["avg"] for b in store.buckets(conn, "bat.pct", c["start"] - 60, c["end"] + 120, 60) if b and b["avg"] is not None]
        c["from"], c["to"] = (pcts[0], min(pcts)) if pcts else (None, None)
        c["weather"] = weather_at(conn, c["start"])
        if pcts and c["minutes"] >= 5 and pcts[0] > min(pcts):
            drains.append((pcts[0] - min(pcts)) / c["minutes"])  # % per minute
    per_min = sum(drains) / len(drains) if drains else None
    cur = store.latest(conn, "bat.")
    left_h = (cur.get("bat.pct") or 0) / per_min / 60 if per_min else None
    return {"cuts": cuts, "per_min": per_min, "left_h": left_h, "pct": cur.get("bat.pct"), "ac": cur.get("bat.ac"),
            "stop_at": 15, "shutdown_at": 7}


@app.get("/energia")
def power_page(request: Request, r: str = "7d"):
    conn = connect()
    r, start, end, step = window(r)
    view = power_view(conn, int(time.time()))
    mac = mac_info(conn)
    graph = chart([("Batería", GREEN, store.buckets(conn, "bat.pct", start, end, step))], start, end, unit="%", ymax=100)
    return render(request, "energia.html", r=r, view=view, graph=graph, battery=(mac or {}).get("battery"))


@app.get("/servicios")
def services_page(request: Request):
    conn = connect()
    return render(request, "servicios.html", services=services_view(conn, time.time(), 90))


@app.get("/eventos")
def events_page(request: Request, f: str = "todo", d: int = 7, q: str = ""):
    conn = connect()
    now = int(time.time())
    d = min(max(d, 1), 365)
    levels = {"alertas": ("alerta",), "docker": None}.get(f)
    source = {"docker": "docker", "avisos": "ntfy"}.get(f)
    rows = store.events(conn, now - d * 86400, now + 60, levels, limit=1000, source=source, words=q.split())
    days: dict[str, list] = {}
    for e in rows:
        dt = datetime.fromtimestamp(e["ts"])
        days.setdefault(f"{charts.DAYS[dt.weekday()]} {dt.day}/{dt.month}", []).append(e)
    return render(request, "eventos.html", days=days, f=f, d=d, q=q, count=len(rows))


def crashes(conn, start: int, end: int) -> dict:
    """Container -> times it crashed, ran out of memory or stopped answering."""
    out: dict[str, int] = {}
    for e in store.events(conn, start, end, ("alerta",), limit=2000):
        if e["source"] == "docker":
            name = e["title"].split(" ", 1)[0]
            out[name] = out.get(name, 0) + 1
    return out


@app.get("/mapa")
def map_page(request: Request):
    conn = connect()
    st = status(conn, time.time())
    down = [mapa.NODES[n][1] for n, node in mapa.NODES.items() if mapa.state(node[3], st["cur"], st["mac"]) == "down"]
    return render(request, "mapa.html", svg=mapa.draw(st["cur"], st["mac"]), down=down)


@app.get("/servicios/{name}")
def service_page(request: Request, name: str, r: str = "24h"):
    conn = connect()
    r, start, end, step = window(r)
    now = time.time()
    if not store.keys(conn, f"svc.{name}.", 0):
        return render(request, "vacio.html", what=f"No hay datos del servicio {name}.")
    up = store.buckets(conn, f"svc.{name}.up", start, end, step)
    ms = store.buckets(conn, f"svc.{name}.ms", start, end, step)
    downs = store.periods(conn, max(start, end - store.RAW_DAYS * 86400), end, [f"svc.{name}.up"], lambda v: v == 0)
    alerts = [e for e in store.events(conn, start, end, limit=2000, source="ntfy", words=[name]) if name in e["title"]]
    return render(request, "servicio.html", r=r, name=name,
                  view=next((s for s in services_view(conn, now, 90) if s["name"] == name), None),
                  latency=chart([("Respuesta", CYAN, ms)], start, end, unit="ms", loss=up_as_loss(up)),
                  peaks=chart([("Peor respuesta", AMBER, ms)], start, end, unit="ms", field="max"),
                  downs=downs, alerts=alerts)


def up_as_loss(buckets: list) -> list:
    """svc.*.up buckets (1 up, 0 down) as "failures" for the red bars of a graph."""
    return [None if not b or b["avg"] is None else {"n": b["n"] - b["fails"], "fails": round((1 - b["avg"]) * (b["n"] - b["fails"]))}
            for b in buckets]


@app.get("/contenedores/{name}")
def container_page(request: Request, name: str, r: str = "24h"):
    conn = connect()
    r, start, end, step = window(r)
    if not store.keys(conn, f"ctr.{name}.", 0):
        return render(request, "vacio.html", what=f"No hay datos del contenedor {name}.")
    mem = store.buckets(conn, f"ctr.{name}.mem", start, end, step)
    cpu = store.buckets(conn, f"ctr.{name}.cpu", start, end, step)
    evs = [e for e in store.events(conn, start, end, limit=2000, source="docker", words=[name])
           if e["title"].split(" ", 1)[0] == name]
    cur = store.latest(conn, f"ctr.{name}.")
    return render(request, "contenedor.html", r=r, name=name, events=evs,
                  crashes=sum(1 for e in evs if e["level"] == "alerta"),
                  starts=sum(1 for e in evs if e["title"].endswith("arrancó")),
                  mem_now=cur.get(f"ctr.{name}.mem"), cpu_now=cur.get(f"ctr.{name}.cpu"),
                  mem=chart([("RAM", VIOLET, mem)], start, end, unit="B"),
                  mem_peak=chart([("Pico de RAM", RED, mem)], start, end, unit="B", field="max"),
                  cpu=chart([("CPU", GREEN, cpu)], start, end, unit="%"),
                  cpu_peak=chart([("Pico de CPU", AMBER, cpu)], start, end, unit="%", field="max"))


def hours_since(stamp: str | None) -> float | None:
    """Hours since a local "YYYY-MM-DDTHH:MM:SS" written on the Mac. The Mac's clock zone may not be
    this container's (it's set apart), so the Mac sends its own "now" too: see mac_status."""
    try:
        return (time.time() - datetime.fromisoformat(stamp).timestamp()) / 3600
    except (TypeError, ValueError):
        return None


@app.post("/api/mac")
def mac_status(st: dict = Body(...)):
    """What only the Mac can see, every 30 minutes from setup/n8n-feed.sh (its estado.json):
    the last backup, the restore drill, Tailscale, the Wi-Fi link, pending macOS updates."""
    conn = connect()
    # The Mac writes local times in its own zone; its "time" (HH:MM:SS, now) tells the offset.
    skew = 0.0
    mac_now = st.get("time")
    if mac_now:
        here = datetime.now()
        h, m, sec = (int(x) for x in mac_now.split(":"))
        diff = (here.hour * 3600 + here.minute * 60 + here.second) - (h * 3600 + m * 60 + sec)
        skew = round(((diff + 43200) % 86400 - 43200) / 1800) * 0.5  # whole half hours, -12..+12
    backup = st.get("backup") or {}
    if backup.get("last_epoch"):  # exact, whatever the Mac's zone did since (newer desktop-status.py)
        age = max((time.time() - backup["last_epoch"]) / 3600, 0)
    else:
        age = hours_since(backup.get("last"))
        if age is not None:
            age = max(age - skew, 0)
    drill = st.get("simulacro") or {}
    wifi = st.get("wifi") or {}
    ts = st.get("tailscale") or {}
    values = {
        "mac.backup_age_h": round(age, 2) if age is not None else None,
        "mac.ts_up": 1.0 if ts.get("state") == "Running" else 0.0,
        "mac.wifi_signal": wifi.get("signal_dbm"),
        "mac.wifi_rate": wifi.get("rate_mbps"),
        "mac.updates": float(st.get("macos_updates") or 0),
        "mac.bat_health": (st.get("bateria") or {}).get("max_capacity_pct") or None,
        "mac.ssd_used": (st.get("ssd") or {}).get("used_pct"),
        "mac.ssd_written_tb": (st.get("ssd") or {}).get("written_tb"),
        "mac.ssd_temp": (st.get("ssd") or {}).get("temp_c"),
        "mac.bat_cycles": (st.get("bateria") or {}).get("cycles"),
        "mac.backup_added": (st.get("backup") or {}).get("added_bytes"),
        "mac.backup_secs": (st.get("backup") or {}).get("seconds"),
    }
    # A value the Mac didn't send (no Wi-Fi, an older desktop-status.py) is left out, not stored as a failure.
    optional = {"mac.wifi_signal", "mac.wifi_rate", "mac.backup_added", "mac.backup_secs", "mac.bat_health", "mac.bat_cycles",
                "mac.ssd_used", "mac.ssd_written_tb", "mac.ssd_temp"}
    store.insert(conn, int(time.time()) // 60 * 60, {k: v for k, v in values.items() if k not in optional or v is not None})
    set_meta(conn, "mac", json.dumps({
        "received": int(time.time()), "backup_age_h": values["mac.backup_age_h"], "backup_where": (st.get("backup") or {}).get("where"),
        "drill_ok": drill.get("ok"), "drill_age_h": (hours_since(drill.get("fecha")) or 0) - skew if drill.get("fecha") else None,
        "offsite": drill.get("offsite"), "tailscale": ts.get("state"),
        "peers_online": sum(1 for p in ts.get("peers") or [] if p.get("online")), "peers": len(ts.get("peers") or []),
        "wifi": wifi or None, "updates": int(st.get("macos_updates") or 0), "clock_skew_h": skew,
        "battery": st.get("bateria"), "ssd": st.get("ssd"),
    }))
    return {"ok": True}


def mac_info(conn) -> dict | None:
    """The Mac's last report, if it's under 2 hours old."""
    try:
        info = json.loads(get_meta(conn, "mac") or "null")
    except ValueError:
        return None
    return info if info and time.time() - info["received"] < 7200 else None


@app.get("/api/frase")
def api_sentence():
    """One spoken answer for a Siri Shortcut ("¿Cómo está el server?"): Get Contents of URL → Speak."""
    conn = connect()
    now = time.time()
    st = status(conn, now)
    end = int(now)
    day = internet_view(conn, end - 86400, end)
    cuts = store.power_cuts(conn, end - 86400, end)
    parts = [{"verde": "Todo bien.", "amarillo": "Hay algo para mirar: ", "rojo": "Algo anda mal: "}[st["light"]]]
    if st["notes"]:
        parts[0] += ", ".join(st["notes"]) + "."
    if day["outages"]:
        n = len(day["outages"])
        parts.append(f"Internet se cortó {n} {'vez' if n == 1 else 'veces'} en el día, {day['down_min']} minutos en total.")
    elif day["uptime"] is not None:
        parts.append("Internet sin cortes en el día.")
    if cuts:
        parts.append(f"Hubo {len(cuts)} corte{'s' if len(cuts) > 1 else ''} de luz.")
    mac = st["mac"]
    if mac and mac["backup_age_h"] is not None:
        h = mac["backup_age_h"]
        parts.append("El último backup fue hace " + ("menos de una hora." if h < 1 else "una hora." if h < 1.5 else f"{h:.0f} horas."))
    cur = st["cur"]
    if cur.get("sys.disk_free"):
        parts.append(f"Quedan {cur['sys.disk_free'] / 1e9:.0f} gigas libres.")
    unknown = red.unknown_count(conn)
    if unknown:
        parts.append("Hay un equipo desconocido en la Wi-Fi." if unknown == 1 else f"Hay {unknown} equipos desconocidos en la Wi-Fi.")
    return PlainTextResponse(" ".join(parts))


def week_numbers(conn, start: int, end: int) -> dict:
    """One week, summed up: what /semana compares. All None for a week Pulso didn't measure (a 0
    there would read as "no alerts", not "no data")."""
    if _avg(store.buckets(conn, "sys.cpu", start, end, 3600)) is None:
        return {key: None for _, key, _, _ in WEEK_ROWS}
    net = internet_view(conn, start, end)
    cuts = store.power_cuts(conn, start, end)
    lat = [b for b in store.buckets(conn, "inet.cf", start, end, 3600) if b and b["avg"] is not None]
    downs = [b["avg"] for b in store.buckets(conn, "inet.down_mbps", start, end, 3600) if b and b["avg"] is not None]
    free = [b["avg"] for b in store.buckets(conn, "sys.disk_free", start, end, 3600) if b and b["avg"] is not None]
    return {
        "internet": net["uptime"] * 100 if net["uptime"] is not None else None,
        "cortes": len(net["outages"]), "min_sin_internet": net["down_min"],
        "latencia": _avg([b for b in lat]) if lat else None,
        "bajada": sum(downs) / len(downs) if downs else None,
        "luz": len(cuts), "min_sin_luz": sum(c["minutes"] for c in cuts),
        "alertas": len(store.events(conn, start, end, ("alerta",), limit=5000)),
        "caidas": sum(crashes(conn, start, end).values()),
        "cpu": _avg(store.buckets(conn, "sys.cpu", start, end, 3600)),
        "ram": _avg(store.buckets(conn, "sys.mem", start, end, 3600)),
        "disco": (free[-1] - free[0]) if len(free) > 1 else None,
    }


# (label, key, unit, better: +1 when higher is better, -1 when lower is, 0 neutral)
WEEK_ROWS = [
    ("Internet arriba", "internet", "%", +1), ("Cortes de internet", "cortes", "", -1),
    ("Minutos sin internet", "min_sin_internet", "", -1), ("Latencia promedio", "latencia", "ms", -1),
    ("Velocidad de bajada", "bajada", " Mbit/s", +1), ("Cortes de luz", "luz", "", -1),
    ("Minutos sin luz", "min_sin_luz", "", -1), ("Alertas", "alertas", "", -1),
    ("Contenedores caídos", "caidas", "", -1), ("CPU promedio", "cpu", "%", -1), ("RAM promedio", "ram", "%", -1),
    ("Disco libre (cambio)", "disco", "B", +1),
]


@app.get("/semana")
def week_page(request: Request):
    conn = connect()
    end = store.day_start(time.time()) + 86400
    this, last = week_numbers(conn, end - 7 * 86400, end), week_numbers(conn, end - 14 * 86400, end - 7 * 86400)
    rows = []
    for label, key, unit, better in WEEK_ROWS:
        a, b = this[key], last[key]
        trend = ""
        if a is not None and b is not None and better and abs(a - b) > max(abs(b) * 0.05, 1e-9):
            trend = "mejor" if (a - b) * better > 0 else "peor"
        rows.append({"label": label, "now": fmt(a, unit) if unit else ("—" if a is None else f"{a:.0f}"),
                     "before": fmt(b, unit) if unit else ("—" if b is None else f"{b:.0f}"), "trend": trend})
    return render(request, "semana.html", rows=rows)


@app.get("/api/series")
def api_series():
    """Every key Pulso measures, for /api/serie.csv."""
    conn = connect()
    return {"series": store.keys(conn, "", 0)}


@app.get("/api/serie.csv")
def api_series_csv(k: str, r: str = "7d"):
    """One series as CSV (a spreadsheet, or anything else): time, average, min, max, failed samples."""
    conn = connect()
    r, start, end, step = window(r)
    lines = ["hora,promedio,minimo,maximo,fallas"]
    for i, b in enumerate(store.buckets(conn, k, start, end, step)):
        if b:
            t = datetime.fromtimestamp(start + i * step).strftime("%Y-%m-%d %H:%M")
            lines.append(",".join([t] + ["" if b[f] is None else f"{b[f]:g}" for f in ("avg", "min", "max")] + [str(b["fails"])]))
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{k}-{r}.csv"'})


@app.get("/atajo")
def shortcut_help(request: Request):
    return render(request, "atajo.html")


def speed_week(conn, end: int) -> dict | None:
    """Average and worst of the week's speed tests, in Mbit/s."""
    downs = [b["avg"] for b in store.buckets(conn, "inet.down_mbps", end - 7 * 86400, end, 3600) if b and b["avg"] is not None]
    ups = [b["avg"] for b in store.buckets(conn, "inet.up_mbps", end - 7 * 86400, end, 3600) if b and b["avg"] is not None]
    if not downs:
        return None
    return {"bajada": round(sum(downs) / len(downs)), "bajada_min": round(min(downs)),
            "subida": round(sum(ups) / len(ups)) if ups else None, "tests": len(downs)}


@app.get("/api/resumen")
def api_summary():
    """For Homepage, the iPhone widget and n8n: the light and a few numbers, nothing private."""
    conn = connect()
    now = time.time()
    st = status(conn, now)
    cur = st["cur"]
    end = int(now)
    day = internet_view(conn, end - 86400, end)
    week = internet_view(conn, end - 7 * 86400, end)
    f = store.disk_forecast(conn, end)
    cuts = store.power_cuts(conn, end - 7 * 86400, end)
    svc = services_view(conn, now, 7)
    return {
        "luz": st["light"], "avisos": st["notes"],
        "cpu": cur.get("sys.cpu"), "ram": cur.get("sys.mem"), "disco_libre_gb": round((cur.get("sys.disk_free") or 0) / 1e9),
        "disco": disk_sentence(f),
        "internet_ms": cur.get("inet.cf"),
        "internet_24h": round(day["uptime"] * 100, 2) if day["uptime"] is not None else None,
        "internet_cortes_24h": len(day["outages"]), "internet_min_caido_24h": day["down_min"],
        "internet_cortes_7d": len(week["outages"]), "internet_min_caido_7d": week["down_min"],
        "cortes_luz_7d": len(cuts), "min_sin_luz_7d": sum(c["minutes"] for c in cuts),
        "servicios_7d": [{"nombre": s["name"], "uptime": round(s["uptime"] * 100, 2) if s["uptime"] is not None else None,
                          "min_caido": s["down_min"]} for s in svc if s["down_min"]],
        "alertas_24h": len(store.events(conn, end - 86400, end + 60, ("alerta",))),
        "contenedores_caidos_7d": crashes(conn, end - 7 * 86400, end + 60),
        "velocidad_7d": speed_week(conn, end),
        "contenedores": f"{int(cur.get('sys.ctr_running') or 0)}/{int(cur.get('sys.ctr_total') or 0)}",
        "guardia": guardia_text(conn, now),
    }


def guardia_text(conn, now: float) -> str | None:
    """The last weekly report's lines, while they are this week's news (under 8 days old)."""
    g = guardia.last(conn)
    return g["text"] if g and now - g["ts"] < 8 * 86400 else None


# --- Red: who is on the home Wi-Fi (app/red.py) ------------------------------------------------

async def form(request: Request) -> dict:
    """A plain HTML form (application/x-www-form-urlencoded), read without python-multipart."""
    body = (await request.body()).decode(errors="replace")
    return {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}


@app.post("/api/red")
def api_red_scan(scan: dict = Body(...)):
    """A scan of the home Wi-Fi, every 2 minutes from setup/red-scan.sh on the Mac."""
    return red.ingest(connect(), scan)


@app.get("/api/red")
def api_red():
    return red.api(connect())


@app.get("/red")
def red_page(request: Request):
    return render(request, "red.html", v=red.view(connect()))


@app.post("/red/dispositivo")
async def red_name(request: Request):
    f = await form(request)
    red.name_device(connect(), f.get("mac", ""), f.get("name", ""), f.get("kind", ""), f.get("person", ""))
    return RedirectResponse("/red", status_code=303)


@app.post("/red/olvidar")
async def red_forget(request: Request):
    red.forget(connect(), (await form(request)).get("mac", ""))
    return RedirectResponse("/red", status_code=303)


@app.post("/red/ajustes")
async def red_settings(request: Request):
    f = await form(request)
    red.set_settings(connect(), f.get("avisar") == "1", f.get("webhook", ""))
    return RedirectResponse("/red", status_code=303)


# --- Guardia: the weekly look at the logs (app/guardia.py) -------------------------------------

@app.get("/guardia")
def guardia_page(request: Request):
    conn = connect()
    g = guardia.last(conn)
    earlier = conn.execute("SELECT id, ts, text, by_model FROM guardia ORDER BY id DESC LIMIT 12 OFFSET 1").fetchall()
    return render(request, "guardia.html", g=g, earlier=earlier, running=guardia.running(),
                  patterns=guardia.patterns_view(conn, g["id"]) if g else [])


@app.get("/api/guardia")
def api_guardia():
    g = guardia.last(connect())
    if not g:
        return {"texto": None, "corriendo": guardia.running()}
    return {"id": g["id"], "ts": g["ts"], "cuando": when(g["ts"], True), "texto": g["text"], "por_modelo": g["by_model"],
            "hallazgos": g["findings"], "stats": g["stats"], "corriendo": guardia.running()}


@app.post("/api/guardia")
def api_guardia_run():
    """Runs it now, in the background (logs and model take minutes); one at a time."""
    if not guardia.start_now():
        return JSONResponse({"ok": False, "error": "Ya hay uno corriendo."}, status_code=409)
    return JSONResponse({"ok": True, "corriendo": True}, status_code=202)


@app.post("/guardia/correr")
def guardia_run_form():
    guardia.start_now()
    return RedirectResponse("/guardia", status_code=303)
