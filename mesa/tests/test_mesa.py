"""Run inside the container: docker compose run --rm mesa pytest -q
Nothing here touches the network: Yahoo is tests/conftest.py's Falsa, the clock is Reloj."""
import json

import pytest
from fastapi.testclient import TestClient

from app import cartera, db, precios, reglas, tareas
from conftest import ms, sesiones

DIAS = sesiones("2026-01-05", 250)         # 2026-01-05 … mid December
TESIS = "Negocio con márgenes altos y caja neta; crece 10 % anual según el último 10-K y cotiza a 25 veces ganancias."


def mercado(y):
    y.agregar("SPY", DIAS, 500, tipo="ETF", bolsa="PCX", nombre="SPDR S&P 500 ETF Trust", volumen=50_000_000)
    y.agregar("VTI", DIAS, 250, tipo="ETF", bolsa="PCX", nombre="Vanguard Total Stock Market ETF", volumen=3_000_000)
    y.agregar("AAPL", DIAS, 200, nombre="Apple Inc.", volumen=40_000_000)
    y.agregar("MSFT", DIAS, 400, nombre="Microsoft Corporation", volumen=20_000_000)
    y.agregar("TQQQ", DIAS, 60, tipo="ETF", bolsa="NMS", nombre="ProShares UltraPro QQQ", volumen=50_000_000)
    y.agregar("SQQQ", DIAS, 20, tipo="ETF", bolsa="NMS", nombre="ProShares UltraPro Short QQQ", volumen=50_000_000)
    y.agregar("PENNY", DIAS, 3, nombre="Penny Corp", volumen=100_000_000)
    y.agregar("THIN", DIAS, 50, nombre="Thin Corp", volumen=10_000)
    y.agregar("EURO", DIAS, 50, moneda="EUR", nombre="Euro AG", volumen=10_000_000)
    y.agregar("OTCX", DIAS, 50, bolsa="PNK", nombre="Otc Corp", volumen=10_000_000)
    y.agregar("FUND", DIAS, 50, tipo="MUTUALFUND", nombre="Some Fund", volumen=10_000_000)


@pytest.fixture
def m(yahoo):
    mercado(yahoo)
    return yahoo


def compra(ticker, monto=None, cantidad=None, **extra):
    o = {"ticker": ticker, "lado": "compra", "tesis": TESIS, "horizonte_meses": 24,
         "invalida": "Si los márgenes caen dos trimestres seguidos.", "fuentes": ["https://www.sec.gov/10-K"],
         "revisar_en_dias": 90}
    if monto is not None:
        o["monto_usd"] = monto
    if cantidad is not None:
        o["cantidad"] = cantidad
    o.update(extra)
    return o


def venta(ticker, **forma):
    return {"ticker": ticker, "lado": "venta", "motivo": "Pasó lo que invalidaba la tesis.", **(forma or {"todo": True})}


def decision(*ordenes, **extra):
    d = {"quien": "claude", "sesion": "lunes", "resumen": "Prueba.", "regimen": {"techo_exposicion_pct": 80},
         "skills_usadas": ["market-breadth-analyzer"], "ordenes": list(ordenes), "revisiones": []}
    d.update(extra)
    return d


def decidir(conn, cuerpo):
    """Validate and save like the API does; returns (errores, id)."""
    errores, plan = reglas.validar(conn, cuerpo)
    if errores:
        return errores, None
    with db.transaccion(conn):
        did, _ = cartera.guardar_decision(conn, plan)
    return [], did


def pasar(conn, reloj, fecha, hh=10, mm=0):
    """Move the clock and run the fills as the background thread would."""
    reloj.ir(fecha, hh, mm)
    return tareas.paso_ordenes(conn)


def efectivo(conn, clave):
    return conn.execute("SELECT efectivo FROM carteras WHERE clave = ?", (clave,)).fetchone()[0]


def pos(conn, clave, ticker):
    return conn.execute("SELECT p.* FROM posiciones p JOIN carteras c ON c.id = p.cartera_id WHERE c.clave = ? AND ticker = ?",
                        (clave, ticker)).fetchone()


def contar(conn, tabla):
    return conn.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]


# --- prices ----------------------------------------------------------------------------------

