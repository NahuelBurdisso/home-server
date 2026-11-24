"""Run inside the container:  docker compose run --rm pulso python -m pytest -q"""
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app import charts, collect, db, store

KUMA = '''# HELP monitor_status Monitor Status (1 = UP, 0= DOWN, 2= PENDING, 3= MAINTENANCE)
monitor_status{monitor_id="1",monitor_name="Immich",monitor_type="http",monitor_url="x",monitor_hostname="null",monitor_port="null"} 1
monitor_status{monitor_id="2",monitor_name="AdGuard Home (DNS)",monitor_type="dns",monitor_url="null",monitor_hostname="google.com",monitor_port="53"} 0
monitor_status{monitor_id="3",monitor_name="Pending",monitor_type="http",monitor_url="x",monitor_hostname="null",monitor_port="null"} 2
monitor_response_time{monitor_id="1",monitor_name="Immich",monitor_type="http",monitor_url="x",monitor_hostname="null",monitor_port="null"} 12
monitor_response_time{monitor_id="2",monitor_name="AdGuard Home (DNS)",monitor_type="dns",monitor_url="null",monitor_hostname="google.com",monitor_port="53"} -1
'''

T0 = 1_792_000_800  # a whole hour


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(store, "_ids", {})
    monkeypatch.setenv("PULSO_COLLECT", "off")
    return db.connect()


def fill(conn, key, values, t0=T0, step=60):
    for i, v in enumerate(values):
        store.insert(conn, t0 + i * step, {key: v})


def test_parse_kuma():
    got = collect.parse_kuma(KUMA)
    assert got["svc.Immich.up"] == 1 and got["svc.Immich.ms"] == 12
    assert got["svc.AdGuard Home (DNS).up"] == 0
    assert "svc.AdGuard Home (DNS).ms" not in got  # -1 = no answer, not a time
    assert got["svc.Pending.up"] is None


def test_rate_uses_counters_and_survives_a_reset():
    c = collect.Collector()
    assert c.rate("net.rx", 1000, 100.0) is None
    assert c.rate("net.rx", 7000, 160.0) == 100
    assert c.rate("net.rx", 10, 220.0) is None  # counter went back: rebooted


def test_docker_events():
    state = {}
    compose = {"com.docker.compose.project": "n8n", "com.docker.compose.oneoff": "False"}  # a service container
    ev = lambda action, t=1000, **a: {"Action": action, "time": t, "Actor": {"Attributes": {"name": "n8n", **compose, **a}}}
    assert collect.docker_event(ev("die", exitCode="0"), state)[0] == "info"
    assert collect.docker_event(ev("die", exitCode="137"), state)[:2] == ("alerta", "n8n se cayó")
    assert collect.docker_event(ev("kill", t=2000, signal="15"), state) is None
    assert collect.docker_event(ev("die", t=2011, exitCode="137"), state)[0] == "info"  # stop that timed out
    assert collect.docker_event(ev("exec_start: sh"), state) is None
    assert collect.docker_event(ev("health_status: healthy"), state) is None  # nothing to report
    assert collect.docker_event(ev("health_status: unhealthy"), state)[0] == "alerta"
    assert collect.docker_event(ev("health_status: healthy"), state)[0] == "ok"
    assert collect.docker_event(ev("die", exitCode="1", **{"com.docker.compose.oneoff": "True"}), state) is None
    drill = {"Action": "die", "time": 1, "Actor": {"Attributes": {"name": "drill-finanzas", "exitCode": "1"}}}
    assert collect.docker_event(drill, state) is None


def test_ntfy_level():
    assert collect.ntfy_level({"tags": ["red_circle"], "priority": 5}) == "alerta"
    assert collect.ntfy_level({"tags": ["green_circle"], "priority": 5}) == "ok"
    assert collect.ntfy_level({"priority": 4}) == "alerta"
    assert collect.ntfy_level({}) == "info"


