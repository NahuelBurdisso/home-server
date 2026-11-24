"""Red: who is on the home Wi-Fi. A container can't see the home network (OrbStack's NAT), so the
Mac scans it every 2 minutes (setup/red-scan.sh: ping the /24, read the ARP table) and posts what
it found here; this keeps every device ever seen, who each one belongs to, and so who is home.

Rules (pulso/DISENO-red-guardia.md):
- the first accepted scan is the baseline: everything in it is known, nothing is announced;
- a scan counts only if the router is in it (the Mac's Wi-Fi down reads as an empty scan, not as
  "everybody left");
- a MAC never seen before is announced once, on the phone, and stays "desconocido" until named;
- a device (and so its person) is away only after PULSO_RED_AWAY_MIN minutes *and* 3 accepted
  scans without it, both counted from the last accepted scan: a stopped script, a sleeping Mac or
  a restart of Pulso never send anyone away. Arrival is immediate."""
import json
import logging
import os
import re
import threading
import time
import urllib.request
from datetime import datetime

from . import collect, db, store

log = logging.getLogger("pulso")

AWAY_MIN = int(os.environ.get("PULSO_RED_AWAY_MIN", "15"))  # a sleeping iPhone skips scans
AWAY_SCANS = 3
KINDS = {"telefono": "teléfono", "compu": "compu", "tv": "tele", "parlante": "parlante", "otro": "otro"}
BROADCAST = "ff:ff:ff:ff:ff:ff"


def norm_mac(raw) -> str | None:
    """'8a:9a:e:AA:bb:cc' (macOS drops leading zeros) or '8A-9A-0E-...' -> '8a:9a:0e:aa:bb:cc'."""
    parts = re.split(r"[:-]", str(raw or "").strip().lower())
    if len(parts) != 6 or not all(re.fullmatch(r"[0-9a-f]{1,2}", p) for p in parts):
        return None
    return ":".join(p.zfill(2) for p in parts)


def is_unicast(mac: str) -> bool:
    """Not the broadcast address nor a multicast group (01:00:5e:..., 33:33:...): those are not devices."""
    return mac != BROADCAST and not int(mac[:2], 16) & 1


def is_private(mac: str) -> bool:
    """A locally administered address (second hex digit 2, 6, a or e): what phones make up, one per
    network. If the phone rotates it, the same phone shows up as new."""
    return bool(int(mac[:2], 16) & 2)


def _here(row, now: int, scan: int) -> bool:
    """Still home: seen in the last AWAY_MIN minutes, or fewer than 3 accepted scans went by
    without it. `now` is the last accepted scan, never the wall clock."""
    return now - row["last_seen"] < AWAY_MIN * 60 or scan - row["seen_scan"] < AWAY_SCANS


