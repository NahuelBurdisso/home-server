"""Vigilancias: pages checked on a schedule, and a message when something changes.

The sentence the bot or Siri sends ("seguí el precio de <url> cuando baje de 150000") is read by
the rules in parse(); the first check runs at once and is the baseline (decision 6: it alerts
nothing, and the answer says what it read, so a page it can't read fails right there). Each check
after that compares with the last value by rules (decision 5). Three failed checks in a row send
one notice, then silence until it works again (decision 7)."""
import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from statistics import median
from urllib.parse import urlsplit

from . import extract, fetch, notify
from .db import connect, set_meta
from .extract import fold, money

log = logging.getLogger("vigia")

KINDS = ("cambio", "precio", "texto")
MIN_EVERY, DEFAULT_EVERY = 15, 60
FAILS_NOTICE = 3
MAX_TEXT = 100_000  # what a cambio watch keeps of the page
TICK = 30  # seconds between scheduler passes
JITTER = 60  # a random 0–60 s delay per check (decision 8)

URL = re.compile(r"(?:https?://|www\.)[^\s<>\"'«»“”]+", re.I)
QUOTED = re.compile(r"[\"“”«»]([^\"“”«»]+)[\"“”«»]|'([^']+)'")
EVERY = re.compile(r"\bcada\s+(?:(\d+)\s*(min|minutos?|h|hs|horas?)|(media hora|hora|dia))\b")
TARGET = re.compile(r"\b(?:baje|baja|bajo|bajara|este|cueste|valga|llegue|caiga|quede)\s+(?:de|a|por debajo de|debajo de|"
                    r"menos de)\s+\$?\s?(\d[\d.,]*)\s*(mil|k|lucas?)?\b|\bmenos de\s+\$?\s?(\d[\d.,]*)\s*(mil|k|lucas?)?\b")
PRICE_WORDS = re.compile(r"\b(?:precio|precios|baja|baje|oferta|cuesta|cuanto sale|vale)\b")
GONE = re.compile(r"\b(?:desaparezca|desaparece|ya no (?:diga|dice|aparezca|aparece|este|figure)|"
                  r"deje de (?:decir|aparecer|estar|figurar)|no diga|no dice|saquen|quiten)\b")
APPEARS = re.compile(r"\b(?:aparezca|aparece|diga|dice|figure|figura|salga|sale|este|haya|ponga|pongan)\b")
NEEDLE_AFTER = re.compile(r"\b(?:aparezca|aparece|desaparezca|desaparece|diga|dice|figure|figura|salga|sale|haya|"
                          r"ponga|pongan|saquen|quiten)\s+(.+?)(?:\s+(?:en|de)(?:\s+la pagina)?)?\s*$")


class Invalid(ValueError):
    """The sentence or the form doesn't say enough. The message is for the person."""


def every_text(m: int) -> str:
    if m % 1440 == 0:
        return "cada día" if m == 1440 else f"cada {m // 1440} días"
    if m % 60 == 0:
        return "cada hora" if m == 60 else f"cada {m // 60} horas"
    return f"cada {m} minutos"


def clean_url(u: str) -> str:
    u = u.strip().rstrip(".,;:!?)]}")
    return u if re.match(r"https?://", u, re.I) else "https://" + u


