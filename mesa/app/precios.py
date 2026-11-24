"""Daily bars from Yahoo's chart API (unofficial, no key): download, validate, store.

A bar's date is its New York session; its ts is that session's 09:30 New York open in UTC ms (we
compute it from the date, so a live bar whose Yahoo timestamp is "now" still gets the open). Rule:
never invent a price. A bar with open <= 0, or that moves more than 40 % against the previous close
without a split that day, is not stored and is reported (avisos). If Yahoo doesn't answer, nothing
is stored and orders wait.

The source is injectable (`fuente`): tests put a fake there; production uses Yahoo through httpx,
politely: an honest User-Agent, timeouts, at least a second between requests, and no ticker asked
twice within MIN_REFRESCO unless forced.
"""
import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

import httpx

from . import db

log = logging.getLogger("mesa.precios")
NY = ZoneInfo("America/New_York")
# Says what it is: Yahoo answers plain, identified requests (checked 2026-11-15); no browser disguise.
UA = "mesa/1.0 (cartera en papel de un servidor casero; una consulta cada tanto)"
URL = "https://query1.finance.yahoo.com/v8/finance/chart/{t}"
MIN_REFRESCO_MS = 10 * 60_000


class SinDatos(Exception):
    """Yahoo didn't answer or answered something unusable (the orders wait)."""


class NoExiste(SinDatos):
    """Yahoo says there is no such ticker."""


# --- New York time --------------------------------------------------------------------------

def dt_ny(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, NY)


def fecha_ny(ms: int) -> str:
    return dt_ny(ms).date().isoformat()


def apertura_ms(fecha: str) -> int:
    """The 09:30 New York open of that session, UTC ms."""
    return int(datetime.combine(date.fromisoformat(fecha), dtime(9, 30), NY).timestamp() * 1000)


def cierre_ms(fecha: str) -> int:
    return int(datetime.combine(date.fromisoformat(fecha), dtime(16, 0), NY).timestamp() * 1000)


# --- the source -----------------------------------------------------------------------------

@dataclass
class Serie:
    meta: dict
    velas: list = field(default_factory=list)       # [{fecha, ts, open, high, low, close, volume}], oldest first
    dividendos: list = field(default_factory=list)  # [{fecha, monto}]
    splits: list = field(default_factory=list)      # [{fecha, numerador, denominador}]


def _f(x):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else float(x)


def parsear(datos: dict) -> Serie:
    """Yahoo's chart JSON → Serie. Raises NoExiste/SinDatos."""
    chart = (datos or {}).get("chart") or {}
    if chart.get("error"):
        err = chart["error"]
        if (err.get("code") or "").lower() == "not found":
            raise NoExiste(err.get("description") or "No existe.")
        raise SinDatos(err.get("description") or str(err))
    res = (chart.get("result") or [None])[0]
    if not res or "meta" not in res:
        raise SinDatos("Yahoo contestó sin datos.")
    m = res["meta"]
    meta = {"nombre": m.get("longName") or m.get("shortName"), "tipo": m.get("instrumentType"),
            "bolsa": m.get("exchangeName"), "bolsa_nombre": m.get("fullExchangeName"), "moneda": m.get("currency"),
            "precio": _f(m.get("regularMarketPrice"))}
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    velas = {}
    for i, ts in enumerate(res.get("timestamp") or []):
        def col(k):
            v = q.get(k) or []
            return _f(v[i]) if i < len(v) else None
        o, c = col("open"), col("close")
        if o is None or c is None:
            continue
        fecha = fecha_ny(int(ts) * 1000)
        velas[fecha] = {"fecha": fecha, "ts": apertura_ms(fecha), "open": o, "high": col("high"), "low": col("low"),
                        "close": c, "volume": col("volume")}
    ev = res.get("events") or {}
    divs = [{"fecha": fecha_ny(int(d.get("date", k)) * 1000), "monto": float(d["amount"])}
            for k, d in (ev.get("dividends") or {}).items() if d.get("amount")]
    splits = [{"fecha": fecha_ny(int(s.get("date", k)) * 1000), "numerador": float(s["numerator"]),
               "denominador": float(s["denominator"])}
              for k, s in (ev.get("splits") or {}).items() if s.get("numerator") and s.get("denominator")]
    return Serie(meta=meta, velas=[velas[f] for f in sorted(velas)], dividendos=sorted(divs, key=lambda d: d["fecha"]),
                 splits=sorted(splits, key=lambda s: s["fecha"]))