def test_parsea_el_json_de_yahoo():
    t1, t2 = ms("2026-11-12") // 1000, ms("2026-11-13", 15, 59) // 1000  # the live bar's ts is "now", not the open
    datos = {"chart": {"result": [{"meta": {"currency": "USD", "instrumentType": "ETF", "exchangeName": "PCX",
                                            "fullExchangeName": "NYSEArca", "longName": "SPDR S&P 500 ETF Trust",
                                            "regularMarketPrice": 760.0},
                                   "timestamp": [t1, t2, t2 + 86400],
                                   "events": {"dividends": {str(t1): {"amount": 1.9, "date": t1}},
                                              "splits": {str(t2): {"date": t2, "numerator": 4, "denominator": 1}}},
                                   "indicators": {"quote": [{"open": [750.0, 755.0, None], "high": [752.0, 761.0, None],
                                                             "low": [748.0, 754.0, None], "close": [751.0, 760.0, None],
                                                             "volume": [1e7, 2e7, None]}]}}], "error": None}}
    s = precios.parsear(datos)
    assert s.meta == {"nombre": "SPDR S&P 500 ETF Trust", "tipo": "ETF", "bolsa": "PCX", "bolsa_nombre": "NYSEArca",
                      "moneda": "USD", "precio": 760.0}
    assert [v["fecha"] for v in s.velas] == ["2026-11-12", "2026-11-13"]   # the empty bar is dropped
    assert s.velas[1]["ts"] == ms("2026-11-13", 9, 30)                      # the session's open, not Yahoo's ts
    assert s.dividendos == [{"fecha": "2026-11-12", "monto": 1.9}]
    assert s.splits == [{"fecha": "2026-11-13", "numerador": 4.0, "denominador": 1.0}]
    with pytest.raises(precios.NoExiste):
        precios.parsear({"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found"}}})


def test_velas_malas_no_se_guardan_y_se_avisa(conn, yahoo, reloj, avisos):
    f = sesiones("2026-10-01", 6)
    yahoo.agregar("ODD", f, 100, precios={f[2]: (150, 150), f[4]: (0, 100)},
                  splits=[])
    yahoo.agregar("SPLT", f, 100, precios={f[3]: (25, 25), f[4]: (25, 25), f[5]: (25, 25)}, splits=[(f[3], 4, 1)])
    reloj.ir("2026-10-20")
    precios.bajar(conn, ["ODD", "SPLT"])
    guardadas = [v["fecha"] for v in precios.velas(conn, "ODD")]
    assert f[2] not in guardadas and f[4] not in guardadas and f[3] in guardadas
    assert len(precios.velas(conn, "SPLT")) == 6        # a 75 % drop on a split day is fine
    assert avisos and "ODD" in avisos[0][1]


# --- fills -------------------------------------------------------------------------------------

def test_ejecuta_en_la_apertura_siguiente_y_nunca_antes(conn, m, reloj):
    reloj.ir("2026-11-03", 11, 0)                 # Tuesday, during the session: today's open already passed
    m.poner("AAPL", "2026-11-03", 210, 205)
    m.poner("AAPL", "2026-11-04", 220, 221)
    assert decidir(conn, decision(compra("AAPL", 1000)))[0] == []
    pasar(conn, reloj, "2026-11-03", 15, 0)
    assert pos(conn, "claude", "AAPL") is None   # not at an open before the order existed
    pasar(conn, reloj, "2026-11-04", 9, 45)       # 15 minutes after the open: still waiting
    assert pos(conn, "claude", "AAPL") is None
    pasar(conn, reloj, "2026-11-04", 10, 0)
    o = conn.execute("SELECT * FROM ordenes WHERE ticker = 'AAPL'").fetchone()
    assert o["estado"] == "ejecutada" and o["fecha_rueda"] == "2026-11-04"
    assert o["precio"] == pytest.approx(220 * 1.0005)
    assert o["cantidad_ejecutada"] == pytest.approx(cartera.floor6(1000 / (220 * 1.0005)))
    assert o["comision"] == 1 and o["deslizamiento_usd"] == pytest.approx(o["cantidad_ejecutada"] * 220 * 0.0005)
    assert efectivo(conn, "claude") == pytest.approx(10000 - o["cantidad_ejecutada"] * o["precio"] - 1)


