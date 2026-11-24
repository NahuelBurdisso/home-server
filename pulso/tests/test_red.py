"""Red: who is on the home Wi-Fi (app/red.py). No network: the phone and the webhook are faked."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import db, red

ROUTER = ("192.168.1.1", "84:c6:65:00:00:01")
TV = ("192.168.1.9", "b4:19:74:00:00:02")
PHONE = ("192.168.1.17", "8a:9a:e:aa:bb:cc")   # as macOS prints it: no leading zero
LAPTOP = ("192.168.1.18", "14:c1:4e:00:00:03")
STRANGER = ("192.168.1.66", "de:ad:be:ef:00:01")
T = 1_792_000_800


def scan(*devices, router=True):
    devs = ([ROUTER] if router else []) + list(devices)
    return {"iface": "en0", "router": ROUTER[0], "devices": [{"ip": ip, "mac": mac} for ip, mac in devs]}


@pytest.fixture
def phone(monkeypatch):
    """What reached the phone, and the webhooks that would have gone out."""
    got = {"phone": [], "hook": []}
    monkeypatch.setattr(red.collect, "alert_phone", lambda title, body, key, **kw: got["phone"].append((title, key)) or True)
    monkeypatch.setattr(red, "_fire", lambda url, payload: got["hook"].append((url, payload)))
    return got


def events(conn):
    return [(r["level"], r["title"]) for r in conn.execute("SELECT * FROM events WHERE source = 'red' ORDER BY id")]


def test_mac_normalization():
    assert red.norm_mac("8a:9a:e:AA:bb:cc") == "8a:9a:0e:aa:bb:cc"
    assert red.norm_mac("8A-9A-0E-AA-BB-CC") == "8a:9a:0e:aa:bb:cc"
    assert red.norm_mac("(incomplete)") is None and red.norm_mac("") is None and red.norm_mac("1:2:3") is None
    assert not red.is_unicast("ff:ff:ff:ff:ff:ff") and not red.is_unicast("01:00:5e:00:00:fb")
    assert red.is_unicast("84:c6:65:00:00:01")
    assert red.is_private("8a:9a:0e:aa:bb:cc") and red.is_private("fa:f5:83:00:00:04")
    assert not red.is_private("84:c6:65:00:00:01")


def test_baseline_is_silent(conn, phone):
    got = red.ingest(conn, scan(TV, PHONE), now=T)
    assert got["ok"] and got["linea_base"] and got["presentes"] == 3
    assert events(conn) == [] and phone["phone"] == []
    assert conn.execute("SELECT COUNT(*) FROM red_devices WHERE known = 1").fetchone()[0] == 3
    assert conn.execute("SELECT mac FROM red_devices WHERE last_ip = '192.168.1.17'").fetchone()[0] == "8a:9a:0e:aa:bb:cc"


def test_scan_without_the_router_is_ignored(conn, phone):
    red.ingest(conn, scan(TV), now=T)
    got = red.ingest(conn, scan(router=False), now=T + 120)  # the Mac's Wi-Fi down: empty scan
    assert not got["ok"]
    assert db.get_meta(conn, "red.scans") == "1" and db.get_meta(conn, "red.last_scan") == str(T)
    for i in range(20):  # however many: nobody leaves
        red.ingest(conn, scan(router=False), now=T + 240 + i * 120)
    assert conn.execute("SELECT COUNT(*) FROM red_devices WHERE present = 1").fetchone()[0] == 2


def test_a_new_device_alerts_exactly_once(conn, phone):
    red.ingest(conn, scan(TV), now=T)
    red.ingest(conn, scan(TV, STRANGER), now=T + 120)
    assert events(conn) == [("alerta", "Equipo nuevo en la Wi-Fi")]
    assert phone["phone"] == [("Equipo nuevo en la Wi-Fi", "red:de:ad:be:ef:00:01")]
    t = T + 240
    for _ in range(15):  # it leaves for half an hour, then comes back: not new any more
        red.ingest(conn, scan(TV), now=t)
        t += 120
    red.ingest(conn, scan(TV, STRANGER), now=t)
    assert len(phone["phone"]) == 1 and len(events(conn)) == 1
    whats = [r[0] for r in conn.execute("SELECT what FROM red_log ORDER BY id")]
    assert whats == ["nuevo", "salio", "entro"]
    assert red.view(conn, t)["unknown"][0]["mac"] == STRANGER[1]  # still "desconocido" until named


def _sofia(conn):
    red.ingest(conn, scan(TV, PHONE), now=T)
    assert red.name_device(conn, PHONE[1], "iPhone de Sofía", "telefono", "Sofía")
    assert db.get_meta(conn, "red.people") == '["Sofía"]'  # naming a phone that is here announces nothing


def test_away_needs_the_minutes_and_three_scans(conn, phone):
    _sofia(conn)
    t = T
    for _ in range(7):  # 14 minutes without the phone, 7 scans: a sleeping iPhone, still home
        t += 120
        red.ingest(conn, scan(TV), now=t)
    assert json.loads(db.get_meta(conn, "red.people")) == ["Sofía"]
    red.ingest(conn, scan(TV), now=t + 120)  # 16 minutes
    assert json.loads(db.get_meta(conn, "red.people")) == []
    assert events(conn) == [("info", "Sofía salió"), ("info", "No queda nadie en casa")]


def test_minutes_without_three_scans_send_nobody_away(conn, phone):
    _sofia(conn)
    red.ingest(conn, scan(TV), now=T + 20 * 60)  # 20 minutes, but only 2 scans after the sighting
    red.ingest(conn, scan(TV), now=T + 22 * 60)
    assert json.loads(db.get_meta(conn, "red.people")) == ["Sofía"]
    red.ingest(conn, scan(TV), now=T + 24 * 60)
    assert json.loads(db.get_meta(conn, "red.people")) == []


def test_a_gap_in_the_scans_sends_nobody_away(conn, phone):
    _sofia(conn)
    red.ingest(conn, scan(TV), now=T + 3 * 3600)  # the Mac slept 3 hours: one scan after it
    assert json.loads(db.get_meta(conn, "red.people")) == ["Sofía"]
    red.ingest(conn, scan(TV, PHONE), now=T + 3 * 3600 + 120)  # and she is there
    assert events(conn) == []


def test_arrival_is_immediate_and_the_house_events(conn, phone):
    _sofia(conn)
    t = T
    for _ in range(8):
        t += 120
        red.ingest(conn, scan(TV), now=t)
    red.ingest(conn, scan(TV, PHONE), now=t + 120)  # one sighting is enough
    assert events(conn)[-2:] == [("info", "Sofía llegó"), ("info", "Volvió alguien a casa (Sofía)")]
    assert conn.execute("SELECT body FROM events WHERE title = 'Sofía llegó'").fetchone()[0] == "iPhone de Sofía"
    assert red.view(conn, t + 120)["home"] == ["Sofía"]
    samples = {k: v for k, v in red.store.latest(conn, "red.", within=10**10).items()}
    assert samples == {"red.devices": 3.0, "red.home": 1.0}


def test_two_people_only_the_last_one_empties_the_house(conn, phone):
    red.ingest(conn, scan(TV, PHONE, LAPTOP), now=T)
    red.name_device(conn, PHONE[1], "iPhone", "telefono", "Sofía")
    red.name_device(conn, LAPTOP[1], "MacBook", "compu", "Nahuel")
    t = T
    for _ in range(8):  # Sofía leaves, Nahuel stays
        t += 120
        red.ingest(conn, scan(TV, LAPTOP), now=t)
    assert events(conn) == [("info", "Sofía salió")]


def test_people_reach_the_phone_only_when_asked(conn, phone):
    _sofia(conn)
    t = T
    for _ in range(8):
        t += 120
        red.ingest(conn, scan(TV), now=t)
    assert phone["phone"] == []  # off by default: the events log only
    red.set_settings(conn, True, "")
    red.ingest(conn, scan(TV, PHONE), now=t + 120)
    assert [title for title, _ in phone["phone"]] == ["Sofía llegó", "Volvió alguien a casa (Sofía)"]


def test_webhook_payload(conn, phone):
    red.set_settings(conn, False, "http://host.docker.internal:5678/webhook/casa")
    _sofia(conn)
    red.ingest(conn, scan(TV, PHONE, STRANGER), now=T + 120)
    t = T + 120
    for _ in range(8):
        t += 120
        red.ingest(conn, scan(TV, STRANGER), now=t)
    hooks = [p for _, p in phone["hook"]]
    assert {u for u, _ in phone["hook"]} == {"http://host.docker.internal:5678/webhook/casa"}
    assert [h["evento"] for h in hooks] == ["nuevo", "salio", "nadie"]
    assert hooks[0]["dispositivo"] == STRANGER[0] and hooks[0]["persona"] == "" and hooks[0]["en_casa"] == ["Sofía"]
    assert hooks[1]["persona"] == "Sofía" and hooks[1]["en_casa"] == []
    assert set(hooks[0]) == {"evento", "persona", "dispositivo", "en_casa", "ts"} and hooks[0]["ts"].startswith("20")
    red.set_settings(conn, False, "javascript:alert(1)")  # not a URL: no webhook
    assert db.get_meta(conn, "red.webhook") == ""


def test_naming_and_forgetting_from_the_page(conn, phone):
    from app import main
    now = int(time.time())
    c = TestClient(main.app)
    assert c.post("/api/red", json=scan(TV)).json()["linea_base"]
    assert c.post("/api/red", json=scan(TV, STRANGER)).json()["nuevos"] == 1
    page = c.get("/red").text
    assert "desconocidos" in page and STRANGER[1] in page and "es de casa" in page
    assert "Hay un equipo desconocido en la Wi-Fi." in c.get("/api/frase").text
    api = c.get("/api/red").json()
    assert api["desconocidos"][0]["mac"] == STRANGER[1] and api["ultimo_scan"] >= now
    assert set(api) == {"en_casa", "dispositivos", "desconocidos", "ultimo_scan"}
    r = c.post("/red/dispositivo", data={"mac": STRANGER[1], "name": "Tele del living", "kind": "tv", "person": ""},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/red"
    assert c.get("/api/red").json()["desconocidos"] == []
    assert "desconocido" not in c.get("/api/frase").text
    assert "Tele del living" in c.get("/red").text
    assert "Tele del living" in [d["nombre"] for d in c.get("/ia/red").json()["conectados"]]
    assert c.get("/ia/casa").json()["en_casa"] == "nadie"
    c.post("/red/olvidar", data={"mac": STRANGER[1]})
    assert conn.execute("SELECT COUNT(*) FROM red_devices WHERE mac = ?", (STRANGER[1],)).fetchone()[0] == 0
    c.post("/api/red", json=scan(TV, STRANGER))  # forgotten: it comes back as new
    assert len(phone["phone"]) == 2
    c.post("/red/ajustes", data={"avisar": "1", "webhook": "http://n8n:5678/webhook/x"})
    assert db.get_meta(conn, "red.avisar_personas") == "1" and db.get_meta(conn, "red.webhook") == "http://n8n:5678/webhook/x"
    c.post("/red/ajustes", data={"webhook": ""})
    assert db.get_meta(conn, "red.avisar_personas") == "0"
    assert c.post("/red/olvidar", data={"mac": STRANGER[1]}, headers={"sec-fetch-site": "cross-site"}).status_code == 403


def test_stale_scans_are_shown(conn, phone):
    red.ingest(conn, scan(TV), now=time.time() - 3600)
    v = red.view(conn)
    assert 59 <= v["stale_min"] <= 61
    from app import main
    assert "Sin datos desde hace" in TestClient(main.app).get("/red").text