def parse(sentence: str) -> dict:
    """What a sentence asks to watch: {url, kind, needle, cuando, objetivo, every_min}.

    "seguí el precio de <url>", "… cuando baje de 150000" → precio (objetivo 150000);
    "avisame si cambia <url>", "vigilá <url>" → cambio;
    "avisame cuando aparezca "turnos" en <url>", "… cuando desaparezca "No hay turnos" …" → texto;
    "… cada 30 minutos" sets how often (15 at least)."""
    raw = " ".join((sentence or "").split())
    m = URL.search(raw)
    if not m:
        raise Invalid("Mandame el link de la página que querés que vigile.")
    url = clean_url(m.group())
    rest_raw = (raw[:m.start()] + " " + raw[m.end():]).strip()
    rest = fold(rest_raw)
    out = {"url": url, "kind": "cambio", "needle": "", "cuando": "aparece", "objetivo": None,
           "every_min": DEFAULT_EVERY}

    if e := EVERY.search(rest):
        if e[1]:
            n = int(e[1])
            out["every_min"] = n * 60 if e[2].startswith("h") else n
        else:
            out["every_min"] = {"media hora": 30, "hora": 60, "dia": 1440}[e[3]]
        out["every_min"] = max(MIN_EVERY, out["every_min"])
        rest = " ".join(EVERY.sub(" ", rest).split())

    target = TARGET.search(rest)
    quoted = QUOTED.search(rest_raw)
    if not quoted and (target or PRICE_WORDS.search(rest)):
        out["kind"] = "precio"
        if target:
            number, mult = (target[1], target[2]) if target[1] else (target[3], target[4])
            value = extract.parse_price(number)
            if value is not None and mult:
                value *= 1000
            out["objetivo"] = value
        return out
    if quoted or GONE.search(rest) or APPEARS.search(rest):
        needle = (quoted[1] or quoted[2]).strip() if quoted else ""
        if not needle and (n := NEEDLE_AFTER.search(rest)):
            needle = n[1].strip(" .,:;")
        if needle and fold(needle) not in ("", "algo", "algo nuevo", "cambia", "cambie"):
            out.update(kind="texto", needle=needle, cuando="desaparece" if GONE.search(rest) else "aparece")
    return out


# --- reading a page ---------------------------------------------------------------------------------

def name_for(html: str, url: str) -> str:
    title = extract.title_of(html)
    title = re.split(r"\s+[|–—]\s+|\s+-\s+(?=[^-]+$)", title)[0].strip() if title else ""
    return (title or urlsplit(url).hostname or url)[:70]


def read(w: dict, html: str) -> tuple[float | None, str]:
    """(value_num, value_text) of a page for a watch. Raises extract.NotFound."""
    if w["kind"] == "precio":
        v = extract.price_of(html, w["selector"])
        return v, money(v)
    text = extract.page_text(html, w["selector"])
    if not text:
        raise extract.NotFound("La página vino sin texto (seguramente se arma con JavaScript, y eso no lo leo).")
    if w["kind"] == "texto":
        present = extract.has_needle(text, w["needle"])
        return (1.0 if present else 0.0), (extract.around(text, w["needle"]) if present else "")
    return None, text[:MAX_TEXT]


