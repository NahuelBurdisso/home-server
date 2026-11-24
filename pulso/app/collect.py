"""The collector. Every minute it writes down what the Mac, its containers, its services and the
internet connection are doing (samples); two listeners write down what happens (events): every
phone alert that goes through ntfy, and every container that stops, crashes or turns unhealthy.

Nothing here can change the server: Glances, Uptime Kuma's /metrics and socket-proxy (Docker,
read-only) are only read."""
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import db, store

log = logging.getLogger("pulso")

GLANCES = os.environ.get("GLANCES_URL", "http://host.docker.internal:61208/api/4")
DOCKER = os.environ.get("DOCKER_URL", "http://socket-proxy:2375")
KUMA = os.environ.get("KUMA_URL", "http://host.docker.internal:3011")
ADGUARD = os.environ.get("ADGUARD_URL", "http://host.docker.internal:3000")
LAN_PREFIX = os.environ.get("PULSO_LAN_PREFIX", "192.168.1.")
MAC_IP = os.environ.get("PULSO_MAC_IP", "192.168.1.200")
NTFY = os.environ.get("NTFY_URL", "http://host.docker.internal:8091")
NTFY_TOPIC_FILE = os.environ.get("NTFY_TOPIC_FILE", "/run/secrets/ntfy-topic")
ROUTER = os.environ.get("PULSO_ROUTER", "192.168.1.1")
DISK = os.environ.get("PULSO_DISK", "/System/Volumes/Data")
NIC = os.environ.get("PULSO_NIC", "en0")
DISKIO = os.environ.get("PULSO_DISKIO", "disk0")
INTERVAL = 60
PUBLIC_URL = os.environ.get("PULSO_URL", "https://mac-server.tailnet-ejemplo.ts.net:3015")

# Internet = either of these answers a TCP connection (a ping needs root in a container).
# The router is measured apart, to tell "the Wi-Fi/router is down" from "the provider is down".
PROBES = {"inet.cf": ("1.1.1.1", 443), "inet.google": ("8.8.8.8", 443), "lan.router": (ROUTER, 80)}


