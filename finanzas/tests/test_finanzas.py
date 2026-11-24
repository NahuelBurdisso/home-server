"""Run inside the container:  docker compose run --rm finanzas python -m pytest -q"""
from datetime import date

import pytest

from app import correo, db, ledger, mpsync, pagos, reports
from app.importers import bbva_tarjeta, mercadopago, parse_file
from app.money import ars, parse_amount, parse_date, usd


def _no_recordatorios(method, path, body=None):
    raise AssertionError(f"a test reached Recordatorios: {method} {path}")


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("FINANZAS_CLASSIFIER", "none")  # tests never call a real model
    monkeypatch.setenv("FINANZAS_NOTIFY", "0")         # nor send notifications
    monkeypatch.setattr(mpsync, "TOKEN_FILE", str(tmp_path / "no-token"))  # nor talk to Mercado Pago
    monkeypatch.setattr(correo, "SECRET_FILE", str(tmp_path / "no-gmail"))  # nor read the real Gmail
    monkeypatch.delenv("FINANZAS_PAGOS_AVISOS", raising=False)               # nor make reminders
    monkeypatch.setattr(pagos, "request", _no_recordatorios)                  # ...not even by mistake
    c = db.connect()
    # Fixed dollar prices, and no downloads during tests.
    c.executemany("INSERT INTO rates (date, kind, value) VALUES (?, ?, ?)",
                  [("2026-09-01", "oficial", 1500.0), ("2026-09-30", "bolsa", 1550.0)])
    c.execute("INSERT INTO meta (key, value) VALUES ('rates_checked', ?)", (date.today().isoformat(),))
    c.commit()
    return c


def test_amounts():
    assert parse_amount("1.053.632,02") == 105363202
    assert parse_amount("-40.000,00") == -4000000
    assert parse_amount("$ 1,234.56") == 123456
    assert parse_amount("$ -1.234,56") == parse_amount("ARS -1.234,56") == parse_amount("−1.234,56") == -123456
    assert parse_amount("(500)") == -50000
    assert parse_amount("1.234-") == -123400
    assert parse_amount("166,08") == 16608
    assert parse_amount(99.5) == 9950
    assert ars(-123456789) == "−$ 1.234.568"
    assert usd(-12.4) == "−US$ 12"


def test_dates():
    assert parse_date("01-09-2026") == date(2026, 9, 1)
    assert parse_date("29/09/26") == date(2026, 9, 29)
    assert parse_date("2026-09-29T10:00:00") == date(2026, 9, 29)


BBVA_CARD = [
    ["", None, None],
    ["Movimientos del Período"],
    ["Nro. Tarjeta", "Fecha", "Establecimiento", "Cuota", "Importe en $ ", "Importe en U$S"],
    ["", "01/10/2026", "IMPUESTO DE SELLOS  P   $", "/", "500,61", ""],
    ["", "29/09/2026", "ANTHROPIC* CLAUD in1UL4", "/", "", "100,00"],
    ["", "10/09/2026", "MERPAGO*MAVIMA", "/", "227.763,26", ""],
    ["", "15/08/2026", "FRAVEGA", "03/06", "50.000,00", ""],
    ["Total Tarjeta Nro ****1234", "", "", "", "278.263,87", "100,00"],
]


def test_bbva_card():
    rows = bbva_tarjeta.parse_table(BBVA_CARD)
    assert len(rows) == 4
    assert rows[0].amount == -50061 and rows[0].month is None
    assert rows[1].amount is None and rows[1].usd == -10000
    # An installment counts in the statement's month (latest date in the file), not August.
    assert rows[3].description == "FRAVEGA (cuota 3/6)" and rows[3].month == "2026-10"


MP_CSV = """INITIAL_BALANCE;CREDITS;DEBITS;FINAL_BALANCE
323.686,46;2.813.508,58;-2.878.110,76;259.084,28

RELEASE_DATE;TRANSACTION_TYPE;REFERENCE_ID;TRANSACTION_NET_AMOUNT;PARTIAL_BALANCE
01-09-2026;Rendimientos ;1;166,08;323.852,54
01-09-2026;Pago con QR Farmacity;2;-83.829,74;200.022,80
02-09-2026;Reserva por gastos Ahorro;3;-5.000,00;192.926,69
02-09-2026;Reserva por gastos Ahorro;4;-5.000,00;187.926,69
03-09-2026;Ingreso de dinero ;5;520.000,00;714.026,10
17-09-2026;Dinero retirado Ahorro;6;269.904,29;343.698,98
21-09-2026;Transferencia recibida Abril Gazulla;7;765.000,00;973.509,40
27-09-2026;Pago EBANX S.A.;8;-14.800,00;332.796,47
27-09-2026;Devolución de pago EBANX S.A.;8;14.800,00;319.746,47
""".encode()


def test_mercadopago_kinds_and_dedupe(conn):
    name = "account_statement-abc.csv"
    assert mercadopago.detect(name, MP_CSV)
    acc, rows = parse_file(name, MP_CSV, None)
    assert acc == "mercadopago" and len(rows) == 9
    first = ledger.store(conn, name, acc, rows)
    assert first["new"] == 9
    kinds = {r["description"]: r["kind"] for r in conn.execute("SELECT description, kind FROM transactions")}
    assert kinds["Rendimientos"] == "income"
    assert kinds["Reserva por gastos Ahorro"] == "internal"
    assert kinds["Dinero retirado Ahorro"] == "internal"
    assert kinds["Ingreso de dinero"] == "internal"
    assert kinds["Devolución de pago EBANX S.A."] == "expense"
    # Same file again: nothing new, and the two identical savings moves stay two.
    again = ledger.store(conn, name, acc, rows)
    assert again["new"] == 0 and again["dup"] == 9
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE description LIKE 'Reserva%'").fetchone()[0] == 2

    s = reports.summary(conn, "2026-09")
    assert s["income"] == 16608 + 76500000
    assert s["spent"] == 8382974  # the EBANX payment and its refund cancel out
    assert s["left"] == s["income"] - s["spent"]


def test_transfer_between_accounts_cancels_out(conn):
    ledger.store(conn, "mp.csv", "mercadopago", mercadopago.parse("mp.csv", MP_CSV))
    bbva = [ledger.Row(date(2026, 9, 2), "TRANSFERENCIA A CVU PROPIA", -52000000),
            ledger.Row(date(2026, 9, 5), "PAGO TARJETA VISA", -30000000),
            ledger.Row(date(2026, 9, 1), "HABERES ACME SA", 150000000)]
    res = ledger.store(conn, "bbva.csv", "bbva", bbva)
    assert res["internal"] == 1
    kind = dict(conn.execute("SELECT description, kind FROM transactions WHERE account = 'bbva'").fetchall())
    assert kind["TRANSFERENCIA A CVU PROPIA"] == "internal"
    assert kind["PAGO TARJETA VISA"] == "card_payment"
    assert kind["HABERES ACME SA"] == "income"

    # Undoing one half by hand frees the other half.
    tx = conn.execute("SELECT id, matched_id FROM transactions WHERE description = 'TRANSFERENCIA A CVU PROPIA'").fetchone()
    ledger.set_kind(conn, tx["id"], "expense")
    other = conn.execute("SELECT kind, matched_id FROM transactions WHERE id = ?", (tx["matched_id"],)).fetchone()
    assert other["matched_id"] is None and other["kind"] == "internal"  # 'Ingreso de dinero' stays internal by rule


def test_card_dollars_use_official_rate(conn):
    rows = bbva_tarjeta.parse_table(BBVA_CARD)
    ledger.store(conn, "card.xls", "bbva_tarjeta", rows)
    t = conn.execute("SELECT amount, amount_usd FROM transactions WHERE description LIKE 'ANTHROPIC%'").fetchone()
    assert t["amount_usd"] == -10000 and t["amount"] == -15000000  # US$ 100 x 1500
    assert reports.mep_for(conn, "2026-09") == 1550.0


def test_screens_and_access(conn, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, "ALLOWED", {"owner@github"})
    client = TestClient(main.app)
    assert client.get("/mes").status_code == 200            # direct, on this Mac
    assert "Todavía no hay datos" in client.get("/mes").text
    via_serve = {"x-forwarded-for": "100.64.0.20"}
    assert client.get("/mes", headers={**via_serve, "tailscale-user-login": "Owner@GitHub"}).status_code == 200
    assert client.get("/mes", headers={**via_serve, "tailscale-user-login": "guest@github"}).status_code == 403
    assert client.get("/mes", headers=via_serve).status_code == 403   # proxied without identity
    assert client.get("/health", headers=via_serve).status_code == 200

    r = client.post("/cargar", files=[("files", ("account_statement-x.csv", MP_CSV, "text/csv"))],
                    data={"account": ""}, follow_redirects=True)
    assert "9 nuevos" in r.text
    page = client.get("/mes?m=2026-09").text
    # Only Mercado Pago loaded: it says so, and doesn't look like a result.
    assert "Con lo cargado, te quedaron" in page and "Septiembre 2026" in page
    assert "hero partial" in page and "Faltan los gastos de BBVA cuenta y BBVA tarjeta" in page
    assert "Faltan los gastos de alguna cuenta este mes" in client.get("/gastos?m=2026-09").text
    assert client.get("/evolucion?ver=usd").status_code == 200
    assert client.get("/movimientos?m=2026-09&tipo=internal").status_code == 200

    r = client.post("/manual", data={"kind": "income", "when": "2026-09-05", "amount": "1.500.000",
                                     "description": "Sueldo"}, follow_redirects=False)
    assert r.status_code == 303
    assert reports.summary(conn, "2026-09")["income"] == 16608 + 76500000 + 150000000



def test_whose_movement(conn, monkeypatch):
    """Imports are the holder's; what someone writes down is theirs (login, or the bot's X-Quien);
    each one can be changed, and the month and spending can be seen per person."""
    from fastapi.testclient import TestClient
    from app import main, people
    monkeypatch.setenv("FINANZAS_PERSONAS", "Owner@github=nahuel, sofi@gmail.com=Sofía")
    monkeypatch.setattr(main, "ALLOWED", {"owner@github", "sofi@gmail.com"})
    assert people.logins() == {"owner@github": "nahuel", "sofi@gmail.com": "sofia"}
    assert [people.from_name(n) for n in ("Sofi", "Sofía Herrero", "NAHUEL", "Juan", "")] == ["sofia", "sofia", "nahuel", None, None]
    client = TestClient(main.app)
    client.post("/cargar", files=[("files", ("account_statement-x.csv", MP_CSV, "text/csv"))], data={"account": ""})
    assert {r[0] for r in conn.execute("SELECT DISTINCT who FROM transactions")} == {"nahuel"}

    via_serve = {"x-forwarded-for": "100.64.0.20"}
    assert client.post("/api/gasto", content="gasté 8500 en la verdulería",
                       headers={**via_serve, "tailscale-user-login": "Sofi@gmail.com"}).status_code == 200
    r = client.post("/api/gasto", content="gasté 3000 en pan", headers={"X-Quien": "Sof%C3%ADa"})  # the bot
    sofia = conn.execute("SELECT id, who FROM transactions WHERE account = 'efectivo' ORDER BY id").fetchall()
    assert [t["who"] for t in sofia] == ["sofia", "sofia"] and int(r.headers["X-Id"]) == sofia[-1]["id"]
    client.post("/api/gasto", content="gasté 1000 en kiosco")  # nobody said who: the holder
    client.post("/manual", data={"kind": "expense", "when": date.today().isoformat(), "amount": "200.000",
                                 "description": "Alquiler", "quien": "casa"})
    rent = conn.execute("SELECT id, who FROM transactions WHERE description = 'Alquiler'").fetchone()
    assert rent["who"] == "casa"

    month = date.today().isoformat()[:7]
    split = {p["who"]: p["spent"] for p in reports.summary(conn, month)["by_person"]}
    assert split == {"sofia": 1150000, "nahuel": 100000, "casa": 20000000}
    assert reports.summary(conn, month, "sofia")["spent"] == 1150000
    assert sum(it["total"] for it in reports.spending(conn, [month], who="casa")) == 20000000

    client.post(f"/movimientos/{rent['id']}/quien", data={"quien": "sofia"})
    client.post(f"/movimientos/{rent['id']}/quien", data={"quien": "otro"})  # ignored
    assert conn.execute("SELECT who FROM transactions WHERE id = ?", (rent["id"],)).fetchone()[0] == "sofia"
    page = client.get(f"/mes?m={month}").text
    assert "Quién gastó" in page and "Sofía" in page
    assert "Alquiler" in client.get(f"/movimientos?m={month}&quien=sofia").text
    assert "Alquiler" not in client.get(f"/movimientos?m={month}&quien=nahuel").text
    assert client.get(f"/gastos?m={month}&quien=sofia").status_code == 200
    assert "De quién" in client.get(f"/movimientos/{rent['id']}").text


def test_whose_backfill(tmp_path, monkeypatch):
    """A database from before there were two people: everything in it is the holder's."""
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA)
    old.execute("INSERT INTO transactions (account, date, month, description, amount, kind)"
                " VALUES ('efectivo', '2026-09-01', '2026-09', 'Pan', -100, 'expense')")
    old.commit()
    old.close()
    assert db.connect(path).execute("SELECT who FROM transactions").fetchone()[0] == "nahuel"