def _people(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT person FROM red_devices WHERE person != '' AND present = 1 ORDER BY person")]


def label(row) -> str:
    """How a device is called on the page and in the alerts."""
    if row["name"]:
        return row["name"]
    return ("dirección privada " if is_private(row["mac"]) else "") + (row["last_ip"] or row["mac"])


def ingest(conn, scan: dict, now: float | None = None) -> dict:
    """One scan from the Mac: {"iface", "router", "devices": [{"ip", "mac"}]}."""
    now = int(now or time.time())
    router = str(scan.get("router") or collect.ROUTER)
    seen: dict[str, str] = {}
    for d in scan.get("devices") or []:
        if not isinstance(d, dict):
            continue
        mac = norm_mac(d.get("mac"))
        if mac and is_unicast(mac):
            seen[mac] = str(d.get("ip") or "")[:45]
    if router not in seen.values():
        return {"ok": False, "ignorado": "el router no está en el escaneo: la Wi-Fi del Mac no anda"}
    notices = []  # (evento, persona, dispositivo, level, title, body, mac)
    with db.tx(conn):
        n = int(db.get_meta(conn, "red.scans") or 0) + 1
        baseline = n == 1
        rows = {r["mac"]: r for r in conn.execute("SELECT * FROM red_devices")}
        for mac, ip in seen.items():
            row = rows.get(mac)
            if row is None:
                conn.execute("""INSERT INTO red_devices (mac, known, first_seen, last_seen, last_ip, present, seen_scan)
                                VALUES (?, ?, ?, ?, ?, 1, ?)""", (mac, int(baseline), now, now, ip, n))
                if not baseline:
                    conn.execute("INSERT INTO red_log (ts, mac, what) VALUES (?, ?, 'nuevo')", (now, mac))
                    body = f"{ip} · {mac}" + (" · dirección privada (seguramente un teléfono)" if is_private(mac) else "")
                    notices.append(("nuevo", "", ip or mac, "alerta", "Equipo nuevo en la Wi-Fi",
                                    body + "\nNombralo u olvidalo en Pulso → red.", mac))
                continue
            conn.execute("UPDATE red_devices SET last_seen = ?, last_ip = ?, present = 1, seen_scan = ? WHERE mac = ?",
                         (now, ip, n, mac))
            if not row["present"]:
                conn.execute("INSERT INTO red_log (ts, mac, what) VALUES (?, ?, 'entro')", (now, mac))
        for mac, row in rows.items():
            if mac not in seen and row["present"] and not _here(row, now, n):
                conn.execute("UPDATE red_devices SET present = 0 WHERE mac = ?", (mac,))
                conn.execute("INSERT INTO red_log (ts, mac, what) VALUES (?, ?, 'salio')", (now, mac))
        before = set(json.loads(db.get_meta(conn, "red.people") or "[]"))
        home = _people(conn)
        for p in sorted(before - set(home)):
            notices.append(("salio", p, "", "info", f"{p} salió", "", None))
        if before and not home:
            notices.append(("nadie", "", "", "info", "No queda nadie en casa", "", None))
        for p in sorted(set(home) - before):
            mine = [label(r) for r in conn.execute("SELECT * FROM red_devices WHERE person = ? AND present = 1", (p,))]
            notices.append(("llego", p, ", ".join(mine), "info", f"{p} llegó", ", ".join(mine), None))
        if home and not before:
            notices.append(("volvio", "", "", "info", f"Volvió alguien a casa ({', '.join(home)})", "", None))
        for _, _, _, level, title, body, _ in notices:
            store.add_event(conn, now, "red", level, title, body)
        present = conn.execute("SELECT COUNT(*) FROM red_devices WHERE present = 1").fetchone()[0]
        db.set_meta(conn, "red.scans", str(n))
        db.set_meta(conn, "red.last_scan", str(now))
        db.set_meta(conn, "red.people", json.dumps(home, ensure_ascii=False))
    store.insert(conn, now // 60 * 60, {"red.devices": float(present), "red.home": float(len(home))})
    tell(conn, notices, home, now)
    return {"ok": True, "presentes": present, "nuevos": sum(1 for x in notices if x[0] == "nuevo"),
            "linea_base": baseline}


def tell(conn, notices: list, home: list[str], now: int):
    """The phone and the webhook. A new device always reaches the phone; people's comings and goings
    only with the setting on (off by default). Nothing here blocks or fails the scan."""
    people_too = db.get_meta(conn, "red.avisar_personas") == "1"
    hook = db.get_meta(conn, "red.webhook") or ""
    stamp = datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")
    for evento, persona, dispositivo, _, title, body, mac in notices:
        if evento == "nuevo":
            collect.alert_phone(title, body, f"red:{mac}", every=0, click="/red")
        elif people_too:
            collect.alert_phone(title, body or "Pulso → red.", f"red:{evento}:{persona}", every=0, click="/red",
                                priority=3, tags=("house",))
        if hook:
            _fire(hook, {"evento": evento, "persona": persona, "dispositivo": dispositivo, "en_casa": home, "ts": stamp})


def _fire(url: str, payload: dict):
    """Best effort, 5 s, in its own thread: a slow n8n never holds the scan."""
    threading.Thread(target=_post, args=(url, payload), name="red-webhook", daemon=True).start()


def _post(url: str, payload: dict):
    try:
        req = urllib.request.Request(url, method="POST", data=json.dumps(payload, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).close()
    except Exception as e:
        log.warning("red webhook: %s", e)


# --- naming, forgetting, settings (the page's forms) -------------------------------------------

def name_device(conn, mac: str, name: str, kind: str, person: str) -> bool:
    """"Es de casa": known from now on. Who is home is recounted silently: naming a phone that is
    here must not announce that its owner just arrived."""
    mac = norm_mac(mac)
    if not mac:
        return False
    with db.tx(conn):
        done = conn.execute("UPDATE red_devices SET name = ?, kind = ?, person = ?, known = 1 WHERE mac = ?",
                            (name.strip()[:60], kind if kind in KINDS else "", person.strip()[:40], mac)).rowcount
        db.set_meta(conn, "red.people", json.dumps(_people(conn), ensure_ascii=False))
    return bool(done)


def forget(conn, mac: str) -> bool:
    """As if it had never been seen: its row and its log go. If it ever shows up again, it is new
    again (and announced again)."""
    mac = norm_mac(mac)
    if not mac:
        return False
    with db.tx(conn):
        done = conn.execute("DELETE FROM red_devices WHERE mac = ?", (mac,)).rowcount
        conn.execute("DELETE FROM red_log WHERE mac = ?", (mac,))
        db.set_meta(conn, "red.people", json.dumps(_people(conn), ensure_ascii=False))
    return bool(done)


def set_settings(conn, avisar: bool, webhook: str):
    webhook = webhook.strip()
    if not re.match(r"https?://[^\s]+$", webhook):
        webhook = ""
    with db.tx(conn):
        db.set_meta(conn, "red.avisar_personas", "1" if avisar else "0")
        db.set_meta(conn, "red.webhook", webhook[:500])


# --- what the page, the API and /ia read -------------------------------------------------------

def device(row, now: float) -> dict:
    return {"mac": row["mac"], "name": row["name"], "label": label(row), "kind": row["kind"],
            "kind_label": KINDS.get(row["kind"], ""), "person": row["person"], "known": bool(row["known"]),
            "ip": row["last_ip"], "private": is_private(row["mac"]), "present": bool(row["present"]),
            "first_seen": row["first_seen"], "last_seen": row["last_seen"],
            "ago_min": max(0, int(now - row["last_seen"]) // 60)}


def view(conn, now: float | None = None) -> dict:
    now = now or time.time()
    rows = [device(r, now) for r in conn.execute("SELECT * FROM red_devices ORDER BY name = '', name, last_ip")]
    last = int(db.get_meta(conn, "red.last_scan") or 0)
    names = {d["mac"]: d["label"] for d in rows}
    logs = [{"ts": r["ts"], "mac": r["mac"], "what": r["what"], "label": names.get(r["mac"], r["mac"])}
            for r in conn.execute("SELECT * FROM red_log ORDER BY ts DESC, id DESC LIMIT 60")]
    return {
        "home": json.loads(db.get_meta(conn, "red.people") or "[]"),
        "unknown": sorted((d for d in rows if not d["known"]), key=lambda d: -d["first_seen"]),
        "present": [d for d in rows if d["present"]],
        "known": [d for d in rows if d["known"]],
        "log": logs, "last_scan": last or None,
        # The script runs every 2 minutes: after 10 without one, say so.
        "stale_min": int(now - last) // 60 if last and now - last > 600 else None,
        "scans": int(db.get_meta(conn, "red.scans") or 0),
        "avisar": db.get_meta(conn, "red.avisar_personas") == "1",
        "webhook": db.get_meta(conn, "red.webhook") or "",
        "away_min": AWAY_MIN, "kinds": KINDS,
    }


def unknown_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM red_devices WHERE known = 0").fetchone()[0]


def api(conn) -> dict:
    v = view(conn)
    out = lambda d: {"nombre": d["label"], "tipo": d["kind"], "persona": d["person"], "ip": d["ip"], "mac": d["mac"],
                     "direccion_privada": d["private"], "visto_hace_min": d["ago_min"]}
    return {"en_casa": v["home"], "dispositivos": [out(d) for d in v["present"]],
            "desconocidos": [out(d) for d in v["unknown"]], "ultimo_scan": v["last_scan"]}
