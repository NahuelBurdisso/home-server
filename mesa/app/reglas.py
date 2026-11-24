"""The contract of DISENO.md in code: the numbers (each one an env var with DISENO's default), the
universe, and the limits a decision has to meet. validar() checks a whole decision and says every
reason it fails; it never writes. A decision that breaks any limit is rejected whole.
"""
import math
import os
import re
from datetime import datetime, timedelta

from . import db, precios

# name: (env var, default, what it is, unit). Changing one after the first fill is noted in `cambios`.
NUMEROS = {
    "capital_inicial": ("MESA_CAPITAL_INICIAL", 10000, "Capital inicial de cada cartera", "USD"),
    "deslizamiento_pb": ("MESA_DESLIZAMIENTO_PB", 5, "Deslizamiento en contra, en cada ejecución", "pb"),
    "comision_usd": ("MESA_COMISION_USD", 1, "Comisión por orden ejecutada", "USD"),
    "retencion_dividendos_pct": ("MESA_RETENCION_DIVIDENDOS_PCT", 30, "Retención sobre los dividendos", "%"),
    "max_accion_pct": ("MESA_MAX_ACCION_PCT", 15, "Peso máximo de una acción después de una compra", "%"),
    "max_etf_pct": ("MESA_MAX_ETF_PCT", 50, "Peso máximo de un ETF después de una compra", "%"),
    "max_ordenes": ("MESA_MAX_ORDENES", 6, "Órdenes por decisión, como máximo", "órdenes"),
    "max_decisiones_dia": ("MESA_MAX_DECISIONES_DIA", 1, "Decisiones con órdenes por día (Nueva York)", "decisiones"),
    "max_decisiones_semana": ("MESA_MAX_DECISIONES_SEMANA", 3, "Decisiones con órdenes por semana (lunes a domingo, Nueva York)", "decisiones"),
    "tesis_min_caracteres": ("MESA_TESIS_MIN_CARACTERES", 80, "Largo mínimo de la tesis de una compra", "caracteres"),
    "horizonte_min_meses": ("MESA_HORIZONTE_MIN_MESES", 12, "Horizonte mínimo de una compra", "meses"),
    "venta_anticipada_dias": ("MESA_VENTA_ANTICIPADA_DIAS", 30, "Vender antes de esto exige motivo_venta_anticipada", "días"),
    "precio_min_usd": ("MESA_PRECIO_MIN_USD", 5, "Precio mínimo para entrar al universo", "USD"),
    "volumen_min_usd": ("MESA_VOLUMEN_MIN_USD", 20_000_000, "Mediana de volumen en dólares (20 ruedas) mínima", "USD"),
    "orden_min_usd": ("MESA_ORDEN_MIN_USD", 10, "Una compra que queda por debajo de esto se rechaza", "USD"),
    "ruedas_sin_precio": ("MESA_RUEDAS_SIN_PRECIO", 5, "Ruedas sin precio después de las que una orden se cancela", "ruedas"),
    "salto_max_pct": ("MESA_SALTO_MAX_PCT", 40, "Movimiento contra el cierre anterior (sin split) que invalida una vela", "%"),
    "dias_sin_decision": ("MESA_DIAS_SIN_DECISION", 5, "Días sin decisión después de los que avisa", "días"),
    "veredicto_meses": ("MESA_VEREDICTO_MESES", 12, "El veredicto se lee una vez, a los", "meses"),
    "veredicto_caida_pts": ("MESA_VEREDICTO_CAIDA_PTS", 5, "La peor caída de Claude puede superar la de la vara en", "puntos"),
    "salud_dias": ("MESA_SALUD_DIAS", 4, "/health da 503 si la última bajada buena de Yahoo tiene más de", "días"),
}

VARA = "SPY"
TIPOS = {"EQUITY": "accion", "ETF": "etf"}
# Yahoo's exchangeName codes for NYSE, Nasdaq (Global Select, Global, Capital), NYSE Arca, Cboe BZX, NYSE American.
BOLSAS = {"NYQ": "NYSE", "NMS": "Nasdaq", "NGM": "Nasdaq", "NCM": "Nasdaq", "NAS": "Nasdaq", "PCX": "NYSE Arca",
          "BTS": "Cboe BZX", "ASE": "NYSE American"}
# Leveraged or inverse products, by name (case-insensitive; the short words as whole words).
PROHIBIDOS = [r"\b2x\b", r"\b3x\b", r"leveraged", r"inverse", r"ultrapro", r"proshares ultra", r"direxion daily",
              r"\bbear\b", r"\betn\b"]
