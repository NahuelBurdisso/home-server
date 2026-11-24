"""The background thread (what the old motor did), every 10 minutes:

1. Pending orders: on weekdays from the open to 20:00 New York, download those tickers (and SPY, the
   session calendar); fill what can be filled,
   notify once per decision when none of its orders is left pending.
2. Once per New York day after 17:15: download what is held and SPY, apply dividends and splits,
   store the equity point of both portfolios (when that day was a session).
3. Friday after the close: the week's summary (Claude against the vara).
4. 5 days without a decision: one notice («Claude no vino»), once until one comes.

Off with MESA_TAREAS=0 (the tests). Nothing here can take the web down: every pass catches,
logs and goes on.
"""
import logging
import os
import threading
from datetime import datetime, time as dtime

from . import cartera, db, notify, precios, reglas

log = logging.getLogger("mesa.tareas")
CADA_S = int(os.environ.get("MESA_TAREAS_CADA_S", "600"))
CIERRE = dtime(17, 15)
_parar = threading.Event()
_hilo: threading.Thread | None = None


def activas() -> bool:
    return os.environ.get("MESA_TAREAS", "1") != "0"


def usd(x: float) -> str:
    return f"{x:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def cant(x: float) -> str:
    """Shares: up to 6 decimals, the trailing zeros off (12,5 / 0,034512)."""
    t = f"{x:,.6f}".rstrip("0").rstrip(".")
    return t.replace(",", "_").replace(".", ",").replace("_", ".")


def pct(x: float | None) -> str:
    return "—" if x is None else (("+" if x >= 0 else "−") + f"{abs(x) * 100:.1f} %".replace(".", ","))


# --- 1. fills ---------------------------------------------------------------------------------

def paso_ordenes(conn) -> list[dict]:
    pend = cartera.pendientes(conn)
    if not pend:
        return []
    t = precios.dt_ny(db.ahora_ms())
    if t.weekday() < 5 and dtime(9, 30) <= t.time() < dtime(20, 0):  # only when a new open can be there
        precios.bajar(conn, [o["ticker"] for o in pend] + [reglas.VARA], rango="1mo")
    hechos = cartera.ejecutar_pendientes(conn)
    if hechos:
        avisar_ejecuciones(conn, {h["decision_id"] for h in hechos if h.get("decision_id")})
    return hechos


def avisar_ejecuciones(conn, decisiones: set):
    avisadas = set(db.leer(conn, "decisiones_avisadas", []))
    for did in sorted(decisiones - avisadas):
        ords = conn.execute("SELECT o.*, c.clave FROM ordenes o JOIN carteras c ON c.id = o.cartera_id WHERE decision_id = ? "
                            "ORDER BY o.lado DESC, o.id", (did,)).fetchall()
        if any(o["estado"] == "pendiente" for o in ords):
            continue
        partes = []
        for o in ords:
            if o["estado"] == "ejecutada":
                verbo = "compró" if o["lado"] == "compra" else "vendió"
                verbo = f"La vara {verbo}" if o["clave"] == "vara" else verbo.capitalize()
                partes.append(f"{verbo} {cant(o['cantidad_ejecutada'])} {o['ticker']} a {usd(o['precio'])} USD "
                              f"({usd(o['cantidad_ejecutada'] * o['precio'])} USD)")
            else:
                partes.append(f"{o['ticker']}: {o['estado']} ({o['motivo_rechazo']})")
        if partes:
            notify.avisar("Mesa: se ejecutó la decisión", ". ".join(partes) + ".", ruta=f"/decisiones/{did}", tags="moneybag")
        avisadas.add(did)
    with db.transaccion(conn):
        db.guardar(conn, "decisiones_avisadas", sorted(avisadas)[-200:])


# --- 2. the close -----------------------------------------------------------------------------

def paso_cierre(conn, ahora_ms: int) -> bool:
    t = precios.dt_ny(ahora_ms)
    hoy = t.date().isoformat()
    if t.time() < CIERRE or db.leer(conn, "ultimo_cierre") == hoy:
        return False
    tenidos = {p["ticker"] for c in cartera.carteras(conn).values() for p in cartera.posiciones(conn, c["id"])}
    r = precios.bajar(conn, sorted(tenidos | {reglas.VARA}), rango="3mo", forzar=True)
    if r.get(reglas.VARA):  # Yahoo down: try again on the next pass
        return False
    hechos = cartera.aplicar_eventos(conn, hoy)
    for h in hechos:
        log.info("evento: %s", h)
    if conn.execute("SELECT 1 FROM precios WHERE ticker = ? AND fecha = ?", (reglas.VARA, hoy)).fetchone():
        cartera.guardar_equity(conn, hoy)
    congelar_veredicto(conn)
    with db.transaccion(conn):
        db.guardar(conn, "ultimo_cierre", hoy)
    return True