def get_json(url: str, timeout: float = 5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def get_text(url: str, timeout: float = 5) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode()


def tcp_ms(host: str, port: int, timeout: float = 3.0) -> float | None:
    """Milliseconds to open a TCP connection, or None when nobody answered."""
    t = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return round((time.monotonic() - t) * 1000, 1)
    except OSError:
        return None


def docker_mem(stats: dict) -> float | None:
    """What `docker stats` shows: usage minus the page cache the kernel can drop."""
    m = stats.get("memory_stats") or {}
    if "usage" not in m:
        return None
    return m["usage"] - (m.get("stats") or {}).get("inactive_file", 0)


KUMA_LINE = re.compile(r'^(monitor_status|monitor_response_time)\{([^}]*)\}\s+(\S+)$')
KUMA_NAME = re.compile(r'monitor_name="((?:[^"\\]|\\.)*)"')


def parse_kuma(text: str) -> dict:
    """Uptime Kuma's Prometheus page -> {"svc.<name>.up": 1/0/None, "svc.<name>.ms": ...}.
    Status 1 = up, 0 = down; 2 (pending) and 3 (maintenance) count as neither."""
    out = {}
    for line in text.splitlines():
        m = KUMA_LINE.match(line)
        if not m:
            continue
        name = KUMA_NAME.search(m[2])
        if not name:
            continue
        key, value = name[1], float(m[3])
        if m[1] == "monitor_status":
            out[f"svc.{key}.up"] = 1.0 if value == 1 else 0.0 if value == 0 else None
        elif value >= 0:
            out[f"svc.{key}.ms"] = value
    return out


class Collector:
    """Keeps the previous counters, so rates (network, disk, container CPU) are the average of
    the whole minute instead of whatever Glances measured in its last two seconds."""

    def __init__(self):
        self.prev: dict[str, tuple[float, float]] = {}

    def rate(self, key: str, counter: float, now: float, scale: float = 1.0) -> float | None:
        before = self.prev.get(key)
        self.prev[key] = (now, counter)
        if not before or counter < before[1] or now <= before[0]:
            return None  # first sample, or the counter restarted (reboot)
        return (counter - before[1]) / (now - before[0]) * scale

    def glances(self, now: float) -> dict:
        names = ["cpu", "mem", "memswap", "load", "fs", "network", "diskio", "sensors"]
        with ThreadPoolExecutor(len(names)) as ex:
            got = dict(zip(names, ex.map(lambda n: _try(get_json, f"{GLANCES}/{n}"), names)))
        out = {}
        if got["cpu"]:
            out["sys.cpu"] = got["cpu"].get("total")
        if got["mem"]:
            out["sys.mem"] = got["mem"].get("percent")
            out["sys.mem_used"] = got["mem"].get("used")
        if got["memswap"]:
            out["sys.swap"] = got["memswap"].get("percent")
        if got["load"]:
            out["sys.load"] = got["load"].get("min1")
        for fs in got["fs"] or []:
            if fs.get("mnt_point") == DISK:
                out["sys.disk"] = fs.get("percent")
                out["sys.disk_free"] = fs.get("free")
        for nic in got["network"] or []:
            if nic.get("interface_name") == NIC:
                out["net.rx"] = self.rate("net.rx", nic["bytes_recv_gauge"], now)
                out["net.tx"] = self.rate("net.tx", nic["bytes_sent_gauge"], now)
        for d in got["diskio"] or []:
            if d.get("disk_name") == DISKIO:
                out["io.read"] = self.rate("io.read", d["read_bytes_gauge"], now)
                out["io.write"] = self.rate("io.write", d["write_bytes_gauge"], now)
        for s in got["sensors"] or []:
            if s.get("type") == "battery":
                out["bat.pct"] = s.get("value")
                out["bat.ac"] = 0.0 if str(s.get("status")).lower() == "discharging" else 1.0
        return out

    def containers(self, now: float) -> dict:
        listed = [c for c in _try(get_json, f"{DOCKER}/containers/json?all=1") or [] if is_service(c.get("Labels") or {})]
        running = [c for c in listed if c.get("State") == "running"]
        out = {"sys.ctr_running": float(len(running)), "sys.ctr_total": float(len(listed))} if listed else {}

        def one(c):
            return c, _try(get_json, f"{DOCKER}/containers/{c['Id']}/stats?stream=false&one-shot=true")

        with ThreadPoolExecutor(8) as ex:
            for c, st in ex.map(one, running):
                if not st:
                    continue
                name = c["Names"][0].lstrip("/")
                cpu = (st.get("cpu_stats") or {})
                total = (cpu.get("cpu_usage") or {}).get("total_usage")
                system = cpu.get("system_cpu_usage")
                cpus = cpu.get("online_cpus") or 1
                # CPU % as `docker stats` shows it (100% = one core), from the change since last minute.
                if total is not None and system:
                    before = self.prev.get(f"ctr.{name}.cpu")
                    self.prev[f"ctr.{name}.cpu"] = (system, total)
                    if before and system > before[0] and total >= before[1]:
                        out[f"ctr.{name}.cpu"] = round((total - before[1]) / (system - before[0]) * cpus * 100, 2)
                out[f"ctr.{name}.mem"] = docker_mem(st)
        return out

    def services(self) -> dict:
        text = _try(get_text, f"{KUMA}/metrics")
        return parse_kuma(text) if text else {}

    def dns(self) -> dict:
        """AdGuard's own numbers (no login: Tailscale is the login), kept as they come: queries and
        blocked are its rolling 24 h sums; the average answer time includes the upstreams."""
        st = _try(get_json, f"{ADGUARD}/control/stats")
        if not st:
            return {}
        out = {"dns.queries_24h": float(st.get("num_dns_queries") or 0),
               "dns.blocked_24h": float(st.get("num_blocked_filtering") or 0),
               "dns.ms": round((st.get("avg_processing_time") or 0) * 1000, 2)}
        # Devices on the home Wi-Fi that really ask AdGuard (the router may hand out its own DNS).
        log_ = _try(get_json, f"{ADGUARD}/control/querylog?limit=1000")
        if log_:
            lan = {q.get("client") for q in log_.get("data") or []}
            out["dns.lan_clients"] = float(len({c for c in lan if c and c.startswith(LAN_PREFIX) and c != MAC_IP}))
        return out

    def probes(self) -> dict:
        with ThreadPoolExecutor(len(PROBES)) as ex:
            got = ex.map(lambda kv: (kv[0], tcp_ms(*kv[1])), PROBES.items())
        return dict(got)

    def sample(self) -> dict:
        now = time.time()
        with ThreadPoolExecutor(5) as ex:
            parts = [ex.submit(self.glances, now), ex.submit(self.containers, now),
                     ex.submit(self.services), ex.submit(self.probes), ex.submit(self.dns)]
            out = {}
            for p in parts:
                try:
                    out.update(p.result(timeout=40))
                except Exception as e:  # one source down never stops the others
                    log.warning("sample part failed: %s", e)
        return out


_last_error: dict[str, float] = {}


def _try(fn, url):
    """None when a source doesn't answer; logged at most once every 10 minutes per source."""
    try:
        return fn(url)
    except Exception as e:
        src = url.split("/")[2]
        if time.time() - _last_error.get(src, 0) > 600:
            _last_error[src] = time.time()
            log.warning("%s: %s", url, e)
        return None


# --- the threads ------------------------------------------------------------------------------

def sampler():
    conn = db.connect()
    c = Collector()
    c.sample()  # primes the counters; the first stored minute already has rates
    while True:
        ts = (int(time.time()) // INTERVAL + 1) * INTERVAL
        time.sleep(max(0, ts - time.time()))
        try:
            store.insert(conn, ts, c.sample())
            store.rollup(conn, ts)
        except Exception:
            log.exception("sampler")


NTFY_OK_TAGS = {"green_circle", "white_check_mark", "heavy_check_mark", "electric_plug", "+1"}
NTFY_BAD_TAGS = {"red_circle", "warning", "rotating_light", "x", "fire", "battery", "skull"}


def ntfy_level(m: dict) -> str:
    tags = set(m.get("tags") or [])
    if tags & NTFY_BAD_TAGS:
        return "alerta"
    if tags & NTFY_OK_TAGS:
        return "ok"
    return "alerta" if (m.get("priority") or 3) >= 4 else "info"


def ntfy_listener():
    """Every message on the server's alert topic. ntfy keeps 72 h, so after a restart it
    replays what was missed (since=<last id>); here it is kept for good."""
    try:
        topic = open(NTFY_TOPIC_FILE).read().strip()
    except OSError:
        log.warning("no ntfy topic at %s: alerts are not logged", NTFY_TOPIC_FILE)
        return
    conn = db.connect()
    while True:
        since = db.get_meta(conn, "ntfy_since") or "all"
        try:
            with urllib.request.urlopen(f"{NTFY}/{topic}/json?since={since}", timeout=120) as r:
                for line in r:  # ntfy sends a keepalive every 45 s
                    m = json.loads(line)
                    if m.get("event") != "message" or PULSO_TAG in (m.get("tags") or []):
                        continue
                    store.add_event(conn, m["time"], "ntfy", ntfy_level(m), m.get("title") or "Aviso",
                                    m.get("message") or "", ref="ntfy:" + m["id"])
                    db.set_meta(conn, "ntfy_since", m["id"])
        except Exception as e:
            log.info("ntfy stream: %s (reconnecting)", e)
            time.sleep(15)


def is_service(labels: dict) -> bool:
    """A container of the server's compose files, not a one-off: `docker compose run` (tests) and
    the restore drill's temporary copies come and go and would look like crashes. Compose sets
    oneoff=False on every service container; a plain `docker run` of a compose-built image (tests,
    the drill's `drill-*` copies) carries the image's project label but no oneoff at all."""
    return labels.get("com.docker.compose.oneoff") == "False"


def docker_event(ev: dict, state: dict) -> tuple[str, str, str] | None:
    """(level, title, body) worth keeping, or None for the noise (exec, attach, ...).
    `state` remembers, per container, its last health and when it was last sent a signal."""
    action = ev.get("Action") or ev.get("status") or ""
    attrs = (ev.get("Actor") or {}).get("Attributes") or {}
    if not is_service(attrs):  # an event's attributes carry the container's labels
        return None
    name = attrs.get("name", "?")
    mine = state.setdefault(name, {})
    if action == "kill":
        mine["killed"] = int(ev.get("time") or time.time())
        return None
    if action == "die":
        code = attrs.get("exitCode", "?")
        # Stopped on purpose (update, restart, shutdown): Docker signals it first, and one that
        # doesn't stop in 10 s gets killed (137), which is not a crash either.
        if code in ("0", "143") or int(ev.get("time") or 0) - mine.get("killed", 0) <= 30:
            return "info", f"{name} se detuvo", ""
        return "alerta", f"{name} se cayó", f"Código de salida {code}"
    if action == "oom":
        return "alerta", f"{name} se quedó sin memoria", ""
    if action == "start":
        mine.pop("killed", None)  # a crash right after this start is a crash, not that old stop
        return "info", f"{name} arrancó", ""
    if action.startswith("health_status"):
        health = action.split(":", 1)[-1].strip()
        before, mine["health"] = mine.get("health"), health
        if health == "unhealthy":
            return "alerta", f"{name} no responde (unhealthy)", ""
        if health == "healthy" and before == "unhealthy":
            return "ok", f"{name} volvió a responder", ""
    return None


PULSO_TAG = "pulso"  # its own alerts on ntfy: already in the log, so the ntfy listener skips them
_alerted: dict[str, float] = {}


def alert_phone(title: str, body: str, key: str, every: int = 900, click: str = "/eventos?f=docker",
                priority: int = 4, tags: tuple[str, ...] = ("warning",)) -> bool:
    """A crash or OOM on the phone, at most once every 15 minutes per container: Uptime Kuma
    only checks web pages, so a database or a worker that dies would otherwise go unnoticed.
    Red and Guardia use it too, with their own page as the click. True if ntfy took it."""
    if os.environ.get("PULSO_ALERTS", "on") == "off" or time.time() - _alerted.get(key, 0) < every:
        return False
    try:
        topic = open(NTFY_TOPIC_FILE).read().strip()
        req = urllib.request.Request(NTFY, method="POST", data=json.dumps({
            "topic": topic, "title": title, "message": body or "Detalle en Pulso → eventos.", "priority": priority,
            "tags": [*tags, PULSO_TAG], "click": PUBLIC_URL + click}).encode())
        urllib.request.urlopen(req, timeout=10).close()
        _alerted[key] = time.time()
        return True
    except Exception as e:
        log.warning("ntfy alert failed: %s", e)
        return False


def docker_listener():
    """Containers that start, stop, crash or turn unhealthy. After a restart of this app it
    asks Docker for what happened since the last one it saw."""
    conn = db.connect()
    state: dict[str, dict] = {}
    started = time.time()
    flt = urllib.parse.quote(json.dumps({"type": ["container"]}))
    while True:
        since = db.get_meta(conn, "docker_since") or str(int(time.time()))
        try:
            with urllib.request.urlopen(f"{DOCKER}/events?since={since}&filters={flt}", timeout=None) as r:
                for line in r:
                    ev = json.loads(line)
                    got = docker_event(ev, state)
                    if got:
                        level, title, body = got
                        ref = f"docker:{ev.get('timeNano')}:{ev.get('id', '')[:12]}:{ev.get('Action')}"
                        new = store.add_event(conn, int(ev["time"]), "docker", level, title, body, ref=ref)
                        # Not old ones replayed after a long absence; a crash while Pulso was down
                        # for a restart or an update (minutes) still reaches the phone.
                        if new and level == "alerta" and int(ev["time"]) >= started - 3600:
                            alert_phone(title, body, title.split(" ", 1)[0])
                    db.set_meta(conn, "docker_since", str(int(ev["time"])))  # same second again: ref dedups
        except Exception as e:
            log.info("docker events: %s (reconnecting)", e)
            time.sleep(15)


SPEED_HOURS = int(os.environ.get("PULSO_SPEEDTEST_HOURS", "6"))  # 0 = never
SPEED_DOWN = "https://speed.cloudflare.com/__down?bytes=25000000"
SPEED_UP = "https://speed.cloudflare.com/__up"


def speed_once() -> dict:
    """Download 25 MB and upload 10 MB from Cloudflare's speed test: Mbit/s each way.
    About 140 MB a day at the default 4 tests."""
    out = {}
    ua = {"User-Agent": "pulso/1 (mac-server speed test)"}  # Cloudflare refuses Python's default one
    t = time.monotonic()
    with urllib.request.urlopen(urllib.request.Request(SPEED_DOWN, headers=ua), timeout=120) as r:
        size = sum(len(chunk) for chunk in iter(lambda: r.read(1 << 16), b""))
    out["inet.down_mbps"] = round(size * 8 / (time.monotonic() - t) / 1e6, 1)
    body = os.urandom(10_000_000)
    t = time.monotonic()
    req = urllib.request.Request(SPEED_UP, data=body, method="POST", headers={**ua, "Content-Type": "application/octet-stream"})
    urllib.request.urlopen(req, timeout=120).close()
    out["inet.up_mbps"] = round(len(body) * 8 / (time.monotonic() - t) / 1e6, 1)
    return out


def speedtest():
    """Every SPEED_HOURS hours, at minute 17 (away from the backups and other jobs on the hour)."""
    if SPEED_HOURS <= 0:
        return
    conn = db.connect()
    while True:
        now = time.time()
        slot = SPEED_HOURS * 3600
        nxt = (int(now) // slot + 1) * slot + 17 * 60
        if nxt - slot > now:  # this slot's minute 17 hasn't come yet
            nxt -= slot
        time.sleep(max(1, nxt - now))
        ts = int(time.time()) // INTERVAL * INTERVAL
        try:
            values = speed_once()
        except Exception as e:
            log.info("speed test: %s", e)
            values = {"inet.down_mbps": None, "inet.up_mbps": None}
        try:
            store.insert(conn, ts, values)
        except Exception:  # a locked database once must not stop the speed tests for good
            log.exception("speed test: could not save it")


def disk_watch():
    """Once a day: if the disk, at its pace of the last 30 days, reaches the 15% reserve within
    45 days, say so (at most once a week). disk-alert.sh only speaks once it's already at 85%."""
    conn = db.connect()
    while True:
        time.sleep(3600)
        if time.localtime().tm_hour != 10:
            continue
        try:
            disk_check(conn)
        except Exception:  # one bad day must not end the forecast alerts until the next restart
            log.exception("disk watch")


def disk_check(conn):
    f = store.disk_forecast(conn, int(time.time()))
    last = float(db.get_meta(conn, "disk_alert_at") or 0)
    if f.get("status") == "bajando" and f["days_left"] < 45 and time.time() - last > 7 * 86400:
        title = "El disco se está llenando"
        # per_day is the slope of free space: negative while it fills.
        body = (f"Al ritmo de los últimos 30 días ({-f['per_day'] / 1e9:.1f} GB por día) llega al 85% "
                f"en unos {f['days_left']:.0f} días ({f['date']}). Mirá qué crece en Pulso → gráficos.")
        if alert_phone(title, body, "disk", every=0):  # not sent: try again tomorrow, not in a week
            db.set_meta(conn, "disk_alert_at", str(time.time()))
            # The ntfy listener skips Pulso's own alerts, so it is logged here.
            store.add_event(conn, int(time.time()), "pulso", "alerta", title, body)


WEATHER_URL = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
               "&current=temperature_2m,precipitation,wind_gusts_10m,weather_code&timezone=auto")
LAT, LON = os.environ.get("PULSO_LAT", "-31.42"), os.environ.get("PULSO_LON", "-64.18")  # Córdoba


def weather_once() -> dict:
    """Outside, now: to see whether the internet or the power go when it storms."""
    req = urllib.request.Request(WEATHER_URL.format(lat=LAT, lon=LON), headers={"User-Agent": "pulso/1 (mac-server)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        cur = json.load(r)["current"]
    return {"wx.temp": cur.get("temperature_2m"), "wx.rain": cur.get("precipitation"),
            "wx.gust": cur.get("wind_gusts_10m"), "wx.code": cur.get("weather_code")}


def weather():
    """Every 10 minutes (open-meteo refreshes about every 15)."""
    conn = db.connect()
    while True:
        try:
            values = {k: v for k, v in weather_once().items() if v is not None}
            store.insert(conn, int(time.time()) // INTERVAL * INTERVAL, values)
        except Exception as e:
            log.info("weather: %s", e)
        time.sleep(600)


def start():
    from . import guardia  # it uses this module's Docker access and alert_phone
    for target in (sampler, ntfy_listener, docker_listener, speedtest, disk_watch, weather, guardia.weekly):
        threading.Thread(target=target, name=target.__name__, daemon=True).start()