def first_sentence(w: dict, num: float | None, text: str) -> str:
    """What the first check read, said back: so a page it can't read is known right away."""
    every = every_text(w["every_min"])
    if w["kind"] == "precio":
        conds = [f"si baja {w['umbral']:g}% o más", "si es el más bajo que vi"]
        if w["objetivo"]:
            conds.append(f"si baja de {money(w['objetivo'])}")
        return (f"Listo, sigo el precio de *{w['name']}*: hoy está {money(num)}. Te aviso "
                + ", ".join(conds[:-1]) + " o " + conds[-1] + f". Lo miro {every}.")
    if w["kind"] == "texto":
        now = "ya dice" if num else "no dice"
        when = "aparezca" if w["cuando"] == "aparece" else "desaparezca"
        return f"Listo, vigilo *{w['name']}* {every}. Ahora {now} «{w['needle']}»: te aviso cuando {when}."
    snippet = " ".join(text.split())
    snippet = snippet[:160] + ("…" if len(snippet) > 160 else "")
    return f"Listo, vigilo *{w['name']}* {every}. Ahora dice: «{snippet}». Te aviso cuando cambie."


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def add(conn, fields: dict, get=None) -> tuple[int | None, str, bool]:
    """(id, the sentence to answer, whether it's new). id None: nothing was created, the sentence
    says why. The first check runs here, synchronously: it's the baseline."""
    get = get or fetch.get
    kind = fields.get("kind") or "cambio"
    if kind not in KINDS:
        raise Invalid("Puedo vigilar si cambia, un precio o si aparece un texto.")
    url = clean_url(fields.get("url") or "") if (fields.get("url") or "").strip() else ""
    if not url:
        raise Invalid("Falta el link de la página.")
    needle = (fields.get("needle") or "").strip()
    if kind == "texto" and not needle:
        raise Invalid("Decime qué texto busco, entre comillas: avisame cuando aparezca \"turnos\" en <link>.")
    w = {"url": url, "kind": kind, "selector": (fields.get("selector") or "").strip(), "needle": needle,
         "cuando": "desaparece" if fields.get("cuando") == "desaparece" else "aparece",
         "objetivo": fields.get("objetivo") or None,
         "umbral": max(0.1, float(fields.get("umbral") or 1)),
         "every_min": max(MIN_EVERY, int(fields.get("every_min") or DEFAULT_EVERY)),
         "added_by": fields.get("added_by") or "", "chat": str(fields["chat"]) if fields.get("chat") else None}

    same = conn.execute("SELECT * FROM watches WHERE url = ? AND kind = ? AND needle = ? AND selector = ?",
                        (w["url"], kind, needle, w["selector"])).fetchone()
    if same:
        if not same["active"]:
            conn.execute("UPDATE watches SET active = 1, fails = 0 WHERE id = ?", (same["id"],))
            conn.commit()
            return same["id"], f"Ya la tenía: *{same['name']}*. Estaba en pausa y la volví a prender.", False
        return same["id"], f"Ya lo estoy vigilando: *{same['name']}*.", False

    try:
        page = get(url)
    except (fetch.Refused, fetch.FetchError) as e:
        return None, f"No pude leer esa página: {e}", False
    try:
        num, text = read(w, page.text)
    except extract.NotFound as e:
        return None, f"{e} No la agregué.", False
    w["name"] = (fields.get("name") or "").strip()[:70] or name_for(page.text, page.url)
    now = _now()
    cur = conn.execute(
        "INSERT INTO watches (name, url, kind, selector, needle, cuando, objetivo, umbral, every_min, added_by, chat,"
        " last_check, last_ok, value_num, value_text, last_change) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (w["name"], url, kind, w["selector"], needle, w["cuando"], w["objetivo"], w["umbral"], w["every_min"],
         w["added_by"], w["chat"], now, now, num, text, now))
    conn.execute("INSERT INTO checks (watch_id, ts, value_num, changed, note) VALUES (?, ?, ?, 0, ?)",
                 (cur.lastrowid, now, num, "Primera lectura"))
    conn.commit()
    return cur.lastrowid, first_sentence(w, num, text), True


# --- one check ----------------------------------------------------------------------------------------

def link(wid: int) -> str:
    return f"{notify.PUBLIC_URL}/v/{wid}"


def evaluate(w, num: float | None, text: str, lowest: float | None) -> tuple[bool, str | None, str]:
    """(changed, the alert or None, the note for the history), by rules only. w is the stored row
    (the last value); num/text what was just read; lowest the lowest price seen before."""
    name = w["name"]
    if w["kind"] == "cambio":
        if text == (w["value_text"] or ""):
            return False, None, ""
        d = extract.diff(w["value_text"] or "", text) or "(cambió el formato, no el texto)"
        return True, f"🔭 Cambió *{name}*:\n{d}\n{link(w['id'])}", d
    if w["kind"] == "texto":
        before, now = bool(w["value_num"]), bool(num)
        if before == now:
            return False, None, ""
        note = f"Ahora dice «{w['needle']}»" if now else f"Ya no dice «{w['needle']}»"
        wanted = now if w["cuando"] == "aparece" else not now
        if not wanted:
            return True, None, note
        msg = f"🔭 *{name}*: " + (f"ahora dice «{w['needle']}»." if now else f"ya no dice «{w['needle']}».")
        if now and text:
            msg += f"\n_{text}_"
        return True, msg + f"\n{link(w['id'])}", note
    # precio
    old = w["value_num"]
    if old is not None and abs(num - old) < 0.005:
        return False, None, ""
    note = f"{money(old)} → {money(num)}"
    if old is None:
        return True, None, note
    reasons = []
    drop = (old - num) / old * 100 if old else 0
    if drop >= (w["umbral"] or 1):
        reasons.append(f"bajó {drop:.0f}%" if drop >= 1 else f"bajó {drop:.1f}%")
    if lowest is not None and num < lowest:
        reasons.append("es el más bajo que vi")
    if w["objetivo"] and num < w["objetivo"] <= old:
        reasons.append(f"está debajo de tu objetivo de {money(w['objetivo'])}")
    if not reasons:
        return True, None, note  # a rise, or a drop too small: kept, not announced
    return True, (f"🔭 *{name}*: {money(num)} (antes {money(old)}). " + _upper_first(", ".join(reasons))
                  + f".\n{link(w['id'])}"), note


