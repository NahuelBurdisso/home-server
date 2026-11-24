#!/usr/bin/python3 -I
"""Live server status as JSON for the Übersicht desktop widget.

Every check runs in parallel with short timeouts, so the whole thing
returns in about 2 seconds even when a service is down.
"""
import json
import os
import re
import subprocess
import time
import urllib.request
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

HOME = os.path.expanduser("~")
TS = "/opt/homebrew/bin/tailscale"
DOCKER = "/usr/local/bin/docker"
RESTIC = "/opt/homebrew/bin/restic"
TS_HOST = "mac-server.tailnet-ejemplo.ts.net"


def sh(*cmd, timeout=3):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def http(url, auth=None, timeout=2):
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", "Basic " + b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            try:
                return r.status, json.loads(body)
            except ValueError:
                return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def up(url):
    code, _ = http(url)
    return 200 <= code < 400


# name, local check URL, port the widget's shortcut opens, role
SERVICES = [
    ("Immich", "http://127.0.0.1:2283/api/server/ping", 2283, "Fotos"),
    ("Jellyfin", "http://127.0.0.1:8096/health", 8096, "Pelis y series"),
    ("Open WebUI", "http://127.0.0.1:3012/health", 3012, "IA local"),
    ("n8n", "http://127.0.0.1:5678/healthz", 5678, "Automatizaciones"),
    ("AdGuard", "http://127.0.0.1:3000/", 3000, "Anti-anuncios"),
    ("Homepage", "http://127.0.0.1:3010/", 3010, "Panel"),
    ("Uptime Kuma", "http://127.0.0.1:3011/", 3011, "Monitoreo"),
    ("ntfy", "http://127.0.0.1:8091/v1/health", 8091, "Alertas"),
    ("Ollama", "http://127.0.0.1:11434/api/version", 11434, "Motor IA"),
    ("Glances", "http://127.0.0.1:61208/api/4/status", 61208, "Gráficos"),
    ("Pulso", "http://127.0.0.1:3015/health", 3015, "Historia"),
    ("Lista", "http://127.0.0.1:3016/health", 3016, "Compras"),
    ("Papeles", "http://127.0.0.1:3017/health", 3017, "Documentos"),
    ("Hoy", "http://127.0.0.1:3018/health", 3018, "La mañana"),
    ("Dozzle", "http://127.0.0.1:8080/healthcheck", 8080, "Logs"),
]
# Opened through the tailnet instead of localhost: they log you in from Tailscale's headers.
OPEN_URL = {"Dozzle": f"https://{TS_HOST}:8080", "n8n": f"https://{TS_HOST}:5678"}


def services():
    with ThreadPoolExecutor(len(SERVICES)) as ex:
        states = list(ex.map(lambda s: up(s[1]), SERVICES))
    return [
        {"name": n, "role": role, "port": port, "up": ok, "url": OPEN_URL.get(n, f"http://localhost:{port}")}
        for (n, _, port, role), ok in zip(SERVICES, states)
    ]


def system():
    _, ql = http("http://127.0.0.1:61208/api/4/quicklook")
    _, cpu = http("http://127.0.0.1:61208/api/4/cpu")
    _, load = http("http://127.0.0.1:61208/api/4/load")
    _, net = http("http://127.0.0.1:61208/api/4/network")
    en0 = next((n for n in (net or []) if n.get("interface_name") == "en0"), {})
    st = os.statvfs("/System/Volumes/Data")
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    batt = sh("pmset", "-g", "batt")
    m = re.search(r"(\d+)%", batt)
    boot = re.search(r"sec = (\d+)", sh("sysctl", "-n", "kern.boottime"))
    ip = sh("ipconfig", "getifaddr", "en0").strip()
    return {
        "cpu": round((cpu or {}).get("total", (ql or {}).get("cpu", 0))),
        "mem": round((ql or {}).get("mem", 0)),
        "load": round((load or {}).get("min5", 0), 1),
        "disk_used_pct": round(100 * (total - free) / total) if total else 0,
        "disk_free_gb": round(free / 1e9),
        "net_down_kbs": round(en0.get("bytes_recv_rate_per_sec", 0) / 1024),
        "net_up_kbs": round(en0.get("bytes_sent_rate_per_sec", 0) / 1024),
        "battery": int(m.group(1)) if m else None,
        "on_ac": "AC Power" in batt,
        "uptime_s": int(time.time()) - int(boot.group(1)) if boot else 0,
        "ip": ip,
        "internet": sh("ping", "-c", "1", "-t", "2", "1.1.1.1", timeout=3).find(" 0.0% packet loss") >= 0,
    }