TICKER = re.compile(r"^[A-Z][A-Z0-9\-]{0,9}$")
SESIONES = ("lunes", "jueves", "manual")


def n_ar(x: float, dec: int = 2) -> str:
    """es-AR: 1.234,56."""
    return f"{x:,.{dec}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def numero(nombre: str) -> float:
    env, defecto, _, _ = NUMEROS[nombre]
    v = os.environ.get(env)
    if v is None or not v.strip():
        return defecto
    try:
        x = float(v.replace(",", "."))
    except ValueError:
        return defecto
    return int(x) if x.is_integer() else x


def todos() -> dict:
    return {k: numero(k) for k in NUMEROS}


def descripcion() -> list[dict]:
    return [{"nombre": k, "valor": numero(k), "texto": t, "unidad": u, "entorno": e}
            for k, (e, _, t, u) in NUMEROS.items()]


def normalizar(ticker) -> str:
    t = str(ticker or "").strip().upper().replace(".", "-").replace("/", "-")
    return t


# --- the universe -----------------------------------------------------------------------------

def mediana(xs: list[float]) -> float | None:
    xs = sorted(xs)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def universo(conn, ticker: str) -> dict:
    """{en_universo, motivos (why not, in Spanish), tipo ('accion'|'etf'|None), precio, volumen_usd_mediana}
    from what is stored (call precios.asegurar first)."""
    meta = precios.meta(conn, ticker)
    velas = precios.velas(conn, ticker, 20)
    motivos = []
    if meta is None:
        return {"en_universo": False, "motivos": ["No tengo datos de Yahoo de este ticker."], "tipo": None,
                "precio": None, "volumen_usd_mediana": None}
    tipo = TIPOS.get((meta["tipo"] or "").upper())
    if tipo is None:
        motivos.append(f"Es {meta['tipo'] or 'de un tipo desconocido'}: solo acciones comunes (EQUITY) y ETF.")
    if (meta["moneda"] or "").upper() != "USD":
        motivos.append(f"Cotiza en {meta['moneda'] or 'una moneda desconocida'}, no en USD.")
    if (meta["bolsa"] or "").upper() not in BOLSAS:
        motivos.append(f"Cotiza en {meta['bolsa_nombre'] or meta['bolsa'] or 'una bolsa desconocida'}: solo NYSE, Nasdaq, "
                       "NYSE Arca, Cboe BZX o NYSE American.")
    nombre = meta["nombre"] or ""
    for p in PROHIBIDOS:
        if re.search(p, nombre, re.IGNORECASE):
            motivos.append(f"Por el nombre («{nombre}») parece apalancado, inverso o un ETN: no se permite.")
            break
    precio = velas[-1]["close"] if velas else None
    vol = mediana([v["close"] * v["volume"] for v in velas if v["volume"] is not None]) if velas else None
    if precio is None:
        motivos.append("No hay velas recientes: no sé el precio.")
    elif precio < numero("precio_min_usd"):
        motivos.append(f"Vale {n_ar(precio, 2)} USD: el mínimo es {numero('precio_min_usd')} USD.")
    if len(velas) < 20:
        motivos.append(f"Hay {len(velas)} ruedas de historia: hacen falta 20 para medir el volumen.")
    elif vol is None or vol < numero("volumen_min_usd"):
        motivos.append(f"La mediana de volumen de 20 ruedas es {n_ar(0 if vol is None else vol / 1e6, 1)} millones de USD: "
                       f"el mínimo es {n_ar(numero('volumen_min_usd') / 1e6, 0)} millones.")
    return {"en_universo": not motivos, "motivos": motivos, "tipo": tipo, "precio": precio, "volumen_usd_mediana": vol}


def peso_maximo_pct(tipo: str | None) -> float:
    return numero("max_etf_pct") if tipo == "etf" else numero("max_accion_pct")


# --- a whole decision -------------------------------------------------------------------------

def _texto(x) -> str:
    return x.strip() if isinstance(x, str) else ""


def _numero_positivo(x) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0:
        return None
    return float(x)


def _dias(x, campo: str, donde: str, errores: list) -> int | None:
    if x is None:
        return None
    if isinstance(x, bool) or not isinstance(x, int) or not 1 <= x <= 3650:
        errores.append(f"{donde}: {campo} es un número entero de días (1 a 3650).")
        return None
    return x


def semana_ny(ms: int) -> tuple[int, int]:
    return precios.dt_ny(ms).isocalendar()[:2]