def test_buckets_mix_hourly_and_minutes(conn):
    fill(conn, "sys.cpu", [10.0] * 60 + [30.0] * 60 + [None, 50.0])  # 2 h and 2 minutes
    store.rollup(conn, T0 + 2 * 3600 + 120)
    assert conn.execute("SELECT COUNT(*) FROM hourly").fetchone()[0] == 2
    hours = store.buckets(conn, "sys.cpu", T0, T0 + 3 * 3600, 3600)
    assert [round(b["avg"]) for b in hours] == [10, 30, 50]
    assert hours[2]["fails"] == 1 and hours[2]["n"] == 2  # the last hour comes from the minutes
    minutes = store.buckets(conn, "sys.cpu", T0, T0 + 600, 300)
    assert [b["avg"] for b in minutes] == [10.0, 10.0]


def test_rollup_drops_old_minutes(conn):
    fill(conn, "sys.cpu", [1.0, 2.0])
    later = T0 + (store.RAW_DAYS + 1) * 86400
    store.rollup(conn, later)
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0
    assert conn.execute("SELECT avg FROM hourly").fetchone()[0] == 1.5


def test_outages_need_both_pings_down(conn):
    up, down = 20.0, None
    fill(conn, "inet.cf", [up, down, down, down, up, down, up])
    fill(conn, "inet.google", [up, down, down, down, up, up, up])
    got = store.internet_outages(conn, T0, T0 + 3600)
    assert [(o["start"] - T0, o["minutes"]) for o in got] == [(60, 3)]


def test_outage_shows_the_weather(conn):
    from app import main
    fill(conn, "inet.cf", [20.0, None, None, 20.0])
    fill(conn, "inet.google", [20.0, None, None, 20.0])
    store.insert(conn, T0 + 60, {"wx.code": 95.0, "wx.rain": 6.0, "wx.gust": 70.0})
    o = main.internet_view(conn, T0, T0 + 3600)["outages"][0]
    assert o["weather"] == "⛈️ tormenta, ráfagas 70 km/h"
    assert main.weather_at(conn, T0 + 86400) == ""


def test_power_page_measures_autonomy(conn):
    from app import main
    now = int(main.time.time()) // 60 * 60
    for i in range(20):  # 10 minutes unplugged, 1% every 2 minutes, then back
        store.insert(conn, now - (20 - i) * 60, {"bat.ac": 0.0 if 5 <= i < 15 else 1.0,
                                                  "bat.pct": 80.0 - max(0, min(i, 15) - 5) / 2})
    view = main.power_view(conn, now)
    assert len(view["cuts"]) == 1 and view["cuts"][0]["from"] == 80.0 and view["cuts"][0]["to"] == 75.0
    assert view["per_min"] == pytest.approx(0.5) and view["left_h"] is not None
    with TestClient(main.app) as client:
        assert "aguantaría a batería" in client.get("/energia").text


def test_power_cuts(conn):
    fill(conn, "bat.ac", [1, 0, 0, 1, 1, 0, 1])
    assert [c["minutes"] for c in store.power_cuts(conn, T0, T0 + 3600)] == [2, 1]


def test_uptime_days(conn):
    day = store.day_start(T0)
    fill(conn, "svc.X.up", [1.0] * 98 + [0.0] * 2, t0=day)
    days = store.uptime_days(conn, "svc.X.up", 2, day)
    assert days[0]["up"] is None
    assert days[1]["up"] == pytest.approx(0.98) and days[1]["down_min"] == 2


def test_disk_forecast(conn):
    for h in range(24 * 10):  # 10 days losing 1 GB a day, 100 GB free of ~460
        store.insert(conn, T0 + h * 3600, {"sys.disk_free": 100e9 - h * 1e9 / 24, "sys.disk": 78.0})
    store.rollup(conn, T0 + 240 * 3600)
    f = store.disk_forecast(conn, T0 + 240 * 3600)
    assert f["status"] == "bajando"
    assert f["per_day"] == pytest.approx(-1e9, rel=1e-3)
    assert 25 < f["days_left"] < 35  # ~31 GB above the 15% reserve


