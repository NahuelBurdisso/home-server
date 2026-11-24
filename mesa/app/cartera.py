"""The paper broker: orders, fills, dividends, splits, value and metrics. The same code for Claude's
portfolio and for the vara (SPY bought and held).

Fills: an order fills at the open of the first session that opens at or after it was created
(and only once that session has been open 30 minutes, so the open is Yahoo's final one). Sales
before buys. Slippage against (buys dearer, sales cheaper), a commission per order, fractional
shares to 6 decimals; a buy that doesn't fit in the cash is cut to what there is, and below the
minimum it is rejected. An order that finds no price in 5 sessions (counted on SPY) is cancelled.
"""
import json
import logging
import math

from . import db, precios, reglas

log = logging.getLogger("mesa.cartera")
EPS = 1e-9
ESPERA_APERTURA_MS = 30 * 60_000


def floor6(x: float) -> float:
    return math.floor(x * 1e6 + 1e-6) / 1e6


def _slip() -> float:
    return reglas.numero("deslizamiento_pb") / 10_000


def carteras(conn) -> dict:
    return {f["clave"]: dict(f) for f in conn.execute("SELECT * FROM carteras")}


def posiciones(conn, cid: int) -> list:
    return conn.execute("SELECT * FROM posiciones WHERE cartera_id = ? ORDER BY ticker", (cid,)).fetchall()


def pendientes(conn, cid: int | None = None) -> list:
    q = "SELECT * FROM ordenes WHERE estado = 'pendiente'" + (" AND cartera_id = ?" if cid else "") + " ORDER BY creada_ts, id"
    return conn.execute(q, (cid,) if cid else ()).fetchall()


def empezo(conn) -> bool:
    return conn.execute("SELECT 1 FROM carteras WHERE inicio_ts IS NOT NULL").fetchone() is not None


def inicio_ts(conn) -> int | None:
    return conn.execute("SELECT MIN(inicio_ts) FROM carteras").fetchone()[0]


# --- what the portfolio will be after what's pending (for validating a decision) ------------

def proyeccion(conn, cid: int) -> dict:
    c = conn.execute("SELECT * FROM carteras WHERE id = ?", (cid,)).fetchone()
    slip, com = _slip(), reglas.numero("comision_usd")
    cantidades, valores, tenidas = {}, {}, set()
    valor = c["efectivo"]
    for p in posiciones(conn, cid):
        px = precios.ultimo_cierre(conn, p["ticker"]) or p["precio_prom"]
        valor += p["cantidad"] * px
        cantidades[p["ticker"]] = p["cantidad"]
        valores[p["ticker"]] = p["cantidad"] * px
        tenidas.add(p["ticker"])
    libre = c["efectivo"]
    for o in pendientes(conn, cid):
        px = precios.ultimo_cierre(conn, o["ticker"]) or 0.0
        if o["lado"] == "compra":
            gasto = o["monto_usd"] if o["monto_usd"] is not None else (o["cantidad"] or 0) * px * (1 + slip)
            libre -= gasto + com
            valores[o["ticker"]] = valores.get(o["ticker"], 0.0) + gasto
        else:
            tiene = cantidades.get(o["ticker"], 0.0)
            q = tiene if o["todo"] else (o["cantidad"] if o["cantidad"] is not None else
                                         (o["monto_usd"] / (px * (1 - slip)) if px else tiene))
            q = min(q, tiene)
            cantidades[o["ticker"]] = tiene - q
            valores[o["ticker"]] = max(0.0, valores.get(o["ticker"], 0.0) - q * px)
    return {"valor": valor, "efectivo": c["efectivo"], "efectivo_libre": max(0.0, libre),
            "cantidades": {t: q for t, q in cantidades.items() if q > EPS}, "valores": valores, "tenidas": tenidas}


def ultima_compra_ts(conn, cid: int, ticker: str) -> int | None:
    """When it was last bought (the fill's open), or a buy still pending (its creation)."""
    f = conn.execute("SELECT MAX(CASE WHEN estado = 'ejecutada' THEN ejecutada_ts ELSE creada_ts END) FROM ordenes "
                     "WHERE cartera_id = ? AND ticker = ? AND lado = 'compra' AND estado IN ('ejecutada', 'pendiente')",
                     (cid, ticker)).fetchone()[0]
    return f