class Yahoo:
    PAUSA_S = 15 * 60  # after a refusal (429, 5xx, no network), leave Yahoo alone this long

    def __init__(self):
        self._lock = threading.Lock()
        self._ultimo = 0.0
        self._pausa_hasta = 0.0

    def serie(self, ticker: str, rango: str = "1y") -> Serie:
        with self._lock:  # one request at a time, a second apart
            if time.monotonic() < self._pausa_hasta:
                raise SinDatos("Yahoo nos frenó hace poco: espero antes de volver a pedir.")
            espera = 1.0 - (time.monotonic() - self._ultimo)
            if espera > 0:
                time.sleep(espera)
            try:
                r = httpx.get(URL.format(t=ticker), params={"interval": "1d", "range": rango, "events": "div,splits"},
                              headers={"User-Agent": UA, "Accept": "application/json"}, timeout=15)
            except httpx.HTTPError as e:
                self._pausa_hasta = time.monotonic() + self.PAUSA_S
                raise SinDatos(f"Yahoo no contesta: {e.__class__.__name__}") from e
            finally:
                self._ultimo = time.monotonic()
            if r.status_code == 429 or r.status_code >= 500:
                self._pausa_hasta = time.monotonic() + self.PAUSA_S
        if r.status_code == 404:
            try:
                return parsear(r.json())
            except ValueError:
                raise NoExiste("Yahoo no lo conoce.")
        if r.status_code != 200:
            raise SinDatos(f"Yahoo contestó {r.status_code}.")
        try:
            return parsear(r.json())
        except ValueError as e:
            raise SinDatos("Yahoo contestó algo que no es JSON.") from e


fuente = Yahoo()


# --- validate and store ---------------------------------------------------------------------

def validar_velas(serie: Serie, salto_max_pct: float) -> tuple[list, list[str]]:
    """(good bars, problems). Each bar against the previous close of the same download."""
    split_en = {s["fecha"] for s in serie.splits}
    buenas, problemas = [], []
    previo = None
    for v in serie.velas:
        if v["open"] <= 0 or v["close"] <= 0:
            problemas.append(f"{v['fecha']}: apertura o cierre ≤ 0")
            continue
        if previo is not None and v["fecha"] not in split_en:
            salto = abs(v["open"] / previo - 1) * 100
            if salto > salto_max_pct:
                problemas.append(f"{v['fecha']}: abre {salto:.0f} % contra el cierre anterior sin un split")
                previo = v["close"]
                continue
        buenas.append(v)
        previo = v["close"]
    return buenas, problemas


def guardar_serie(conn, ticker: str, serie: Serie) -> list[str]:
    """Store the good bars, the meta, the dividends and splits. Returns the problems found."""
    from . import reglas
    buenas, problemas = validar_velas(serie, reglas.numero("salto_max_pct"))
    m = serie.meta
    with db.transaccion(conn):
        conn.executemany("INSERT INTO precios (ticker, fecha, ts, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?,?) "
                         "ON CONFLICT (ticker, fecha) DO UPDATE SET ts=excluded.ts, open=excluded.open, high=excluded.high, "
                         "low=excluded.low, close=excluded.close, volume=excluded.volume",
                         [(ticker, v["fecha"], v["ts"], v["open"], v["high"], v["low"], v["close"], v["volume"]) for v in buenas])
        conn.execute("INSERT INTO tickers (ticker, nombre, tipo, bolsa, bolsa_nombre, moneda, meta_json, visto_ts, error) "
                     "VALUES (?,?,?,?,?,?,?,?,NULL) ON CONFLICT (ticker) DO UPDATE SET nombre=excluded.nombre, tipo=excluded.tipo, "
                     "bolsa=excluded.bolsa, bolsa_nombre=excluded.bolsa_nombre, moneda=excluded.moneda, "
                     "meta_json=excluded.meta_json, visto_ts=excluded.visto_ts, error=NULL",
                     (ticker, m.get("nombre"), m.get("tipo"), m.get("bolsa"), m.get("bolsa_nombre"), m.get("moneda"),
                      json.dumps(m, ensure_ascii=False), db.ahora_ms()))
        conn.executemany("INSERT OR REPLACE INTO dividendos_yahoo (ticker, fecha, monto) VALUES (?,?,?)",
                         [(ticker, d["fecha"], d["monto"]) for d in serie.dividendos])
        conn.executemany("INSERT OR REPLACE INTO splits_yahoo (ticker, fecha, numerador, denominador) VALUES (?,?,?,?)",
                         [(ticker, s["fecha"], s["numerador"], s["denominador"]) for s in serie.splits])
        db.guardar(conn, "ultima_bajada", db.ahora_ms())
    return problemas