def congelar_veredicto(conn):
    """The verdict is read once: the day it arrives, it's written down and never recomputed."""
    if db.leer(conn, "veredicto") is not None:
        return
    v = cartera.veredicto(conn)
    if not v.get("empezo") or not v["llego"]:
        return
    mc = cartera.metricas(conn, db.cartera_id(conn, "claude"))
    mv = cartera.metricas(conn, db.cartera_id(conn, "vara"))
    texto = "Pasa" if v["pasaria_hoy"] else "No pasa"
    fijo = {"fecha": v["fecha"], "pasa": bool(v["pasaria_hoy"]), "retorno_claude": mc["retorno"], "retorno_vara": mv["retorno"],
            "caida_claude": mc["max_drawdown"], "caida_vara": mv["max_drawdown"]}
    with db.transaccion(conn):
        db.guardar(conn, "veredicto", fijo)
    notify.avisar(f"Mesa: el veredicto — {texto}",
                  f"A los 12 meses Claude hizo {pct(mc['retorno'])} y la vara {pct(mv['retorno'])}; peor caída "
                  f"{pct(-(mc['max_drawdown'] or 0))} contra {pct(-(mv['max_drawdown'] or 0))}.", prioridad=4, tags="scales")


# --- 3. the week ------------------------------------------------------------------------------

def paso_semana(conn, ahora_ms: int) -> bool:
    t = precios.dt_ny(ahora_ms)
    if t.weekday() < 4 or (t.weekday() == 4 and t.time() < CIERRE) or not cartera.empezo(conn):
        return False
    semana = "%d-%02d" % tuple(t.isocalendar()[:2])
    if db.leer(conn, "resumen_semana") == semana:
        return False
    notify.avisar("Mesa: la semana", frase(conn), tags="bar_chart")
    with db.transaccion(conn):
        db.guardar(conn, "resumen_semana", semana)
    return True


# --- 4. Claude didn't come ----------------------------------------------------------------------

def paso_ausencia(conn, ahora_ms: int) -> bool:
    ult = conn.execute("SELECT MAX(ts) FROM decisiones").fetchone()[0]
    desde = ult if ult is not None else db.leer(conn, "creada", ahora_ms)
    if ahora_ms - desde < reglas.numero("dias_sin_decision") * 86400_000:
        return False
    if db.leer(conn, "aviso_ausencia") == desde:
        return False
    dias = int((ahora_ms - desde) // 86400_000)
    notify.avisar("Mesa: Claude no vino", f"Hace {dias} días que no entra ninguna decisión. ¿Está abierta la app de Claude "
                  "en el Mac y la tarea mesa-sesion activa?", prioridad=4, tags="warning")
    with db.transaccion(conn):
        db.guardar(conn, "aviso_ausencia", desde)
    return True


# --- the sentence -----------------------------------------------------------------------------

def frase(conn) -> str:
    """One sentence for Siri, Hoy and Telegram (and the week's summary)."""
    pausa = " La mesa está en pausa." if db.leer(conn, "pausada", False) else ""
    if not cartera.empezo(conn):
        pend = len(cartera.pendientes(conn))
        if pend:
            return (f"La Mesa todavía no empezó: hay {pend} {'orden' if pend == 1 else 'órdenes'} esperando la próxima apertura."
                    + pausa)
        return "La Mesa todavía no empezó: Claude no hizo su primera compra." + pausa
    mc = cartera.metricas(conn, db.cartera_id(conn, "claude"))
    mv = cartera.metricas(conn, db.cartera_id(conn, "vara"))
    if mc["retorno"] is None or mv["retorno"] is None:
        return "La Mesa empezó pero le falta algún precio para valuarse." + pausa
    dif = (mc["retorno"] - mv["retorno"]) * 100
    if abs(dif) < 0.05:
        cmp = "van empatados"
    elif dif > 0:
        cmp = f"Claude le gana por {abs(dif):.1f} puntos".replace(".", ",")
    else:
        cmp = f"Claude pierde por {abs(dif):.1f} puntos".replace(".", ",")
    v = cartera.veredicto(conn)
    fijo = db.leer(conn, "veredicto")
    cola = (f" Veredicto: {'pasa' if fijo['pasa'] else 'no pasa'}." if fijo else
            f" Faltan {v['dias']} días para el veredicto.")
    return (f"Claude va {pct(mc['retorno'])} ({usd(mc['valor'])} USD) y la vara (SPY) {pct(mv['retorno'])}: {cmp}." + cola + pausa)


# --- the loop ---------------------------------------------------------------------------------

def una_pasada():
    conn = db.conectar()
    try:
        ahora = db.ahora_ms()
        with db.transaccion(conn):
            db.guardar(conn, "latido", ahora)
        for nombre, paso in (("órdenes", lambda: paso_ordenes(conn)), ("cierre", lambda: paso_cierre(conn, ahora)),
                             ("semana", lambda: paso_semana(conn, ahora)), ("ausencia", lambda: paso_ausencia(conn, ahora))):
            try:
                paso()
            except Exception:
                log.exception("tareas: falló el paso %s", nombre)
    finally:
        conn.close()


def _arranque(conn):
    """On start: SPY's meta and bars, so /health and the pages have something (once)."""
    if not conn.execute("SELECT 1 FROM tickers WHERE ticker = ? AND error IS NULL", (reglas.VARA,)).fetchone():
        precios.asegurar(conn, reglas.VARA)


def _bucle():
    try:
        conn = db.conectar()
        try:
            _arranque(conn)
        finally:
            conn.close()
    except Exception:
        log.exception("tareas: falló el arranque")
    while not _parar.is_set():
        try:
            una_pasada()
        except Exception:
            log.exception("tareas: falló una pasada")
        _parar.wait(CADA_S)


def arrancar():
    global _hilo
    if not activas() or (_hilo is not None and _hilo.is_alive()):
        return
    _parar.clear()
    _hilo = threading.Thread(target=_bucle, name="mesa-tareas", daemon=True)
    _hilo.start()


def parar():
    _parar.set()