# --- a decision -----------------------------------------------------------------------------

def guardar_decision(conn, plan: dict) -> tuple[int, list]:
    """Write the decision, its orders and theses (inside the caller's transaction). The first decision
    with orders also creates the vara's buy of SPY, with the same creation time."""
    ahora = db.ahora_ms()
    claude, vara = db.cartera_id(conn, "claude"), db.cartera_id(conn, "vara")
    cur = conn.execute("INSERT INTO decisiones (ts, quien, sesion, resumen, regimen_json, skills_json, con_ordenes, lecciones) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (ahora, plan["quien"], plan["sesion"], plan["resumen"],
                        json.dumps(plan["regimen"], ensure_ascii=False) if plan["regimen"] is not None else None,
                        json.dumps(plan["skills_usadas"], ensure_ascii=False), 1 if plan["ordenes"] else 0, plan["lecciones"]))
    did = cur.lastrowid
    tenidas = {p["ticker"] for p in posiciones(conn, claude)} | \
        {o["ticker"] for o in pendientes(conn, claude) if o["lado"] == "compra"}
    salida = []
    orden_ventas = sorted(plan["ordenes"], key=lambda o: o["lado"] != "venta")
    for o in orden_ventas:
        oid = conn.execute("INSERT INTO ordenes (cartera_id, decision_id, ticker, lado, monto_usd, cantidad, todo, creada_ts) "
                           "VALUES (?,?,?,?,?,?,?,?)",
                           (claude, did, o["ticker"], o["lado"], o["monto_usd"], o["cantidad"], 1 if o["todo"] else 0,
                            ahora)).lastrowid
        rev = reglas.revisar_el(o.get("revisar_en_dias"), ahora)
        if o["lado"] == "compra":
            tipo = "refuerzo" if o["ticker"] in tenidas else "apertura"
            tenidas.add(o["ticker"])
            conn.execute("INSERT INTO tesis (cartera_id, ticker, decision_id, ts, tipo, texto, horizonte_meses, invalida, "
                         "fuentes_json, revisar_el) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (claude, o["ticker"], did, ahora, tipo, o["tesis"], o["horizonte_meses"], o["invalida"],
                          json.dumps(o["fuentes"], ensure_ascii=False), rev))
        else:
            texto = o["motivo"] + (f" (venta anticipada: {o['motivo_venta_anticipada']})" if o.get("motivo_venta_anticipada") else "")
            conn.execute("INSERT INTO tesis (cartera_id, ticker, decision_id, ts, tipo, texto, revisar_el) VALUES (?,?,?,?,?,?,?)",
                         (claude, o["ticker"], did, ahora, "cierre" if o["cierra"] else "revision",
                          texto if o["cierra"] else "Venta parcial: " + texto, rev))
        salida.append({"id": oid, "ticker": o["ticker"], "lado": o["lado"], "monto_usd": o["monto_usd"],
                       "cantidad": o["cantidad"], "todo": o["todo"], "estado": "pendiente", "estimado": o["estimado"]})
    for r in plan["revisiones"]:
        conn.execute("INSERT INTO tesis (cartera_id, ticker, decision_id, ts, tipo, texto, revisar_el) VALUES (?,?,?,?,?,?,?)",
                     (claude, r["ticker"], did, ahora, "revision", r["nota"], reglas.revisar_el(r.get("revisar_en_dias"), ahora)))
    if plan["ordenes"] and conn.execute("SELECT 1 FROM ordenes WHERE cartera_id = ?", (vara,)).fetchone() is None:
        efectivo = conn.execute("SELECT efectivo FROM carteras WHERE id = ?", (vara,)).fetchone()[0]
        conn.execute("INSERT INTO ordenes (cartera_id, decision_id, ticker, lado, monto_usd, creada_ts, automatica) "
                     "VALUES (?,?,?,?,?,?,1)", (vara, did, reglas.VARA, "compra", efectivo, ahora))
    return did, salida


# --- fills ----------------------------------------------------------------------------------

