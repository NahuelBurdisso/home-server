"""Run inside the container:  docker compose run --rm hoy python -m pytest -q"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main, sources

TODAY = date.today()
WEATHER = {
    "current": {"temperature_2m": 24.4, "apparent_temperature": 25.1, "weather_code": 2, "wind_gusts_10m": 20},
    "hourly": {"time": [f"{TODAY}T{h:02d}:00" for h in range(12, 24)], "temperature_2m": [24] * 12,
               "precipitation_probability": [0, 0, 10, 40, 80, 60, 10, 0, 0, 0, 0, 0], "precipitation": [0] * 12,
               "weather_code": [2, 2, 3, 95, 99, 95, 3, 1, 1, 0, 0, 0], "wind_gusts_10m": [20, 20, 30, 65, 70, 40, 20, 10, 10, 10, 10, 10]},
    "daily": {"temperature_2m_min": [14.2], "temperature_2m_max": [29.6], "precipitation_probability_max": [80],
              "sunrise": [f"{TODAY}T06:40"], "sunset": [f"{TODAY}T19:50"]},
}
DATA = {
    "weather": WEATHER,
    "holidays": [{"fecha": (TODAY + timedelta(days=3)).isoformat(), "nombre": "Día de prueba"}],
    "agenda": {"recordatorios": [{"titulo": "Dentista", "fecha": TODAY.isoformat(), "hora": "18:00"},
                                 {"titulo": "Cumple de Ana", "fecha": (TODAY + timedelta(days=1)).isoformat(), "hora": None}]},
    "lista": {"cantidad": 3, "por_seccion": {"Almacén": ["Yerba"], "Lácteos y fiambres": ["Leche", "Queso"]}},
    "plata": {"mep": 1549, "para_revisar": 18,
              "vencimientos": [{"cuenta": "BBVA Visa", "vence": (TODAY + timedelta(days=5)).isoformat(), "total": "$ 250.000"}]},
    "papeles": {"documentos": [{"titulo": "Póliza auto", "vence": (TODAY + timedelta(days=20)).isoformat(), "link": "x"}]},
    "server": {"luz": "verde", "avisos": [], "internet_24h": 100.0},
    "mesa": {"empezo": True, "desde": "2026-11-16", "valor": 10320.0, "retorno_pct": 3.2, "vara_valor": 10210.0,
             "vara_retorno_pct": 2.1, "exceso_pts": 1.1, "efectivo_pct": 40.0,
             "posiciones": [{"ticker": "MSFT", "peso_pct": 12.5, "pnl_pct": 4.0}, {"ticker": "VOO", "peso_pct": 30.0, "pnl_pct": 1.0}],
             "pendientes": [{"ticker": "KO"}], "pausada": False, "ultima_decision": {"ts": 1, "resumen": "x"},
             "proxima_sesion": "jueves", "dias_para_veredicto": 300,
             "frase": "Claude va 3,2 % desde el comienzo y la vara 2,1 %: un punto arriba."},
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "ALLOWED", {"yo@github"})
    return TestClient(main.app)


def test_everything_on_one_page(client, monkeypatch):
    monkeypatch.setattr(sources, "gather", lambda: DATA)
    page = client.get("/").text
    for text in ("24°", "Algo nublado", "Posible granizo", "Ráfagas de hasta 70 km/h", "Dentista", "Mañana: Cumple de Ana",
                 "Día de prueba", "Yerba, Leche, Queso", "$ 1.549", "BBVA Visa vence", "Póliza auto", "Todo bien"):
        assert text in page, text


def test_an_app_down_leaves_only_its_box_empty(client, monkeypatch):
    monkeypatch.setattr(sources, "gather", lambda: {**{k: None for k in DATA}, "lista": DATA["lista"]})
    page = client.get("/").text
    assert client.get("/").status_code == 200
    assert "Recordatorios no respondió" in page and "Finanzas no respondió" in page and "Yerba" in page


def test_quick_boxes_forward_the_sentence(client, monkeypatch):
    sent = []
    monkeypatch.setattr(sources, "post_text", lambda url, text: (sent.append((url, text)) or (True, "Anotado: yerba.")))
    r = client.post("/lista", data={"texto": "yerba"}, follow_redirects=False)
    assert r.status_code == 303 and "Anotado" in r.headers["location"].replace("%3A", ":").replace("%20", " ")
    client.post("/recordar", data={"texto": "mañana 18hs dentista"}, follow_redirects=False)
    assert sent[0][0].endswith(":3016/api/agregar") and sent[1][0].endswith(":3014/api/rapido")


def test_weather_and_holiday_helpers():
    assert sources.sky(99) == ("tormenta con granizo", "⛈️") and sources.sky(0, night=True)[1] == "🌙"
    h = sources.next_holiday([{"fecha": "2026-11-23", "nombre": "Soberanía"}], date(2026, 11, 18))
    assert h["days"] == 5 and h["long"]  # a Monday
    assert sources.next_holiday([{"fecha": "mal"}], TODAY) is None
    assert sources.weather_view(None) is None


def test_owner_only(client):
    assert client.get("/", headers={"x-forwarded-for": "1.2.3.4"}).status_code == 403
    assert client.get("/health").status_code == 200


def test_csrf_from_another_site_is_refused(client, monkeypatch):
    monkeypatch.setattr(sources, "post_text", lambda url, text: (True, "ok"))
    assert client.post("/lista", data={"texto": "x"}, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert client.post("/lista", data={"texto": "x"}, headers={"sec-fetch-site": "same-origin"}, follow_redirects=False).status_code == 303


def test_null_hours_from_the_weather_dont_break_the_page(client, monkeypatch):
    w = {**WEATHER, "hourly": {**WEATHER["hourly"], "temperature_2m": [None] + [24] * 11,
                               "wind_gusts_10m": [None] * 12, "precipitation_probability": [None] * 12}}
    v = sources.weather_view(w)
    assert len(v["hours"]) == 11 and v["warn"] == ["posible granizo en las próximas horas"]
    assert sources.weather_view({"current": {"temperature_2m": None}}) is None
    monkeypatch.setattr(sources, "gather", lambda: {**DATA, "weather": {"current": {}}})
    assert client.get("/").status_code == 200


def test_new_year_holiday_in_december():
    h = sources.next_holiday([{"fecha": "2027-01-01", "nombre": "Año nuevo"}, {"fecha": "2026-12-25", "nombre": "Navidad"}],
                             date(2026, 12, 27))
    assert h["name"] == "Año nuevo" and h["days"] == 5


def test_spoken_morning(client, monkeypatch):
    monkeypatch.setattr(sources, "gather", lambda: DATA)
    text = client.get("/api/frase").text
    assert text.startswith("Hoy es ") and "Hacen 24 grados, algo nublado, con mínima de 14 y máxima de 30" in text
    assert "Atención: posible granizo" in text and "Tenés a las 18:00 Dentista." in text
    assert "En la lista falta yerba, leche y queso." in text and "server" not in text  # a quiet server isn't said


def test_mesa_card_and_sentence(client, monkeypatch):
    monkeypatch.setattr(sources, "gather", lambda: DATA)
    page = client.get("/").text
    assert "+3,2 %" in page and "La vara (S&amp;P): +2,1 %" in page and "+1,1 puntos arriba" in page and ":3020" in page
    assert "MSFT 12,5% · VOO 30%" in page and "1 orden pendiente" in page and "en pausa" not in page
    assert "Claude va 3,2 % desde el comienzo" in client.get("/api/frase").text
    # behind the vara, paused
    behind = {**DATA, "mesa": {**DATA["mesa"], "pausada": True, "retorno_pct": -1.5, "exceso_pts": -3.6, "pendientes": []}}
    monkeypatch.setattr(sources, "gather", lambda: behind)
    page = client.get("/").text
    assert "−1,5 %" in page and "−3,6 puntos abajo" in page and "en pausa" in page and "pendiente" not in page
    # not started yet: no numbers
    new = {**DATA, "mesa": {"empezo": False, "frase": "La mesa todavía no empezó."}}
    monkeypatch.setattr(sources, "gather", lambda: new)
    page = client.get("/").text
    assert "Todavía no empezó" in page and "La vara" not in page
    assert "La mesa todavía no empezó." in client.get("/api/frase").text


def test_mesa_card_disappears_when_mesa_is_down_or_not_for_you(client, monkeypatch):
    monkeypatch.setattr(sources, "gather", lambda: {**DATA, "mesa": None})
    r = client.get("/")
    assert r.status_code == 200 and "Mesa" not in r.text and "La mesa" not in client.get("/api/frase").text
    # the 503 of a mesa that doesn't operate yet comes back as None; a strange answer is the same
    monkeypatch.setattr(sources, "gather", lambda: {**DATA, "mesa": {"error": "x"}})
    assert client.get("/").status_code == 200
    # an odd shape inside an otherwise normal payload leaves the card out too
    odd = {**DATA, "mesa": {**DATA["mesa"], "posiciones": [{"peso_pct": "x"}], "retorno_pct": "?"}}
    monkeypatch.setattr(sources, "gather", lambda: odd)
    r = client.get("/")
    assert r.status_code == 200 and "La vara" not in r.text
    # Sofía opens Hoy, but Mesa isn't hers
    monkeypatch.setattr(sources, "gather", lambda: DATA)
    monkeypatch.setattr(main, "ALLOWED", {"yo@github", "ella@github"})
    monkeypatch.setattr(main, "MESA_LOGINS", {"yo@github"})
    hdr = {"Tailscale-User-Login": "ella@github", "X-Forwarded-For": "1.2.3.4"}
    assert "Mesa" not in client.get("/", headers=hdr).text and "La mesa" not in client.get("/api/frase", headers=hdr).text
    hdr["Tailscale-User-Login"] = "yo@github"
    assert "Mesa" in client.get("/", headers=hdr).text


# --- Review of 2026-11-06 ----------------------------------------------------------------------

def test_mesa_left_out_when_the_bot_asks_for_someone_else(client, monkeypatch):
    """Mensajes' «buen día» comes straight from its container (no login): mesa=0 for anyone but Nahuel."""
    monkeypatch.setattr(sources, "gather", lambda: DATA)
    assert "Claude va 3,2 %" in client.get("/api/frase").text
    assert "Claude va 3,2 %" not in client.get("/api/frase?mesa=0").text