BBVA_ACCOUNT = [
    [""],
    ["Detalle de Movimientos de Cuenta: CA$ 207-000000/0"],
    ["Fecha", "Concepto", "", "Importe", "Saldo"],
    ["05/09/2026", "Cambio de moneda extranjera", "104 - BANCA MOVIL", "5.215.000,00", "5.336.210,02"],
    ["04/09/2026", "Transferencia inmediata", "100 - BANCA ONLINE", "-520.000,00", "1.391.099,01"],
    ["01/09/2026", "PAGO DE TARJETA VISA Nro:00000000", "104 - BANCA MOVIL", "-3.480.662,23", "1.911.099,01"],
    ["01/09/2026", "PAGO VISA CANCE DEUD USD. Nro:00000000", "104 - BANCA MOVIL", "-338.375,40", "5.391.761,24"],
    ["07/09/2026", "PAGO CON VISA DEBITO 69692006 OP7229", "733 - ", "-8.303,49", "1.382.795,52"],
    ["21/09/2026", "DEBITO DIRECTO", "569 - MEDIOS DE PAGO - SERVICIO", "-163.368,00", "121.287,52"],
    ["01/09/2026", "INTERESES GANADOS", "207 - RAFAELA", "68,87", "5.730.136,64"],
]


def test_bbva_account(conn):
    from app.importers import bbva_cuenta
    rows = bbva_cuenta.parse_table(BBVA_ACCOUNT)
    assert len(rows) == 7
    ledger.store(conn, "mp.csv", "mercadopago", mercadopago.parse("mp.csv", MP_CSV))
    res = ledger.store(conn, "cuenta.xls", "bbva", rows)
    assert res["internal"] == 1   # the 520.000 that arrived in Mercado Pago the day before
    kind = dict(conn.execute("SELECT description, kind FROM transactions WHERE account = 'bbva'").fetchall())
    assert kind["Cambio de moneda extranjera"] == "usd_sell"
    assert kind["PAGO DE TARJETA VISA Nro:00000000"] == "card_payment"
    assert kind["PAGO VISA CANCE DEUD USD. Nro:00000000"] == "card_payment"
    assert kind["PAGO CON VISA DEBITO 69692006 OP7229"] == "expense"
    assert kind["DEBITO DIRECTO"] == "expense"
    s = reports.summary(conn, "2026-09")
    assert s["usd_sold"] == 521500000 and "BBVA cuenta" not in s["missing"]


def test_monthly_dollar_items(conn):
    conn.execute("INSERT INTO rates (date, kind, value) VALUES ('2026-08-31', 'bolsa', 1500.0)")
    ledger.add_recurring(conn, "Sueldo BBVA", 350000, "income", "2026-08")
    ledger.add_recurring(conn, "Alquiler viejos", 250000, "expense", "2026-08")
    ledger.apply_recurring(conn, until="2026-09")
    ledger.apply_recurring(conn, until="2026-09")   # idempotent
    rows = conn.execute("SELECT month, amount, amount_usd, kind FROM transactions ORDER BY month, amount").fetchall()
    assert [tuple(r) for r in rows if r["month"] <= "2026-09"] == [
        ("2026-08", -375000000, -250000, "expense"), ("2026-08", 525000000, 350000, "income"),
        ("2026-09", -387500000, -250000, "expense"), ("2026-09", 542500000, 350000, "income")]
    s = reports.summary(conn, "2026-09")
    assert s["left"] == 542500000 - 387500000   # US$ 1.000 at September's MEP (1550)

    # Deleting one month skips only that month, also on later runs.
    aug = conn.execute("SELECT id FROM transactions WHERE month = '2026-08' AND kind = 'expense'").fetchone()["id"]
    ledger.delete_manual(conn, aug)
    ledger.apply_recurring(conn, until="2026-09")
    assert reports.summary(conn, "2026-08")["spent"] == 0
    assert reports.summary(conn, "2026-09")["spent"] == 387500000


def test_categories_rules_and_keys():
    from app import categories
    assert categories.merchant_key("ANTHROPIC* CLAUD in1UL4LeBjIQr") == "anthropic claud"
    assert categories.merchant_key("PAGO CON VISA DEBITO 69692006 OP9522") == "pago con visa debito"
    assert categories.merchant_key("Devolución de pago EBANX S.A.") == categories.merchant_key("Pago EBANX S.A.")
    expect = {
        "Retiro de dinero GRUPO FARMACITY": "efectivo.retiros",   # cash, not pharmacy
        "Pago con QR Farmacity": "salud.farmacia",
        "PERCEPCIÓN AFIP RG 5463 30%": "impuestos.percepciones",
        "Pago de servicio ARCA": "impuestos.monotributo",
        "Pago Dlo*pedidosya compra": "comida.delivery",
        "CAMINOS DE LAS SI0000000020314": "transporte.peajes",
        "PERSONAL FLOW 1003190383410": "vivienda.internet",
        "DLO*PRIMEVIDEO": "digital.streaming",
        "Pago de suscripción Meli+": "digital.otras",
        "Transferencia enviada Maria Sofia Herrero": "personas.personas",
        "Alquiler a mis viejos": "vivienda.alquiler",
        "MERPAGO*MUNICIPALIDADDECO": "impuestos.municipal",
        "DEBITO DIRECTO": None,
    }
    for desc, cat in expect.items():
        assert categories.seed_category(desc) == cat, desc
    assert set(categories.CATEGORIES) >= {c for c, _ in categories.SEED_RULES}


def _expense(conn, desc, amount, month="2026-09", account="mercadopago"):
    ledger.store(conn, "x.csv", account, [ledger.Row(date.fromisoformat(f"{month}-10"), desc, amount)])


def test_learning_applies_to_merchant_and_future(conn):
    _expense(conn, "Pago con QR La Maquinita Co SA", -220000)
    _expense(conn, "Pago con QR La Maquinita Co SA", -330000, "2026-08")
    _expense(conn, "DEBITO DIRECTO", -1814000)
    pending = ledger.pending_review(conn)
    assert {t["description"] for t in pending} == {"Pago con QR La Maquinita Co SA", "DEBITO DIRECTO"}

    one = pending[-1]["id"] if pending[-1]["description"].startswith("Pago") else pending[0]["id"]
    ledger.set_category(conn, one, "comida.salidas", learn=True)
    cats = {r["month"]: r["category"] for r in conn.execute(
        "SELECT month, category FROM transactions WHERE merchant = 'pago con qr la maquinita co sa'")}
    assert cats == {"2026-08": "comida.salidas", "2026-09": "comida.salidas"}
    _expense(conn, "Pago con QR La Maquinita Co SA", -100000, "2026-10")   # a future import follows the rule
    assert conn.execute("SELECT category_source FROM transactions WHERE month = '2026-10'").fetchone()[0] == "learned"

    # A one-off choice only changes that movement, and survives re-categorizing.
    debit = conn.execute("SELECT id FROM transactions WHERE description = 'DEBITO DIRECTO'").fetchone()["id"]
    ledger.set_category(conn, debit, "salud.prepaga", learn=False)
    ledger.categorize(conn)
    assert conn.execute("SELECT category, category_source FROM transactions WHERE id = ?", (debit,)).fetchone()[:] \
        == ("salud.prepaga", "user")
    assert ledger.pending_review(conn) == []


def test_ai_suggestions_wait_for_review(conn, monkeypatch):
    from app import classifier
    class Fake(classifier.Classifier):
        name = "fake"
        def available(self): return True
        def classify(self, merchants): return [("compras.ropa", 0.95) if "mavima" in m else ("otros.sin", 0.3) for m in merchants]

    class FakeSure(Fake):
        calibrated, auto_accept = True, 0.9

    _expense(conn, "Pago con QR Tienda Mavima", -21999000)
    _expense(conn, "Pago EBANX S.A.", -1480000)
    _expense(conn, "Pago con QR Tienda Mavima", -5000000, "2026-08")

    class Keep:  # suggest_pending closes its connection; keep ours open
        def __getattr__(self, name): return getattr(conn, name)
        def close(self): pass
    monkeypatch.setattr(db, "connect", lambda path=None: Keep())

    classifier.suggest_pending(Fake())
    rows = {r["description"] + r["month"]: (r["category"], r["category_source"]) for r in conn.execute("SELECT * FROM transactions")}
    assert rows["Pago con QR Tienda Mavima2026-09"] == ("compras.ropa", "ai")
    assert rows["Pago EBANX S.A.2026-09"] == ("otros.sin", "ai")
    assert len(ledger.pending_review(conn)) == 3          # an uncalibrated model never skips review

    conn.execute("UPDATE transactions SET category = NULL, category_source = NULL")
    classifier.suggest_pending(FakeSure())
    assert {t["description"] for t in ledger.pending_review(conn)} == {"Pago EBANX S.A."}   # sure answers apply


def test_spending_levels(conn):
    _expense(conn, "Pago Dlo*pedidosya compra", -9000000)
    _expense(conn, "Pago PedidosYa", -3000000)
    _expense(conn, "Pago con QR Farmacity", -4000000)
    _expense(conn, "Devolución de pago PedidosYa", 1000000)
    _expense(conn, "Pago Dlo*pedidosya compra", -6000000, "2026-08")
    assert reports.spending_view(conn, "2026-09", 1, "group", None)["items"][0]["delta"] is None  # August is incomplete
    for acc in ("bbva", "bbva_tarjeta"):   # now August has every account
        _expense(conn, "COMPENSACION DE FONDOS", -100, "2026-08", acc)
        _expense(conn, "COMPENSACION DE FONDOS", -100, "2026-09", acc)
    groups = reports.spending_view(conn, "2026-09", 1, "group", None)
    assert [(g["id"], g["value"]) for g in groups["items"]][:2] == [("comida", 11000000), ("salud", 4000000)]
    comida = groups["items"][0]
    assert comida["share"] == 73 and comida["width"] == 100.0
    assert comida["delta"] == 83          # 110.000 vs 60.000 in August
    subs = reports.spending_view(conn, "2026-09", 1, "sub", "comida")["items"]
    assert [s["id"] for s in subs] == ["comida.delivery"]
    shops = reports.spending_view(conn, "2026-09", 1, "merchant", "comida.delivery")["items"]
    assert {s["id"] for s in shops} == {"pago dlo pedidosya compra", "pago pedidosya"}  # the refund nets out
    avg = reports.spending_view(conn, "2026-09", 3, "group", None)
    assert avg["items"][0]["value"] == (11000000 + 6000000) / 3


def test_category_screens(conn, monkeypatch):
    from fastapi.testclient import TestClient
    from app import classifier, main
    monkeypatch.setattr(classifier, "suggest_pending", lambda *a: None)
    client = TestClient(main.app)
    _expense(conn, "Pago Dlo*pedidosya compra", -9000000)
    _expense(conn, "Pago con QR La Maquinita Co SA", -220000)
    page = client.get("/gastos?m=2026-09").text
    assert "Comida" in page and "En qué se fue" in page
    assert "Delivery" in client.get("/gastos?m=2026-09&g=comida").text
    assert "Movimientos" in client.get("/gastos?m=2026-09&c=comida.delivery&r=3").text
    assert "Pago con QR La Maquinita" in client.get("/revisar").text
    tx = conn.execute("SELECT id FROM transactions WHERE description LIKE '%Maquinita%'").fetchone()["id"]
    assert "Kiosco y almacén" in client.get(f"/movimientos/{tx}").text
    r = client.post(f"/movimientos/{tx}/categoria", data={"category": "comida.salidas", "learn": "1", "back": "//evil.com"},
                    follow_redirects=False)
    assert r.headers["location"] == "/movimientos"     # no redirects off the app
    assert "Todo en orden" in client.get("/revisar").text
    assert "En qué gastaste" in client.get("/mes?m=2026-09").text


def _w(text, x0, top, x1=None):
    return {"text": text, "x0": x0, "x1": x1 if x1 is not None else x0 + 6 * len(text), "top": top}


PDF_PAGE1_TEXT = """Resumen Tarjetas de Crédito
CIERRE ACTUAL VENCIMIENTO ACTUAL SALDO ACTUAL $ SALDO ACTUAL U$S PAGO MÍNIMO $
01-Oct-26 09-Oct-26 1.053.632,02 327,71 311.820,00
CIERRE ANTERIOR VENCIMIENTO ANTERIOR PRÓXIMO CIERRE PRÓXIMO VENCIMIENTO
27-Ago-26 07-Sep-26 29-Oct-26 11-Nov-26"""

HEADER = [_w("FECHA", 62, 94), _w("DESCRIPCIÓN", 114, 94), _w("NRO.", 380, 94), _w("CUPÓN", 403, 94),
          _w("PESOS", 476, 94, 502), _w("DÓLARES", 536, 94, 574)]