def _upper_first(s: str) -> str:
    return s[:1].upper() + s[1:]


def run(conn, wid: int, get=None, send=None) -> str | None:
    """Checks one watch now. Returns the alert it sent (or the failure notice), else None."""
    get, send = get or fetch.get, send or notify.send
    w = conn.execute("SELECT * FROM watches WHERE id = ?", (wid,)).fetchone()
    if w is None:
        return None
    now = _now()
    try:
        page = get(w["url"])
        num, text = read(w, page.text)
    except (fetch.Refused, fetch.FetchError, extract.NotFound) as e:
        fails = w["fails"] + 1
        conn.execute("UPDATE watches SET last_check = ?, fails = ?, last_error = ? WHERE id = ?",
                     (now, fails, str(e), wid))
        conn.commit()
        if fails == FAILS_NOTICE:  # once; quiet after it, and no notice when it works again
            msg = (f"🔭 No puedo leer *{w['name']}* (probé {FAILS_NOTICE} veces): {e}\n"
                   f"No te aviso más hasta que vuelva a andar.\n{link(wid)}")
            send(msg, w["chat"], title="Vigía: no puedo leer una página", path=f"/v/{wid}")
            return msg
        return None
    except Exception as e:  # noqa: BLE001 - a bug on one page must not kill the scheduler
        log.exception("check %s", wid)
        conn.execute("UPDATE watches SET last_check = ?, last_error = ? WHERE id = ?", (now, f"{type(e).__name__}", wid))
        conn.commit()
        return None

    lowest = None
    if w["kind"] == "precio":
        lowest = conn.execute("SELECT MIN(value_num) FROM checks WHERE watch_id = ?", (wid,)).fetchone()[0]
    changed, alert, note = evaluate(w, num, text, lowest)
    conn.execute("UPDATE watches SET last_check = ?, last_ok = ?, fails = 0, last_error = NULL, value_num = ?,"
                 " value_text = ?" + (", last_change = ?" if changed else "") + " WHERE id = ?",
                 (now, now, num, text, *((now,) if changed else ()), wid))
    today = conn.execute("SELECT 1 FROM checks WHERE watch_id = ? AND date(ts) = date(?)", (wid, now)).fetchone()
    if changed or not today:  # one row per change, plus one per day
        conn.execute("INSERT INTO checks (watch_id, ts, value_num, changed, note) VALUES (?, ?, ?, ?, ?)",
                     (wid, now, num, int(changed), note or None))
    conn.commit()
    if alert:
        send(alert, w["chat"], title=f"Vigía: {w['name']}", path=f"/v/{wid}")
    return alert


# --- reading it back ---------------------------------------------------------------------------------

def card_text(w) -> str:
    """What it read last, in a line, for the cards and the bot's list."""
    if w["kind"] == "precio":
        return money(w["value_num"]) if w["value_num"] is not None else "sin precio todavía"
    if w["kind"] == "texto":
        return f"dice «{w['needle']}»" if w["value_num"] else f"no dice «{w['needle']}»"
    t = " ".join((w["value_text"] or "").split())
    return t[:140] + ("…" if len(t) > 140 else "")


def state(w) -> str:
    if not w["active"]:
        return "en pausa"
    if w["fails"] >= FAILS_NOTICE:
        return f"no la puedo leer ({w['fails']} veces)"
    if w["fails"]:
        return "falló la última vez"
    return "bien"