def test_chart_breaks_lines_on_gaps():
    b = lambda v: {"avg": v, "min": v, "max": v, "n": 1, "fails": 0 if v is not None else 1}
    html = charts.chart([("CPU", "red", [b(1), b(2), None, b(3)])], T0, T0 + 240, unit="%")
    assert html.count('class="line"') == 1 and str(html).count("M") >= 2
    assert charts.fmt(1_500_000, "B/s") == "1.5 MB/s"
    assert charts.fmt(None, "%") == "—"
    assert charts.nice_max(73) == 100 and charts.nice_max(0.3) == 0.5
    assert 'class="line"' in charts.chart([("CPU", "red", [b(0.0), b(0.0)])], T0, T0 + 120, unit="%")  # idle: no crash
    assert 'class="line"' in charts.chart([("Wi-Fi", "red", [b(-60.0), b(-40.0)])], T0, T0 + 120, unit=" dBm")
    assert 'class="line"' in charts.chart([("Nada", "red", [None, None])], T0, T0 + 120, unit="%")
    heat = str(charts.heatmap({(0, 9): (20.0, 0), (0, 10): (80.0, 0.5)}))
    assert heat.count("<i ") == 7 * 24 and "hsl(130" in heat and "hsl(0" in heat and 'class="fail"' in heat
    assert "Hacen falta" in charts.heatmap({})


def test_pages(conn):
    from app import main
    now = int(time.time()) // 60 * 60
    for i in range(30):
        store.insert(conn, now - i * 60, {"sys.cpu": 12.0, "sys.mem": 60.0, "sys.disk_free": 4e11, "inet.cf": 15.0,
                                          "inet.google": 18.0, "lan.router": 2.0, "bat.ac": 1.0, "bat.pct": 90.0,
                                          "svc.Immich.up": 1.0, "svc.Immich.ms": 9.0, "ctr.caddy.mem": 1.4e7,
                                          "ctr.caddy.cpu": 0.1, "sys.ctr_running": 15.0, "sys.ctr_total": 15.0,
                                          "dns.lan_clients": 0.0})
    store.add_event(conn, now - 30, "ntfy", "alerta", "Backup", "falló", ref="ntfy:1")
    store.add_event(conn, now - 30, "ntfy", "alerta", "Backup", "falló", ref="ntfy:1")  # same id twice: one row
    with TestClient(main.app) as client:
        from datetime import datetime
        stamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        mac = {"time": datetime.now().strftime("%H:%M:%S"), "backup": {"last": stamp, "where": "internal"},
               "simulacro": {"fecha": stamp, "ok": True, "offsite": False}, "tailscale": {"state": "Running", "peers": []},
               "wifi": {"signal_dbm": -40, "rate_mbps": 286}, "macos_updates": 2,
               "bateria": {"cycles": 329, "condition": "Good", "max_capacity_pct": 84},
               "ssd": {"ok": True, "used_pct": 0, "spare_pct": 100, "critical": 0, "media_errors": 0, "temp_c": 33, "written_tb": 22.94}}
        assert client.post("/api/mac", json=mac).json()["ok"]
        store.insert(conn, now - 600, {"inet.down_mbps": 300.0, "inet.up_mbps": 50.0})
        for path in ["/", "/graficos?r=6h", "/graficos?r=1a", "/contenedores", "/internet?r=7d", "/internet?r=1a",
                     "/servicios", "/servicios/Immich?r=1h", "/servicios/Nada", "/contenedores/caddy?r=7d",
                     "/eventos?f=alertas&d=30", "/mapa", "/atajo", "/semana", "/health"]:
            r = client.get(path)
            assert r.status_code == 200, path
        assert "Immich" in client.get("/").text
        assert "300 Mbit/s" in client.get("/internet").text
        page = client.get("/mapa").text
        assert 'class="node up" data-id="immich"' in page and 'class="node na" data-id="lan"' in page
        assert 'class="node up" data-id="backup"' in page
        home = client.get("/").text
        assert "ningún equipo de la casa lo usa" in home
        assert "salud 84% · 329 ciclos" in home and "sano · 0% gastado · 22.94 TB escritos" in home
        assert "backup hace 0 h" in home and "2 actualizaciones esperando" in home and "-40 dBm" in home
        assert client.get("/eventos").text.count("falló") == 1
        assert client.get("/api/frase").text.startswith("Hay algo para mirar")
        assert "sys.cpu" in client.get("/api/series").json()["series"]
        csv = client.get("/api/serie.csv?k=sys.cpu&r=6h").text.splitlines()
        assert csv[0] == "hora,promedio,minimo,maximo,fallas" and csv[-1].endswith(",12,12,12,0")
        assert "falló" in client.get("/eventos?q=backup").text and "falló" not in client.get("/eventos?q=nada").text
        s = client.get("/api/resumen").json()
        assert s["luz"] == "amarillo" and s["alertas_24h"] == 1 and s["contenedores"] == "15/15"
        assert s["internet_cortes_24h"] == 0 and s["contenedores_caidos_7d"] == {}
        assert s["velocidad_7d"]["bajada"] == 300 and s["velocidad_7d"]["subida"] == 50
        assert client.get("/", headers={"x-forwarded-for": "1.2.3.4"}).status_code == 403
        for path in ["/ia/estado", "/ia/internet?dias=3", "/ia/luz", "/ia/servicios", "/ia/contenedores", "/ia/eventos?solo_alertas=true"]:
            assert client.get(path).status_code == 200, path
        assert client.get("/ia/eventos?dias=1").json()["eventos"][0]["titulo"] == "Backup"
        assert "historia_de_internet" in client.get("/ia/openapi.json").text