def test_antes_de_la_apertura_ejecuta_ese_mismo_dia_con_la_vara(conn, m, reloj):
    reloj.ir("2026-11-02", 6, 30)                 # Monday 8:30 Córdoba
    m.poner("SPY", "2026-11-02", 510, 512)
    assert decidir(conn, decision(compra("AAPL", 1000)))[0] == []
    vara = conn.execute("SELECT * FROM ordenes WHERE automatica = 1").fetchall()
    claude = conn.execute("SELECT * FROM ordenes WHERE automatica = 0").fetchone()
    assert len(vara) == 1 and vara[0]["ticker"] == "SPY" and vara[0]["creada_ts"] == claude["creada_ts"]
    pasar(conn, reloj, "2026-11-02", 10, 0)
    assert {o["fecha_rueda"] for o in conn.execute("SELECT * FROM ordenes")} == {"2026-11-02"}
    spy = pos(conn, "vara", "SPY")
    assert spy["cantidad"] == pytest.approx(cartera.floor6(9999 / (510 * 1.0005)))
    assert efectivo(conn, "vara") == pytest.approx(10000 - spy["cantidad"] * 510 * 1.0005 - 1)
    ini = {c["clave"]: c["inicio_ts"] for c in conn.execute("SELECT * FROM carteras")}
    assert ini["claude"] == ini["vara"] == ms("2026-11-02", 9, 30)
    # A second decision with orders doesn't buy the vara again.
    reloj.ir("2026-11-05", 6, 30)
    assert decidir(conn, decision(compra("MSFT", 500), sesion="jueves"))[0] == []
    assert conn.execute("SELECT COUNT(*) FROM ordenes WHERE automatica = 1").fetchone()[0] == 1


def test_ventas_antes_que_compras(conn, m, reloj):
    reloj.ir("2026-11-02", 6, 30)
    assert decidir(conn, decision(compra("VTI", 4900), compra("AAPL", 1400), compra("MSFT", 1400),
                                  compra("SPY", 2000)))[0] == []
    pasar(conn, reloj, "2026-11-02")
    libre = efectivo(conn, "claude")
    # A month later: sell all VTI and buy more SPY than the free cash; it only fits after the sale.
    reloj.ir("2026-12-07", 6, 30)
    monto = round(libre + 2500, 2)
    errores, did = decidir(conn, decision(compra("SPY", monto), venta("VTI")))
    assert errores == [], errores
    pasar(conn, reloj, "2026-12-07")
    ords = conn.execute("SELECT * FROM ordenes WHERE decision_id = ? ORDER BY id", (did,)).fetchall()
    assert all(o["estado"] == "ejecutada" for o in ords)
    spy = next(o for o in ords if o["ticker"] == "SPY")
    assert spy["cantidad_ejecutada"] * spy["precio"] == pytest.approx(monto, abs=0.01)  # not cut: the sale came first
    assert pos(conn, "claude", "VTI") is None


def test_recorta_al_efectivo_y_rechaza_si_queda_poco(conn, m, reloj):
    reloj.ir("2026-11-02", 6, 30)
    assert decidir(conn, decision(compra("VTI", 4900), compra("AAPL", 1400)))[0] == []
    pasar(conn, reloj, "2026-11-02")
    # A buy that doesn't fit at the open is cut to the cash.
    claude = db.cartera_id(conn, "claude")
    ef = efectivo(conn, "claude")
    with db.transaccion(conn):
        conn.execute("INSERT INTO ordenes (cartera_id, ticker, lado, monto_usd, creada_ts) VALUES (?, 'MSFT', 'compra', ?, ?)",
                     (claude, ef + 500, db.ahora_ms()))
    pasar(conn, reloj, "2026-11-03")
    o = conn.execute("SELECT * FROM ordenes WHERE ticker = 'MSFT'").fetchone()
    assert o["estado"] == "ejecutada"
    assert o["cantidad_ejecutada"] * o["precio"] == pytest.approx(ef - 1, abs=0.01)
    assert 0 <= efectivo(conn, "claude") < 0.01
    # With almost nothing left, it is rejected.
    with db.transaccion(conn):
        conn.execute("INSERT INTO ordenes (cartera_id, ticker, lado, monto_usd, creada_ts) VALUES (?, 'AAPL', 'compra', 100, ?)",
                     (claude, db.ahora_ms()))
    pasar(conn, reloj, "2026-11-04")
    o = conn.execute("SELECT * FROM ordenes WHERE ticker = 'AAPL' AND monto_usd = 100").fetchone()
    assert o["estado"] == "rechazada" and "menos de 10" in o["motivo_rechazo"]