def _pdf_pages():
    page1 = {"text": PDF_PAGE1_TEXT, "words": [
        _w("01-Oct-26", 88, 301), _w("09-Oct-26", 180, 301),          # summary box: not movements
        *[{**w, "top": 714} for w in HEADER],
        _w("01-Sep-26", 62, 730), _w("SU", 114, 730), _w("PAGO", 130, 730), _w("EN", 160, 730), _w("PESOS", 175, 730),
        _w("-3.480.662,23", 446, 730, 502),
        _w("01-Sep-26", 62, 743), _w("SU", 114, 743), _w("PAGO", 130, 743), _w("EN", 160, 743), _w("USD", 175, 743),
        _w("-220,44", 543, 743, 574),
    ]}
    page2 = {"text": "", "words": [
        *HEADER,
        _w("09-Sep-26", 62, 188), _w("CAMINOS", 114, 188), _w("DE", 153, 188), _w("LAS", 166, 188), _w("S", 183, 188),
        _w("000000002031450", 190, 189), _w("000001", 400, 188, 432), _w("42.110,28", 462, 188, 502),
        _w("29-Sep-26", 62, 279), _w("ANTHROPIC*", 114, 279), _w("CLAUD", 166, 279), _w("in1UL4LeBUSD", 196, 279),
        _w("100,00", 269, 279, 299), _w("652495", 400, 279, 432), _w("100,00", 546, 279, 574),
        _w("15-Sep-26", 62, 300), _w("FRAVEGA", 114, 300), _w("C.02/06", 170, 300), _w("123456", 400, 300, 432),
        _w("50.000,00", 462, 300, 502),
        _w("TOTAL", 114, 320), _w("CONSUMOS", 143, 320), _w("142.110,28", 457, 320, 502),
        _w("01-Oct-26", 62, 375), _w("DB.RG", 114, 375), _w("5617", 150, 375), _w("30%", 180, 375),
        _w("143.347,39", 457, 375, 502),
        _w("SALDO", 114, 486), _w("ACTUAL", 143, 486), _w("1.053.632,02", 439, 486, 502),
        _w("02-Oct-26", 62, 500), _w("NOT A ROW", 114, 500), _w("1,00", 480, 500, 502),  # after SALDO ACTUAL
    ]}
    return [page1, page2]


def test_card_statement_pdf():
    from app.importers import bbva_tarjeta_pdf as pdf
    st = pdf.parse_pages(_pdf_pages())
    assert (st.close, st.due, st.prev_close, st.next_close) == ("2026-10-01", "2026-10-09", "2026-08-27", "2026-10-29")
    assert (st.total, st.total_usd, st.minimum) == (105363202, 32771, 31182000)
    got = [(r.date.isoformat(), r.description, r.amount, r.usd, r.month) for r in st]
    assert got == [
        ("2026-09-01", "SU PAGO EN PESOS", 348066223, None, None),
        ("2026-09-01", "SU PAGO EN USD", None, 22044, None),
        ("2026-09-09", "CAMINOS DE LAS S 000000002031450", -4211028, None, None),
        ("2026-09-29", "ANTHROPIC* CLAUD in1UL4LeB", None, -10000, None),
        ("2026-09-15", "FRAVEGA C.02/06", -5000000, None, "2026-10"),   # installment: statement month
        ("2026-10-01", "DB.RG 5617 30%", -14334739, None, None),
    ]


def test_statement_replaces_excel_period(conn):
    from app.importers import bbva_tarjeta_pdf as pdf
    ledger.store(conn, "card.xls", "bbva_tarjeta", bbva_tarjeta.parse_table(BBVA_CARD))   # 4 rows, 15/08..01/10
    st = pdf.parse_pages(_pdf_pages())
    res = ledger.store(conn, "20261003.pdf", "bbva_tarjeta", st)
    # The 3 purchases in its window, and the Excel's FRAVEGA installment charged in October (dated
    # 15/08, before the window): the statement brings its own, so October doesn't count it twice.
    assert res["replaced"] == 4 and res["new"] == 6 and res["statement"] == "2026-10-01"
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE description LIKE 'FRAVEGA%' AND month = '2026-10'").fetchone()[0] == 1
    kinds = dict(conn.execute("SELECT description, kind FROM transactions WHERE import_id = (SELECT MAX(id) FROM imports)").fetchall())
    assert kinds["SU PAGO EN PESOS"] == kinds["SU PAGO EN USD"] == "card_payment"
    cats = dict(conn.execute("SELECT description, category FROM transactions").fetchall())
    assert cats["CAMINOS DE LAS S 000000002031450"] == "transporte.peajes"
    assert cats["DB.RG 5617 30%"] == "impuestos.percepciones"
    assert cats["ANTHROPIC* CLAUD in1UL4LeB"] == "digital.software"
    # The same statement again changes nothing; an Excel for the covered period adds nothing.
    assert ledger.store(conn, "again.pdf", "bbva_tarjeta", pdf.parse_pages(_pdf_pages()))["new"] == 0
    assert ledger.store(conn, "card.xls", "bbva_tarjeta", bbva_tarjeta.parse_table(BBVA_CARD))["new"] == 0


def test_notifications(conn):
    from app import notify
    from app.importers import bbva_tarjeta_pdf as pdf
    ledger.store(conn, "mp.csv", "mercadopago", mercadopago.parse("mp.csv", MP_CSV))
    msgs = {m[0]: m for m in notify.due_messages(conn, date(2026, 10, 5))}
    assert "summary:2026-09" not in msgs           # no card data for September yet
    assert "mp:2026-09" not in msgs                # Mercado Pago is there
    ledger.store(conn, "20261003.pdf", "bbva_tarjeta", pdf.parse_pages(_pdf_pages()))
    msgs = {m[0]: m for m in notify.due_messages(conn, date(2026, 10, 5))}
    title, body = msgs["summary:2026-09"][1:3]
    assert title == "Resumen de septiembre" and body.startswith("Septiembre 2026: ") and "Provisorio" not in body
    assert "card:2026-10-29" in {m[0] for m in notify.due_messages(conn, date(2026, 11, 1))}
    assert "mp:2026-10" in {m[0] for m in notify.due_messages(conn, date(2026, 11, 3))}
    assert notify._once(conn, "x") and not notify._once(conn, "x")


