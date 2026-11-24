"""The server's history as tools for the local AI: Open WebUI reads /ia/openapi.json and lets the
model (qwen3 on Ollama) answer "¿cómo anduvo internet esta semana?" or "¿se cortó la luz?".

Like Finanzas and Recordatorios: every answer comes already counted and written out (minutes,
percentages, dates), so the model only reads. Mounted at /ia in main.py."""
import time
from datetime import datetime

from fastapi import FastAPI, Query

from . import charts, store
from .charts import fmt
from .db import connect

ia = FastAPI(
    title="Pulso",
    version="1",
    description="La historia del servidor casero (un Mac): si anda, internet, cortes de luz, servicios, "
                "contenedores y alertas. Los números ya vienen calculados: no los recalcules.",
    docs_url=None, redoc_url=None, root_path_in_servers=False,
)

DIAS = Query(7, description="Cuántos días hacia atrás mirar, de 1 a 14.")


def _when(ts: int) -> str:
    d = datetime.fromtimestamp(ts)
    return f"{charts.DAYS[d.weekday()]} {d.day}/{d.month} {d.strftime('%H:%M')}"


def _period(p: dict) -> dict:
    return {"desde": _when(p["start"]), "minutos": p["minutes"]}


def _days(dias: int) -> tuple[int, int]:
    end = int(time.time()) + 60
    return end - max(1, min(dias, 14)) * 86400, end


@ia.get("/estado", operation_id="estado_del_server", summary="Cómo está el servidor ahora: si anda todo, CPU, memoria, disco, internet, batería")
def status():
    from .main import disk_sentence, status as light
    conn = connect()
    st = light(conn, time.time())
    cur = st["cur"]
    return {
        "estado": {"verde": "todo bien", "amarillo": "hay algo para mirar", "rojo": "algo anda mal"}[st["light"]],
        "avisos": st["notes"],
        "cpu": fmt(cur.get("sys.cpu"), "%"), "memoria": fmt(cur.get("sys.mem"), "%"),
        "disco_libre": fmt(cur.get("sys.disk_free"), "B"), "disco": disk_sentence(store.disk_forecast(conn, int(time.time()))),
        "internet_latencia": fmt(cur.get("inet.cf"), "ms") if cur.get("inet.cf") is not None else "sin conexión",
        "enchufado": cur.get("bat.ac") == 1, "bateria": fmt(cur.get("bat.pct"), "%"),
        "contenedores_corriendo": f"{int(cur.get('sys.ctr_running') or 0)} de {int(cur.get('sys.ctr_total') or 0)}",
    }


@ia.get("/internet", operation_id="historia_de_internet", summary="Cómo anduvo internet: cortes (cuándo y cuánto duraron), porcentaje con conexión, latencia y velocidad")
def internet(dias: int = DIAS):
    from .main import internet_view
    conn = connect()
    start, end = _days(dias)
    v = internet_view(conn, start, end)
    lat = [b for b in store.buckets(conn, "inet.cf", start, end, 3600) if b and b["avg"] is not None]
    speeds = store.recent(conn, "inet.down_mbps", 4)
    ups = dict(store.recent(conn, "inet.up_mbps", 4))
    return {
        "dias": dias, "con_internet": fmt(v["uptime"] * 100, "%") if v["uptime"] is not None else "sin datos",
        "cantidad_de_cortes": len(v["outages"]), "minutos_sin_internet": v["down_min"],
        "cortes": [_period(o) for o in v["outages"]][-20:],
        "latencia_promedio": fmt(sum(b["avg"] for b in lat) / len(lat), "ms") if lat else "sin datos",
        "pings_perdidos": fmt(v["cf_loss"], "%"),
        "ultimos_tests_de_velocidad": [{"cuando": _when(ts), "bajada": f"{d:.0f} Mbit/s" if d else "falló",
                                        "subida": f"{ups[ts]:.0f} Mbit/s" if ups.get(ts) else "—"} for ts, d in speeds],
    }