def price_stats(conn, w) -> dict | None:
    """now, lowest, highest, the median of the last 30 days (one value per day: the day's last) and
    how today compares."""
    rows = conn.execute("SELECT ts, value_num FROM checks WHERE watch_id = ? AND value_num IS NOT NULL ORDER BY ts, id",
                        (w["id"],)).fetchall()
    if not rows or w["value_num"] is None:
        return None
    nums = [r["value_num"] for r in rows]
    since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    by_day = {}
    for r in rows:
        if r["ts"][:10] >= since:
            by_day[r["ts"][:10]] = r["value_num"]
    now = w["value_num"]
    usual = median(by_day.values()) if by_day else None
    if usual is None or len(by_day) < 3:
        verdict = "Todavía no hay historia para saber si es oferta."
    else:
        pct = (now - usual) / usual * 100
        if pct <= -3:
            verdict = f"{abs(pct):.0f}% abajo de su precio habitual"
        elif pct >= 3:
            verdict = f"{pct:.0f}% arriba de su precio habitual"
        else:
            verdict = "igual que siempre: no es oferta"
    return {"now": now, "low": min(nums + [now]), "high": max(nums + [now]), "usual": usual, "verdict": verdict,
            "points": [(r["ts"], r["value_num"]) for r in rows]}


def svg_line(points: list[tuple[str, float]], width: int = 600, height: int = 160) -> str:
    """The price over time as a stepped line (a price holds until it changes)."""
    if len(points) < 2:
        return ""
    t = [datetime.fromisoformat(p[0]).timestamp() for p in points]
    v = [p[1] for p in points]
    t0, t1 = t[0], max(t[-1], time.time())
    lo, hi = min(v), max(v)
    pad = (hi - lo) * 0.1 or max(1.0, hi * 0.05)
    lo, hi = lo - pad, hi + pad

    def x(ts):
        return round((ts - t0) / ((t1 - t0) or 1) * (width - 8) + 4, 1)

    def y(val):
        return round(height - 4 - (val - lo) / (hi - lo) * (height - 8), 1)
    d = f"M{x(t[0])},{y(v[0])}"
    for i in range(1, len(t)):
        d += f" H{x(t[i])} V{y(v[i])}"
    d += f" H{x(t1)}"
    return (f'<svg class="line" viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" '
            f'aria-label="El precio en el tiempo"><path d="{d}" fill="none" stroke="var(--accent)" stroke-width="2.5" '
            f'vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>')


def list_sentence(conn) -> str:
    rows = conn.execute("SELECT * FROM watches ORDER BY active DESC, id").fetchall()
    if not rows:
        return "No estoy vigilando nada. Mandame «seguí el precio de <link>» o «avisame si cambia <link>»."
    parts = []
    for w in rows:
        extra = "" if w["active"] and not w["fails"] else f" ({state(w)})"
        parts.append(f"{w['name']}: {card_text(w)}{extra}")
    n = len(rows)
    return (f"Vigilo {n} página{'s' if n > 1 else ''}. " if n else "") + " · ".join(parts) + "."


# --- the scheduler ---------------------------------------------------------------------------------

def due(conn, now: datetime | None = None) -> list[int]:
    now = now or datetime.now()
    rows = conn.execute("SELECT id, last_check, every_min FROM watches WHERE active = 1").fetchall()
    out = []
    for r in rows:
        last = datetime.fromisoformat(r["last_check"]) if r["last_check"] else None
        if last is None or now - last >= timedelta(minutes=max(MIN_EVERY, r["every_min"])):
            out.append(r["id"])
    return out


_inflight: set[int] = set()
_inflight_lock = threading.Lock()


def _job(wid: int):
    try:
        time.sleep(random.uniform(0, JITTER))
        conn = connect()
        try:
            run(conn, wid)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        log.exception("watch %s", wid)
    finally:
        with _inflight_lock:
            _inflight.discard(wid)


def scheduler():
    """Every 30 s: the watches that are due go to a small pool (the fetcher keeps one request at a
    time per site). meta 'loop' is what /health reads."""
    conn = connect()
    pool = ThreadPoolExecutor(4, thread_name_prefix="check")
    while True:
        try:
            set_meta(conn, "loop", str(int(time.time())))
            for wid in due(conn):
                with _inflight_lock:
                    if wid in _inflight:
                        continue
                    _inflight.add(wid)
                pool.submit(_job, wid)
        except Exception:  # noqa: BLE001 - the loop must outlive any error
            log.exception("scheduler")
        time.sleep(TICK)