def tailscale():
    try:
        st = json.loads(sh(TS, "status", "--json"))
    except ValueError:
        return {"state": "Unknown", "peers": []}
    peers = [
        {"name": (p.get("DNSName") or p.get("HostName", "?")).split(".")[0], "online": p.get("Online", False)}
        for p in (st.get("Peer") or {}).values()
    ]
    return {"state": st.get("BackendState"), "ip": (st.get("TailscaleIPs") or [""])[0], "peers": peers}


def adguard():
    try:
        pw = open(f"{HOME}/server/secrets/adguard-admin-password").read().strip()
    except OSError:
        return {}
    _, s = http("http://127.0.0.1:3000/control/stats", auth=f"admin:{pw}")
    if not s:
        return {}
    q, b = s.get("num_dns_queries", 0), s.get("num_blocked_filtering", 0)
    return {"queries": q, "blocked": b, "blocked_pct": round(100 * b / q) if q else 0}


def ai():
    _, ps = http("http://127.0.0.1:11434/api/ps")
    _, tags = http("http://127.0.0.1:11434/api/tags")
    return {
        "loaded": [m["name"] for m in (ps or {}).get("models", [])],
        "installed": [m["name"] for m in (tags or {}).get("models", [])],
    }


def containers():
    out = sh(DOCKER, "ps", "-a", "--format", "{{.Names}}\t{{.State}}")
    rows = [l.split("\t") for l in out.strip().splitlines() if "\t" in l]
    return {"running": sum(1 for _, s in rows if s == "running"), "total": len(rows),
            "down": [n for n, s in rows if s != "running"]}


def backup():
    env = dict(os.environ)
    try:
        for line in open(f"{HOME}/server/secrets/restic.env"):
            m = re.match(r'\s*export (RESTIC_PASSWORD)="([^"]*)"', line)
            if m:
                env[m.group(1)] = m.group(2)
    except OSError:
        return {}
    repo = "/Volumes/ServerBackup/restic-mac-server"
    if not os.path.isdir("/Volumes/ServerBackup"):
        repo = f"{HOME}/Backups/restic-mac-server"
    env["RESTIC_REPOSITORY"] = repo
    try:
        out = subprocess.run([RESTIC, "snapshots", "--latest", "1", "--json", "--no-lock"],
                             capture_output=True, text=True, timeout=4, env=env).stdout
        snaps = json.loads(out or "[]")
    except Exception:
        return {"last": None, "where": "usb" if "Volumes" in repo else "internal"}
    summary = (snaps[-1].get("summary") or {}) if snaps else {}
    took = None
    try:  # restic writes 1 to 9 decimals; this Mac's Python 3.9 only reads 3 or 6, so they're dropped
        start, end = (datetime.fromisoformat(re.sub(r"\.\d+", "", summary[k])) for k in ("backup_start", "backup_end"))
        took = round((end - start).total_seconds())
    except (KeyError, TypeError, ValueError):
        pass
    try:  # the same moment as an absolute time, for readers in another zone (Pulso)
        last_epoch = int(datetime.fromisoformat(re.sub(r"\.\d+", "", snaps[-1]["time"])).timestamp())
    except (IndexError, KeyError, ValueError):
        last_epoch = None
    return {"last": snaps[-1]["time"][:19] if snaps else None, "last_epoch": last_epoch,
            "where": "usb" if "Volumes" in repo else "internal",
            # What the last night added to the repository (compressed) and how much it read.
            "added_bytes": summary.get("data_added_packed"), "processed_bytes": summary.get("total_bytes_processed"),
            "seconds": took}


def main():
    jobs = {"services": services, "system": system, "tailscale": tailscale,
            "adguard": adguard, "ai": ai, "containers": containers, "backup": backup}
    with ThreadPoolExecutor(len(jobs)) as ex:
        futures = {k: ex.submit(f) for k, f in jobs.items()}
        result = {k: f.result() for k, f in futures.items()}
    result["host"] = TS_HOST
    result["time"] = time.strftime("%H:%M:%S")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