def test_sin_precio_en_5_ruedas_se_cancela(conn, m, reloj):
    m.agregar("GONE", [f for f in DIAS if f <= "2026-10-30"], 50, nombre="Gone Corp", volumen=10_000_000)  # last bar 2026-10-30
    reloj.ir("2026-11-02", 6, 30)
    assert decidir(conn, decision(compra("GONE", 500)))[0] == []
    for f in ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]:
        pasar(conn, reloj, f)
        assert conn.execute("SELECT estado FROM ordenes WHERE ticker = 'GONE'").fetchone()[0] == "pendiente"
    pasar(conn, reloj, "2026-11-06")
    o = conn.execute("SELECT * FROM ordenes WHERE ticker = 'GONE'").fetchone()
    assert o["estado"] == "cancelada" and "5 ruedas" in o["motivo_rechazo"]


def test_yahoo_caido_las_ordenes_esperan(conn, m, reloj):
    reloj.ir("2026-11-02", 6, 30)
    assert decidir(conn, decision(compra("AAPL", 500)))[0] == []
    m.caida = True
    pasar(conn, reloj, "2026-11-02")
    assert conn.execute("SELECT COUNT(*) FROM ordenes WHERE estado = 'pendiente'").fetchone()[0] == 2


# --- dividends, splits, the vara -----------------------------------------------------------

def test_dividendos_con_retencion_y_la_vara_reinvierte(conn, m, reloj):
    m.series["AAPL"]["dividendos"] = [{"fecha": "2026-11-06", "monto": 1.0}]
    m.series["SPY"]["dividendos"] = [{"fecha": "2026-11-06", "monto": 2.0}]
    reloj.ir("2026-11-02", 6, 30)
    assert decidir(conn, decision(compra("AAPL", 1000)))[0] == []
    pasar(conn, reloj, "2026-11-02")
    q_aapl, q_spy = pos(conn, "claude", "AAPL")["cantidad"], pos(conn, "vara", "SPY")["cantidad"]
    ef_c, ef_v = efectivo(conn, "claude"), efectivo(conn, "vara")
    reloj.ir("2026-11-06", 17, 30)
    assert tareas.paso_cierre(conn, db.ahora_ms())
    assert efectivo(conn, "claude") == pytest.approx(ef_c + q_aapl * 1.0 * 0.7)
    assert efectivo(conn, "vara") == pytest.approx(ef_v + q_spy * 2.0 * 0.7)
    ev = conn.execute("SELECT * FROM eventos WHERE ticker = 'AAPL'").fetchone()
    assert ev["tipo"] == "dividendo" and ev["neto_usd"] == pytest.approx(q_aapl * 0.7)
    # Only once.
    reloj.ir("2026-11-07", 17, 30)
    tareas.paso_cierre(conn, db.ahora_ms())
    assert contar(conn, "eventos") == 2
    # The vara reinvests at the next open.
    reinv = conn.execute("SELECT * FROM ordenes WHERE automatica = 1 AND estado = 'pendiente'").fetchone()
    assert reinv["ticker"] == "SPY" and reinv["monto_usd"] == pytest.approx(round(efectivo(conn, "vara") - 1, 2))
    pasar(conn, reloj, "2026-11-09")
    assert pos(conn, "vara", "SPY")["cantidad"] > q_spy
    assert efectivo(conn, "vara") < 1


def test_dividendo_solo_a_quien_la_tenia_antes_del_ex(conn, m, reloj):
    m.series["AAPL"]["dividendos"] = [{"fecha": "2026-11-02", "monto": 1.0}]
    reloj.ir("2026-11-02", 6, 30)
    assert decidir(conn, decision(compra("AAPL", 1000)))[0] == []
    pasar(conn, reloj, "2026-11-02")               # bought at the ex-date's open: no dividend
    reloj.ir("2026-11-02", 17, 30)
    tareas.paso_cierre(conn, db.ahora_ms())
    assert conn.execute("SELECT COUNT(*) FROM eventos WHERE ticker = 'AAPL'").fetchone()[0] == 0