def test_health_turns_503_when_the_collector_stops(conn, monkeypatch):
    from app import main
    monkeypatch.setenv("PULSO_COLLECT", "on")  # as in production, but nothing writes: stale
    monkeypatch.setattr(main, "STARTED", time.time() - 3600)
    client = TestClient(main.app)  # no lifespan: the collector threads don't start
    assert client.get("/health").status_code == 503
    store.insert(conn, int(time.time()) // 60 * 60, {"inet.cf": 9.0})  # the sampler's probe, every minute
    assert client.get("/health").status_code == 200


def test_failed_write_leaves_no_open_transaction(conn):
    with pytest.raises(Exception):
        store.insert(conn, T0, {"sys.cpu": object()})  # not a value SQLite can store
    assert not conn.in_transaction
    assert "sys.cpu" not in store._ids or store.sid(conn, "sys.cpu") is not None
    store.insert(conn, T0, {"sys.cpu": 5.0})  # the next minute still writes
    assert store.latest(conn, "sys.", within=10**10)["sys.cpu"] == 5.0


def test_hour_buckets_with_an_unaligned_start(conn):
    fill(conn, "x", [0.0] * 60 + [1.0] * 60 + [2.0] * 20)
    store.rollup(conn, T0 + 2 * 3600 + 1200)
    got = store.buckets(conn, "x", T0 + 2400, T0 + 3 * 3600, 3600)  # start 40 min into the first hour
    assert [round(b["avg"], 2) for b in got] == [0.0, 1.0, 2.0]


def test_uptime_never_negative(conn):
    from app import main
    for i in (0, 3, 6):  # three failed pings, minutes apart: the outage bridges the gaps
        store.insert(conn, T0 + i * 60, {"inet.cf": None, "inet.google": None})
    assert main.internet_view(conn, T0, T0 + 3600)["uptime"] == 0.0


def test_replayed_event_is_not_new(conn):
    assert store.add_event(conn, T0, "docker", "alerta", "x se cayó", ref="docker:1")
    assert not store.add_event(conn, T0, "docker", "alerta", "x se cayó", ref="docker:1")


def test_csrf_from_another_site_is_refused(conn):
    from app import main
    client = TestClient(main.app)
    assert client.post("/api/mac", json={}, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert client.post("/api/mac", json={"time": "10:00:00"}).status_code == 200  # the Mac's curl


# --- Review of 2026-11-06 ----------------------------------------------------------------------

IMAGE_LABELS = {"com.docker.compose.project": "pulso", "com.docker.compose.service": "pulso",
                "com.docker.compose.version": "5.5.0"}  # what a compose-built image carries


def test_plain_run_of_a_compose_image_is_not_a_service():
    """Tests and the restore drill `docker run` *:local images: their exit is no crash."""
    assert not collect.is_service(IMAGE_LABELS)
    for name in ("magical_mendel", "drill-pulso"):
        ev = {"Action": "die", "time": 1000, "Actor": {"Attributes": {"name": name, "exitCode": "1", **IMAGE_LABELS}}}
        assert collect.docker_event(ev, {}) is None
    assert collect.is_service({**IMAGE_LABELS, "com.docker.compose.oneoff": "False"})


def test_crash_right_after_a_restart_is_a_crash():
    st = {}
    lab = {"com.docker.compose.project": "n8n", "com.docker.compose.oneoff": "False"}
    ev = lambda a, t, **x: {"Action": a, "time": t, "Actor": {"Attributes": {"name": "n8n", **lab, **x}}}
    collect.docker_event(ev("kill", 1000, signal="15"), st)
    assert collect.docker_event(ev("die", 1001, exitCode="0"), st)[0] == "info"
    assert collect.docker_event(ev("start", 1002), st)[0] == "info"
    assert collect.docker_event(ev("die", 1022, exitCode="1"), st)[:2] == ("alerta", "n8n se cayó")


def test_event_search_and_filters_reach_older_events(conn):
    from app import main
    now = int(time.time())
    store.add_event(conn, now - 40 * 86400, "ntfy", "alerta", "Backup falló", "restic 100%_x", ref="ntfy:old")
    for i in range(1000):
        store.add_event(conn, now - i * 60, "docker", "info", f"x{i} arrancó", ref=f"docker:{i}")
    c = TestClient(main.app)
    assert "Backup falló" in c.get("/eventos?d=365&q=backup").text
    assert "Backup falló" in c.get("/eventos?d=365&f=avisos").text
    assert "Backup falló" in c.get("/eventos?d=365&q=100%25_").text  # % and _ are literal
    assert "Backup falló" not in c.get("/eventos?d=365&q=100%25z").text


def test_speedtest_survives_a_db_error(conn, monkeypatch):
    rounds = {"n": 0}

    class Stop(Exception):
        pass

    def sleep(s):
        rounds["n"] += 1
        if rounds["n"] > 2:
            raise Stop
    monkeypatch.setattr(collect, "SPEED_HOURS", 6)
    monkeypatch.setattr(collect.time, "sleep", sleep)
    monkeypatch.setattr(collect, "speed_once", lambda: {"inet.down_mbps": 1.0})

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(collect.store, "insert", boom)
    with pytest.raises(Stop):  # it went around again instead of ending the thread
        collect.speedtest()


def test_disk_alert_text_and_bookkeeping(conn, monkeypatch):
    monkeypatch.setattr(collect.store, "disk_forecast", lambda c, n: {"status": "bajando", "days_left": 20.0,
                                                                      "per_day": -1.2e9, "date": "2026-11-26"})
    sent = []
    monkeypatch.setattr(collect, "alert_phone", lambda t, b, k, every=900: sent.append(b) and False)  # ntfy down
    collect.disk_check(conn)
    assert "(1.2 GB por día)" in sent[0]
    assert db.get_meta(conn, "disk_alert_at") is None  # not sent: tomorrow it tries again
    monkeypatch.setattr(collect, "alert_phone", lambda t, b, k, every=900: True)
    collect.disk_check(conn)
    assert db.get_meta(conn, "disk_alert_at") is not None
    assert conn.execute("SELECT COUNT(*) FROM events WHERE source = 'pulso'").fetchone()[0] == 1


def test_service_uptime_weighted_by_minutes(conn):
    from app import main
    now = time.time()
    today = store.day_start(now)
    rows = [(today - 86400 + i * 60, 1.0) for i in range(1380)]           # yesterday: 23 h, all up
    rows += [(today + i * 60, 0.0 if i < 5 else 1.0) for i in range(10)]  # today: 10 min, 5 down
    for ts, v in rows:
        store.insert(conn, ts, {"svc.X.up": v})
    s = next(s for s in main.services_view(conn, now, 2) if s["name"] == "X")
    assert s["down_min"] == 5
    assert s["uptime"] == pytest.approx(1 - 5 / 1390)


def test_health_ignores_the_weather_thread(conn, monkeypatch):
    from app import main
    monkeypatch.setenv("PULSO_COLLECT", "on")
    monkeypatch.setattr(main, "STARTED", time.time() - 3600)
    store.insert(conn, int(time.time()) // 60 * 60, {"wx.temp": 20.0})  # sampler dead, weather alive
    assert TestClient(main.app).get("/health").status_code == 503


def test_map_unmeasured_container_is_unknown():
    from app import mapa
    assert mapa.state("ctr.uptime-kuma.mem", {"sys.cpu": 1.0}) == "na"
    assert mapa.state("ctr.uptime-kuma.mem", {"sys.ctr_running": 3.0}) == "down"