def validar(conn, cuerpo, refrescar: bool = True) -> tuple[list[str], dict]:
    """(errores, plan). plan: the decision normalized, ready for cartera.guardar_decision, with each
    order's estimate. Every error is a sentence in Spanish naming the order it is about."""
    from . import cartera
    errores: list[str] = []
    if not isinstance(cuerpo, dict):
        return ["El cuerpo tiene que ser un objeto JSON."], {}
    quien = _texto(cuerpo.get("quien"))
    if not quien:
        errores.append("Falta quien (por ejemplo «claude»).")
    sesion = _texto(cuerpo.get("sesion"))
    if sesion not in SESIONES:
        errores.append("sesion es «lunes», «jueves» o «manual».")
    resumen = _texto(cuerpo.get("resumen"))
    if not resumen:
        errores.append("Falta resumen: qué leíste del mercado, qué hacés y por qué.")
    regimen = cuerpo.get("regimen")
    if regimen is not None and not isinstance(regimen, dict):
        errores.append("regimen es un objeto ({techo_exposicion_pct, accion, notas}).")
        regimen = None
    skills = cuerpo.get("skills_usadas", [])
    if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
        errores.append("skills_usadas es una lista de nombres.")
        skills = []
    lecciones = cuerpo.get("lecciones")
    if lecciones is not None and not isinstance(lecciones, str):
        errores.append("lecciones es texto.")
        lecciones = None
    ordenes = cuerpo.get("ordenes", [])
    if ordenes is None:
        ordenes = []
    if not isinstance(ordenes, list):
        errores.append("ordenes es una lista (vacía si no hacés nada).")
        ordenes = []
    revisiones = cuerpo.get("revisiones", [])
    if revisiones is None:
        revisiones = []
    if not isinstance(revisiones, list):
        errores.append("revisiones es una lista.")
        revisiones = []

    ahora = db.ahora_ms()
    claude = db.cartera_id(conn, "claude")
    n_max = numero("max_ordenes")
    if len(ordenes) > n_max:
        errores.append(f"Son {len(ordenes)} órdenes: el máximo es {n_max} por decisión.")
    if ordenes:
        hoy = precios.fecha_ny(ahora)
        semana = semana_ny(ahora)
        previas = [f["ts"] for f in conn.execute("SELECT ts FROM decisiones WHERE con_ordenes = 1 AND ts >= ?",
                                                 (ahora - 8 * 86400_000,))]
        en_el_dia = sum(1 for t in previas if precios.fecha_ny(t) == hoy)
        en_la_semana = sum(1 for t in previas if semana_ny(t) == semana)
        if en_el_dia >= numero("max_decisiones_dia"):
            errores.append(f"Ya hubo {en_el_dia} decisión con órdenes hoy ({hoy}, Nueva York): el máximo es "
                           f"{numero('max_decisiones_dia')} por día.")
        if en_la_semana >= numero("max_decisiones_semana"):
            errores.append(f"Ya hubo {en_la_semana} decisiones con órdenes esta semana: el máximo es "
                           f"{numero('max_decisiones_semana')}.")

    # The portfolio as it will be after what is already pending, at the last closes.
    estado = cartera.proyeccion(conn, claude)
    valor_total = estado["valor"]
    efectivo = estado["efectivo_libre"]
    tenencias = estado["cantidades"]          # ticker → shares, pending sales already taken out
    valores = dict(estado["valores"])          # ticker → USD at the last close
    slip = numero("deslizamiento_pb") / 10_000
    com = numero("comision_usd")
    plan_ordenes = []
    compras_usd = 0.0
    ventas_usd = 0.0
    tipos = {}
    vistos_venta = set()

    for i, o in enumerate(ordenes, 1):
        donde = f"Orden {i}"
        if not isinstance(o, dict):
            errores.append(f"{donde}: tiene que ser un objeto.")
            continue
        ticker = normalizar(o.get("ticker"))
        if not TICKER.match(ticker):
            errores.append(f"{donde}: ticker «{o.get('ticker')}» no parece un ticker de EE. UU.")
            continue
        donde = f"Orden {i} ({ticker})"
        lado = o.get("lado")
        if lado not in ("compra", "venta"):
            errores.append(f"{donde}: lado es «compra» o «venta».")
            continue
        formas = [k for k in ("monto_usd", "cantidad", "todo") if o.get(k) not in (None, False)]
        if len(formas) != 1:
            errores.append(f"{donde}: va exactamente uno de monto_usd, cantidad o todo.")
            continue
        forma = formas[0]
        monto = cantidad = None
        todo = False
        if forma == "monto_usd":
            monto = _numero_positivo(o.get("monto_usd"))
            if monto is None:
                errores.append(f"{donde}: monto_usd es un número mayor que cero.")
                continue
            monto = round(monto, 2)
        elif forma == "cantidad":
            cantidad = _numero_positivo(o.get("cantidad"))
            if cantidad is None:
                errores.append(f"{donde}: cantidad es un número de acciones mayor que cero.")
                continue
            cantidad = math.floor(cantidad * 1e6 + 1e-6) / 1e6
            if cantidad <= 0:
                errores.append(f"{donde}: cantidad tiene hasta 6 decimales.")
                continue
        else:
            if o.get("todo") is not True:
                errores.append(f"{donde}: todo es true.")
                continue
            if lado == "compra":
                errores.append(f"{donde}: «todo» es solo para vender; una compra va con monto_usd o cantidad.")
                continue
            todo = True
        revisar = _dias(o.get("revisar_en_dias"), "revisar_en_dias", donde, errores)

        if lado == "compra":
            tesis = _texto(o.get("tesis"))
            if len(tesis) < numero("tesis_min_caracteres"):
                errores.append(f"{donde}: la tesis tiene {len(tesis)} caracteres; hacen falta {numero('tesis_min_caracteres')} "
                               "o más (por qué, con números y de dónde salen).")
            h = o.get("horizonte_meses")
            if isinstance(h, bool) or not isinstance(h, (int, float)) or h < numero("horizonte_min_meses"):
                errores.append(f"{donde}: horizonte_meses tiene que ser {numero('horizonte_min_meses')} o más.")
                h = None
            invalida = _texto(o.get("invalida"))
            if not invalida:
                errores.append(f"{donde}: falta invalida (qué tendría que pasar para venderla).")
            fuentes = o.get("fuentes")
            if not isinstance(fuentes, list) or not [f for f in fuentes if _texto(f)]:
                errores.append(f"{donde}: faltan las fuentes (una lista con al menos una).")
                fuentes = []
            fuentes = [_texto(f) for f in fuentes if _texto(f)] if isinstance(fuentes, list) else []
            # The universe, from Yahoo (cached).
            if refrescar:
                precios.asegurar(conn, ticker)
            u = universo(conn, ticker)
            if not u["en_universo"]:
                errores.append(f"{donde}: no está en el universo: " + " ".join(u["motivos"]))
                continue
            tipos[ticker] = u["tipo"]
            px = u["precio"] * (1 + slip)
            estimado = monto if monto is not None else cantidad * px
            if estimado < numero("orden_min_usd"):
                errores.append(f"{donde}: son {n_ar(estimado, 2)} USD; el mínimo es {numero('orden_min_usd')} USD.")
                continue
            compras_usd += estimado
            valores[ticker] = valores.get(ticker, 0.0) + estimado
            plan_ordenes.append({"ticker": ticker, "lado": "compra", "monto_usd": monto, "cantidad": cantidad, "todo": False,
                                 "tesis": tesis, "horizonte_meses": int(h) if h is not None else None, "invalida": invalida,
                                 "fuentes": fuentes, "revisar_en_dias": revisar, "tipo": u["tipo"],
                                 "precio_referencia": u["precio"],
                                 "estimado": {"monto_usd": round(estimado, 2),
                                              "cantidad": round(estimado / px, 6), "precio": round(px, 4),
                                              "comision": com}})
        else:
            motivo = _texto(o.get("motivo"))
            if not motivo:
                errores.append(f"{donde}: una venta lleva su motivo.")
            tiene = tenencias.get(ticker, 0.0)
            if ticker in vistos_venta:
                errores.append(f"{donde}: hay dos ventas de {ticker} en la misma decisión: juntalas en una.")
                continue
            vistos_venta.add(ticker)
            if tiene <= 0:
                errores.append(f"{donde}: no hay {ticker} en la cartera (o ya tiene una venta pendiente): no se vende "
                               "lo que no se tiene.")
                continue
            ultima = cartera.ultima_compra_ts(conn, claude, ticker)
            if ultima is not None and ahora - ultima < numero("venta_anticipada_dias") * 86400_000 \
                    and not _texto(o.get("motivo_venta_anticipada")):
                errores.append(f"{donde}: se compró hace menos de {numero('venta_anticipada_dias')} días: venderla exige "
                               "motivo_venta_anticipada.")
            precio_ult = precios.ultimo_cierre(conn, ticker)
            if monto is not None:
                if precio_ult is None:
                    errores.append(f"{donde}: no tengo precio para convertir monto_usd en acciones.")
                    continue
                cantidad_est = monto / (precio_ult * (1 - slip))
                if cantidad_est > tiene * (1 + 1e-9):
                    errores.append(f"{donde}: {n_ar(monto, 2)} USD son unas {n_ar(cantidad_est, 6)} acciones y hay {n_ar(tiene, 6)}: "
                                   "no se vende lo que no se tiene (usá «todo»).")
                    continue
            elif cantidad is not None:
                if cantidad > tiene + 1e-9:
                    errores.append(f"{donde}: vende {n_ar(cantidad, 6)} y hay {n_ar(tiene, 6)}: no se vende lo que no se tiene.")
                    continue
                cantidad_est = cantidad
            else:
                cantidad_est = tiene
            cantidad_est = min(cantidad_est, tiene)
            ingreso = cantidad_est * (precio_ult or 0) * (1 - slip)
            ventas_usd += max(0.0, ingreso - com)
            valores[ticker] = max(0.0, valores.get(ticker, 0.0) - cantidad_est * (precio_ult or 0))
            cierra = todo or abs(cantidad_est - tiene) < 1e-6
            plan_ordenes.append({"ticker": ticker, "lado": "venta", "monto_usd": monto, "cantidad": cantidad, "todo": todo,
                                 "motivo": motivo, "motivo_venta_anticipada": _texto(o.get("motivo_venta_anticipada")) or None,
                                 "revisar_en_dias": revisar, "cierra": cierra,
                                 "estimado": {"cantidad": round(cantidad_est, 6),
                                              "precio": round((precio_ult or 0) * (1 - slip), 4),
                                              "monto_usd": round(ingreso, 2), "comision": com}})

    # Cash: the buys can't be more than the free cash plus what the sales bring (at the last close).
    disponible = efectivo + ventas_usd
    if compras_usd > disponible + 0.005:
        errores.append(f"Las compras suman {n_ar(compras_usd, 2)} USD y hay {n_ar(disponible, 2)} USD (efectivo libre más lo que "
                       "traen las ventas): no se compra más que el efectivo.")
    # Weights after the decision: a stock can't go past 15 % by a buy, an ETF past 50 %.
    total = valor_total - com * len(plan_ordenes)
    for o in plan_ordenes:
        if o["lado"] != "compra" or total <= 0:
            continue
        peso = valores.get(o["ticker"], 0.0) / total * 100
        maximo = peso_maximo_pct(o["tipo"])
        if peso > maximo + 1e-6:
            errores.append(f"Orden de compra de {o['ticker']}: lo dejaría en {n_ar(peso, 1)} % de la cartera; el máximo para "
                           f"{'un ETF' if o['tipo'] == 'etf' else 'una acción'} es {maximo} %.")
        o["estimado"]["peso_despues_pct"] = round(peso, 2)

    # Reviews: only of something held.
    plan_revisiones = []
    for j, r in enumerate(revisiones, 1):
        if not isinstance(r, dict):
            errores.append(f"Revisión {j}: tiene que ser un objeto.")
            continue
        t = normalizar(r.get("ticker"))
        nota = _texto(r.get("nota"))
        if t not in estado["tenidas"]:
            errores.append(f"Revisión {j} ({t or '¿?'}): no está en la cartera.")
            continue
        if not nota:
            errores.append(f"Revisión {j} ({t}): falta la nota.")
            continue
        plan_revisiones.append({"ticker": t, "nota": nota,
                                "revisar_en_dias": _dias(r.get("revisar_en_dias"), "revisar_en_dias", f"Revisión {j} ({t})", errores)})

    plan = {"quien": quien, "sesion": sesion, "resumen": resumen, "regimen": regimen, "skills_usadas": skills,
            "lecciones": lecciones, "ordenes": plan_ordenes, "revisiones": plan_revisiones,
            "efectivo_libre": round(efectivo, 2), "valor_cartera": round(valor_total, 2)}
    return errores, plan


def revisar_el(dias: int | None, desde_ms: int) -> str | None:
    if not dias:
        return None
    return (precios.dt_ny(desde_ms) + timedelta(days=dias)).date().isoformat()


def proxima_sesion(ahora_ms: int) -> str:
    """The next Monday or Thursday at 8:30 Córdoba (the scheduled task's time), ISO with offset."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Argentina/Cordoba")
    t = datetime.fromtimestamp(ahora_ms / 1000, tz)
    for d in range(0, 8):
        dia = (t + timedelta(days=d)).replace(hour=8, minute=30, second=0, microsecond=0)
        if dia.weekday() in (0, 3) and dia > t:
            return dia.isoformat()
    return t.isoformat()