def test_split_ajusta_cantidad_y_precio(conn, m, reloj):
    reloj.ir("2026-11-02", 6, 30)
    assert decidir(conn, decision(compra("AAPL", 1000)))[0] == []
    pasar(conn, reloj, "2026-11-02")
    antes = pos(conn, "claude", "AAPL")
    for f in sesiones("2026-11-04", 20):
        m.poner("AAPL", f, 50, 50)
    m.series["AAPL"]["splits"] = [{"fecha": "2026-11-04", "numerador": 4, "denominador": 1}]
    reloj.ir("2026-11-04", 17, 30)
    tareas.paso_cierre(conn, db.ahora_ms())
    despues = pos(conn, "claude", "AAPL")
    assert despues["cantidad"] == pytest.approx(antes["cantidad"] * 4)
    assert despues["precio_prom"] == pytest.approx(antes["precio_prom"] / 4)
    v, _ = cartera.valor(conn, db.cartera_id(conn, "claude"))
    assert v == pytest.approx(efectivo(conn, "claude") + antes["cantidad"] * 4 * 50)
    reloj.ir("2026-11-05", 17, 30)
    tareas.paso_cierre(conn, db.ahora_ms())
    assert pos(conn, "claude", "AAPL")["cantidad"] == pytest.approx(antes["cantidad"] * 4)  # once


def test_equity_y_drawdown(conn, m, reloj):
    assert cartera.max_drawdown([100, 120, 90, 130, 117]) == pytest.approx(0.25)
    assert cartera.max_drawdown([100, 101, 102]) == 0
    reloj.ir("2026-11-02", 6, 30)
    assert decidir(conn, decision(compra("AAPL", 1400)))[0] == []
    pasar(conn, reloj, "2026-11-02")
    for f, px in [("2026-11-02", 200), ("2026-11-03", 240), ("2026-11-04", 180)]:
        m.poner("AAPL", f, 200 if f == "2026-11-02" else px, px)
        reloj.ir(f, 17, 30)
        assert tareas.paso_cierre(conn, db.ahora_ms())
    curva = cartera.curva(conn, db.cartera_id(conn, "claude"))
    assert [f for f, _ in curva] == ["2026-11-02", "2026-11-03", "2026-11-04"]
    vals = [v for _, v in curva]
    assert cartera.metricas(conn, db.cartera_id(conn, "claude"))["max_drawdown"] == pytest.approx((vals[1] - vals[2]) / vals[1])
    # A weekend day writes no point (no SPY bar), and the close runs once per New York day.
    reloj.ir("2026-11-07", 17, 30)
    tareas.paso_cierre(conn, db.ahora_ms())
    assert len(cartera.curva(conn, db.cartera_id(conn, "claude"))) == 3
    assert not tareas.paso_cierre(conn, db.ahora_ms())


def test_cierre_cuenta_las_ganadoras(conn, m, reloj):
    reloj.ir("2026-11-02", 6, 30)
    assert decidir(conn, decision(compra("AAPL", 1000)))[0] == []
    pasar(conn, reloj, "2026-11-02")
    m.poner("AAPL", "2026-12-07", 260, 260)
    reloj.ir("2026-12-07", 6, 30)
    assert decidir(conn, decision(venta("AAPL")))[0] == []
    pasar(conn, reloj, "2026-12-07")
    mc = cartera.metricas(conn, db.cartera_id(conn, "claude"))
    assert mc["cerradas"] == 1 and mc["ganadoras"] == 1 and mc["costos"] > 2


# --- the limits ------------------------------------------------------------------------------

def errores_de(conn, *ordenes, **extra):
    return reglas.validar(conn, decision(*ordenes, **extra))[0]


@pytest.mark.parametrize("ticker, texto", [
    ("TQQQ", "apalancado"), ("SQQQ", "apalancado"), ("PENNY", "mínimo es 5"), ("THIN", "mediana de volumen"),
    ("EURO", "no en USD"), ("OTCX", "solo NYSE"), ("FUND", "solo acciones comunes"), ("NOPE", "No tengo datos"),
])
def test_universo(conn, m, reloj, ticker, texto):
    reloj.ir("2026-11-02", 6, 30)
    e = errores_de(conn, compra(ticker, 100))
    assert len(e) == 1 and "no está en el universo" in e[0] and texto in e[0], e