@ia.get("/luz", operation_id="cortes_de_luz", summary="Cortes de luz: cuándo el Mac quedó a batería y cuánto duró cada uno")
def power(dias: int = DIAS):
    conn = connect()
    start, end = _days(dias)
    cuts = store.power_cuts(conn, start, end)
    return {"dias": dias, "cantidad": len(cuts), "minutos_sin_luz": sum(c["minutes"] for c in cuts),
            "cortes": [_period(c) for c in cuts][-20:]}


@ia.get("/servicios", operation_id="servicios_del_server", summary="Cada servicio (Immich, Jellyfin, n8n, Finanzas...): si anda ahora, porcentaje arriba y minutos caído")
def services(dias: int = DIAS):
    from .main import services_view
    conn = connect()
    out = []
    for s in services_view(conn, time.time(), max(1, min(dias, 90))):
        out.append({"servicio": s["name"], "ahora": "arriba" if s["up"] == 1 else "caído" if s["up"] == 0 else "sin datos",
                    "arriba": fmt(s["uptime"] * 100, "%") if s["uptime"] is not None else "sin datos",
                    "minutos_caido": s["down_min"]})
    return {"dias": dias, "servicios": out}


@ia.get("/contenedores", operation_id="contenedores_del_server", summary="Los contenedores Docker: memoria y CPU ahora, y cuántas veces se cayeron")
def containers(dias: int = DIAS):
    from .main import containers_view
    conn = connect()
    start, end = _days(dias)
    return {"dias": dias, "contenedores": [
        {"nombre": c["name"], "corriendo": c["running"], "memoria": fmt(c["mem"], "B"), "cpu": fmt(c["cpu"], "%"),
         "caidas": c["crashes"]} for c in containers_view(conn, start, end, 3600)]}


@ia.get("/eventos", operation_id="eventos_del_server", summary="Lo que pasó en el servidor: alertas que llegaron al iPhone, contenedores caídos, avisos")
def events(dias: int = Query(1, description="Cuántos días hacia atrás, de 1 a 30."),
           solo_alertas: bool = Query(False, description="true: solo los problemas.")):
    conn = connect()
    end = int(time.time()) + 60
    rows = store.events(conn, end - max(1, min(dias, 30)) * 86400, end, ("alerta",) if solo_alertas else None, limit=40)
    return {"dias": dias, "eventos": [{"cuando": _when(e["ts"]), "tipo": e["level"], "titulo": e["title"],
                                       "detalle": e["body"][:300]} for e in rows]}


@ia.get("/casa", operation_id="quien_esta_en_casa", summary="Quién está en casa ahora, según los teléfonos y compus conectados a la Wi-Fi")
def home():
    from . import red
    v = red.view(connect())
    return {"en_casa": v["home"] or "nadie", "equipos_conectados": len(v["present"]),
            "equipos_desconocidos": len(v["unknown"]),
            "ultimo_escaneo": _when(v["last_scan"]) if v["last_scan"] else "ninguno todavía",
            "sin_datos_hace": f"{v['stale_min']} min" if v["stale_min"] else None}


@ia.get("/red", operation_id="dispositivos_en_la_red", summary="Los equipos conectados a la Wi-Fi de casa ahora (nombre, de quién, IP) y los desconocidos")
def network():
    from . import red
    v = red.view(connect())
    one = lambda d: {"nombre": d["label"], "tipo": d["kind_label"] or "—", "de": d["person"] or "—", "ip": d["ip"],
                     "desconocido": not d["known"], "visto_hace": f"{d['ago_min']} min"}
    return {"conectados": [one(d) for d in v["present"]], "desconocidos": [one(d) for d in v["unknown"]]}


@ia.get("/guardia", operation_id="informe_de_guardia", summary="El informe semanal de Guardia: lo que encontraron las reglas en los logs y las tablas del server, con la evidencia")
def guardia_report():
    from . import guardia
    g = guardia.last(connect())
    if not g:
        return {"informe": "Todavía no hay ningún informe de Guardia."}
    return {"cuando": _when(g["ts"]), "informe": g["text"], "escrito_por": "el modelo" if g["by_model"] else "las reglas",
            "hallazgos": [{"gravedad": f["gravedad"], "que": f["que"], "evidencia": f["evidencia"],
                           "sugerencia": f["sugerencia"], "sigue_igual": f["sigue_igual"]} for f in g["findings"][:12]]}