def _sesiones_desde(conn, desde_ms: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM precios WHERE ticker = ? AND ts >= ?", (reglas.VARA, desde_ms)).fetchone()[0]


def _cerrar_orden(conn, oid: int, estado: str, motivo: str):
    conn.execute("UPDATE ordenes SET estado = ?, motivo_rechazo = ?, ejecutada_ts = ? WHERE id = ?",
                 (estado, motivo, db.ahora_ms(), oid))


def ejecutar_pendientes(conn) -> list[dict]:
    """Fill (or drop) every pending order that can be: the bars must already be stored. Returns what
    happened, one dict per order touched."""
    hechos = []
    ahora = db.ahora_ms()
    with db.transaccion(conn):
        listas = []
        for o in pendientes(conn):
            v = precios.primera_vela_desde(conn, o["ticker"], o["creada_ts"])
            if v is None:
                n = _sesiones_desde(conn, o["creada_ts"])
                if n >= reglas.numero("ruedas_sin_precio"):
                    motivo = f"{n} ruedas sin precio de {o['ticker']} (¿suspendida o deslistada?): se cancela."
                    _cerrar_orden(conn, o["id"], "cancelada", motivo)
                    hechos.append({**dict(o), "estado": "cancelada", "motivo_rechazo": motivo})
                continue
            if ahora < v["ts"] + ESPERA_APERTURA_MS:
                continue
            listas.append((v["fecha"], 0 if o["lado"] == "venta" else 1, o["id"], o, v))
        for _, _, _, o, v in sorted(listas, key=lambda x: x[:3]):
            hechos.append(_ejecutar(conn, o, v))
    return hechos


def _ejecutar(conn, o, v) -> dict:
    cid, t = o["cartera_id"], o["ticker"]
    c = conn.execute("SELECT * FROM carteras WHERE id = ?", (cid,)).fetchone()
    com, slip = reglas.numero("comision_usd"), _slip()
    p = conn.execute("SELECT * FROM posiciones WHERE cartera_id = ? AND ticker = ?", (cid, t)).fetchone()
    abre = v["open"]
    if o["lado"] == "compra":
        px = abre * (1 + slip)
        gasto = o["monto_usd"] if o["monto_usd"] is not None else o["cantidad"] * px
        if gasto + com > c["efectivo"]:
            gasto = c["efectivo"] - com
        q = floor6(max(0.0, gasto) / px)
        gasto = q * px
        if gasto < reglas.numero("orden_min_usd"):
            motivo = (f"Con el efectivo que había ({reglas.n_ar(c['efectivo'], 2)} USD) la compra quedaba en {reglas.n_ar(max(gasto, 0), 2)} USD: "
                      f"menos de {reglas.numero('orden_min_usd')} USD, se rechaza.")
            _cerrar_orden(conn, o["id"], "rechazada", motivo)
            return {**dict(o), "estado": "rechazada", "motivo_rechazo": motivo}
        efectivo = c["efectivo"] - gasto - com
        if p:
            nueva = p["cantidad"] + q
            prom = (p["cantidad"] * p["precio_prom"] + gasto + com) / nueva
            conn.execute("UPDATE posiciones SET cantidad = ?, precio_prom = ? WHERE cartera_id = ? AND ticker = ?",
                         (nueva, prom, cid, t))
        else:
            conn.execute("INSERT INTO posiciones (cartera_id, ticker, cantidad, precio_prom, abierta_ts) VALUES (?,?,?,?,?)",
                         (cid, t, q, (gasto + com) / q, v["ts"]))
    else:
        px = abre * (1 - slip)
        tiene = p["cantidad"] if p else 0.0
        if tiene <= EPS:
            motivo = f"No había {t} para vender cuando abrió."
            _cerrar_orden(conn, o["id"], "rechazada", motivo)
            return {**dict(o), "estado": "rechazada", "motivo_rechazo": motivo}
        if o["todo"]:
            q = tiene
        elif o["cantidad"] is not None:
            q = min(o["cantidad"], tiene)
        else:
            q = min(floor6(o["monto_usd"] / px), tiene)
        if tiene - q < 1e-6:
            q = tiene
        ingreso = q * px
        efectivo = c["efectivo"] + ingreso - com
        if tiene - q <= EPS:
            conn.execute("DELETE FROM posiciones WHERE cartera_id = ? AND ticker = ?", (cid, t))
        else:
            conn.execute("UPDATE posiciones SET cantidad = ? WHERE cartera_id = ? AND ticker = ?", (tiene - q, cid, t))
    desl = abs(px - abre) * q
    conn.execute("UPDATE carteras SET efectivo = ?, inicio_ts = COALESCE(inicio_ts, ?) WHERE id = ?", (efectivo, v["ts"], cid))
    conn.execute("UPDATE ordenes SET estado = 'ejecutada', ejecutada_ts = ?, fecha_rueda = ?, precio = ?, cantidad_ejecutada = ?, "
                 "comision = ?, deslizamiento_usd = ? WHERE id = ?", (v["ts"], v["fecha"], px, q, com, desl, o["id"]))
    if o["lado"] == "venta" and p and p["cantidad"] - q <= EPS:
        _anotar_cierre(conn, cid, t, p["abierta_ts"], v["ts"])
    return {**dict(o), "estado": "ejecutada", "precio": px, "cantidad_ejecutada": q, "fecha_rueda": v["fecha"],
            "comision": com, "deslizamiento_usd": desl, "apertura": abre}


def _anotar_cierre(conn, cid: int, ticker: str, abierta_ts: int, cerrada_ts: int):
    ords = conn.execute("SELECT * FROM ordenes WHERE cartera_id = ? AND ticker = ? AND estado = 'ejecutada' "
                        "AND ejecutada_ts >= ? AND ejecutada_ts <= ?", (cid, ticker, abierta_ts, cerrada_ts)).fetchall()
    costo = sum(o["cantidad_ejecutada"] * o["precio"] + o["comision"] for o in ords if o["lado"] == "compra")
    ingreso = sum(o["cantidad_ejecutada"] * o["precio"] - o["comision"] for o in ords if o["lado"] == "venta")
    divs = conn.execute("SELECT COALESCE(SUM(neto_usd), 0) FROM eventos WHERE cartera_id = ? AND ticker = ? AND tipo = 'dividendo' "
                        "AND fecha >= ? AND fecha <= ?", (cid, ticker, precios.fecha_ny(abierta_ts),
                                                          precios.fecha_ny(cerrada_ts))).fetchone()[0]
    conn.execute("INSERT INTO cierres (cartera_id, ticker, abierta_ts, cerrada_ts, costo, ingreso, dividendos, resultado) "
                 "VALUES (?,?,?,?,?,?,?,?)", (cid, ticker, abierta_ts, cerrada_ts, costo, ingreso, divs, ingreso + divs - costo))


# --- dividends and splits -------------------------------------------------------------------

def _factor_splits(conn, cid: int, ticker: str, despues_de: str) -> float:
    f = 1.0
    for e in conn.execute("SELECT valor FROM eventos WHERE cartera_id = ? AND ticker = ? AND tipo = 'split' AND fecha > ?",
                          (cid, ticker, despues_de)):
        f *= e["valor"]
    return f


def cantidad_antes_de(conn, cid: int, ticker: str, fecha: str) -> float:
    """Shares held at the close before `fecha` (in today's units): what's held now minus what was
    bought (plus what was sold) at that session's open or later."""
    p = conn.execute("SELECT cantidad FROM posiciones WHERE cartera_id = ? AND ticker = ?", (cid, ticker)).fetchone()
    q = p["cantidad"] if p else 0.0
    for o in conn.execute("SELECT * FROM ordenes WHERE cartera_id = ? AND ticker = ? AND estado = 'ejecutada' AND fecha_rueda >= ?",
                          (cid, ticker, fecha)):
        x = o["cantidad_ejecutada"] * _factor_splits(conn, cid, ticker, o["fecha_rueda"])
        q += -x if o["lado"] == "compra" else x
    return max(0.0, q)


def aplicar_eventos(conn, hasta: str) -> list[dict]:
    """Credit dividends (net of withholding) whose ex-date came, and adjust for splits, in both
    portfolios. The vara reinvests its cash in SPY at the next open. Returns what it applied."""
    hechos = []
    ret = reglas.numero("retencion_dividendos_pct") / 100
    with db.transaccion(conn):
        for c in conn.execute("SELECT * FROM carteras WHERE inicio_ts IS NOT NULL").fetchall():
            cid = c["id"]
            desde = precios.fecha_ny(c["inicio_ts"])
            tickers = {p["ticker"] for p in posiciones(conn, cid)} | \
                {f["ticker"] for f in conn.execute("SELECT DISTINCT ticker FROM ordenes WHERE cartera_id = ? AND estado = 'ejecutada' "
                                                   "AND ejecutada_ts >= ?", (cid, db.ahora_ms() - 20 * 86400_000))}
            eventos = []
            for t in tickers:
                eventos += [("dividendo", t, d["fecha"], d["monto"], None) for d in conn.execute(
                    "SELECT * FROM dividendos_yahoo WHERE ticker = ? AND fecha >= ? AND fecha <= ?", (t, desde, hasta))]
                eventos += [("split", t, s["fecha"], s["numerador"] / s["denominador"], None) for s in conn.execute(
                    "SELECT * FROM splits_yahoo WHERE ticker = ? AND fecha > ? AND fecha <= ?", (t, desde, hasta))]
            for tipo, t, fecha, valor, _ in sorted(eventos, key=lambda e: (e[2], e[0] != "dividendo")):
                if conn.execute("SELECT 1 FROM eventos WHERE cartera_id = ? AND ticker = ? AND fecha = ? AND tipo = ?",
                                (cid, t, fecha, tipo)).fetchone():
                    continue
                if tipo == "dividendo":
                    q = cantidad_antes_de(conn, cid, t, fecha)
                    if q <= EPS:
                        continue
                    neto = q * valor * (1 - ret)
                    conn.execute("UPDATE carteras SET efectivo = efectivo + ? WHERE id = ?", (neto, cid))
                    conn.execute("INSERT INTO eventos (cartera_id, ticker, fecha, tipo, valor, cantidad, neto_usd, ts) "
                                 "VALUES (?,?,?,?,?,?,?,?)", (cid, t, fecha, tipo, valor, q, neto, db.ahora_ms()))
                    hechos.append({"cartera": c["clave"], "tipo": tipo, "ticker": t, "fecha": fecha, "neto": neto})
                else:
                    p = conn.execute("SELECT * FROM posiciones WHERE cartera_id = ? AND ticker = ?", (cid, t)).fetchone()
                    if p is None or precios.fecha_ny(p["abierta_ts"]) >= fecha or valor <= 0:
                        continue
                    conn.execute("UPDATE posiciones SET cantidad = ?, precio_prom = ? WHERE cartera_id = ? AND ticker = ?",
                                 (p["cantidad"] * valor, p["precio_prom"] / valor, cid, t))
                    conn.execute("UPDATE ordenes SET cantidad = cantidad * ? WHERE cartera_id = ? AND ticker = ? AND "
                                 "estado = 'pendiente' AND cantidad IS NOT NULL AND creada_ts < ?",
                                 (valor, cid, t, precios.apertura_ms(fecha)))
                    conn.execute("INSERT INTO eventos (cartera_id, ticker, fecha, tipo, valor, cantidad, neto_usd, ts) "
                                 "VALUES (?,?,?,?,?,?,0,?)", (cid, t, fecha, tipo, valor, p["cantidad"], db.ahora_ms()))
                    hechos.append({"cartera": c["clave"], "tipo": tipo, "ticker": t, "fecha": fecha, "ratio": valor})
        # The vara reinvests: its cash back into SPY at the next open.
        vara = conn.execute("SELECT * FROM carteras WHERE clave = 'vara'").fetchone()
        if vara["inicio_ts"] is not None and vara["efectivo"] >= reglas.numero("orden_min_usd") + reglas.numero("comision_usd") \
                and not pendientes(conn, vara["id"]):
            conn.execute("INSERT INTO ordenes (cartera_id, ticker, lado, monto_usd, creada_ts, automatica) VALUES (?,?,?,?,?,1)",
                         (vara["id"], reglas.VARA, "compra", round(vara["efectivo"] - reglas.numero("comision_usd"), 2),
                          db.ahora_ms()))
    return hechos


# --- value ----------------------------------------------------------------------------------

def valor(conn, cid: int, hasta: str | None = None) -> tuple[float | None, float]:
    """(value at the last close up to `hasta`, cash). None when a holding has no price at all."""
    c = conn.execute("SELECT efectivo FROM carteras WHERE id = ?", (cid,)).fetchone()
    total = c["efectivo"]
    for p in posiciones(conn, cid):
        px = precios.ultimo_cierre(conn, p["ticker"], hasta)
        if px is None:
            return None, c["efectivo"]
        total += p["cantidad"] * px
    return total, c["efectivo"]


def guardar_equity(conn, fecha: str) -> int:
    """The point of each started portfolio at that session's close. Returns how many it wrote."""
    n = 0
    with db.transaccion(conn):
        for c in conn.execute("SELECT * FROM carteras WHERE inicio_ts IS NOT NULL").fetchall():
            if precios.fecha_ny(c["inicio_ts"]) > fecha:
                continue
            v, ef = valor(conn, c["id"], fecha)
            if v is None:
                log.warning("equity %s %s: falta un precio, no lo guardo", c["clave"], fecha)
                continue
            conn.execute("INSERT INTO equity (cartera_id, fecha, valor, efectivo) VALUES (?,?,?,?) "
                         "ON CONFLICT (cartera_id, fecha) DO UPDATE SET valor = excluded.valor, efectivo = excluded.efectivo",
                         (c["id"], fecha, v, ef))
            n += 1
    return n


def curva(conn, cid: int) -> list[tuple[str, float]]:
    return [(f["fecha"], f["valor"]) for f in conn.execute("SELECT fecha, valor FROM equity WHERE cartera_id = ? ORDER BY fecha", (cid,))]


def max_drawdown(valores: list[float]) -> float:
    """The worst fall from a peak, as a fraction ≥ 0."""
    pico, peor = None, 0.0
    for v in valores:
        pico = v if pico is None or v > pico else pico
        if pico and pico > 0:
            peor = max(peor, (pico - v) / pico)
    return peor


def metricas(conn, cid: int) -> dict:
    c = conn.execute("SELECT * FROM carteras WHERE id = ?", (cid,)).fetchone()
    v, ef = valor(conn, cid)
    serie = [x for _, x in curva(conn, cid)]
    if v is not None and c["inicio_ts"] is not None:
        serie = serie + [v]
    ords = conn.execute("SELECT * FROM ordenes WHERE cartera_id = ? AND estado = 'ejecutada'", (cid,)).fetchall()
    compras = sum(o["cantidad_ejecutada"] * o["precio"] for o in ords if o["lado"] == "compra")
    ventas = sum(o["cantidad_ejecutada"] * o["precio"] for o in ords if o["lado"] == "venta")
    comisiones = sum(o["comision"] or 0 for o in ords)
    deslizamiento = sum(o["deslizamiento_usd"] or 0 for o in ords)
    dividendos = conn.execute("SELECT COALESCE(SUM(neto_usd), 0) FROM eventos WHERE cartera_id = ? AND tipo = 'dividendo'",
                              (cid,)).fetchone()[0]
    cierres = conn.execute("SELECT resultado FROM cierres WHERE cartera_id = ?", (cid,)).fetchall()
    dias = None
    rot_anual = None
    rot = None
    if c["inicio_ts"] is not None:
        dias = max(1, (db.ahora_ms() - c["inicio_ts"]) / 86400_000)
        prom = sum(serie) / len(serie) if serie else None
        if prom:
            rot = (compras + ventas) / 2 / prom
            rot_anual = rot * 365 / dias if dias >= 30 else None
    capital = c["capital_inicial"]
    return {"valor": v, "efectivo": ef, "capital": capital,
            "retorno": (v / capital - 1) if v is not None and c["inicio_ts"] is not None else None,
            "max_drawdown": max_drawdown(serie) if serie else None,
            "rotacion": rot, "rotacion_anualizada": rot_anual, "dias": dias,
            "compras_usd": compras, "ventas_usd": ventas,
            "comisiones": comisiones, "deslizamiento": deslizamiento, "costos": comisiones + deslizamiento,
            "dividendos_netos": dividendos, "cerradas": len(cierres),
            "ganadoras": sum(1 for f in cierres if f["resultado"] > 0), "inicio_ts": c["inicio_ts"]}


def tesis_vigente(conn, cid: int, ticker: str) -> dict | None:
    """The last opening/added-to thesis, and the last review date set by any entry."""
    t = conn.execute("SELECT * FROM tesis WHERE cartera_id = ? AND ticker = ? AND tipo IN ('apertura', 'refuerzo') "
                     "ORDER BY ts DESC, id DESC LIMIT 1", (cid, ticker)).fetchone()
    if t is None:
        return None
    rev = conn.execute("SELECT revisar_el FROM tesis WHERE cartera_id = ? AND ticker = ? AND revisar_el IS NOT NULL "
                       "ORDER BY ts DESC, id DESC LIMIT 1", (cid, ticker)).fetchone()
    ult = conn.execute("SELECT * FROM tesis WHERE cartera_id = ? AND ticker = ? AND tipo = 'revision' ORDER BY ts DESC, id DESC LIMIT 1",
                       (cid, ticker)).fetchone()
    return {"texto": t["texto"], "horizonte_meses": t["horizonte_meses"], "invalida": t["invalida"],
            "fuentes": json.loads(t["fuentes_json"] or "[]"), "desde_ts": t["ts"], "decision_id": t["decision_id"],
            "revisar_el": rev["revisar_el"] if rev else None,
            "ultima_revision": {"ts": ult["ts"], "nota": ult["texto"]} if ult else None}


def detalle_posiciones(conn, cid: int) -> list[dict]:
    v, _ = valor(conn, cid)
    salida = []
    for p in posiciones(conn, cid):
        px = precios.ultimo_cierre(conn, p["ticker"])
        val = p["cantidad"] * px if px is not None else None
        m = precios.meta(conn, p["ticker"])
        salida.append({"ticker": p["ticker"], "nombre": m["nombre"] if m else None,
                       "tipo": reglas.TIPOS.get((m["tipo"] or "").upper()) if m else None,
                       "cantidad": p["cantidad"], "precio_prom": p["precio_prom"], "precio": px, "valor": val,
                       "peso": (val / v) if v and val is not None else None,
                       "pnl": (val - p["cantidad"] * p["precio_prom"]) if val is not None else None,
                       "pnl_pct": (px / p["precio_prom"] - 1) if px is not None and p["precio_prom"] else None,
                       "abierta_ts": p["abierta_ts"], "tesis": tesis_vigente(conn, cid, p["ticker"])})
    return sorted(salida, key=lambda x: -(x["valor"] or 0))


def veredicto(conn) -> dict:
    """Days left, and what it would say today (informative until the 12 months)."""
    from datetime import date, timedelta
    ini = inicio_ts(conn)
    if ini is None:
        return {"empezo": False}
    d0 = date.fromisoformat(precios.fecha_ny(ini))
    meses = reglas.numero("veredicto_meses")
    y, m = divmod(d0.month - 1 + int(meses), 12)
    try:
        dia = d0.replace(year=d0.year + y, month=m + 1)
    except ValueError:
        dia = d0.replace(year=d0.year + y, month=m + 2, day=1) - timedelta(days=1)
    hoy = date.fromisoformat(precios.fecha_ny(db.ahora_ms()))
    mc, mv = metricas(conn, db.cartera_id(conn, "claude")), metricas(conn, db.cartera_id(conn, "vara"))
    pasa = None
    if mc["retorno"] is not None and mv["retorno"] is not None:
        pasa = mc["retorno"] >= mv["retorno"] and \
            ((mc["max_drawdown"] or 0) - (mv["max_drawdown"] or 0)) * 100 <= reglas.numero("veredicto_caida_pts")
    return {"empezo": True, "desde": d0.isoformat(), "fecha": dia.isoformat(), "dias": (dia - hoy).days,
            "llego": hoy >= dia, "pasaria_hoy": pasa}