def test_cada_limite(conn, m, reloj):
    reloj.ir("2026-11-02", 6, 30)
    assert errores_de(conn, *[compra("VTI", 100) for _ in range(7)])[0].startswith("Son 7 órdenes")
    assert "15 %" in " ".join(errores_de(conn, compra("AAPL", 1600)))       # a stock past 15 %
    assert errores_de(conn, compra("AAPL", 1450)) == []
    assert "50 %" in " ".join(errores_de(conn, compra("VTI", 5100)))        # an ETF past 50 %
    assert errores_de(conn, compra("VTI", 4900)) == []
    assert "no se compra más que el efectivo" in " ".join(
        errores_de(conn, compra("VTI", 4900), compra("SPY", 4900), compra("AAPL", 1400)))
    assert "caracteres" in " ".join(errores_de(conn, compra("AAPL", 500, tesis="Corta.")))
    assert "horizonte_meses" in " ".join(errores_de(conn, compra("AAPL", 500, horizonte_meses=6)))
    assert "invalida" in " ".join(errores_de(conn, compra("AAPL", 500, invalida="")))
    assert "fuentes" in " ".join(errores_de(conn, compra("AAPL", 500, fuentes=[])))
    assert "mínimo es 10" in " ".join(errores_de(conn, compra("AAPL", 5)))
    assert "solo para vender" in " ".join(errores_de(conn, {"ticker": "AAPL", "lado": "compra", "todo": True}))
    assert "exactamente uno" in " ".join(errores_de(conn, compra("AAPL", 500, cantidad=2)))
    assert "no se vende lo que no se tiene" in " ".join(errores_de(conn, venta("MSFT")))
    assert "motivo" in " ".join(errores_de(conn, {"ticker": "MSFT", "lado": "venta", "todo": True}))
    assert "sesion" in " ".join(reglas.validar(conn, {**decision(), "sesion": "martes"})[0])
    assert "resumen" in " ".join(reglas.validar(conn, {**decision(), "resumen": ""})[0])
    assert "no está en la cartera" in " ".join(errores_de(conn, revisiones=[{"ticker": "AAPL", "nota": "Sigue."}]))


def test_venta_anticipada_y_cantidades(conn, m, reloj):
    reloj.ir("2026-11-02", 6, 30)
    assert decidir(conn, decision(compra("AAPL", 1000)))[0] == []
    pasar(conn, reloj, "2026-11-02")
    q = pos(conn, "claude", "AAPL")["cantidad"]
    reloj.ir("2026-11-09", 6, 30)
    assert "motivo_venta_anticipada" in " ".join(errores_de(conn, venta("AAPL")))
    assert errores_de(conn, venta("AAPL", todo=True) | {"motivo_venta_anticipada": "Fraude contable publicado."}) == []
    big = venta("AAPL", cantidad=q + 1) | {"motivo_venta_anticipada": "x"}
    assert "no se vende lo que no se tiene" in " ".join(errores_de(conn, big))
    reloj.ir("2026-12-07", 6, 30)
    assert errores_de(conn, venta("AAPL", cantidad=round(q / 2, 6))) == []


def test_una_por_dia_y_tres_por_semana(conn, m, reloj):
    reloj.ir("2026-11-02", 6, 30)                  # Monday
    assert decidir(conn, decision(compra("SPY", 100)))[0] == []
    assert "por día" in " ".join(errores_de(conn, compra("SPY", 100)))
    assert errores_de(conn) == []                   # a decision without orders is always fine
    for f in ["2026-11-03", "2026-11-04"]:
        reloj.ir(f, 6, 30)
        assert decidir(conn, decision(compra("SPY", 100)))[0] == []
    reloj.ir("2026-11-05", 6, 30)
    assert "esta semana" in " ".join(errores_de(conn, compra("SPY", 100)))
    reloj.ir("2026-11-09", 6, 30)                  # next Monday
    assert errores_de(conn, compra("SPY", 100)) == []


