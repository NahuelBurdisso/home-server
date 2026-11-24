"""For every test: a fresh mesa.db in a temp dir, the background thread off, a fake Yahoo (no
network), a clock the test moves, and no real notifications (notify.send fails loudly; the tests
that check notifications capture notify.avisar)."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import db, notify, precios

NY = ZoneInfo("America/New_York")


def ms(fecha: str, hh: int = 9, mm: int = 30) -> int:
    """A New York wall time as UTC ms."""
    return int(datetime.combine(date.fromisoformat(fecha), time(hh, mm), NY).timestamp() * 1000)


def sesiones(desde: str, n: int) -> list[str]:
    """n weekdays from desde (no holidays: the fake market opens every weekday)."""
    d, salida = date.fromisoformat(desde), []
    while len(salida) < n:
        if d.weekday() < 5:
            salida.append(d.isoformat())
        d += timedelta(days=1)
    return salida


class Reloj:
    def __init__(self):
        self.t = ms("2026-11-02", 8, 0)

    def __call__(self):
        return self.t

    def ir(self, fecha: str, hh: int = 9, mm: int = 30):
        self.t = ms(fecha, hh, mm)


class Falsa:
    """Yahoo in memory: ticker → meta, bars {fecha: (open, close)}, dividends, splits. Only bars that
    already opened (by the test's clock) are returned, like the real thing."""

    def __init__(self):
        self.series = {}
        self.pedidos = []
        self.caida = False

    def agregar(self, ticker, fechas, precio=100.0, tipo="EQUITY", bolsa="NMS", moneda="USD", nombre=None,
                volumen=1_000_000, precios=None, dividendos=(), splits=()):
        barras = {}
        for f in fechas:
            o, c = (precios or {}).get(f, (precio, precio))
            barras[f] = (o, c)
        self.series[ticker] = {"meta": {"nombre": nombre or f"{ticker} Inc.", "tipo": tipo, "bolsa": bolsa,
                                        "bolsa_nombre": bolsa, "moneda": moneda, "precio": precio},
                               "barras": barras, "volumen": volumen,
                               "dividendos": [{"fecha": f, "monto": m} for f, m in dividendos],
                               "splits": [{"fecha": f, "numerador": a, "denominador": b} for f, a, b in splits]}

    def poner(self, ticker, fecha, open_, close=None):
        self.series[ticker]["barras"][fecha] = (open_, close if close is not None else open_)

    def serie(self, ticker, rango="1y"):
        self.pedidos.append(ticker)
        if self.caida:
            raise precios.SinDatos("Yahoo contestó 429.")
        s = self.series.get(ticker)
        if s is None:
            raise precios.NoExiste("No existe.")
        ahora = db.ahora_ms()
        velas = [{"fecha": f, "ts": precios.apertura_ms(f), "open": o, "high": max(o, c), "low": min(o, c), "close": c,
                  "volume": s["volumen"]} for f, (o, c) in sorted(s["barras"].items()) if precios.apertura_ms(f) <= ahora]
        return precios.Serie(meta=dict(s["meta"]), velas=velas,
                             dividendos=[d for d in s["dividendos"] if precios.apertura_ms(d["fecha"]) <= ahora],
                             splits=[x for x in s["splits"] if precios.apertura_ms(x["fecha"]) <= ahora])


@pytest.fixture(autouse=True)
def entorno(tmp_path, monkeypatch):
    monkeypatch.setenv("MESA_DB", str(tmp_path / "mesa.db"))
    monkeypatch.setenv("MESA_TAREAS", "0")
    monkeypatch.setenv("MESA_NOTIFY", "0")

    def prohibido(*a, **k):
        raise AssertionError("un test intentó mandar un aviso real por ntfy")
    monkeypatch.setattr(notify, "send", prohibido)


@pytest.fixture
def reloj(monkeypatch):
    r = Reloj()
    monkeypatch.setattr(db, "_reloj", r)
    return r


@pytest.fixture
def yahoo(monkeypatch, reloj):
    f = Falsa()
    monkeypatch.setattr(precios, "fuente", f)
    return f


@pytest.fixture
def avisos(monkeypatch):
    lista = []
    monkeypatch.setattr(notify, "avisar", lambda titulo, cuerpo, **k: lista.append((titulo, cuerpo)))
    return lista


@pytest.fixture
def conn(yahoo):
    c = db.conectar()
    yield c
    c.close()