def bajar(conn, tickers, rango: str = "3mo", forzar: bool = False) -> dict:
    """Download and store each ticker (skipping those fetched in the last 10 minutes unless forzar).
    {ticker: None | 'error text'}; problems with bars are noted for the background thread to report."""
    from . import notify
    salida = {}
    for t in dict.fromkeys(tickers):
        f = conn.execute("SELECT visto_ts FROM tickers WHERE ticker = ? AND error IS NULL", (t,)).fetchone()
        if not forzar and f and db.ahora_ms() - f["visto_ts"] < MIN_REFRESCO_MS:
            salida[t] = None
            continue
        try:
            serie = fuente.serie(t, rango)
        except SinDatos as e:
            log.warning("yahoo %s: %s", t, e)
            with db.transaccion(conn):
                conn.execute("UPDATE tickers SET error = ? WHERE ticker = ?", (str(e), t))
            salida[t] = str(e)
            continue
        problemas = guardar_serie(conn, t, serie)
        salida[t] = None
        if problemas:
            avisados = set(db.leer(conn, "velas_malas", []))
            nuevos = [p for p in problemas if f"{t} {p}" not in avisados]
            if nuevos:
                log.warning("velas malas %s: %s", t, nuevos)
                notify.avisar("Mesa: velas raras de Yahoo", f"{t}: " + "; ".join(nuevos) + ". No las uso.", prioridad=4,
                              tags="warning")
                with db.transaccion(conn):
                    db.guardar(conn, "velas_malas", sorted(avisados | {f"{t} {p}" for p in nuevos})[-500:])
    return salida


def asegurar(conn, ticker: str, rango: str = "2y") -> str | None:
    """Make sure there is fresh data for one ticker (for /api/ticker and validating a buy): fetch
    when what's stored is short (two years: the 12-month return and the 200-day mean need ~260 bars),
    else the last three months. Returns the error, if any (NoExiste: 'no existe')."""
    n = conn.execute("SELECT COUNT(*) FROM precios WHERE ticker = ?", (ticker,)).fetchone()[0]
    f = conn.execute("SELECT visto_ts FROM tickers WHERE ticker = ? AND error IS NULL", (ticker,)).fetchone()
    if f and db.ahora_ms() - f["visto_ts"] < MIN_REFRESCO_MS:
        return None
    try:
        serie = fuente.serie(ticker, rango if n < 260 else "3mo")
    except NoExiste:
        return "no existe"
    except SinDatos as e:
        log.warning("yahoo %s: %s", ticker, e)
        return str(e)
    guardar_serie(conn, ticker, serie)
    return None


# --- read -----------------------------------------------------------------------------------

def meta(conn, ticker: str):
    return conn.execute("SELECT * FROM tickers WHERE ticker = ?", (ticker,)).fetchone()


def velas(conn, ticker: str, n: int | None = None, hasta: str | None = None) -> list:
    """The last n bars (oldest first), up to a date if given."""
    q = "SELECT * FROM precios WHERE ticker = ?" + (" AND fecha <= ?" if hasta else "") + " ORDER BY fecha DESC"
    args = [ticker] + ([hasta] if hasta else [])
    if n:
        q += " LIMIT ?"
        args.append(n)
    return list(reversed(conn.execute(q, args).fetchall()))


def ultimo_cierre(conn, ticker: str, hasta: str | None = None) -> float | None:
    v = velas(conn, ticker, 1, hasta)
    return v[0]["close"] if v else None


def primera_vela_desde(conn, ticker: str, desde_ms: int):
    """The first stored bar whose open is at or after desde_ms."""
    return conn.execute("SELECT * FROM precios WHERE ticker = ? AND ts >= ? ORDER BY ts LIMIT 1", (ticker, desde_ms)).fetchone()