def test_una_decision_invalida_no_deja_nada(conn, m, reloj):
    reloj.ir("2026-11-02", 6, 30)
    antes = {t: contar(conn, t) for t in ("decisiones", "ordenes", "tesis")}
    errores, did = decidir(conn, decision(compra("AAPL", 500), compra("TQQQ", 500)))
    assert errores and did is None
    assert {t: contar(conn, t) for t in ("decisiones", "ordenes", "tesis")} == antes


# --- the API -------------------------------------------------------------------------------------

@pytest.fixture
def cliente(m, reloj):
    from app import main
    reloj.ir("2026-11-02", 6, 30)
    with TestClient(main.app) as c:
        yield c


def test_api_decision_valida_simular_y_rechazos(cliente):
    d = decision(compra("AAPL", 1000), compra("VTI", 3000), revisiones=[])
    r = cliente.post("/api/decisiones?simular=1", json=d)
    assert r.status_code == 200 and r.json()["valida"] and r.json()["compra_la_vara"]
    assert len(r.json()["ordenes"]) == 2
    assert cliente.get("/api/decisiones").json()["decisiones"] == []          # simular writes nothing
    assert cliente.get("/api/cartera").json()["pendientes"] == []
    r = cliente.post("/api/decisiones", json=decision(compra("AAPL", 1000), compra("TQQQ", 100)))
    assert r.status_code == 422 and any("TQQQ" in e for e in r.json()["errores"])
    r = cliente.post("/api/decisiones", content=b"no es json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    r = cliente.post("/api/decisiones", json=d)
    assert r.status_code == 201, r.json()
    body = r.json()
    assert body["id"] == 1 and [o["ticker"] for o in body["ordenes"]] == ["AAPL", "VTI"]
    c = cliente.get("/api/cartera").json()
    assert {o["ticker"] for o in c["pendientes"]} == {"AAPL", "VTI", "SPY"} and not c["empezo"]
    assert c["reglas"]["limites"]["peso_max_accion_pct"] == 15
    got = cliente.get("/api/decisiones?ticker=AAPL").json()["decisiones"]
    assert len(got) == 1 and got[0]["tesis"][0]["tipo"] == "apertura"


def test_api_pausa_409(cliente):
    r = cliente.post("/accion/mesa/pausar", json={})
    assert r.status_code == 200 and r.json()["ok"] and "pausa" in r.json()["mensaje"]
    assert cliente.post("/api/decisiones", json=decision(compra("AAPL", 500))).status_code == 409
    assert cliente.post("/api/decisiones?simular=1", json=decision(compra("AAPL", 500))).status_code == 409
    assert cliente.post("/api/decisiones", json=decision()).status_code == 201   # without orders: noted
    assert cliente.get("/api/estado").json()["pausada"] is True
    assert cliente.post("/accion/mesa/reanudar", json={}).json()["ok"]
    assert cliente.post("/api/decisiones", json=decision(compra("AAPL", 500))).status_code == 201


def test_api_lecturas_y_paginas(cliente, reloj):
    assert "todavía no empezó" in cliente.get("/api/frase").text
    e = cliente.get("/api/estado").json()
    assert e["empezo"] is False and e["valor"] is not None
    assert cliente.get("/health").text == "ok"
    t = cliente.get("/api/ticker/AAPL").json()
    assert t["en_universo"] and t["tipo"] == "accion" and t["peso_maximo_pct"] == 15 and t["sma50"] == 200
    assert cliente.get("/api/ticker/VTI").json()["peso_maximo_pct"] == 50
    assert cliente.get("/api/ticker/NOPE").status_code == 404
    assert cliente.get("/api/ticker/TQQQ").json()["en_universo"] is False
    assert "Todavía no empezó" in cliente.get("/").text
    assert cliente.post("/api/decisiones", json=decision(compra("AAPL", 1000))).status_code == 201
    from app import main
    conn = db.conectar()
    reloj.ir("2026-11-02", 10, 0)
    tareas.paso_ordenes(conn)
    reloj.ir("2026-11-02", 17, 30)
    tareas.paso_cierre(conn, db.ahora_ms())
    conn.close()
    assert "Claude va" in cliente.get("/api/frase").text
    for url in ["/", "/reglas", "/ajustes", "/decisiones", "/decisiones/1", "/posiciones/AAPL", "/operaciones",
                "/operaciones?cartera=vara", "/decisiones?ticker=AAPL", "/posiciones/ZZZ", "/decisiones/99"]:
        assert cliente.get(url).status_code == 200, url
    csv = cliente.get("/api/tenencias.csv").text.splitlines()
    assert csv[0] == "symbol,asset_type,quantity,direction,price,currency,cost_basis"
    assert csv[1].startswith("AAPL,equity,") and ",LONG," in csv[1]
    e = cliente.get("/api/estado").json()
    assert e["empezo"] and e["posiciones"][0]["ticker"] == "AAPL" and e["dias_para_veredicto"] > 300
    for ruta in ["/ia/estado", "/ia/posiciones", "/ia/decisiones", "/ia/comparacion", "/ia/openapi.json"]:
        assert cliente.get(ruta).status_code == 200, ruta


def test_health_503_si_yahoo_viejo(cliente, reloj):
    reloj.t += 5 * 86400_000
    r = cliente.get("/health")
    assert r.status_code == 503 and "más de 4 días" in r.text


def test_middleware(cliente, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "ALLOWED", {"duenio@github"})
    assert cliente.get("/api/estado", headers={"X-Forwarded-For": "100.64.0.2"}).status_code == 403
    assert cliente.get("/api/estado", headers={"X-Forwarded-For": "100.64.0.2", "Tailscale-User-Login": "otro@github"}).status_code == 403
    assert cliente.get("/api/estado", headers={"X-Forwarded-For": "100.64.0.2", "Tailscale-User-Login": "duenio@github"}).status_code == 200
    assert cliente.get("/health", headers={"X-Forwarded-For": "100.64.0.2"}).status_code == 200
    r = cliente.post("/accion/mesa/pausar", json={}, headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert cliente.get("/api/estado").json()["pausada"] is False
    assert cliente.get("/api/estado").status_code == 200                   # direct on the Mac


# --- the background thread ----------------------------------------------------------------------

def test_avisos_ejecucion_semana_y_ausencia(conn, m, reloj, avisos):
    reloj.ir("2026-11-02", 6, 30)
    assert decidir(conn, decision(compra("AAPL", 1000)))[0] == []
    pasar(conn, reloj, "2026-11-02")
    assert len(avisos) == 1 and "Compró" in avisos[0][1] and "La vara compró" in avisos[0][1]
    pasar(conn, reloj, "2026-11-02", 10, 20)
    assert len(avisos) == 1                                   # once per decision
    reloj.ir("2026-11-06", 18, 0)
    assert tareas.paso_semana(conn, db.ahora_ms()) and "Claude va" in avisos[-1][1]
    assert not tareas.paso_semana(conn, db.ahora_ms())
    reloj.ir("2026-11-07", 7, 0)                             # 5 days after the decision
    assert tareas.paso_ausencia(conn, db.ahora_ms()) and "no vino" in avisos[-1][0]
    reloj.ir("2026-11-08", 7, 0)
    assert not tareas.paso_ausencia(conn, db.ahora_ms())     # once until one comes


def test_una_pasada_no_rompe(conn, m, reloj, monkeypatch):
    def explota(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(tareas, "paso_ordenes", explota)
    tareas.una_pasada()                                       # logs and goes on
    assert db.leer(conn, "latido") == db.ahora_ms()


def test_cambios_de_reglas_despues_de_empezar(conn, m, reloj, monkeypatch):
    from app import main
    main.anotar_cambios_de_reglas(conn)
    monkeypatch.setenv("MESA_COMISION_USD", "2")
    main.anotar_cambios_de_reglas(conn)                       # before the first fill: not noted
    assert contar(conn, "cambios") == 0
    reloj.ir("2026-11-02", 6, 30)
    assert decidir(conn, decision(compra("AAPL", 1000)))[0] == []
    pasar(conn, reloj, "2026-11-02")
    monkeypatch.setenv("MESA_COMISION_USD", "1")
    main.anotar_cambios_de_reglas(conn)
    c = conn.execute("SELECT * FROM cambios").fetchone()
    assert c["campo"] == "comision_usd" and json.loads(c["antes"]) == 2 and json.loads(c["despues"]) == 1