def test_shortcut_upload(conn, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    client = TestClient(main.app)
    r = client.post("/api/subir", files=[("files", ("account_statement-x.csv", MP_CSV, "text/csv"))])
    assert r.status_code == 200 and r.text.startswith("Mercado Pago: 9 movimientos nuevos.")
    r = client.post("/api/subir", files=[("files", ("raro.pdf", b"%PDF-1.4 nada", "application/pdf"))])
    assert "Todavía no sé leer este PDF" in r.text
    assert "Compartir" in client.get("/atajo").text


def test_status_api(conn, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    from app.importers import bbva_tarjeta_pdf as pdf

    class Oct5(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 5)
    monkeypatch.setattr(main, "date", Oct5)
    ledger.store(conn, "20261003.pdf", "bbva_tarjeta", pdf.parse_pages(_pdf_pages()))
    ledger.store(conn, "mp.csv", "mercadopago", mercadopago.parse("mp.csv", MP_CSV))
    st = TestClient(main.app).get("/api/estado").json()
    assert st["vencimientos"] == [{"cuenta": "BBVA tarjeta", "cierre": "2026-10-01", "vence": "2026-10-09",
                                   "total": "$ 1.053.632", "total_usd": "US$ 328", "minimo": "$ 311.820"}]
    assert st["mes_anterior"]["mes"] == "2026-09" and st["mes_anterior"]["quedo"].startswith(("$", "−"))
    assert st["para_revisar"] >= 0 and st["mep"] == 1550


# --- quick cash entries (Siri, the local AI) ---------------------------------------------

from app import rapido  # noqa: E402

TODAY = date(2026, 10, 21)  # a Wednesday


@pytest.mark.parametrize("text, kind, day, cents, desc", [
    ("gasté 8500 en la verdulería", "expense", "2026-10-21", 850000, "Verdulería"),
    ("8.500 verdulería", "expense", "2026-10-21", 850000, "Verdulería"),
    ("pagué $3.200 de nafta ayer", "expense", "2026-10-20", 320000, "Nafta"),
    ("12 mil en el super", "expense", "2026-10-21", 1200000, "Super"),
    ("gasté 2,5 mil en el kiosco", "expense", "2026-10-21", 250000, "Kiosco"),
    ("15 lucas de ropa el lunes", "expense", "2026-10-19", 1500000, "Ropa"),
    ("el 15 gasté 3000 en la feria", "expense", "2026-10-15", 300000, "Feria"),
    ("el 25 pagué 3000 de expensas", "expense", "2026-09-25", 300000, "Expensas"),  # the 25th hasn't come yet
    ("20 mil en efectivo en el mecánico", "expense", "2026-10-21", 2000000, "Mecánico"),
    ("cobré 50000 de un trabajo", "income", "2026-10-21", 5000000, "Trabajo"),
    ("compré dólares por 120.000", "usd_buy", "2026-10-21", 12000000, "Compra de dólares"),
    ("mil pesos de pan", "expense", "2026-10-21", 100000, "Pan"),
])
def test_quick_entry(text, kind, day, cents, desc):
    f = rapido.parse(text, TODAY)
    assert (f["kind"], f["date"].isoformat(), f["amount"], f["description"]) == (kind, day, cents, desc)


@pytest.mark.parametrize("text, says", [
    ("gasté 8500 con la tarjeta en el super", "la tarjeta"),
    ("5000 con mercado pago", "Mercado Pago"),
    ("pagué 100 dólares de hotel", "en pesos"),
    ("gasté en la verdulería", "cuánto"),
])
def test_quick_entry_refuses(text, says):
    with pytest.raises(ValueError, match=says):
        rapido.parse(text, TODAY)


def test_quick_entry_saved_and_categorized(conn):
    text, unknown = rapido.write_down(conn, "gasté 4500 en farmacity ayer", TODAY)
    assert text == "Anotado: $ 4.500 en Farmacity, ayer, en efectivo. 💊 Farmacia."
    assert not unknown
    t = conn.execute("SELECT * FROM transactions").fetchone()
    assert (t["account"], t["kind"], t["amount"], t["date"], t["category"]) == \
        ("efectivo", "expense", -450000, "2026-10-20", "salud.farmacia")
    _, unknown = rapido.write_down(conn, "3000 en lo de Pepe", TODAY)
    assert unknown


def test_assistant_tools(conn):
    from fastapi.testclient import TestClient
    from app.main import app
    rapido.write_down(conn, "gasté 4500 en farmacity", TODAY)
    rapido.write_down(conn, "8000 en pedidosya", TODAY)
    client = TestClient(app)
    spec = client.get("/ia/openapi.json").json()
    assert {"resumen_del_mes", "gastos_por_categoria", "buscar_movimientos", "anotar_gasto"} <= \
        {op["operationId"] for path in spec["paths"].values() for op in path.values()}
    s = client.get("/ia/resumen_del_mes").json()
    assert s["mes"] == "2026-10" and s["gastos"] == "$ 12.500"
    c = client.get("/ia/gastos_por_categoria", params={"categoria": "delivery"}).json()
    assert c["categoria"].endswith("Delivery") and c["total_del_periodo"] == "$ 8.000"
    assert client.get("/ia/gastos_por_categoria", params={"categoria": "salud"}).json()["total_del_periodo"] == "$ 4.500"
    assert "error" in client.get("/ia/gastos_por_categoria", params={"categoria": "naves espaciales"}).json()
    b = client.get("/ia/buscar_movimientos", params={"texto": "farma"}).json()
    assert b["cantidad"] == 1 and b["total_salio"] == "$ 4.500"
    r = client.post("/ia/anotar_gasto", json={"texto": "5000 con la tarjeta"}).json()
    assert r["anotado"] is False
    r = client.post("/api/gasto", content="2000 de pan".encode(), headers={"content-type": "text/plain"})
    assert r.status_code == 200 and r.text.startswith("Anotado: $ 2.000 en Pan")
    tx = conn.execute("SELECT description, amount FROM transactions WHERE id = ?", (int(r.headers["X-Id"]),)).fetchone()
    assert tuple(tx) == ("Pan", -200000)


@pytest.mark.parametrize("text, cents, desc", [
    ("compré 2 pizzas por 8500", 850000, "2 pizzas"),
    ("1.500 mil el auto", 150000000, "auto"),
    ("gasté 3 cafés $ 4.200", 420000, "3 cafés"),
])
def test_quick_entry_picks_the_amount(text, cents, desc):
    f = rapido.parse(text, TODAY)
    assert f["amount"] == cents and desc.lower() in f["description"].lower()


def test_installment_not_doubled_by_a_later_excel(conn):
    from app.importers import bbva_tarjeta_pdf as pdf
    ledger.store(conn, "20261003.pdf", "bbva_tarjeta", pdf.parse_pages(_pdf_pages()))
    # The Excel's cuota 3/6 is charged in October, which the statement already covers.
    res = ledger.store(conn, "card.xls", "bbva_tarjeta", bbva_tarjeta.parse_table(BBVA_CARD))
    assert res["new"] == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE description LIKE 'FRAVEGA%'").fetchone()[0] == 1


def test_bad_input_answers_instead_of_500(conn, monkeypatch):
    from fastapi.testclient import TestClient
    from app import asistente, main
    monkeypatch.setattr(main, "ALLOWED", set())
    client = TestClient(main.app)
    ledger.store(conn, "card.xls", "bbva_tarjeta", bbva_tarjeta.parse_table(BBVA_CARD))
    assert client.get("/mes?m=2026-13").status_code == 200
    r = client.post("/manual", data={"kind": "expense", "when": "2026-10-20", "amount": "$", "description": "x"})
    assert r.status_code == 200 and "No entendí el monto" in r.text
    r = client.post("/manual", data={"kind": "expense", "when": "2026-10-20", "amount": "15 mil", "description": "x"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert conn.execute("SELECT amount FROM transactions WHERE description = 'x'").fetchone()[0] == -1500000
    for mes in ("2026-10-01", "octubre", "octubre 2026"):
        assert client.get(f"/ia/resumen_del_mes?mes={mes}").json()["mes"] == "2026-10", mes
    assert "error" in client.get("/ia/resumen_del_mes?mes=cualquiera").json()
    assert asistente._month("2026-9", []) == "2026-09"


def test_inflation_in_todays_pesos(conn, monkeypatch):
    conn.executemany("INSERT INTO inflation (month, pct) VALUES (?, ?)",
                     [("2026-07", 2.0), ("2026-08", 2.0), ("2026-09", 3.0)])
    f, last = reports.inflation_factors(conn, ["2026-07", "2026-09", "2026-10", "2025-01"])
    assert last == "2026-09"
    assert f["2026-07"] == pytest.approx(1.02 * 1.03)  # August's and September's inflation
    assert f["2026-09"] == 1.0
    assert f["2026-10"] == 1.0 and f["2025-01"] is None
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, "ALLOWED", set())
    monkeypatch.setattr(reports, "refresh_rates", lambda conn: None)
    ledger.store(conn, "card.xls", "bbva_tarjeta", bbva_tarjeta.parse_table(BBVA_CARD))
    page = TestClient(main.app).get("/evolucion?ver=real").text
    assert "pesos de septiembre 2026" in page
    summary = TestClient(main.app).get("/ia/resumen_del_mes?mes=2026-09").json()
    assert summary["inflacion_del_mes"] == "3,0%" and "quedo_en_pesos_de_septiembre_2026" in summary
    assert summary["quedo_en_pesos_de_septiembre_2026"] == summary["quedo"]  # its own month: the same pesos


def test_csrf_from_another_site_is_refused(conn, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, "ALLOWED", set())
    client = TestClient(main.app)
    assert client.post("/fijos/999/borrar", headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/fijos/999/borrar", headers={"sec-fetch-site": "same-site"}, follow_redirects=False).status_code == 303


def test_budgets_follow_inflation_and_warn(conn, monkeypatch):
    from app import notify
    ledger.store(conn, "card.xls", "bbva_tarjeta", bbva_tarjeta.parse_table(BBVA_CARD))
    conn.executemany("INSERT INTO inflation (month, pct) VALUES (?, ?)", [("2026-09", 10.0), ("2026-10", 10.0)])
    reports.set_budget(conn, "compras", 100_000_00, "2026-09")  # $100.000 in September's pesos
    assert reports.budgets_for(conn, "2026-10") == {"compras": 110_000_00}  # October's: +10%
    status = reports.budget_status(conn, "2026-10")
    assert status and status[0]["id"] == "compras" and status[0]["limit"] == 110_000_00
    spent = status[0]["spent"]
    reports.set_budget(conn, "compras", spent // 2, "2026-10")
    msgs = {m[0]: m for m in notify.due_messages(conn, date(2026, 10, 25))}
    assert "budget:2026-10:compras" in msgs and msgs["budget:2026-10:compras"][1] == "Te pasaste en compras"
    reports.set_budget(conn, "compras", None, "2026-10")
    assert reports.budget_status(conn, "2026-10") == []


def test_budget_form(conn, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, "ALLOWED", set())
    client = TestClient(main.app)
    ledger.store(conn, "card.xls", "bbva_tarjeta", bbva_tarjeta.parse_table(BBVA_CARD))
    r = client.post("/presupuesto", data={"grupo": "compras", "monto": "200 mil", "m": "2026-10"}, follow_redirects=False)
    assert r.status_code == 303 and reports.budgets_for(conn, "2026-10")["compras"] == 200_000_00
    page = client.get("/gastos?m=2026-10").text
    assert "de $ 200.000" in page
    assert 'value="200000"' in client.get("/gastos?m=2026-10&g=compras").text
    client.post("/presupuesto", data={"grupo": "compras", "monto": "0", "m": "2026-10"})
    assert reports.budgets_for(conn, "2026-10") == {}


def test_balances_follow_movements_after_the_day_counted(conn, monkeypatch):
    reports.set_balances(conn, {"efectivo": 100000000, "mercadopago": 7838000, "bbva": 2100000, "dolares": 200000},
                         "2026-10-23")
    ledger.add_manual(conn, date(2026, 10, 23), "Ya contado", -500000, "expense")     # that day: already in it
    ledger.add_manual(conn, date(2026, 10, 24), "Verdulería", -850000, "expense")
    ledger.store(conn, "bbva.csv", "bbva", [ledger.Row(date(2026, 10, 25), "EXTRACCION CAJERO", -2000000),
                                            ledger.Row(date(2026, 10, 25), "HABERES ACME SA", 50000000)])
    conn.execute("INSERT INTO rates (date, kind, value) VALUES ('2026-10-23', 'bolsa', 1500.0)")
    conn.commit()
    b = reports.balances(conn, "2026-10-26")
    now = {it["account"]: it["now"] for it in b["accounts"]}
    assert now["efectivo"] == 100000000 - 850000 + 2000000  # the withdrawal is cash now
    assert now["bbva"] == 2100000 - 2000000 + 50000000
    assert now["mercadopago"] == 7838000
    assert now["dolares"] == 200000 * 1500 and b["accounts"][-1]["usd"] == 200000
    assert b["total"] == sum(now.values())

    from fastapi.testclient import TestClient
    from app import main
    client = TestClient(main.app)
    page = client.get("/saldos").text
    assert "Entre todo tenés" in page and "US$ 2.000" in page
    # Saving the form as it came changes nothing; a new number starts that account over from today.
    form = {"efectivo": ars(now["efectivo"])[2:], "mercadopago": "78.380", "bbva": "", "dolares": "2.000"}
    assert client.post("/saldos", data=form, follow_redirects=False).status_code == 303
    assert conn.execute("SELECT COUNT(*) FROM balances WHERE date = '2026-10-23'").fetchone()[0] == 4
    client.post("/saldos", data={"dolares": "2.500"})
    row = conn.execute("SELECT date, amount FROM balances WHERE account = 'dolares'").fetchone()
    assert row["amount"] == 250000 and row["date"] == date.today().isoformat()
    assert "No entendí" in client.post("/saldos", data={"bbva": "mucho"}).text
    assert client.get("/api/estado").json()["saldos"]["cuentas"][0]["cuenta"] == "Efectivo"


RELEASE_CSV = """DATE;SOURCE_ID;DESCRIPTION;NET_CREDIT_AMOUNT;NET_DEBIT_AMOUNT;GROSS_AMOUNT;MP_FEE_AMOUNT;TAXES_AMOUNT;PAYMENT_METHOD;TRANSACTION_APPROVAL_DATE;BUSINESS_UNIT;SUB_UNIT;BALANCE_AMOUNT;PAYMENT_METHOD_TYPE;PURCHASE_ID
2026-10-01T00:00:00.000-03:00;;;100000.00;0.00;100000.00;0.00;0.00;;;;;100000.00;;
2026-10-01T00:50:05.000-03:00;11;asset_management;166.08;0.00;166.08;0.00;0.00;available_money;;;;100166.08;;
2026-10-01T17:54:34.000-03:00;12;reserve_for_payout;0.00;40000.00;-40000.00;0.00;0.00;available_money;;;;60166.08;;
2026-10-01T17:54:35.000-03:00;12;reserve_for_payout;40000.00;0.00;40000.00;0.00;0.00;available_money;;;;100166.08;;
2026-10-01T17:54:35.000-03:00;12;payouts_cash;0.00;40000.00;-40000.00;0.00;0.00;available_money;;;;60166.08;;
2026-10-02T10:00:00.000-03:00;13;payment;0.00;2200.00;-2200.00;0.00;0.00;available_money;;;;57966.08;account_money;
2026-10-02T11:00:00.000-03:00;14;payment;0.00;5000.00;-5000.00;0.00;0.00;available_money;;;;52966.08;account_money;
2026-10-03T17:25:28.000-03:00;15;payment;520000.00;0.00;520000.00;0.00;0.00;debin_transfer;;;;572966.08;bank_transfer;
2026-10-03T17:25:52.000-03:00;16;payout;0.00;400000.00;-400000.00;0.00;0.00;available_money;;;;172966.08;;
2026-10-04T09:00:00.000-03:00;17;payment;17600.00;0.00;17600.00;0.00;0.00;available_money;;;;190566.08;account_money;
2026-10-05T02:55:54.000-03:00;18;payment;0.00;14800.00;-14800.00;0.00;0.00;available_money;;;;175766.08;account_money;
2026-10-05T03:53:26.000-03:00;18;refund;14800.00;0.00;14800.00;0.00;0.00;available_money;;;;190566.08;account_money;
2026-10-06T08:00:00.000-03:00;19;payment;0.00;13990.00;-13990.00;0.00;0.00;available_money;;;;176576.08;account_money;
2026-10-07T23:30:00.000-03:00;20;payment;0.00;16907.00;-16907.00;0.00;0.00;available_money;;;;159669.08;account_money;
;;;159669.08;0.00;159669.08;0.00;0.00;;;;;0.00;;
"""

MP_PAYMENTS = {
    "13": {"operation_type": "regular_payment", "description": "Producto de La Maquinita Co SA",
           "point_of_interaction": {"business_info": {"sub_unit": "qr"}}},
    "14": {"operation_type": "partition_transfer", "description": None},
    "15": {"operation_type": "account_fund", "description": "Pago Debin"},
    "17": {"operation_type": "money_transfer", "description": "Varios", "payer": {"email": "franco@example.com"}},
    "18": {"operation_type": "recurring_payment", "description": "Uber"},
    "19": {"operation_type": "recurring_payment", "description": "Suscripción a Meli+"},
    "20": {"operation_type": "regular_payment", "description": "Cable Display Port 1.4",
           "order": {"type": "mercadolibre"}},
}


def test_mercadopago_release_report_rows(conn):
    moves = mpsync.parse_release(RELEASE_CSV)
    assert len(moves) == 11 and moves[-1].balance == 15966908  # reserve lines and balance lines left out
    assert moves[-1].when.date() == date(2026, 10, 7)          # Argentina's day, not UTC's
    rows = mpsync.rows_from(moves, MP_PAYMENTS)
    res = ledger.store(conn, mpsync.SOURCE, "mercadopago", rows)
    assert res["new"] == 11
    got = {r["description"]: (r["amount"], r["kind"], r["category"]) for r in conn.execute("SELECT * FROM transactions")}
    assert got["Rendimientos"] == (16608, "income", None)
    assert got["Retiro de dinero"] == (-4000000, "expense", "efectivo.retiros")
    assert got["Pago con QR La Maquinita Co SA"][1] == "expense"
    assert got["Reserva por gastos Ahorro"][1] == "internal"
    assert got["Ingreso de dinero"][1] == "internal"
    assert got["Transferencia enviada"] == (-40000000, "expense", "personas.personas")
    assert got["Transferencia recibida franco"][1] == "income"
    assert got["Devolución de pago Uber"][1] == "expense"
    assert got["Pago de suscripción Meli+"][2] == "digital.otras"
    assert got["Compra en Mercado Libre: Cable Display Port 1.4"][2] == "compras.varias"
    # The next report overlaps the last day: nothing twice.
    assert ledger.store(conn, mpsync.SOURCE, "mercadopago", rows)["new"] == 0
    # A report downloaded by hand for those days doesn't double them either.
    csv_rows = [ledger.Row(date(2026, 10, 2), "Pago con QR La Maquinita Co SA", -220000),
                ledger.Row(date(2026, 10, 9), "Pago PedidosYa", -4564900)]
    assert ledger.store(conn, "account_statement-x.csv", "mercadopago", csv_rows)["new"] == 1


def test_mercadopago_sync_rounds(conn, monkeypatch, tmp_path):
    from datetime import datetime
    from app import notify
    (tmp_path / "token").write_text("APP_USR-test\n")
    monkeypatch.setattr(mpsync, "TOKEN_FILE", str(tmp_path / "token"))
    ledger.store(conn, "mp.csv", "mercadopago", mercadopago.parse("mp.csv", MP_CSV))  # loaded by hand up to 27/09
    task = {"id": 77, "status": "data-ready", "file_name": None}
    calls = []

    def fake(tok, method, path, body=None, raw=False):
        calls.append((method, path.split("?")[0], body))
        if method == "POST":
            return {"id": 77, "status": "pending"}
        if path.endswith("/task/77"):
            return task
        if "/release_report/" in path:
            return RELEASE_CSV.encode()
        if path.startswith("/v1/payments/search"):
            return {"results": [{"id": int(k), **v} for k, v in MP_PAYMENTS.items()], "paging": {"total": 7}}
        raise mpsync.urllib.error.HTTPError(path, 404, "not found", {}, None)
    monkeypatch.setattr(mpsync, "_call", fake)
    at = lambda h, d=8: datetime(2026, 10, d, h, 0, tzinfo=mpsync.TZ)

    assert mpsync.sync(conn, at(3)) == "idle"                  # one report a day, in the morning
    assert mpsync.sync(conn, at(7)) == "asked"
    # From the day after the last movement loaded by hand, to the end of yesterday.
    assert calls[-1][2] == {"begin_date": "2026-09-28T03:00:00Z", "end_date": "2026-10-08T02:59:59Z"}
    assert mpsync.sync(conn, at(9)) == "waiting"
    task["file_name"] = "r.csv"
    assert mpsync.sync(conn, at(11)) == "saved 11"
    st = mpsync.status(conn)
    assert st["balance"] == 15966908 and st["from"] == "2026-10-08" and st["on"] and not st["error"]
    assert mpsync.sync(conn, at(12)) == "idle"                 # already asked today
    task["file_name"] = None
    assert mpsync.sync(conn, at(7, 9)) == "asked"
    assert calls[-1][2]["begin_date"] == "2026-10-08T03:00:00Z"
    # A report that never came: noted, and asked for again (20 hours later).
    assert mpsync.sync(conn, at(8, 10)) == "asked" and "no terminó" in mpsync.status(conn)["error"]
    # The automatic load replaces the monthly reminder; nothing for 2 days is a warning.
    assert not any(m[0].startswith("mp:") for m in notify.due_messages(conn, date(2026, 11, 3)))
    assert any(m[0].startswith("mpsync:") for m in notify.due_messages(conn, date(2026, 10, 10)))
    assert not any(m[0].startswith("mpsync:") for m in notify.due_messages(conn, date(2026, 10, 9)))


# --- BBVA's "ya podés descargar tu resumen" mail -----------------------------------------

from app import aviso  # noqa: E402

NOTICE_TEXT = """Fw: Ya podes descargar tu resumen de tarjeta Visa BBVA
Hola, NAHUEL BURDISSO El resumen de tu tarjeta de crédito Visa Signature ya está disponible.
Descargar resumen<https://online.bbva.com.ar/descarga-resumen/x>
Número de cuenta ****5678 Fecha de vencimiento 09-11-2026 Saldo en pesos 1.234.567,89
Saldo en dólares 327,71 Pago mínimo 311.820,00"""
NOTICE_HTML = ("<html><style>p{margin:0}</style><body><p>Fecha de vencimiento</p><td>09-11-2026</td>"
               "<p>Saldo en pesos</p><td>1.234.567,89</td><p>Saldo en d&oacute;lares</p><td>327,71</td>"
               "<p>Pago m&iacute;nimo</p><td>311.820,00</td></body></html>")


def test_card_notice_parse():
    want = {"due": date(2026, 11, 9), "total": 123456789, "total_usd": 32771, "minimum": 31182000}
    assert aviso.parse(NOTICE_TEXT) == want
    assert aviso.parse(NOTICE_HTML) == want
    assert aviso.parse("Saldo en pesos 1.000,00") is None  # no due date: not a notice
    assert aviso.parse("Tu compra con Visa por $ 5.000 fue aprobada") is None


def test_card_notice_store_and_status(conn, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    from app.importers import bbva_tarjeta_pdf as pdf

    class Nov2(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 2)
    monkeypatch.setattr(main, "date", Nov2)
    ledger.store(conn, "20261003.pdf", "bbva_tarjeta", pdf.parse_pages(_pdf_pages()))  # announces 29/10
    client = TestClient(main.app)
    r = client.post("/api/aviso-tarjeta", content=NOTICE_TEXT.encode())
    assert r.status_code == 200
    assert r.text.startswith("BBVA tarjeta: vence el 09/11, $ 1.234.568 + US$ 328 (mínimo $ 311.820).")
    client.post("/api/aviso-tarjeta", content=NOTICE_TEXT.encode())  # the same mail twice: one notice
    rows = conn.execute("SELECT due, close, total FROM card_notices").fetchall()
    assert [tuple(r) for r in rows] == [("2026-11-09", "2026-10-29", 123456789)]
    assert client.get("/api/estado").json()["vencimientos"] == [
        {"cuenta": "BBVA tarjeta", "cierre": "2026-10-29", "vence": "2026-11-09",
         "total": "$ 1.234.568", "total_usd": "US$ 328", "minimo": "$ 311.820"}]
    assert "Tarjeta por pagar" in client.get("/saldos").text
    assert client.post("/api/aviso-tarjeta", content=b"hola").status_code == 422

    # When the PDF of that period comes, it's the one that counts.
    conn.execute("INSERT INTO statements (account, close, prev_close, due, total) VALUES"
                 " ('bbva_tarjeta', '2026-10-29', '2026-10-01', '2026-11-09', 123400000)")
    assert [(d["due"], d["total"], d["pdf"]) for d in aviso.upcoming(conn, "2026-11-02")] == [
        ("2026-11-09", 123400000, 1)]


def test_card_notice_without_matching_close(conn):
    n = aviso.store(conn, aviso.parse(NOTICE_TEXT), date(2026, 11, 2))  # no PDF ever loaded
    assert n["close"] is None


# --- Gmail, read by Finanzas itself (app/correo.py) ----------------------------------------

import email as email_lib  # noqa: E402
import imaplib  # noqa: E402
from datetime import datetime  # noqa: E402
from email.message import EmailMessage  # noqa: E402


def _mail(sender: str, subject: str, body: str = "", pdf: tuple[str, bytes] | None = None,
          attached: bytes | None = None) -> bytes:
    m = EmailMessage()
    m["From"], m["Subject"] = sender, subject
    m.set_content(body or "Hola")
    if pdf:
        m.add_attachment(pdf[1], maintype="application", subtype="pdf", filename=pdf[0])
    if attached:  # forwarded as an attachment
        m.add_attachment(email_lib.message_from_bytes(attached), filename="reenviado.eml")
    return m.as_bytes()


class FakeGmail:
    """What Gmail answers to imaplib, enough for correo.sync: SELECT, SEARCH, FETCH, LOGOUT."""

    def __init__(self, mails: dict[int, bytes], validity: int = 1):
        self.mails, self.validity, self.downloaded, self.readonly = dict(mails), validity, [], None

    def select(self, box, readonly=False):
        self.readonly = readonly
        self.answers = {"UIDVALIDITY": [str(self.validity).encode()],
                        "UIDNEXT": [str(max(self.mails, default=0) + 1).encode()]}
        return "OK", [str(len(self.mails)).encode()]

    def response(self, code):
        return code, self.answers.get(code, [None])

    def uid(self, command, *args):
        if command == "SEARCH":
            kind, value = args[1], args[2]
            if kind == "UID":  # 'N:*' always includes the newest one, even below N
                low = int(value.split(":")[0])
                hits = [u for u in self.mails if u >= low] or [max(self.mails)]
            else:
                hits = list(self.mails)
            return "OK", [" ".join(map(str, sorted(hits))).encode()]
        uid, what = int(args[0]), args[1]
        raw = self.mails[uid]
        if what == "(BODY.PEEK[])":
            self.downloaded.append(uid)
            return "OK", [(f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        head_msg = email_lib.message_from_bytes(raw)  # keeps the encoded headers as they travel
        head = f"From: {head_msg['From']}\r\nSubject: {head_msg['Subject']}\r\n\r\n".encode()
        shape = ('("APPLICATION" "PDF" NIL NIL NIL "BASE64" 10)' if b"application/pdf" in raw  # as Gmail writes it
                 else '("MESSAGE" "RFC822" NIL)' if b"message/rfc822" in raw else '("TEXT" "PLAIN")')
        return "OK", [(f"1 (UID {uid} RFC822.SIZE {len(raw)} BODYSTRUCTURE ({shape}) "
                       f"BODY[HEADER.FIELDS (FROM SUBJECT)] {{{len(head)}}}".encode(), head), b")"]

    def logout(self):
        pass


@pytest.fixture
def gmail(tmp_path, monkeypatch):
    secret = tmp_path / "gmail-imap"
    secret.write_text("casa@gmail.com\nabcd efgh ijkl mnop\n")
    monkeypatch.setattr(correo, "SECRET_FILE", str(secret))
    sent = []

    def run(conn, box, at, connect=None):
        return correo.sync(conn, at, connect or (lambda cfg: box), lambda *note: sent.append(note))
    run.sent = sent
    return run


FORWARDED = _mail("Nahuel <nahuel@hotmail.com>", "RV: Ya podés descargar tu resumen de tarjeta Visa BBVA",
                  "De: BBVA <avisos@bbva.com.ar>\n" + NOTICE_TEXT)


def test_gmail_config(tmp_path, monkeypatch):
    monkeypatch.setattr(correo, "SECRET_FILE", str(tmp_path / "none"))
    assert correo.config() is None
    (tmp_path / "none").write_text("")  # the empty file docker needs before the password is in
    assert correo.config() is None
    (tmp_path / "none").write_text("casa@gmail.com\nabcd efgh ijkl mnop\n")
    assert correo.config() == {"user": "casa@gmail.com", "password": "abcdefghijklmnop", "host": "imap.gmail.com"}


def test_gmail_reads_the_forwarded_notice_once(conn, gmail):
    box = FakeGmail({10: _mail("Ana <ana@gmail.com>", "Asado el sábado"), 11: FORWARDED})
    assert gmail(conn, box, datetime(2026, 11, 5, 10, 0)) == "read 2"
    assert box.readonly is True          # nothing is marked as read
    assert box.downloaded == [11]        # the rest of the inbox is never downloaded
    assert [tuple(r) for r in conn.execute("SELECT due, total, minimum FROM card_notices")] == [
        ("2026-11-09", 123456789, 31182000)]
    assert gmail.sent == [("Resumen de la tarjeta", "BBVA tarjeta: vence el 09/11, $ 1.234.568 + US$ 328 "
                           "(mínimo $ 311.820). Para el detalle, bajá el PDF y compartilo con Finanzas.",
                           "/saldos", "credit_card")]
    assert correo.status(conn)["last_uid"] == 11 and correo.status(conn)["on"]

    # Nothing new: 'UID 12:*' still answers 11, and it isn't read again.
    assert gmail(conn, box, datetime(2026, 11, 5, 10, 10)) == "read 0"
    # The same notice forwarded again: saved once, said once.
    box.mails[12] = FORWARDED
    assert gmail(conn, box, datetime(2026, 11, 5, 10, 20)) == "read 1"
    assert len(gmail.sent) == 1 and conn.execute("SELECT COUNT(*) FROM card_notices").fetchone()[0] == 1


def test_gmail_first_round_starts_from_now(conn, gmail):
    box = FakeGmail({500: _mail("x@y.com", "hola")})
    gmail(conn, box, datetime(2026, 11, 5, 10, 0))
    assert correo.status(conn)["last_uid"] == 500
    # Gmail renumbered the inbox: the last two weeks are read again, nothing said twice.
    box = FakeGmail({3: FORWARDED}, validity=2)
    gmail(conn, box, datetime(2026, 11, 5, 10, 10))
    box = FakeGmail({3: FORWARDED, 4: FORWARDED}, validity=3)
    gmail(conn, box, datetime(2026, 11, 5, 10, 20))
    assert len(gmail.sent) == 1 and correo.status(conn)["last_uid"] == 4


def test_gmail_outage_catches_up_and_says_so(conn, gmail):
    box = FakeGmail({1: _mail("x@y.com", "hola")})
    gmail(conn, box, datetime(2026, 11, 5, 7, 0))

    def no_dns(cfg):
        raise OSError("[Errno -3] Temporary failure in name resolution")
    box.mails[2] = FORWARDED  # arrives while there's no internet
    for hour, minute in ((8, 0), (13, 50), (14, 10), (14, 20)):
        assert gmail(conn, box, datetime(2026, 11, 5, hour, minute), no_dns) == "error"
    s = correo.status(conn)
    assert s["failing_since"] == "2026-11-05T08:00:00" and "name resolution" in s["error"] and s["last_uid"] == 1
    assert [n[0] for n in gmail.sent] == ["Finanzas no puede leer el Gmail"]  # once, after 6 hours
    assert "05/11 a las 08:00" in gmail.sent[0][1] and "Compartir" in gmail.sent[0][1]

    assert gmail(conn, box, datetime(2026, 11, 5, 14, 30)) == "read 1"
    # "volvió" once the whole round went well, after what it brought.
    assert [n[0] for n in gmail.sent] == ["Finanzas no puede leer el Gmail", "Resumen de la tarjeta",
                                          "Finanzas volvió a leer el Gmail"]
    s = correo.status(conn)
    assert s["error"] is None and "failing_since" not in s and s["last_uid"] == 2


def test_gmail_wrong_password_and_night(conn, gmail):
    def refused(cfg):
        raise imaplib.IMAP4.error("b'[AUTHENTICATIONFAILED] Invalid credentials (Failure)'")
    gmail(conn, None, datetime(2026, 11, 5, 16, 0), refused)
    gmail(conn, None, datetime(2026, 11, 5, 23, 0), refused)  # 7 hours, but at night
    assert gmail.sent == []
    gmail(conn, None, datetime(2026, 11, 6, 9, 0), refused)
    assert len(gmail.sent) == 1 and "contraseña de aplicación" in gmail.sent[0][1]


def test_gmail_bbva_mails_it_does_not_understand(conn, gmail):
    box = FakeGmail({
        1: _mail("BBVA <avisos@bbva.com.ar>", "Ya podés descargar tu resumen de tarjeta Visa BBVA", "Hola"),
        2: _mail("BBVA <avisos@bbva.com.ar>", "Ya podés descargar tu resumen de cuenta", "Hola"),  # not the card
        3: _mail("BBVA <avisos@bbva.com.ar>", "Tu compra con Visa fue aprobada", "Visa $ 5.000"),
        4: _mail("Nahuel <nahuel@hotmail.com>", "Fwd", "", attached=FORWARDED),  # forwarded as attachment
    })
    gmail(conn, box, datetime(2026, 11, 5, 10, 0))
    assert [n[0] for n in gmail.sent] == ["Un mail de BBVA que no entendí", "Resumen de la tarjeta"]
    assert "«Ya podés descargar tu resumen de tarjeta Visa BBVA»" in gmail.sent[0][1]


def test_gmail_statement_pdf(conn, gmail, monkeypatch):
    from app.importers import ImportError_
    from app.importers import bbva_tarjeta_pdf as pdf

    def parse_file(name, data, account):
        if data != b"%PDF bbva":
            raise ImportError_("Todavía no sé leer este PDF.")
        return "bbva_tarjeta", pdf.parse_pages(_pdf_pages())
    monkeypatch.setattr(correo, "parse_file", parse_file)
    box = FakeGmail({
        1: _mail("Edenor <facturas@edenor.com>", "Tu factura", pdf=("factura.pdf", b"%PDF luz")),
        2: _mail("Nahuel <nahuel@icloud.com>", "", pdf=("20261003.pdf", b"%PDF bbva")),  # sent from the phone
    })
    gmail(conn, box, datetime(2026, 11, 5, 10, 0))
    assert box.downloaded == [1, 2]
    assert [n[0] for n in gmail.sent] == ["Resumen de la tarjeta"] and "llegó el resumen por mail" in gmail.sent[0][1]
    assert conn.execute("SELECT close FROM statements").fetchone()[0] == "2026-10-01"
    box.mails[3] = box.mails[2]  # the same PDF again: already loaded, nothing to say
    gmail(conn, box, datetime(2026, 11, 5, 10, 10))
    assert len(gmail.sent) == 1


def test_gmail_never_stuck_on_one_mail(conn, gmail, monkeypatch):
    real = correo.handle

    def handle(conn, mail, today):
        if "rompe" in mail["subject"]:
            raise ValueError("un mail raro")
        return real(conn, mail, today)
    monkeypatch.setattr(correo, "handle", handle)
    box = FakeGmail({1: _mail("BBVA <avisos@bbva.com.ar>", "rompe resumen"), 2: FORWARDED})
    assert gmail(conn, box, datetime(2026, 11, 5, 10, 0)) == "read 2"
    assert "UID 1: ValueError" in correo.status(conn)["skipped"]
    assert [n[0] for n in gmail.sent] == ["Un mail que no pude leer", "Resumen de la tarjeta"]  # the skip is said once
    assert "«rompe resumen»" in gmail.sent[0][1]


def test_gmail_status_endpoint(conn):
    from fastapi.testclient import TestClient
    from app import main
    client = TestClient(main.app)
    assert client.get("/api/correo").json()["on"] is False
    assert client.post("/api/correo").text == "off"
    correo._save(conn, {"ok": datetime.now().isoformat(timespec="seconds"), "last_uid": 7})
    assert client.get("/api/correo").json()["ok_age_s"] < 5


# --- regressions: what a statement replaces, mails that break, n8n reading the same Gmail -----

import sqlite3  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402


def _statement_only(name, data, account=None):
    """parse_file for the mail tests: only b'%PDF bbva' is the card statement of _pdf_pages()."""
    from app.importers import ImportError_
    from app.importers import bbva_tarjeta_pdf as pdf
    if data != b"%PDF bbva":
        raise ImportError_("Todavía no sé leer este PDF.")
    return "bbva_tarjeta", pdf.parse_pages(_pdf_pages())


def test_statement_keeps_your_choices(conn):
    """The PDF replaces what the Excel loaded for its period; whose each charge is, a kind changed by
    hand and a category picked go on to the same charge (same day and amount, in dollars if in dollars)."""
    from app.importers import bbva_tarjeta_pdf as pdf
    excel = BBVA_CARD[:-1] + [["", "09/09/2026", "CAMINOS DE LAS SIERRAS", "/", "42.110,28", ""]] + BBVA_CARD[-1:]
    ledger.store(conn, "card.xls", "bbva_tarjeta", bbva_tarjeta.parse_table(excel))
    ids = dict(conn.execute("SELECT description, id FROM transactions").fetchall())
    ledger.set_who(conn, ids["ANTHROPIC* CLAUD in1UL4"], "casa")
    ledger.set_category(conn, ids["ANTHROPIC* CLAUD in1UL4"], "digital.juegos", learn=False)
    ledger.set_who(conn, ids["CAMINOS DE LAS SIERRAS"], "sofia")
    ledger.set_kind(conn, ids["CAMINOS DE LAS SIERRAS"], "internal")
    res = ledger.store(conn, "20261003.pdf", "bbva_tarjeta", pdf.parse_pages(_pdf_pages()))
    assert res["replaced"] == 5 and res["new"] == 6
    got = {r["description"]: r for r in conn.execute("SELECT * FROM transactions")}
    claude, peaje = got["ANTHROPIC* CLAUD in1UL4LeB"], got["CAMINOS DE LAS S 000000002031450"]
    assert (claude["who"], claude["category"], claude["category_source"]) == ("casa", "digital.juegos", "user")
    assert (peaje["who"], peaje["kind"], peaje["kind_source"]) == ("sofia", "internal", "manual")
    assert got["FRAVEGA C.02/06"]["who"] == "nahuel" and got["DB.RG 5617 30%"]["kind"] == "expense"


def test_gmail_transient_error_comes_again(conn, gmail, monkeypatch):
    """No dollar rate yet (or the database busy): the round fails and the mail comes again, instead
    of being skipped for good."""
    monkeypatch.setattr(correo, "parse_file", _statement_only)
    box = FakeGmail({1: _mail("Nahuel <nahuel@icloud.com>", "", pdf=("20261003.pdf", b"%PDF bbva"))})
    oficial = conn.execute("SELECT date, kind, value FROM rates WHERE kind = 'oficial'").fetchall()
    conn.execute("DELETE FROM rates WHERE kind = 'oficial'")  # the PDF has a charge in dollars
    conn.commit()
    assert gmail(conn, box, datetime(2026, 11, 5, 10, 0)) == "error"
    assert "ImportError_" in correo.status(conn)["error"] and gmail.sent == []
    conn.executemany("INSERT INTO rates (date, kind, value) VALUES (?, ?, ?)", [tuple(r) for r in oficial])
    conn.commit()
    assert gmail(conn, box, datetime(2026, 11, 5, 10, 10)) == "read 1"
    assert conn.execute("SELECT close FROM statements").fetchone()[0] == "2026-10-01"
    assert [n[0] for n in gmail.sent] == ["Resumen de la tarjeta"]

    real, calls = ledger.store, []

    def busy(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)
    monkeypatch.setattr(ledger, "store", busy)
    box.mails[2] = _mail("Nahuel <nahuel@icloud.com>", "otra vez", pdf=("20261003.pdf", b"%PDF bbva"))
    assert gmail(conn, box, datetime(2026, 11, 5, 10, 20)) == "error"
    assert correo.status(conn)["last_uid"] == 1
    assert gmail(conn, box, datetime(2026, 11, 5, 10, 30)) == "read 1" and len(calls) == 2


def test_gmail_mail_that_always_breaks_is_skipped(conn, gmail):
    """A mail Gmail can't hand over breaks the round; after STUCK_ROUNDS rounds in a row it's skipped
    with one warning, and what came after it is read."""
    class Broken(FakeGmail):
        def uid(self, command, *args):
            if command == "FETCH" and int(args[0]) == 2:
                raise imaplib.IMAP4.error("FETCH: Some messages could not be FETCHed (Failure)")
            return super().uid(command, *args)
    box = Broken({1: _mail("x@y.com", "hola")})
    gmail(conn, box, datetime(2026, 11, 5, 9, 0))
    box.mails.update({2: _mail("x@y.com", "roto"), 3: FORWARDED})
    for i in range(correo.STUCK_ROUNDS - 1):
        assert gmail(conn, box, datetime(2026, 11, 5, 10, i * 10)) == "error"
        assert correo.status(conn)["last_uid"] == 1
    assert gmail.sent == []
    assert gmail(conn, box, datetime(2026, 11, 5, 11, 0)) == "error"  # the last try: skipped
    assert correo.status(conn)["last_uid"] == 2 and "UID 2" in correo.status(conn)["skipped"]
    assert gmail(conn, box, datetime(2026, 11, 5, 11, 10)) == "read 1"
    assert [n[0] for n in gmail.sent] == ["Un mail que no pude leer", "Resumen de la tarjeta"]


def test_gmail_flaky_rounds_alarm_once(conn, gmail):
    """A round that connects and then breaks isn't 'back': the outage is told once, its end once."""
    box = FakeGmail({1: _mail("x@y.com", "hola")})
    gmail(conn, box, datetime(2026, 11, 5, 7, 0))

    def no_dns(cfg):
        raise OSError("dns")
    gmail(conn, box, datetime(2026, 11, 5, 8, 0), no_dns)
    gmail(conn, box, datetime(2026, 11, 5, 14, 10), no_dns)

    class Flaky(FakeGmail):
        def uid(self, command, *args):
            if command == "FETCH":
                raise OSError("connection reset by peer")
            return super().uid(command, *args)
    mails = {1: _mail("x@y.com", "hola"), 2: FORWARDED}
    for minute in (20, 30, 40):
        assert gmail(conn, Flaky(mails), datetime(2026, 11, 5, 14, minute)) == "error"
    assert [n[0] for n in gmail.sent] == ["Finanzas no puede leer el Gmail"]
    assert gmail(conn, FakeGmail(mails), datetime(2026, 11, 5, 14, 50)) == "read 1"
    assert [n[0] for n in gmail.sent] == ["Finanzas no puede leer el Gmail", "Resumen de la tarjeta",
                                          "Finanzas volvió a leer el Gmail"]


def test_gmail_notice_waits_for_ntfy(conn, gmail):
    """ntfy down when the notice arrives: it goes next round (the notice is told only once)."""
    box, sent, down = FakeGmail({11: FORWARDED}), [], [True]

    def send(*note):
        if down[0]:
            raise OSError("ntfy down")
        sent.append(note)
    assert correo.sync(conn, datetime(2026, 11, 5, 10, 0), lambda cfg: box, send) == "read 1"
    assert sent == [] and len(correo.status(conn)["unsent"]) == 1
    down[0] = False
    correo.sync(conn, datetime(2026, 11, 5, 10, 10), lambda cfg: box, send)
    assert [n[0] for n in sent] == ["Resumen de la tarjeta"] and not correo.status(conn)["unsent"]


def test_n8n_and_finanzas_read_the_same_mail(conn, gmail, monkeypatch):
    """n8n's mail workflow pushes whatever /api/aviso-tarjeta answers, and what /api/subir answers
    with a 2xx: what Finanzas already had gets a short line, or a 409 that n8n doesn't push."""
    from fastapi.testclient import TestClient
    from app import main
    client = TestClient(main.app)
    gmail(conn, FakeGmail({11: FORWARDED}), datetime(2026, 11, 5, 10, 0))  # Finanzas read it first
    r = client.post("/api/aviso-tarjeta", content=NOTICE_TEXT.encode())
    assert r.status_code == 200 and r.text == "BBVA tarjeta: ese aviso ya estaba anotado."
    assert conn.execute("SELECT COUNT(*) FROM card_notices").fetchone()[0] == 1
    r = client.post("/api/aviso-tarjeta", content="Hola, el resumen de tu cuenta ya está disponible.".encode())
    assert r.status_code == 422 and "no es de la tarjeta" in r.text
    assert "No encontré" in client.post("/api/aviso-tarjeta", content=b"hola").text

    monkeypatch.setattr(main, "parse_file", _statement_only)
    pdf = [("files", ("20261003.pdf", b"%PDF bbva", "application/pdf"))]
    first = client.post("/api/subir", files=pdf)
    assert first.status_code == 200 and "6 movimientos nuevos" in first.text
    again = client.post("/api/subir", files=pdf)
    assert again.status_code == 409 and "ya estaba cargado" in again.text

    conn.execute("INSERT INTO statements (account, close, prev_close, due) VALUES"
                 " ('bbva_tarjeta', '2026-10-29', '2026-10-01', '2026-11-20')")
    conn.commit()
    later = NOTICE_TEXT.replace("09-11-2026", "20-11-2026")  # its PDF came before the notice
    assert "ya está cargado" in client.post("/api/aviso-tarjeta", content=later.encode()).text
    assert conn.execute("SELECT COUNT(*) FROM card_notices").fetchone()[0] == 1


def test_card_notice_balance_in_your_favor():
    base = "Fecha de vencimiento 09-11-2026 Saldo en pesos {} Saldo en dólares {} Pago mínimo 0,00"
    assert aviso.parse(base.format("$ -5.000,00", "0,00"))["total"] == -500000
    assert aviso.parse(base.format("-$ 5.000,00", "0,00"))["total"] == -500000
    assert aviso.parse(base.format("5.000,00-", "0,00"))["total"] == -500000
    assert aviso.parse(base.format("1.234,56", "USD 12,34"))["total_usd"] == 1234
    assert aviso.parse(base.format("1.234,56", "u$d -12,34"))["total_usd"] == -1234
    assert aviso.parse(NOTICE_TEXT)["total"] == 123456789
    # A dash between spaces is a separator, never a sign.
    assert (aviso.parse("Fecha de vencimiento 09-11-2026 Saldo en pesos - 1.234,56") or {}).get("total") != -123456


def test_rates_download_holds_no_write_lock(tmp_path, monkeypatch):
    """While the dollar rates download, the others can write (Siri's cash, the Gmail, Mercado Pago)."""
    import io
    import json
    path = str(tmp_path / "r.db")
    a, b = db.connect(path), db.connect(path)
    b.execute("PRAGMA busy_timeout = 0")  # any lock held would fail right away

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def slow_api(req, timeout=None):
        if "oficial" in req.full_url:
            time.sleep(1)
        return Resp(json.dumps([{"fecha": "2026-11-05", "venta": 1500}]).encode())
    monkeypatch.setattr(reports.urllib.request, "urlopen", slow_api)
    t = threading.Thread(target=reports.refresh_rates, args=(a,))
    t.start()
    time.sleep(0.3)
    ledger.add_manual(b, date(2026, 11, 5), "Pan", -100, "expense")
    t.join()
    assert {r[0] for r in b.execute("SELECT kind FROM rates")} == {"bolsa", "oficial"}


def test_quick_entry_id_is_its_own(conn, monkeypatch):
    """X-Id (the bot's undo) is the movement just written, even if someone else wrote right after."""
    from fastapi.testclient import TestClient
    from app import main
    real = ledger.categorize

    def someone_else_writes(c):
        ledger.add_manual(c, date.today(), "Lo de Sofía", -100, "expense", who="sofia")
        real(c)
    monkeypatch.setattr(ledger, "categorize", someone_else_writes)
    r = TestClient(main.app).post("/api/gasto", content="gasté 8500 en la verdulería")
    row = conn.execute("SELECT description FROM transactions WHERE id = ?", (int(r.headers["X-Id"]),)).fetchone()
    assert row["description"] == "Verdulería"


# --- Pagos del mes (app/pagos.py) ---------------------------------------------------------------

import re  # noqa: E402

NOV6 = date(2026, 11, 6)  # a Friday


class _Nov6(date):
    @classmethod
    def today(cls):
        return cls(2026, 11, 6)


def _house_bills(conn):
    """Made-up bills: no real account numbers."""
    luz = pagos.add(conn, "Luz (EPEC)", 10, "2026-10", emoji="💡", details="Cliente: 1111\n\nContrato: 2222",
                    url="https://pagos.example.com/luz", match="epec")
    gas = pagos.add(conn, "Gas (Ecogas)", 4, "2026-10", emoji="🔥", match="ecogas, 99887766")
    muni = pagos.add(conn, "Municipal (inmueble)", 10, "2026-10", match="municipalidad")
    casa = pagos.add(conn, "Casa (cuota a mis viejos)", 5, "2026-10", emoji="🏠", amount_usd=250000,
                     total_usd=14590400, paid_before_usd=830000)
    return luz, gas, muni, casa


def _tx(conn, day, desc, cents, month=None, account="bbva"):
    cur = conn.execute("INSERT INTO transactions (account, date, month, description, amount, kind, who)"
                       " VALUES (?, ?, ?, ?, ?, 'expense', 'nahuel')", (account, day, month or day[:7], desc, -cents))
    conn.commit()
    return cur.lastrowid


def test_bill_paid_by_a_movement(conn):
    luz, gas, muni, casa = _house_bills(conn)
    tx = _tx(conn, "2026-11-04", "Pago Pago Ecogas CTA 99887766 DGCE", 1865600)
    _tx(conn, "2026-10-12", "PAGO EPEC FACTURA", 2100000)
    # A card purchase on 28/10 that the statement charges in November: it pays October, not November.
    _tx(conn, "2026-10-28", "MERPAGO*MUNICIPALIDADDECO", 900000, month="2026-11", account="bbva_tarjeta")
    st = {e["bill"]["id"]: e for e in pagos.month_status(conn, "2026-11", NOV6)}
    assert [e["bill"]["id"] for e in pagos.month_status(conn, "2026-11", NOV6)] == [gas, casa, luz, muni]
    g = st[gas]
    assert g["paid"] and g["via"] == "movimiento" and g["tx_id"] == tx and g["paid_on"] == date(2026, 11, 4)
    assert g["amount"] == 1865600 and not g["late"]
    assert not st[muni]["paid"] and not st[muni]["late"]  # due on the 10th
    assert st[casa]["late"] and st[casa]["due"] == date(2026, 11, 5)
    assert st[luz]["last"] == {"amount": 2100000, "month": "2026-10"} and not st[luz]["paid"]
    october = {e["bill"]["id"]: e for e in pagos.month_status(conn, "2026-10", NOV6)}
    assert october[muni]["paid"] and october[muni]["paid_on"] == date(2026, 10, 28)
    assert not october[gas]["late"]  # late is only about the current month
    # A dollar bill is never paid by a movement, even one with its words.
    _tx(conn, "2026-11-05", "Transferencia casa cuota", 100)
    assert not pagos.status_of(conn, casa, "2026-11", NOV6)["paid"]


def test_bill_hand_mark_and_its_removal(conn):
    luz, gas, muni, casa = _house_bills(conn)
    _tx(conn, "2026-11-04", "Pago Ecogas", 1865600)
    before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    pagos.mark_paid(conn, casa, "2026-11", date(2026, 11, 5))
    pagos.mark_paid(conn, luz, "2026-11", date(2026, 11, 6), amount=2000000)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == before  # never a movement
    st = {e["bill"]["id"]: e for e in pagos.month_status(conn, "2026-11", NOV6)}
    assert st[casa]["paid"] and st[casa]["via"] == "manual" and st[casa]["amount"] == 250000 and not st[casa]["late"]
    assert st[luz]["amount"] == 2000000 and st[luz]["tx_id"] is None
    pagos.unmark(conn, casa, "2026-11")
    assert not pagos.status_of(conn, casa, "2026-11", NOV6)["paid"]
    pagos.mark_paid(conn, gas, "2026-11", date(2026, 11, 2), amount=1)
    gas_st = pagos.status_of(conn, gas, "2026-11", NOV6)
    assert gas_st["via"] == "movimiento" and gas_st["amount"] == 1865600  # the movement wins
    pagos.unmark(conn, gas, "2026-11")
    assert pagos.status_of(conn, gas, "2026-11", NOV6)["paid"]  # only the hand mark went


def test_bill_sentence(conn):
    assert pagos.summary(conn, NOV6) is None
    assert pagos.sentence(conn, NOV6) == "No hay pagos fijos cargados."
    luz, gas, muni, casa = _house_bills(conn)
    _tx(conn, "2026-11-04", "Pago Ecogas", 1865600)
    pagos.mark_paid(conn, luz, "2026-11", NOV6)
    s = pagos.summary(conn, NOV6)
    assert s["frase"] == "Pagos de noviembre: 2 de 4 hechos. Faltan casa (venció el 5) y municipal (el 10)."
    assert {k: s[k] for k in ("mes", "nombre", "total", "pagados")} == \
        {"mes": "2026-11", "nombre": "noviembre", "total": 4, "pagados": 2}
    assert s["faltan"][0] == {"id": casa, "nombre": "Casa (cuota a mis viejos)", "corto": "casa", "dia": 5,
                              "vence": "2026-11-05", "atrasado": True}
    pagos.mark_paid(conn, casa, "2026-11", NOV6)
    assert pagos.sentence(conn, NOV6) == "Pagos de noviembre: 3 de 4 hechos. Falta municipal (el 10)."
    pagos.mark_paid(conn, muni, "2026-11", NOV6)
    assert pagos.sentence(conn, NOV6) == "Pagos de noviembre: los 4 hechos."
    assert pagos.summary(conn, date(2026, 9, 1)) is None  # nothing started yet


def test_bill_find(conn):
    luz, gas, muni, casa = _house_bills(conn)
    for text, want in [("pagué la luz", luz), ("ya pagué el gas.", gas), ("pagamos el Municipal", muni),
                       ("pagué la cuota de la casa", casa), ("/pague luz", luz), ("muni", muni), ("epec", luz),
                       ("Ecogas", gas), ("casa", casa),
                       ("pagué la luz de noviembre", luz), ("ya pagué la casa este mes", casa),
                       ("pagué la luz con la tarjeta", luz), ("pagamos el gas por mercado pago", gas)]:
        assert pagos.find(conn, text)["id"] == want, text
    for text in ("pagué el almuerzo", "luz y gas", "", "la", "pagué el almuerzo en casa", "pagué la luz del auto"):
        with pytest.raises(ValueError, match=r"^No encontré ese pago\. Tengo: gas, casa, luz, municipal\.$"):
            pagos.find(conn, text)
    pagos.update(conn, muni, NOV6, active=0)
    with pytest.raises(ValueError):
        pagos.find(conn, "muni")  # paused


def test_bill_debt_progress(conn):
    luz, gas, muni, casa = _house_bills(conn)
    pagos.mark_paid(conn, casa, "2026-10", date(2026, 10, 5))
    p = pagos.progress(conn, pagos.get(conn, casa))
    assert (p["paid"], p["total"], p["left"], p["pct"], p["cuotas"]) == (1080000, 14590400, 13510400, 7, 55)
    assert p["text"] == "Pagaste US$ 10.800 de US$ 145.904 (7%) · faltan US$ 135.104 · ≈ 55 cuotas"
    pagos.mark_paid(conn, casa, "2026-11", date(2026, 11, 5), amount=300000)  # paid more this time
    assert pagos.progress(conn, pagos.get(conn, casa))["paid"] == 1380000
    assert pagos.progress(conn, pagos.get(conn, luz)) is None


class FakeRecordatorios:
    """Recordatorios' API as pagos.request sees it."""

    def __init__(self):
        self.items, self.next_id, self.calls, self.down = {}, 41, [], False

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.down:
            raise OSError("Connection refused")
        if (method, path) == ("POST", "/api/recordatorio"):
            self.next_id += 1
            self.items[self.next_id] = {**body, "id": self.next_id, "hecho": False}
            return 200, {"id": self.next_id, "nuevo": True, "texto": "ok"}
        m = re.fullmatch(r"/(?:api/)?r/(\d+)(/hecho\?occ=(.+)|/borrar)?", path)
        rid = int(m[1])
        if method == "POST" and m[2] == "/borrar":
            self.items.pop(rid, None)
            return 200, None
        if rid not in self.items:
            return 404, {"error": "Ese recordatorio ya no existe."}
        if method == "POST":
            assert m[3] == self.items[rid]["fecha"] + "T09:00"
            self.items[rid]["hecho"] = True
            return 200, None
        return 200, self.items[rid]

    def made(self):
        return [c[2] for c in self.calls if c[:2] == ("POST", "/api/recordatorio")]


def test_bill_reminders(conn, monkeypatch):
    from app import notify
    fake = FakeRecordatorios()
    monkeypatch.setattr(pagos, "request", fake)
    luz, gas, muni, casa = _house_bills(conn)
    _tx(conn, "2026-11-04", "Pago Ecogas", 1865600)
    pagos.sync_reminders(conn, NOV6)
    made = {b["titulo"]: b for b in fake.made()}
    assert set(made) == {"Pagar luz (EPEC)", "Pagar municipal (inmueble)", "Pagar casa (cuota a mis viejos) (US$ 2.500)"}
    assert made["Pagar luz (EPEC)"] == {
        "titulo": "Pagar luz (EPEC)", "fecha": "2026-11-10", "avisos": [1440, 0],
        "notas": f"Cliente: 1111 · Contrato: 2222\nhttps://pagos.example.com/luz\n"
                 f"Al pagarlo tocá Hecho. Detalle: {notify.APP_URL}/pagos/{luz}"}
    assert made["Pagar casa (cuota a mis viejos) (US$ 2.500)"]["fecha"] == "2026-11-06"  # already late: from today
    assert made["Pagar municipal (inmueble)"]["notas"] == f"Al pagarlo tocá Hecho. Detalle: {notify.APP_URL}/pagos/{muni}"
    rid = {e["bill"]["id"]: e["reminder_id"] for e in pagos.month_status(conn, "2026-11", NOV6)}
    assert rid[gas] is None  # paid already: no reminder
    pagos.sync_reminders(conn, NOV6)
    assert len(fake.made()) == 3  # not twice

    # The bill gets paid by a movement: its reminder is told it's done, once.
    _tx(conn, "2026-11-07", "PAGO EPEC", 2000000)
    pagos.sync_reminders(conn, date(2026, 11, 7))
    assert fake.items[rid[luz]]["hecho"]
    assert pagos.status_of(conn, luz, "2026-11", NOV6)["reminder_closed"]
    n = len(fake.calls)
    pagos.sync_reminders(conn, date(2026, 11, 7))
    assert not [c for c in fake.calls[n:] if str(rid[luz]) in c[1]]

    # "Hecho" tapped on the phone: the bill is paid, by its reminder; no movement.
    fake.items[rid[casa]]["hecho"] = True
    pagos.sync_reminders(conn, date(2026, 11, 7))
    c = pagos.status_of(conn, casa, "2026-11", NOV6)
    assert c["paid"] and c["via"] == "recordatorio" and c["paid_on"] == date(2026, 11, 7) and c["amount"] == 250000

    # Deleted in Recordatorios: left alone, not made again.
    del fake.items[rid[muni]]
    pagos.sync_reminders(conn, date(2026, 11, 7))
    assert len(fake.made()) == 3 and pagos.status_of(conn, muni, "2026-11", NOV6)["reminder_id"] == rid[muni]

    # Recordatorios down: nothing breaks, the next round tries again.
    agua = pagos.add(conn, "Agua (Aguas Cordobesas)", 10, "2026-11", details="Cuenta: 5555")
    fake.down = True
    pagos.sync_reminders(conn, date(2026, 11, 7))
    fake.down = False
    pagos.sync_reminders(conn, date(2026, 11, 7))
    first = pagos.status_of(conn, agua, "2026-11", NOV6)["reminder_id"]
    assert first and fake.items[first]["titulo"] == "Pagar agua (Aguas Cordobesas)"

    # Edited: the old reminder goes and the next round makes one with the new details.
    pagos.update(conn, agua, date(2026, 11, 7), details="Cuenta: 6666")
    assert first not in fake.items and pagos.status_of(conn, agua, "2026-11", NOV6)["reminder_id"] is None
    pagos.sync_reminders(conn, date(2026, 11, 7))
    second = pagos.status_of(conn, agua, "2026-11", NOV6)["reminder_id"]
    assert second != first and fake.items[second]["notas"].startswith("Cuenta: 6666\n")
    pagos.update(conn, agua, date(2026, 11, 7), notes="solo una nota")  # not in the reminder: it stays
    assert second in fake.items
    pagos.delete(conn, agua, date(2026, 11, 7))
    assert second not in fake.items and pagos.get(conn, agua) is None


def test_bill_reminders_only_when_enabled(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(pagos, "request", lambda *a, **k: calls.append(a) or (200, {"id": 1}))
    _house_bills(conn)
    pagos.sync_now()
    assert calls == []
    monkeypatch.setenv("FINANZAS_PAGOS_AVISOS", "1")
    pagos.sync_now()
    assert calls


def test_bill_late_nudge(conn):
    from app import notify
    luz, gas, muni, casa = _house_bills(conn)
    _tx(conn, "2026-11-04", "Pago Ecogas", 1865600)
    keys = lambda today: {m[0]: m for m in notify.due_messages(conn, today) if m[0].startswith("bill:")}  # noqa: E731
    assert keys(NOV6) == {}
    msgs = keys(date(2026, 11, 8))
    assert list(msgs) == [f"bill:2026-11:{casa}"]
    assert msgs[f"bill:2026-11:{casa}"][1:] == ("Falta pagar casa", "Casa (cuota a mis viejos) de noviembre: era para el 5.",
                                                 f"/pagos/{casa}", "receipt")
    pagos.mark_paid(conn, casa, "2026-11", date(2026, 11, 8))
    msgs = keys(date(2026, 11, 13))
    assert set(msgs) == {f"bill:2026-11:{luz}", f"bill:2026-11:{muni}"}
    assert msgs[f"bill:2026-11:{luz}"][2] == "Luz (EPEC) de noviembre: era para el 10. Cliente: 1111 · Contrato: 2222"
    # Due on the 28th: its nudge comes in the next month; last month's older ones don't come back.
    expensas = pagos.add(conn, "Expensas", 28, "2026-10")
    assert set(keys(date(2026, 12, 1))) == {f"bill:2026-11:{expensas}"}


def test_bill_account_number_is_a_whole_number(conn):
    gas = pagos.add(conn, "Gas (Ecogas)", 4, "2026-10", match="99887766")
    _tx(conn, "2026-11-03", "DEB AUT 0001998877660042", 500000)  # the digits inside another code
    assert not pagos.status_of(conn, gas, "2026-11", NOV6)["paid"]
    _tx(conn, "2026-11-04", "Pago CTA 99887766 DGCE", 1865600)
    assert pagos.status_of(conn, gas, "2026-11", NOV6)["amount"] == 1865600


def test_bill_api(conn, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, "date", _Nov6)
    client = TestClient(main.app)
    assert client.get("/api/pagos").text == "No hay pagos fijos cargados."
    assert client.get("/api/estado").json()["pagos"] is None
    luz, gas, muni, casa = _house_bills(conn)
    _tx(conn, "2026-11-04", "Pago Pago Ecogas CTA 99887766 DGCE", 1865600)
    r = client.get("/api/pagos")
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text == "Pagos de noviembre: 1 de 4 hecho. Faltan casa (venció el 5), luz (el 10) y municipal (el 10)."
    assert client.get("/api/estado").json()["pagos"]["faltan"][1]["corto"] == "luz"

    before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    r = client.post("/api/pagos/pagar", json={"texto": "pagué la luz"})
    assert r.status_code == 200 and r.text == "Listo: luz (EPEC) de noviembre, pagado hoy."
    assert r.headers["X-Id"] == str(luz) and r.headers["X-Mes"] == "2026-11"
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == before
    r = client.post("/api/pagos/pagar", data={"texto": "la luz"})
    assert r.status_code == 200 and r.text == "Luz (EPEC) de noviembre ya estaba pagado (el 6/11)." and "X-Id" not in r.headers
    r = client.post("/api/pagos/pagar", content="gas".encode(), headers={"content-type": "text/plain"})
    assert r.text == "Gas (Ecogas) de noviembre ya estaba pagado (el 4/11)." and "X-Id" not in r.headers
    r = client.post("/api/pagos/pagar", json={"texto": "pagué el almuerzo"})
    assert r.status_code == 422 and r.text.startswith("No encontré ese pago. Tengo: ")

    r = client.post(f"/api/pagos/{luz}/pendiente?m=2026-11")
    assert r.status_code == 200 and r.text == "Listo: luz (EPEC) de noviembre vuelve a estar pendiente."
    assert not pagos.status_of(conn, luz, "2026-11", NOV6)["paid"]
    assert "no estaba marcado" in client.post(f"/api/pagos/{luz}/pendiente?m=2026-11").text
    assert client.post("/api/pagos/999/pendiente?m=2026-11").status_code == 404
    # A bill that starts next month: said so, and nothing marked or to take back.
    agua = pagos.add(conn, "Agua (Aguas)", 10, "2026-12", match="aguas")
    r = client.post("/api/pagos/pagar", json={"texto": "pagué el agua"})
    assert r.status_code == 200 and "X-Id" not in r.headers
    assert r.text == "Agua (Aguas) recién va desde diciembre: el de noviembre no te toca."
    assert not conn.execute("SELECT 1 FROM bill_months WHERE bill_id = ?", (agua,)).fetchone()


def test_bill_pages(conn, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, "date", _Nov6)
    client = TestClient(main.app)
    _tx(conn, "2026-11-02", "Verdulería", 500000)
    assert "No hay pagos para este mes" in client.get("/pagos").text
    assert "Pagos del mes" not in client.get("/mes?m=2026-11").text
    assert client.get("/pagos/nuevo").status_code == 200
    assert client.get("/pagos/999", follow_redirects=False).status_code == 303

    r = client.post("/pagos", data={"nombre": "Luz (EPEC)", "emoji": "💡", "datos": "Cliente: 1111", "url": "pagos.example.com/luz",
                                    "dia": "10", "detectar": "epec", "desde": "2026-10", "activo": "1"}, follow_redirects=False)
    assert r.status_code == 303
    luz = int(r.headers["location"].rsplit("/", 1)[1])
    assert pagos.get(conn, luz)["url"] == "https://pagos.example.com/luz"
    r = client.post("/pagos", data={"nombre": "Casa (cuota a mis viejos)", "dia": "5", "desde": "2026-10", "activo": "1",
                                    "monto_usd": "2.500", "total_usd": "145.904", "pagado_antes_usd": "8.300"},
                    follow_redirects=False)
    casa = int(r.headers["location"].rsplit("/", 1)[1])
    b = pagos.get(conn, casa)
    assert (b["amount_usd"], b["total_usd"], b["paid_before_usd"]) == (250000, 14590400, 830000)
    assert "entre 1 y 28" in client.post("/pagos", data={"nombre": "X", "dia": "31", "desde": "2026-10"}).text
    assert "https://" in client.post("/pagos", data={"nombre": "X", "dia": "3", "desde": "2026-10",
                                                     "url": "javascript:alert(1)"}).text

    page = client.get("/pagos").text
    assert "Luz (EPEC)" in page and "Ya lo pagué" in page and 'href="https://pagos.example.com/luz"' in page
    assert "Venció el 5" in page and "Pagaste US$ 8.300 de US$ 145.904" in page
    assert "Pagos de noviembre: 0 de 2 hechos." in client.get("/mes?m=2026-11").text
    assert client.get("/pagos?m=2026-10").status_code == 200 and client.get("/pagos?m=raro").status_code == 200

    assert client.post(f"/pagos/{casa}/pagada", data={"m": "2026-11"}, follow_redirects=False).status_code == 303
    assert pagos.status_of(conn, casa, "2026-11", NOV6)["paid_on"] == NOV6
    assert "Desmarcar" in client.get("/pagos").text
    client.post(f"/pagos/{casa}/pendiente", data={"m": "2026-11"})
    assert not pagos.status_of(conn, casa, "2026-11", NOV6)["paid"]
    client.post(f"/pagos/{luz}/pagada", data={"m": "2026-10", "fecha": "2026-10-09", "monto": "21.000", "back": f"/pagos/{luz}"})
    assert pagos.status_of(conn, luz, "2026-10", NOV6)["amount"] == 2100000

    detail = client.get(f"/pagos/{luz}").text
    assert "Cliente: 1111" in detail and "Octubre 2026" in detail and "marcado a mano" in detail
    form = {"nombre": "Luz (EPEC)", "emoji": "💡", "datos": "Cliente: 1111\nContrato: 2222", "url": "https://pagos.example.com/luz",
            "dia": "12", "detectar": "epec", "desde": "2026-10"}  # no "activo": paused
    assert client.post(f"/pagos/{luz}", data=form, follow_redirects=False).status_code == 303
    b = pagos.get(conn, luz)
    assert b["day"] == 12 and b["details"] == "Cliente: 1111\nContrato: 2222" and not b["active"]
    assert "Pausado" in client.get("/pagos").text
    assert client.post(f"/pagos/{luz}/borrar", follow_redirects=False).status_code == 303
    assert pagos.get(conn, luz) is None


def test_bill_assistant_tool(conn, monkeypatch):
    from fastapi.testclient import TestClient
    from app import asistente, main
    monkeypatch.setattr(asistente, "date", _Nov6)
    luz, gas, muni, casa = _house_bills(conn)
    _tx(conn, "2026-11-04", "Pago Ecogas", 1865600)
    _tx(conn, "2026-10-12", "PAGO EPEC", 2100000)
    pagos.mark_paid(conn, casa, "2026-10", date(2026, 10, 5))
    client = TestClient(main.app)
    spec = client.get("/ia/openapi.json").json()
    assert "pagos_del_mes" in {op["operationId"] for path in spec["paths"].values() for op in path.values()}
    r = client.get("/ia/pagos_del_mes", params={"mes": "2026-11"}).json()
    by = {p["pago"]: p for p in r["pagos"]}
    assert r["pagados"] == 1 and r["total"] == 4
    assert by["🔥 Gas (Ecogas)"]["estado"] == "pagado" and by["🔥 Gas (Ecogas)"]["monto"] == "$ 18.656"
    l = by["💡 Luz (EPEC)"]
    assert l["estado"] == "pendiente" and l["datos"] == ["Cliente: 1111", "Contrato: 2222"]
    assert l["donde_se_paga"] == "https://pagos.example.com/luz" and l["ultimo_monto"] == "$ 21.000 (octubre 2026)"
    c = by["🏠 Casa (cuota a mis viejos)"]
    assert c["estado"] == "atrasado" and c["lo_que_falta_pagar_de_la_deuda"].startswith("Pagaste US$ 10.800 de US$ 145.904")
    assert client.get("/ia/pagos_del_mes").json()["mes"] == "2026-11"
    assert client.get("/ia/pagos_del_mes", params={"mes": "2026-01"}).json()["pagos"] == []


def test_month_still_going_and_evolution_average(conn, monkeypatch):
    """This month is never a result yet; the average leaves out incomplete months; the table fits."""
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(reports, "refresh_rates", lambda conn: None)
    monkeypatch.setattr(main, "date", _Nov6)
    for month, spent in (("2026-09", 100_000_00), ("2026-10", 300_000_00), ("2026-11", 50_000_00)):
        for acc in ("bbva", "bbva_tarjeta", "mercadopago"):
            conn.execute("INSERT INTO transactions (account, date, month, description, amount, kind, who)"
                         " VALUES (?, ?, ?, 'x', ?, 'expense', 'nahuel')", (acc, month + "-10", month, -spent // 3))
        conn.execute("INSERT INTO transactions (account, date, month, description, amount, kind, who)"
                     " VALUES ('bbva', ?, ?, 'Sueldo', 1_000_000_00, 'income', 'nahuel')", (month + "-01", month))
    conn.execute("DELETE FROM transactions WHERE month = '2026-10' AND account = 'mercadopago'")  # October incomplete
    conn.commit()
    client = TestClient(main.app)
    page = client.get("/mes?m=2026-11").text
    assert "Hasta hoy te quedan" in page and "El mes sigue" in page and "hero partial" in page
    page = client.get("/mes?m=2026-09").text
    assert ">Te quedaron" in page and "hero partial" not in page
    page = client.get("/evolucion").text
    # Only September is finished and complete: $ 1.000.000 - $ 100.000.
    assert "$ 900.000</strong> por mes (solo el mes completo:\n    sep 26)" in page.replace("\r", "")
    assert ">$ 900 mil<" in page and ">$ 1,0 M<" in page and "en curso" in page and "incompleto" in page
    assert main.short_ars(-84_500_000) == "−$ 845 mil" and main.short_ars(1_032_000_000) == "$ 10,3 M"
    assert "/api/pagos/pagar" in client.get("/atajo").text
