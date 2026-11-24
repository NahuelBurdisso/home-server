"""Run inside the container:  docker compose run --rm recordatorios python -m pytest -q"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import db, ics, notify, when
from app.parse import parse

NOW = datetime(2026, 10, 21, 10, 0)  # a Wednesday


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("RECORDATORIOS_ALERTS", "off")
    monkeypatch.setenv("RECORDATORIOS_IA", "none")  # tests never call a real model
    return db.connect()


def rem(conn, date, time=None, repeat="none", alerts="0", title="Algo", created="2026-10-21 09:00:00"):
    cur = conn.execute("INSERT INTO reminders (title, date, time, repeat, alerts, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (title, date, time, repeat, alerts, created))
    conn.commit()
    return conn.execute("SELECT * FROM reminders WHERE id = ?", (cur.lastrowid,)).fetchone()


@pytest.mark.parametrize("text, title, date, time, repeat", [
    ("mañana 18hs dentista", "Dentista", "2026-10-22", "18:00", "none"),
    ("Dentista mañana a las 18:30", "Dentista", "2026-10-22", "18:30", "none"),
    ("cumple de mamá 15/3", "Cumple de mamá", "2027-03-15", None, "yearly"),
    ("cumpleaños de Juan el 3 de diciembre", "Cumpleaños de Juan", "2026-12-03", None, "yearly"),
    ("el jueves a las 9 llamar al banco", "Llamar al banco", "2026-10-22", "09:00", "none"),
    ("el miércoles reunión", "Reunión", "2026-10-28", None, "none"),  # today is Wednesday: next week's
    ("pagar la tarjeta el 10 todos los meses", "Pagar la tarjeta", "2026-11-10", None, "monthly"),
    ("en 20 minutos sacar la ropa", "Sacar la ropa", "2026-10-21", "10:20", "none"),
    ("en media hora llamar a Ana", "Llamar a Ana", "2026-10-21", "10:30", "none"),
    ("recordame que el 3 de diciembre vence el seguro", "Vence el seguro", "2026-12-03", None, "none"),
    ("a las 5 buscar a los chicos", "Buscar a los chicos", "2026-10-21", "17:00", "none"),
    ("a las 8 gimnasio", "Gimnasio", "2026-10-22", "08:00", "none"),  # 8:00 already passed today
    ("todos los lunes a las 7:30 sacar la basura", "Sacar la basura", "2026-10-26", "07:30", "weekly"),
    ("mañana a la tarde comprar pan", "Comprar pan", "2026-10-22", "17:00", "none"),
    ("esta noche regar las plantas", "Regar las plantas", "2026-10-21", "21:00", "none"),
    ("turno médico 5/11/2026 10.15", "Turno médico", "2026-11-05", "10:15", "none"),
    ("comprar 2 kilos de pan pasado mañana", "Comprar 2 kilos de pan", "2026-10-23", None, "none"),
    ("aniversario 20 de enero de 2015", "Aniversario", "2015-01-20", None, "yearly"),
    ("a las 9 de la noche llamar a papá", "Llamar a papá", "2026-10-21", "21:00", "none"),
])
def test_parse(text, title, date, time, repeat):
    p = parse(text, NOW)
    assert (p["title"], p["date"], p["time"], p["repeat"]) == (title, date, time, repeat)


def test_parse_monthly_day_already_passed_this_month():
    assert parse("vencimiento monotributo los 20 de cada mes", NOW)["date"] == "2026-11-20"


def test_parse_without_date():
    assert parse("comprar pan", NOW) is None
    assert parse("pagar 500 pesos", NOW) is None


def test_parse_default_alerts():
    assert parse("mañana 18hs dentista", NOW)["alerts"] == "60,0"
    assert parse("cumple de mamá 15/3", NOW)["alerts"] == "1440,0"
    assert parse("en 20 minutos sacar la ropa", NOW)["alerts"] == "0"
    assert parse("el 3 de diciembre vence el seguro", NOW)["alerts"] == "0"


def test_repeats(conn):
    month_end = rem(conn, "2026-01-31", "10:00", "monthly")
    assert when.next_on_or_after(month_end, datetime(2026, 2, 1)) == datetime(2026, 2, 28, 10, 0)
    assert when.next_on_or_after(month_end, datetime(2026, 3, 1)) == datetime(2026, 3, 31, 10, 0)
    leap = rem(conn, "2024-02-29", None, "yearly")
    assert when.next_on_or_after(leap, datetime(2026, 1, 1)) == datetime(2026, 2, 28, 9, 0)
    daily = rem(conn, "2020-01-01", "07:00", "daily")
    assert when.next_on_or_after(daily, NOW) == datetime(2026, 10, 22, 7, 0)
    once = rem(conn, "2026-10-01", "07:00")
    assert when.next_on_or_after(once, NOW) is None


def test_years_only_when_the_real_year_was_given(conn):
    mom = rem(conn, "1960-03-15", None, "yearly")
    assert when.years(mom, datetime(2027, 3, 15, 9)) == 67
    no_year = rem(conn, "2027-03-15", None, "yearly")
    assert when.years(no_year, datetime(2027, 3, 15, 9)) is None


def test_texts(conn):
    r = rem(conn, "2026-10-22", "18:00", alerts="60,0", title="Dentista")
    assert when.long_when(r, NOW) == "mañana jueves 22/10 a las 18:00"
    assert when.alerts_text(r) == "Te aviso 1 hora antes y a la hora."
    b = rem(conn, "2027-03-15", None, "yearly", "1440,0")
    assert when.long_when(b, NOW) == "todos los años el 15/3"
    assert when.alerts_text(b) == "Te aviso el día anterior a las 9 y ese día a las 9."
    assert when.when_text(datetime(2026, 10, 24, 9), True, NOW) == "Sábado"
    assert when.when_text(datetime(2027, 3, 15, 9), True, NOW) == "lun 15/3/2027"


def test_alerts_go_out_once(conn):
    rem(conn, "2026-10-21", "12:00", alerts="60,0", title="Dentista")
    out = []
    notify.check(conn, NOW, out.append)  # first pass only looks one minute back
    assert out == []
    notify.check(conn, datetime(2026, 10, 21, 11, 0, 5), out.append)
    assert [m["title"] for m in out] == ["Dentista"]
    assert "en 1 h" in out[0]["message"] and out[0]["priority"] == 3
    notify.check(conn, datetime(2026, 10, 21, 11, 30), out.append)
    db.set_meta(conn, "last_check", "2026-10-21T10:00:00")  # as if the clock went back: still not twice
    notify.check(conn, datetime(2026, 10, 21, 11, 30), out.append)
    assert len(out) == 1
    notify.check(conn, datetime(2026, 10, 21, 12, 0, 10), out.append)
    assert len(out) == 2 and out[1]["priority"] == 4


def test_done_and_snooze(conn):
    r = rem(conn, "2026-10-21", "12:00", alerts="60,0")
    out = []
    notify.check(conn, datetime(2026, 10, 21, 10, 59), out.append)
    notify.check(conn, datetime(2026, 10, 21, 11, 1), out.append)
    assert len(out) == 1
    notify.snooze(conn, r["id"], datetime(2026, 10, 21, 12, 0), datetime(2026, 10, 21, 11, 1), minutes=15)
    notify.check(conn, datetime(2026, 10, 21, 11, 16, 30), out.append)
    assert len(out) == 2
    notify.mark_done(conn, r["id"], datetime(2026, 10, 21, 12, 0))
    notify.check(conn, datetime(2026, 10, 21, 12, 0, 30), out.append)
    assert len(out) == 2  # done: the 12:00 alert never comes


def test_repeating_done_only_skips_that_time(conn):
    r = rem(conn, "2026-10-21", "12:00", "daily")
    notify.mark_done(conn, r["id"], datetime(2026, 10, 21, 12, 0))
    out = []
    notify.check(conn, datetime(2026, 10, 21, 11, 59), out.append)
    notify.check(conn, datetime(2026, 10, 21, 12, 1), out.append)
    notify.check(conn, datetime(2026, 10, 22, 11, 59), out.append)
    notify.check(conn, datetime(2026, 10, 22, 12, 1), out.append)
    assert len(out) == 1


def test_catch_up_after_downtime_is_marked_late(conn):
    rem(conn, "2026-10-21", "09:00", title="Pastilla")
    db.set_meta(conn, "last_check", "2026-10-21T08:00:00")
    out = []
    notify.check(conn, NOW, out.append)
    assert len(out) == 1 and "atrasado" in out[0]["message"]


def test_ntfy_failure_retries(conn):
    rem(conn, "2026-10-21", "10:00")
    db.set_meta(conn, "last_check", "2026-10-21T09:59:00")

    def broken(msg):
        raise OSError("ntfy down")
    with pytest.raises(OSError):
        notify.check(conn, datetime(2026, 10, 21, 10, 0, 30), broken)
    out = []
    notify.check(conn, datetime(2026, 10, 21, 10, 1), out.append)
    assert len(out) == 1


def test_ics(conn):
    rem(conn, "2027-03-15", None, "yearly", title="Cumple de mamá, la mejor")
    rem(conn, "2026-10-22", "18:00", title="Dentista")
    cal = ics.calendar(conn.execute("SELECT * FROM reminders").fetchall())
    assert "DTSTART;VALUE=DATE:20270315" in cal and "RRULE:FREQ=YEARLY" in cal
    assert "SUMMARY:Cumple de mamá\\, la mejor" in cal
    assert "DTSTART:20261022T180000" in cal
    assert all(len(line.encode()) <= 75 for line in cal.split("\r\n"))


@pytest.fixture
def client(conn, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "ALLOWED", {"yo@github"})
    return TestClient(main.app)


def test_api_quick_add(client, conn):
    r = client.post("/api/rapido", json={"texto": "pasado mañana a las 10 turno con el médico"})
    assert r.status_code == 200 and r.text.startswith("Listo. Turno con el médico:")
    assert conn.execute("SELECT title FROM reminders WHERE id = ?", (int(r.headers["X-Id"]),)).fetchone()[0] == "Turno con el médico"
    r = client.post("/api/rapido", content="comprar pan".encode(), headers={"content-type": "text/plain"})
    assert r.status_code == 422
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 1


def test_pages(client):
    client.post("/rapido", data={"texto": "cumple de mamá 15/3"})
    for path in ("/", "/todos", "/ayuda", "/nuevo", "/r/1", "/calendario.ics", "/api/proximos?dias=200"):
        assert client.get(path).status_code == 200, path
    assert client.head("/calendario.ics").status_code == 200
    assert 'href="webcal://mac-server.tailnet-ejemplo.ts.net/recordatorios/calendario.ics"' in client.get("/ayuda").text
    r = client.post("/rapido", data={"texto": "comprar pan"}, follow_redirects=False)
    assert r.headers["location"].startswith("/nuevo?titulo=comprar+pan")
    r = client.post("/nuevo", data={"titulo": "Algo", "fecha": "", "hora": ""})
    assert "Falta la fecha" in r.text
    r = client.post("/r/1", data={"titulo": "Cumple mamá", "fecha": "2027-03-15", "repetir": "yearly",
                                  "avisos": ["1440", "0"]}, follow_redirects=False)
    assert r.status_code == 303


def test_owner_only(client):
    proxied = {"x-forwarded-for": "100.64.0.2"}
    assert client.get("/", headers={**proxied, "tailscale-user-login": "otro@github"}).status_code == 403
    assert client.get("/", headers=proxied).status_code == 403
    assert client.get("/", headers={**proxied, "tailscale-user-login": "Yo@github"}).status_code == 200
    assert client.get("/health", headers=proxied).status_code == 200


def test_api_add_once(client):
    item = {"titulo": "Pagar tarjeta BBVA", "fecha": "2027-01-09", "hora": "10:00", "avisos": [0, 1440, 10080]}
    r = client.post("/api/recordatorio", json=item)
    assert r.status_code == 200 and r.json()["nuevo"] and "1 día antes" in r.json()["texto"]
    again = client.post("/api/recordatorio", json=item).json()
    assert not again["nuevo"] and again["id"] == r.json()["id"]
    assert client.post("/api/recordatorio", json={"titulo": "x", "fecha": "mañana"}).status_code == 422


# --- the local AI: fallback for sentences without a readable date, and tools for Open WebUI ---

from app import ia  # noqa: E402


def test_ai_fallback_is_checked(monkeypatch):
    monkeypatch.setenv("RECORDATORIOS_IA", "ollama")
    asked = []

    def fake(answer):
        def asker(prompt):
            asked.append(prompt)
            return answer
        return asker
    got = ia.understand("el finde largo llevar el auto al service", NOW,
                        fake({"titulo": "llevar el auto al service", "fecha": "2026-11-21", "hora": "", "repetir": "none"}))
    assert got == {"title": "Llevar el auto al service", "date": "2026-11-21", "time": None, "repeat": "none",
                   "alerts": "0", "ia": True}
    assert "miercoles 2026-10-21" in asked[0] and "jueves 22 de octubre: 2026-10-22" in asked[0]
    weekly = {"titulo": "pagar el seguro", "fecha": "2026-10-28", "hora": "", "repetir": "weekly"}
    assert ia.understand("la semana que viene pagar el seguro", NOW, fake(weekly))["repeat"] == "none"
    assert ia.understand("cada semana que viene x", NOW, fake(weekly))["repeat"] == "weekly"
    assert ia.understand("x", NOW, fake({"titulo": "x", "fecha": "2026-10-01", "hora": "", "repetir": "none"})) is None
    assert ia.understand("x", NOW, fake({"titulo": "x", "fecha": "", "hora": "", "repetir": "none"})) is None
    assert ia.understand("x", NOW, fake({"titulo": "x", "fecha": "2026-10-30", "hora": "25:00", "repetir": "none"})) is None
    monkeypatch.setenv("RECORDATORIOS_IA", "none")
    assert ia.understand("x", NOW, fake({})) is None


def test_assistant_tools(conn, monkeypatch):
    from app.main import app
    import app.main as main
    monkeypatch.setattr(main, "now", lambda: NOW)
    client = TestClient(app)
    spec = client.get("/ia/openapi.json").json()
    assert {"proximos_recordatorios", "buscar_recordatorio", "agendar_recordatorio"} <= \
        {op["operationId"] for path in spec["paths"].values() for op in path.values()}
    r = client.post("/ia/agendar", json={"texto": "el jueves a las 9 llamar al banco"}).json()
    assert r["guardado"] and (r["titulo"], r["fecha"], r["hora"]) == ("Llamar al banco", "2026-10-22", "09:00")
    assert client.post("/ia/agendar", json={"texto": "algún día ordenar el garage"}).json()["guardado"] is False
    rem(conn, "1960-03-15", repeat="yearly", title="Cumple de mamá")
    p = client.get("/ia/proximos", params={"dias": 7}).json()
    assert [i["titulo"] for i in p["recordatorios"]] == ["Llamar al banco"] and p["recordatorios"][0]["cuando"]
    b = client.get("/ia/buscar", params={"texto": "cumple mama"}).json()
    assert b["cantidad"] == 1 and b["recordatorios"][0]["fecha"] == "2027-03-15" and b["recordatorios"][0]["cumple_anos"] == 67


@pytest.mark.parametrize("text, date, time", [
    ("a las 12 de la noche cerrar la puerta", "2026-10-22", "00:00"),   # midnight, not noon
    ("el viernes a las 12 de la noche salir", "2026-10-24", "00:00"),   # the night of Friday
    ("mañana 5hs gimnasio", "2026-10-22", "17:00"),                     # like "a las 5"
    ("mañana 18hs dentista", "2026-10-22", "18:00"),
])
def test_parse_clock_edge_cases(text, date, time):
    p = parse(text, NOW)
    assert (p["date"], p["time"]) == (date, time)


def test_parse_month_ends():
    oct30 = datetime(2026, 10, 30, 10, 0)
    assert parse("en un mes pagar el seguro", oct30)["date"] == "2026-11-30"
    nov5 = datetime(2026, 11, 5, 10, 0)
    assert parse("pagar el 31 todos los meses", nov5)["date"] == "2026-11-30"  # not skipped to Dec 31
    assert parse("los 31 de cada mes cerrar caja", nov5)["date"] == "2026-11-30"
    assert parse("el 31 cena", nov5)["date"] == "2026-12-31"  # no repeat: the next real 31st


def test_ics_month_end_and_crlf_notes(conn):
    r = rem(conn, "2026-10-31", None, "monthly", title="Fin de mes")
    conn.execute("UPDATE reminders SET notes = ? WHERE id = ?", ("línea 1\r\nlínea 2", r["id"]))
    rem(conn, "2028-02-29", None, "yearly", title="Bisiesto")
    cal = ics.calendar(conn.execute("SELECT * FROM reminders").fetchall())
    assert "RRULE:FREQ=MONTHLY;BYMONTHDAY=28,29,30,31;BYSETPOS=-1" in cal
    assert "RRULE:FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=28,29;BYSETPOS=-1" in cal
    assert "DESCRIPTION:línea 1\\nlínea 2" in cal and "\r" not in cal.replace("\r\n", "")


def test_editing_notes_keeps_done(client, conn):
    r = rem(conn, "2026-10-21", "22:00", "daily", alerts="60,0")
    conn.execute("UPDATE reminders SET done_until = '2026-10-21T22:00' WHERE id = ?", (r["id"],))
    conn.commit()
    form = {"titulo": "Algo", "fecha": "2026-10-21", "hora": "22:00", "repetir": "daily", "avisos": ["60", "0"]}
    client.post(f"/r/{r['id']}", data={**form, "notas": "solo notas"})
    assert conn.execute("SELECT done_until FROM reminders WHERE id = ?", (r["id"],)).fetchone()[0] == "2026-10-21T22:00"
    client.post(f"/r/{r['id']}", data={**form, "hora": "23:00"})  # a new time starts over
    assert conn.execute("SELECT done_until FROM reminders WHERE id = ?", (r["id"],)).fetchone()[0] is None


def test_csrf_from_another_site_is_refused(client):
    for h, ok in [({"sec-fetch-site": "cross-site"}, False), ({"origin": "https://evil.example"}, False),
                  ({"sec-fetch-site": "same-origin", "origin": "https://mac-server.tailnet-ejemplo.ts.net:3016"}, True),
                  ({}, True)]:  # Siri / curl: no browser headers
        r = client.post("/rapido", data={"texto": "mañana pan"}, headers=h, follow_redirects=False)
        assert (r.status_code != 403) == ok, h


# --- regressions from the review (Nov 2026) ---

EVENING = datetime(2026, 10, 21, 20, 0)  # Wednesday 20:00


@pytest.mark.parametrize("text, at, date, time", [
    ("esta noche a las 10 cerrar el gas", EVENING, "2026-10-21", "22:00"),
    ("esta noche a las 12 salir", EVENING, "2026-10-22", "00:00"),      # tonight's midnight
    ("mañana a la noche a las 9 llamar", NOW, "2026-10-22", "21:00"),
    ("a la tarde a las 7 pagar", NOW, "2026-10-21", "19:00"),
    ("el sábado a la noche a las 11 fiesta", NOW, "2026-10-24", "23:00"),
    ("a las 9 a la noche sacar la basura", NOW, "2026-10-21", "21:00"),
    ("mañana a la mañana a las 5 vuelo", NOW, "2026-10-22", "05:00"),   # not 17:00
    ("a las 9 de la mañana a la noche", NOW, "2026-10-22", "09:00"),    # the time's own part wins
    ("mañana a la mañana a las 12 reunión", NOW, "2026-10-22", "12:00"),
])
def test_parse_part_of_day_says_which_hour(text, at, date, time):
    p = parse(text, at)
    assert (p["date"], p["time"]) == (date, time), p


def test_parse_bare_hour_in_the_evening_is_tonight():
    assert (lambda p: (p["date"], p["time"]))(parse("a las 8 buscar a Juan", datetime(2026, 10, 21, 19, 30))) == \
        ("2026-10-21", "20:00")
    assert parse("hoy a las 9 cena", datetime(2026, 10, 21, 18, 0))["time"] == "21:00"
    assert parse("a las 8 gimnasio", NOW)["date"] == "2026-10-22"  # said in the morning: tomorrow's 8:00
    assert parse("a las 11 algo", datetime(2026, 10, 21, 23, 30))["date"] == "2026-10-22"  # 23:00 gone too
    assert (lambda p: (p["date"], p["time"]))(parse("a las 10 llamar al banco", datetime(2026, 10, 21, 12, 30))) == \
        ("2026-10-22", "10:00")  # 22:00 still far: tomorrow morning
    assert parse("a las 10 llamar", datetime(2026, 10, 21, 15, 0))["time"] == "22:00"
    weekly = parse("todos los miércoles a las 8 clase", datetime(2026, 10, 21, 19, 30))
    assert (weekly["time"], weekly["repeat"]) == ("08:00", "weekly")


@pytest.mark.parametrize("text, time, title", [
    ("dentro de 2 horas sacar el pollo", "12:00", "Sacar el pollo"),
    ("en una hora y media llamar a Ana", "11:30", "Llamar a Ana"),
    ("en 2 horas y cuarto salir", "12:15", "Salir"),
    ("dentro de 20 minutos apagar el horno", "10:20", "Apagar el horno"),
])
def test_parse_in_a_while(text, time, title):
    p = parse(text, NOW)
    assert (p["date"], p["time"], p["title"], p["alerts"]) == ("2026-10-21", time, title, "0"), p


def test_parse_feb_29():
    b = parse("cumple de Juan 29/2", NOW)  # 2027 has no Feb 29: its birthday is the 28th, not skipped
    assert (b["date"], b["repeat"]) == ("2027-02-28", "yearly")
    assert parse("cumple de Juan 29 de febrero", datetime(2027, 3, 1, 10, 0))["date"] == "2028-02-29"
    assert parse("el 29/2 algo", NOW)["date"] == "2028-02-29"  # once: the next real Feb 29
    assert parse("algo 31/4", NOW) is None


def test_weekly_week_before_alert_doesnt_hide_the_at_time_one(conn):
    rem(conn, "2026-10-14", "10:00", "weekly", "10080,0", title="Clase")
    rem(conn, "2026-10-14", "10:00", "daily", "1440,0", title="Pastilla")
    db.set_meta(conn, "last_check", "2026-10-21T09:59:00")
    out = []
    notify.check(conn, datetime(2026, 10, 21, 10, 0, 30), out.append)
    assert [(m["title"], m["message"], m["priority"]) for m in out] == \
        [("Clase", "Hoy 10:00", 4), ("Pastilla", "Hoy 10:00", 4)]


def test_one_refused_alert_doesnt_hold_up_the_others(conn):
    rem(conn, "2026-10-21", "10:00", title="Grande")
    rem(conn, "2026-10-21", "10:00", title="Pastilla")
    db.set_meta(conn, "last_check", "2026-10-21T09:59:00")
    out, refuse = [], {"Grande"}

    def ntfy(msg):
        if msg["title"] in refuse:
            raise OSError("HTTP Error 413")
        out.append(msg)
    with pytest.raises(OSError):
        notify.check(conn, datetime(2026, 10, 21, 10, 0, 30), ntfy)
    assert [m["title"] for m in out] == ["Pastilla"]
    assert db.get_meta(conn, "last_check") == "2026-10-21T10:00:00"  # the next pass starts at the refused one
    refuse.clear()
    notify.check(conn, datetime(2026, 10, 21, 10, 1), ntfy)
    assert [m["title"] for m in out] == ["Pastilla", "Grande"]  # Pastilla not twice


def test_alert_thread_survives_a_database_it_cant_open(conn, monkeypatch):
    opened, checked = [], []

    def connect():
        opened.append(1)
        if len(opened) == 1:
            raise db.sqlite3.OperationalError("database is locked")
        return conn

    class Stop(BaseException):
        pass

    def sleep(_):
        if len(opened) >= 2 and checked:
            raise Stop
    monkeypatch.setattr(notify.db, "connect", connect)
    monkeypatch.setattr(notify, "check", lambda c, now: checked.append(c))
    monkeypatch.setattr(notify.clock, "sleep", sleep)
    with pytest.raises(Stop):
        notify.run_forever()
    assert len(opened) == 2 and checked == [conn]


def test_api_quick_add_doesnt_block_the_app_while_the_ai_thinks(conn, monkeypatch):
    import asyncio
    import time as clock
    import httpx
    from app import main
    monkeypatch.setattr(main.ia, "understand", lambda text, at: clock.sleep(1) or None)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t") as c:
            t0 = clock.monotonic()
            slow = asyncio.create_task(c.post("/api/rapido", json={"texto": "algún día ordenar el garage"}))
            await asyncio.sleep(0.05)
            health = await c.get("/health")
            took = clock.monotonic() - t0
            return health.status_code, took, (await slow).status_code
    code, took, slow = asyncio.run(run())
    assert (code, slow) == (200, 422) and took < 0.5, took


def test_api_quick_add_odd_bodies(client):
    assert client.post("/api/rapido", json="mañana pan").status_code == 200  # a bare JSON string is the text
    assert client.post("/api/rapido", json=["x"]).status_code == 422
    assert client.post("/api/rapido", content=b"{no json", headers={"content-type": "application/json"}).status_code == 422


OLD_SCHEMA = """
CREATE TABLE reminders (id INTEGER PRIMARY KEY, title TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
    date TEXT NOT NULL, time TEXT, repeat TEXT NOT NULL DEFAULT 'none', alerts TEXT NOT NULL DEFAULT '0',
    done_until TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')));
CREATE TABLE snoozes (id INTEGER PRIMARY KEY, reminder_id INTEGER NOT NULL REFERENCES reminders(id) ON DELETE CASCADE,
    occ TEXT NOT NULL, fire_at TEXT NOT NULL);
CREATE TABLE sent (reminder_id INTEGER NOT NULL, fire_at TEXT NOT NULL, PRIMARY KEY (reminder_id, fire_at));
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO reminders (id, title, date, time, repeat, alerts, done_until) VALUES
    (1, 'Cumple', '1960-03-15', NULL, 'yearly', '1440,0', NULL),
    (4, 'Dentista', '2026-10-22', '18:00', 'none', '60,0', '2026-10-21T10:00');
INSERT INTO snoozes (reminder_id, occ, fire_at) VALUES (4, '2026-10-22T18:00', '2026-10-22T17:15');
INSERT INTO sent VALUES (4, '2026-10-22T17:00'), (7, '2026-10-20T09:00');  -- 7: deleted since
"""


def test_old_database_gets_autoincrement_keeping_everything(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(OLD_SCHEMA)
    old.close()
    conn = db.connect(path)
    assert "AUTOINCREMENT" in conn.execute("SELECT sql FROM sqlite_master WHERE name = 'reminders'").fetchone()[0]
    assert [tuple(r) for r in conn.execute("SELECT id, title, time, done_until FROM reminders ORDER BY id")] == \
        [(1, "Cumple", None, None), (4, "Dentista", "18:00", "2026-10-21T10:00")]
    assert conn.execute("SELECT COUNT(*) FROM snoozes WHERE reminder_id = 4").fetchone()[0] == 1  # not cascaded away
    assert conn.execute("SELECT COUNT(*) FROM sent").fetchone()[0] == 2
    new = conn.execute("INSERT INTO reminders (title, date) VALUES ('Nuevo', '2026-11-01')").lastrowid
    assert new == 8  # after 7, the deleted one still in `sent`
    conn.execute("DELETE FROM reminders WHERE id = ?", (new,))
    assert conn.execute("INSERT INTO reminders (title, date) VALUES ('Otro', '2026-11-01')").lastrowid == 9  # not reused
    conn.commit()
    conn.execute("DELETE FROM reminders WHERE id = 4")  # foreign keys still on: its snooze goes with it
    assert conn.execute("SELECT COUNT(*) FROM snoozes").fetchone()[0] == 0
    conn.commit()
    again = db.connect(path)  # nothing to do the second time
    assert [tuple(r) for r in again.execute("SELECT name, seq FROM sqlite_sequence")] == [("reminders", 9)]
    assert again.execute("PRAGMA foreign_keys").fetchone()[0] == 1


# --- for the apps that make reminders (Finanzas' monthly bills) -----------------------------

def test_api_get_says_whether_it_was_done(client, conn):
    r = client.post("/api/recordatorio", json={"titulo": "Pagar luz (EPEC)", "fecha": "2026-11-10",
                                               "avisos": [1440, 0], "notas": "Cliente: 123"}).json()
    got = client.get(f"/api/r/{r['id']}").json()
    assert got == {"id": r["id"], "titulo": "Pagar luz (EPEC)", "fecha": "2026-11-10", "hora": None,
                   "notas": "Cliente: 123", "hecho": False}
    assert client.post(f"/api/r/{r['id']}/hecho?occ=2026-11-10T09:00").status_code == 200
    assert client.get(f"/api/r/{r['id']}").json()["hecho"] is True
    monthly = rem(conn, "2026-11-10", repeat="monthly")
    notify.mark_done(conn, monthly["id"], datetime(2026, 11, 10, 9, 0))
    assert client.get(f"/api/r/{monthly['id']}").json()["hecho"] is False  # it comes back next month
    r404 = client.get("/api/r/999")
    assert r404.status_code == 404 and "error" in r404.json()


def test_notification_opens_the_link_in_the_notes(conn):
    r = rem(conn, "2026-11-10")
    msg = notify.message(r, datetime(2026, 11, 10, 9, 0), NOW, datetime(2026, 11, 10, 9, 0))
    assert msg["click"] == f"{notify.PUBLIC_URL}/r/{r['id']}"
    conn.execute("UPDATE reminders SET notes = ? WHERE id = ?",
                 ("Cliente: 123\nhttps://pagos.example.com/luz.\nDetalle: https://finanzas.example/pagos/1", r["id"]))
    r = conn.execute("SELECT * FROM reminders WHERE id = ?", (r["id"],)).fetchone()
    msg = notify.message(r, datetime(2026, 11, 10, 9, 0), NOW, datetime(2026, 11, 10, 9, 0))
    assert msg["click"] == "https://pagos.example.com/luz"
    assert "Cliente: 123" in msg["message"]


def test_edit_page_shows_the_links_in_the_notes(client, conn):
    r = rem(conn, "2026-11-10")
    conn.execute("UPDATE reminders SET notes = 'Pagalo en https://pagos.example.com/agua' WHERE id = ?", (r["id"],))
    conn.commit()
    assert '<a href="https://pagos.example.com/agua"' in client.get(f"/r/{r['id']}").text
