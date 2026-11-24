"""The Mesa as tools for the local AI: Open WebUI reads /ia/openapi.json and lets the model answer
"¿cómo va la mesa?", "¿qué tiene Claude?", "¿por qué compró MSFT?" or "¿le gana al S&P?". READ ONLY on
purpose: nothing here pauses or decides (that's the page, the bot and Claude's own sessions). Every
number comes already written (rioplatense, USD), so the model reads and compares and doesn't
calculate. Mounted at /ia in main.py, behind the same owner check.
"""
from fastapi import FastAPI, Query

from . import db, precios, reglas

ia = FastAPI(
    title="Mesa",
    version="2",
    description="La Mesa: una cartera de 10.000 USD de mentira en acciones y ETF de EE. UU. que decide Claude, "
                "contra la vara (SPY comprado y tenido). Los números ya vienen escritos: no los recalcules. Solo se puede mirar.",
    docs_url=None, redoc_url=None, root_path_in_servers=False,
)


def _web():
    from . import main  # late: main mounts this module
    return main


def _usd(x, signo=False):
    w = _web()
    if x is None:
        return None
    return (w.signo(x) if signo else w.num(x)) + " USD"


@ia.get("/estado", operation_id="estado_de_la_mesa",
        summary="Cómo va la Mesa: valor y retorno de Claude y de la vara (SPY), la diferencia, si está en pausa y una frase lista")
def estado():
    w = _web()
    with w.abrir() as conn:
        e = w.estado(conn)
    mc, mv = e["claude"], e["vara"]
    v = e["veredicto"]
    return {"frase": e["frase"], "empezo": e["empezo"], "desde": w.dia(e["desde"]) if e["desde"] else None,
            "valor_claude": _usd(mc["valor"]), "retorno_claude": w.pct(mc["retorno"]),
            "valor_vara": _usd(mv["valor"]), "retorno_vara": w.pct(mv["retorno"]),
            "diferencia": w.pts(e["exceso"]), "efectivo_claude": _usd(mc["efectivo"]),
            "ordenes_pendientes": len(e["pendientes"]), "en_pausa": e["pausada"],
            "dias_para_el_veredicto": v.get("dias") if v.get("empezo") else None,
            "nota": "Es plata de mentira (papel) sobre precios reales de Yahoo."}


@ia.get("/posiciones", operation_id="posiciones_de_claude",
        summary="Lo que tiene la cartera de Claude: peso, resultado y la tesis de cada posición")
def posiciones():
    w = _web()
    with w.abrir() as conn:
        pos = w.cartera.detalle_posiciones(conn, db.cartera_id(conn, "claude"))
    salida = [{"ticker": p["ticker"], "nombre": p["nombre"], "cantidad": w.cant(p["cantidad"]),
               "precio_de_compra_promedio": _usd(p["precio_prom"]), "precio_ahora": _usd(p["precio"]),
               "valor": _usd(p["valor"]), "peso": w.pct(p["peso"], con_signo=False),
               "resultado": _usd(p["pnl"], signo=True), "resultado_pct": w.pct(p["pnl_pct"]),
               "desde": w.dia(precios.fecha_ny(p["abierta_ts"])),
               "por_que_la_tiene": p["tesis"]["texto"] if p["tesis"] else None,
               "que_la_invalida": p["tesis"]["invalida"] if p["tesis"] else None,
               "revisar_el": w.dia(p["tesis"]["revisar_el"]) if p["tesis"] and p["tesis"]["revisar_el"] else None}
              for p in pos]
    return {"cantidad": len(salida), "posiciones": salida,
            "sin_posiciones": "Claude está todo en efectivo." if not salida else None}


@ia.get("/decisiones", operation_id="decisiones_de_claude",
        summary="Las últimas decisiones de Claude (qué leyó del mercado, qué compró o vendió y por qué), con filtro por ticker")
def decisiones(ticker: str | None = Query(None, description="Un ticker (MSFT, VTI...). Sin esto, todas."),
               limite: int = Query(5, description="Cuántas, de la más nueva a la más vieja. Máximo 20.")):
    w = _web()
    with w.abrir() as conn:
        ds = w.ultimas_decisiones(conn, max(1, min(limite, 20)), reglas.normalizar(ticker) if ticker else None)
    salida = []
    for d in ds:
        ords = []
        for o in d["ordenes"]:
            x = {"ticker": o["ticker"], "lado": o["lado"], "estado": o["estado"]}
            if o["estado"] == "ejecutada":
                x["ejecutada"] = f"{w.cant(o['cantidad_ejecutada'])} a {_usd(o['precio'])} el {w.dia(o['fecha_rueda'])}"
            elif o["motivo_rechazo"]:
                x["por_que_no"] = o["motivo_rechazo"]
            ords.append(x)
        salida.append({"cuando": w.hora(d["ts"]), "sesion": d["sesion"], "resumen": d["resumen"], "ordenes": ords,
                       "por_que": [{"ticker": t["ticker"], "tipo": t["tipo"], "texto": t["texto"]} for t in d["tesis"]],
                       "lecciones": d["lecciones"]})
    return {"decisiones": salida} if salida else {"decisiones": [], "nota": "Todavía no hay decisiones."}


@ia.get("/comparacion", operation_id="claude_contra_la_vara",
        summary="Claude contra la vara (SPY): retorno, peor caída, costos, dividendos, rotación y qué diría hoy el veredicto")
def comparacion():
    w = _web()
    with w.abrir() as conn:
        e = w.estado(conn)
    v = e["veredicto"]

    def fila(m):
        return {"valor": _usd(m["valor"]), "retorno": w.pct(m["retorno"]), "peor_caida": w.pct(-(m["max_drawdown"] or 0)),
                "costos_pagados": _usd(m["costos"]), "dividendos_netos": _usd(m["dividendos_netos"]),
                "rotacion_anualizada": w.num(m["rotacion_anualizada"], 2) if m["rotacion_anualizada"] is not None else "todavía no",
                "posiciones_cerradas": m["cerradas"], "cerradas_ganadoras": m["ganadoras"]}
    fijo = e["veredicto_fijo"]
    return {"empezo": e["empezo"], "claude": fila(e["claude"]), "vara": fila(e["vara"]), "diferencia": w.pts(e["exceso"]),
            "veredicto": ("pasa" if fijo["pasa"] else "no pasa") if fijo else None,
            "si_fuera_hoy": (None if not v.get("empezo") or v.get("pasaria_hoy") is None else
                             "pasaría" if v["pasaria_hoy"] else "no pasaría"),
            "nota": "El veredicto se lee una sola vez, a los 12 meses; antes es informativo."}