def test_odd_holidays_or_server_answers_dont_break_the_page(client, monkeypatch):
    monkeypatch.setattr(sources, "get", lambda url, **k: {"error": "rate limited"} if "feriados" in url else None)
    assert sources.gather()["holidays"] == []
    monkeypatch.setattr(sources, "gather", lambda: {**DATA, "server": {"luz": "amarillo"}})  # no "avisos"
    assert client.get("/").status_code == 200 and client.get("/api/frase").status_code == 200


def test_weather_copy_too_old_is_not_shown_as_now(monkeypatch):
    import time as t
    url = "http://weather.invalid/x"
    sources._cache[url] = (t.time() - 4 * 3600, {"current": {}})

    def fail(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(sources.urllib.request, "urlopen", fail)
    assert sources.get(url, ttl=600, stale=3 * 3600) is None
    assert sources.get(url, ttl=600) == {"current": {}}  # no limit: any copy (the holidays)


# --- Pagos del mes (Finanzas' "pagos" in /api/estado) ----------------------------------------------

def pagos(*faltan, total=4):
    return {"mes": TODAY.isoformat()[:7], "nombre": "noviembre", "total": total, "pagados": total - len(faltan),
            "faltan": [{"id": i + 1, "nombre": n.capitalize(), "corto": n, "dia": d.day, "vence": d.isoformat(),
                        "atrasado": d < TODAY} for i, (n, d) in enumerate(faltan)], "frase": "…"}


def test_bills_left_on_the_plata_card(client, monkeypatch):
    late, soon = TODAY - timedelta(days=2), TODAY + timedelta(days=4)
    data = {**DATA, "plata": {**DATA["plata"], "pagos": pagos(("agua", soon), ("municipal", late))}}
    monkeypatch.setattr(sources, "gather", lambda: data)
    page = client.get("/").text
    assert f"🧾 Falta pagar: agua (el {soon.day}), municipal (venció el {late.day})" in page
    assert ":3013/pagos" in page
    data["plata"]["pagos"] = pagos()
    assert "Pagos del mes al día" in client.get("/").text and "Falta pagar" not in client.get("/").text


def test_no_bills_or_an_odd_answer_leave_the_card_as_it_was(client, monkeypatch):
    for odd in (None, "x", [], {"faltan": None}, {"faltan": [{"corto": "gas"}]}, {"faltan": [{"corto": "gas", "vence": "mal"}]},
                {"faltan": "agua"}, {}):
        data = {**DATA, "plata": {**DATA["plata"], "pagos": odd}}
        monkeypatch.setattr(sources, "gather", lambda: data)
        r = client.get("/")
        assert r.status_code == 200 and "BBVA Visa vence" in r.text, odd
        assert "Falta pagar" not in r.text and "Pagos del mes" not in r.text, odd
        f = client.get("/api/frase")
        assert f.status_code == 200 and "pagar" not in f.text, odd


def test_bills_due_soon_or_late_are_said_in_the_morning(client, monkeypatch):
    d = lambda n: TODAY + timedelta(days=n)  # noqa: E731
    data = {**DATA, "plata": {**DATA["plata"], "pagos": pagos(("gas", d(1)), ("luz", d(0)), ("agua", d(3)),
                                                             ("casa", d(-5)), ("municipal", d(4)))}}
    monkeypatch.setattr(sources, "gather", lambda: data)
    text = client.get("/api/frase").text
    assert "Falta pagar el gas: vence mañana." in text and "Falta pagar la luz: vence hoy." in text
    assert "Falta pagar el agua: vence en 3 días." in text
    assert f"Falta pagar la casa: venció el {d(-5).day}." in text and "municipal" not in text  # 4 days: quiet


def test_bill_helpers():
    today = date(2026, 11, 6)
    p = {"faltan": [{"corto": "expensas", "vence": "2026-11-04"}, {"corto": "gas", "vence": "2026-11-20"}]}
    assert sources.pagos_frases(p, today) == ["Falta pagar las expensas: venció el 4."]
    assert sources.pagos_view(p, today) == {"al_dia": False, "texto": "expensas (venció el 4), gas (el 20)"}
    assert sources.pagos_view(None, today) is None and sources.pagos_frases(None, today) == []
