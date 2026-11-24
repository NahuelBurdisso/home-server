"""Where Hoy's numbers come from: the other apps on this Mac (their own JSON) and two public APIs
(open-meteo for the weather, argentinadatos.com for the holidays). Each one is asked in parallel
with a short timeout; one that doesn't answer leaves its box empty, never the whole page."""
import json
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

HOST = os.environ.get("APPS_HOST", "http://host.docker.internal")
LAT, LON = os.environ.get("HOY_LAT", "-31.42"), os.environ.get("HOY_LON", "-64.18")  # Córdoba
TZ = "America/Argentina/Cordoba"
WEATHER = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,apparent_temperature,weather_code,wind_gusts_10m"
           "&hourly=temperature_2m,precipitation_probability,precipitation,weather_code,wind_gusts_10m&forecast_hours=12"
           "&daily=temperature_2m_min,temperature_2m_max,precipitation_probability_max,sunrise,sunset&forecast_days=1"
           "&timezone=" + urllib.parse.quote(TZ))
HOLIDAYS = "https://api.argentinadatos.com/v1/feriados/{year}"

_cache: dict[str, tuple[float, object]] = {}


def get(url: str, timeout: float = 4, ttl: int = 0, stale: int | None = None):
    """JSON from a URL, or None. ttl: seconds a good answer is reused (the public APIs); stale: how
    old a copy may still stand in when the source fails (None: any age)."""
    hit = _cache.get(url)
    if ttl and hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hoy/1 (mac-server)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except Exception:
        if hit and (stale is None or time.time() - hit[0] < stale):
            return hit[1]  # a stale copy beats an empty box
        return None
    if ttl:
        _cache[url] = (time.time(), data)
    return data


def post_text(url: str, text: str, timeout: float = 15) -> tuple[bool, str]:
    """For the quick boxes: sends the sentence to the app that understands it, gives back its answer."""
    try:
        req = urllib.request.Request(url, data=text.encode(), headers={"Content-Type": "text/plain; charset=utf-8"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, r.read().decode()
    except urllib.error.HTTPError as e:
        return False, e.read().decode(errors="replace") or "No se pudo."
    except Exception:
        return False, "Esa app no respondió."


def _list(data) -> list:
    """The holidays API answers a list; anything else (an error object) counts as none."""
    return data if isinstance(data, list) else []


def gather() -> dict:
    today = date.today()
    jobs = {
        # Weather: a copy older than 3 h would say yesterday's "now" as today's.
        "weather": lambda: get(WEATHER.format(lat=LAT, lon=LON), ttl=600, stale=3 * 3600),
        # In December the next year's list too: 1 January is within reach.
        "holidays": lambda: _list(get(HOLIDAYS.format(year=today.year), ttl=86400))
        + (_list(get(HOLIDAYS.format(year=today.year + 1), ttl=86400)) if today.month == 12 else []),
        "agenda": lambda: get(f"{HOST}:3014/ia/proximos?dias=2"),
        "lista": lambda: get(f"{HOST}:3016/ia/lista"),
        "plata": lambda: get(f"{HOST}:3013/api/estado"),
        "papeles": lambda: get(f"{HOST}:3017/ia/vencimientos?dias=30"),
        "server": lambda: get(f"{HOST}:3015/api/resumen"),
        "mesa": lambda: get(f"{HOST}:3020/api/estado"),  # 503 when it doesn't operate yet: None, no card
    }
    with ThreadPoolExecutor(len(jobs)) as ex:
        futures = {k: ex.submit(f) for k, f in jobs.items()}
        return {k: f.result() for k, f in futures.items()}


def _es(n: float, decimals: int = 0) -> str:
    """12345.6 -> '12.346' (Argentine style: dot for thousands, comma for decimals)."""
    return f"{n:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _signed(n: float, decimals: int = 1) -> str:
    return f"{'+' if n >= 0 else '−'}{_es(abs(n), decimals)}"


def mesa_view(m: dict | None) -> dict | None:
    """Mesa's /api/estado turned into the little card: Claude's return since the start against la
    vara (SPY), the positions, pending orders and whether it's paused. Anything unexpected leaves
    the card out, never the page."""
    try:
        if not isinstance(m, dict) or "empezo" not in m:
            return None
        frase = m.get("frase") if isinstance(m.get("frase"), str) else ""
        pausada = bool(m.get("pausada"))
        if not m["empezo"]:
            return {"empezo": False, "frase": frase, "pausada": pausada}
        ret, vara, exceso = float(m["retorno_pct"]), float(m["vara_retorno_pct"]), float(m["exceso_pts"])
        pos = [f"{str(p['ticker'])} {_es(float(p['peso_pct']), 1).removesuffix(',0')}%" for p in m.get("posiciones") or []]
        pend = m.get("pendientes")
        pend = len(pend) if isinstance(pend, list) else int(pend or 0)
        return {"empezo": True, "ret": _signed(ret) + " %", "up": ret >= 0, "vara": _signed(vara) + " %",
                "exceso": _signed(exceso) + (" punto" if abs(exceso) == 1 else " puntos"), "exceso_up": exceso >= 0,
                "desde": str(m.get("desde") or ""), "posiciones": pos, "pendientes": pend, "pausada": pausada,
                "frase": frase}
    except (TypeError, KeyError, ValueError, AttributeError):
        return None


def _faltan(p) -> list[tuple[str, date]] | None:
    """Finanzas' "pagos" (this month's bills) as (short name, due date) of the unpaid ones; [] when
    all are paid. None when there is nothing to say: an older Finanzas, no bills, or an odd shape."""
    try:
        if not isinstance(p, dict) or not isinstance(p.get("faltan"), list):
            return None
        return [(str(f["corto"]).strip(), date.fromisoformat(f["vence"])) for f in p["faltan"]]
    except (TypeError, KeyError, ValueError, AttributeError):
        return None


def pagos_view(p, today: date) -> dict | None:
    """The line on the Plata card: "agua (el 10), municipal (venció el 4)", or all paid."""
    faltan = _faltan(p)
    if faltan is None:
        return None
    texto = ", ".join(f"{corto} ({'venció ' if vence < today else ''}el {vence.day})" for corto, vence in faltan)
    return {"al_dia": not faltan, "texto": texto}


def _articulo(corto: str) -> str:
    """'el gas', 'la luz', 'el agua', 'las expensas': close enough for a spoken sentence."""
    w = corto.split()[0].lower() if corto.split() else ""
    if w in ("el", "la", "los", "las"):
        return corto
    if w.endswith("as") and w != "gas":
        art = "las"
    elif w.endswith("os"):
        art = "los"
    elif w in ("luz",) or (w.endswith(("a", "ión", "dad")) and w != "agua"):
        art = "la"
    else:
        art = "el"
    return f"{art} {corto}"


def pagos_frases(p, today: date, within: int = 3) -> list[str]:
    """For the spoken morning: the unpaid bills due within a few days or already late, one sentence
    each ("Falta pagar el gas: vence mañana."). Quiet otherwise."""
    frases = []
    for corto, vence in _faltan(p) or []:
        days = (vence - today).days
        if days > within or not corto:
            continue
        cuando = (f"venció el {vence.day}" if days < 0 else "vence hoy" if days == 0 else
                  "vence mañana" if days == 1 else f"vence en {days} días")
        frases.append(f"Falta pagar {_articulo(corto)}: {cuando}.")
    return frases


# WMO weather codes (open-meteo) -> words and an emoji.
def sky(code: int | None, night: bool = False) -> tuple[str, str]:
    if code is None:
        return "—", "·"
    table = [(0, "despejado", "🌙" if night else "☀️"), (2, "algo nublado", "⛅"), (3, "nublado", "☁️"),
             (48, "niebla", "🌫️"), (57, "llovizna", "🌦️"), (67, "lluvia", "🌧️"), (77, "nieve", "🌨️"),
             (82, "chaparrones", "🌧️"), (86, "nieve", "🌨️"), (94, "tormenta", "⛈️"), (95, "tormenta", "⛈️"),
             (99, "tormenta con granizo", "⛈️")]
    for limit, words, emoji in table:
        if code <= limit:
            return words, emoji
    return "tormenta", "⛈️"


def weather_view(w: dict | None) -> dict | None:
    """Now, the day, the next hours and anything to watch out for in them. A gap in what open-meteo
    sent (a null hour) leaves the weather box out, never the page."""
    try:
        return _weather_view(w)
    except (TypeError, KeyError, IndexError, ValueError):
        return None


def _num(values: list | None) -> list:
    return [v for v in values or [] if v is not None]


def _weather_view(w: dict | None) -> dict | None:
    if not w or "current" not in w:
        return None
    cur, d, h = w["current"], w.get("daily") or {}, w.get("hourly") or {}
    now_h = datetime.now().hour
    sunrise = (d.get("sunrise") or [""])[0][11:16]
    sunset = (d.get("sunset") or [""])[0][11:16]
    night = not (sunrise <= datetime.now().strftime("%H:%M") <= sunset) if sunrise and sunset else False
    words, emoji = sky(cur.get("weather_code"), night)
    hours = []
    for i, t in enumerate(h.get("time") or []):
        temp = h["temperature_2m"][i]
        if temp is None:
            continue
        hours.append({"h": t[11:13], "temp": round(temp), "rain": h["precipitation_probability"][i] or 0,
                      "emoji": sky(h["weather_code"][i], not (sunrise <= t[11:16] <= sunset))[1]})
    warn = []
    codes = h.get("weather_code") or []
    if any(c in (96, 99) for c in codes):
        warn.append("posible granizo en las próximas horas")
    elif any(c == 95 for c in codes):
        warn.append("tormenta en las próximas horas")
    gusts = _num(h.get("wind_gusts_10m"))
    if any(g >= 60 for g in gusts):
        warn.append(f"ráfagas de hasta {round(max(gusts))} km/h")
    return {"temp": round(cur["temperature_2m"]), "feels": round(cur.get("apparent_temperature", cur["temperature_2m"])),
            "words": words, "emoji": emoji, "min": round(d["temperature_2m_min"][0]) if d else None,
            "max": round(d["temperature_2m_max"][0]) if d else None,
            "rain": d.get("precipitation_probability_max", [None])[0] if d else None,
            "sunrise": sunrise, "sunset": sunset, "hours": hours[:12], "warn": warn, "now_h": now_h}


def next_holiday(holidays: list | None, today: date, within: int = 45) -> dict | None:
    for f in sorted(holidays or [], key=lambda f: str(f.get("fecha", ""))):
        try:
            d = date.fromisoformat(f["fecha"])
        except (KeyError, ValueError):
            continue
        if 0 <= (d - today).days <= within:
            return {"date": d, "name": f.get("nombre", "Feriado"), "days": (d - today).days,
                    "long": d.weekday() in (0, 4)}
    return None
