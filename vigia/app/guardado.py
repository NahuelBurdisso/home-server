"""Guardado: a link (or a note, or a long voice note through the bot) kept with its text and a
summary, searchable. save() answers at once; one worker thread does the slow part:

1. a YouTube link → its captions (es, then en, then any); none → a plain error. Never downloads
   video or audio.
2. any other link → fetch.get (the same safety rules as the watches) → trafilatura for the title
   and the main text.
3. the summary with the local model (ia.py). Ollama down → sin_resumen, tried again every 15 min.
4. when it's done, the summary and the link to the item go to the chat that asked (notify.py)."""
import json
import logging
import re
import threading
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from . import extract, fetch, ia, notify
from .db import connect

log = logging.getLogger("vigia")

RETRY = timedelta(minutes=15)
SHORT_NOTE = 600  # a note this short is its own summary
TRACKING = re.compile(r"^(?:utm_\w+|fbclid|gclid|igshid|mc_cid|mc_eid|si|ref|ref_src|feature)$", re.I)
YT_ID = re.compile(r"^[\w-]{11}$")
KIND_NAMES = {"articulo": "artículo", "video": "video", "audio": "audio", "nota": "nota"}

wake = threading.Event()


class Invalid(ValueError):
    """Nothing to save. The message is for the person."""


def youtube_id(url: str) -> str | None:
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("m.").removeprefix("music.")
    if host == "youtu.be":
        vid = p.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "youtube-nocookie.com"):
        q = dict(parse_qsl(p.query))
        parts = p.path.strip("/").split("/")
        vid = q.get("v") or (parts[1] if len(parts) > 1 and parts[0] in ("shorts", "live", "embed", "v") else "")
    else:
        return None
    return vid if YT_ID.match(vid or "") else None


def normalize(url: str) -> str:
    """The same page written two ways is one item: no #fragment, no tracking parameters, the host in
    lowercase; a YouTube video always as youtube.com/watch?v=ID."""
    url = url.strip()
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url
    if vid := youtube_id(url):
        return f"https://www.youtube.com/watch?v={vid}"
    p = urlsplit(url)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)])
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", query, ""))


def link(item_id: int) -> str:
    return f"{notify.PUBLIC_URL}/g/{item_id}"


def save(conn, url: str = "", texto: str = "", titulo: str = "", kind: str = "", quien: str = "",
         chat: str | None = None) -> tuple[int, bool, str]:
    """(id, whether it's new, the sentence to answer). The same URL twice is the existing item."""
    chat = str(chat) if chat else None
    if url.strip():
        if not re.match(r"(?:https?://|www\.)\S+$", url.strip(), re.I):
            raise Invalid("Eso no parece un link: mandame la dirección completa (https://…).")
        url = normalize(url)
        old = conn.execute("SELECT * FROM saved WHERE url = ?", (url,)).fetchone()
        if old:
            if old["status"] == "error":  # tried before and failed: the same item, tried again
                conn.execute("UPDATE saved SET status = 'pendiente', error = NULL, chat = COALESCE(?, chat) WHERE id = ?",
                             (chat, old["id"]))
                conn.commit()
                wake.set()
                return old["id"], False, "Ese link ya lo tenía, pero no lo había podido leer: lo vuelvo a intentar."
            name = f"*{old['title']}*" if old["title"] else "ese link"
            return old["id"], False, f"Ya tenía guardado {name}: {link(old['id'])}"
        kind = "video" if youtube_id(url) else "articulo"
        cur = conn.execute("INSERT INTO saved (url, kind, added_by, chat) VALUES (?, ?, ?, ?)", (url, kind, quien, chat))
        conn.commit()
        wake.set()
        what = "los subtítulos del video" if kind == "video" else "la página"
        return cur.lastrowid, True, f"Guardando: leo {what} y te mando el resumen cuando esté."
    texto = (texto or "").strip()
    if not texto:
        raise Invalid("No me mandaste nada para guardar.")
    kind = kind if kind in ("audio", "nota") else "nota"
    titulo = (titulo or "").strip() or " ".join(texto.split()[:8])[:80]
    short = kind == "nota" and len(texto) <= SHORT_NOTE
    cur = conn.execute("INSERT INTO saved (title, kind, text, status, added_by, chat) VALUES (?, ?, ?, ?, ?, ?)",
                       (titulo, kind, texto, "listo" if short else "pendiente", quien, chat))
    conn.commit()
    if short:
        return cur.lastrowid, True, f"Guardado: {link(cur.lastrowid)}"
    wake.set()
    return cur.lastrowid, True, "Guardado: te mando el resumen cuando esté."


# --- reading a link -------------------------------------------------------------------------------

def captions(video_id: str) -> str:
    """The video's captions as plain text: Spanish, then English, then whatever there is."""
    from youtube_transcript_api import YouTubeTranscriptApi
    api = YouTubeTranscriptApi()
    try:
        fetched = api.fetch(video_id, languages=["es", "es-419", "es-ES", "en"])
    except Exception:  # noqa: BLE001 - NoTranscriptFound and friends: any language will do
        listed = list(api.list(video_id))
        if not listed:
            raise
        fetched = listed[0].fetch()
    return " ".join(s.text.replace("\n", " ") for s in fetched).strip()


def video_title(url: str, get) -> str:
    """Best effort, through the same guarded fetcher: YouTube's oEmbed has the title."""
    try:
        page = get("https://www.youtube.com/oembed?format=json&url=" + quote(url, safe=""))
        return (json.loads(page.text).get("title") or "")[:200]
    except Exception:  # noqa: BLE001
        return ""


def article(url: str, get) -> tuple[str, str]:
    """(title, main text) of a web page. Raises Invalid with the reason for the person."""
    import trafilatura
    try:
        page = get(url)
    except (fetch.Refused, fetch.FetchError) as e:
        raise Invalid(str(e)) from e
    ctype = page.content_type.lower()
    if ctype and not any(t in ctype for t in ("html", "xml", "text/plain")):
        raise Invalid("Ese link no es una página sino un archivo (PDF, imagen…): eso todavía no lo leo. "
                      "Si es un papel, mandámelo como archivo y va a Papeles.")
    if "text/plain" in ctype:
        return "", page.text.strip()
    text = trafilatura.extract(page.text, url=page.url, include_comments=False, include_tables=True) or ""
    try:
        meta = trafilatura.extract_metadata(page.text, default_url=page.url)
        title = (meta.title if meta else "") or ""
    except Exception:  # noqa: BLE001
        title = ""
    title = title or extract.title_of(page.text)
    if not text.strip():
        raise Invalid("No encontré texto en esa página (puede que se arme con JavaScript o pida login: eso no lo leo).")
    return " ".join(title.split())[:200], text.strip()


# --- the worker ----------------------------------------------------------------------------------

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def next_item(conn, now: datetime | None = None):
    """The oldest pendiente; else a sin_resumen whose last try was 15 minutes ago or more."""
    row = conn.execute("SELECT * FROM saved WHERE status = 'pendiente' ORDER BY id LIMIT 1").fetchone()
    if row:
        return row
    since = ((now or datetime.now()) - RETRY).strftime("%Y-%m-%d %H:%M:%S")
    return conn.execute("SELECT * FROM saved WHERE status = 'sin_resumen' AND (tried_at IS NULL OR tried_at <= ?)"
                        " ORDER BY id LIMIT 1", (since,)).fetchone()


def process(conn, item, get=None, send=None, summarize=None, get_captions=None) -> str:
    """Does one item; returns its new status."""
    get, send = get or fetch.get, send or notify.send
    summarize, get_captions = summarize or ia.summarize, get_captions or captions
    iid, title, text = item["id"], item["title"], item["text"]
    if not text:
        try:
            if vid := youtube_id(item["url"] or ""):
                try:
                    text = get_captions(vid)
                except Exception as e:  # noqa: BLE001
                    log.info("captions %s: %s", vid, type(e).__name__)
                    raise Invalid("Ese video no tiene subtítulos que pueda leer, así que no lo puedo resumir "
                                  "(no bajo videos ni audio).") from e
                if not text:
                    raise Invalid("Ese video no tiene subtítulos que pueda leer.")
                title = video_title(item["url"], get) or title
            else:
                title, text = article(item["url"], get)
        except Invalid as e:
            conn.execute("UPDATE saved SET status = 'error', error = ? WHERE id = ?", (str(e), iid))
            conn.commit()
            send(f"📌 No pude guardar {item['url']}: {e}", item["chat"], title="Vigía: no pude guardar", path=f"/g/{iid}")
            return "error"
        title = title or (item["url"] or "")[:200]
        conn.execute("UPDATE saved SET title = ?, text = ? WHERE id = ?", (title, text, iid))
        conn.commit()

    try:
        summary = summarize(text, title, item["kind"])
    except Exception as e:  # noqa: BLE001 - Ollama down or busy: the text is kept, the summary comes later
        log.info("summary %s: %s", iid, type(e).__name__)
        conn.execute("UPDATE saved SET status = 'sin_resumen', tried_at = ? WHERE id = ?", (_now(), iid))
        conn.commit()
        if item["status"] == "pendiente":  # said once, not on every retry
            send(f"📌 Guardé *{title}*, pero la IA de casa no respondió: te mando el resumen cuando vuelva.\n{link(iid)}",
                 item["chat"], title="Vigía: guardado sin resumen", path=f"/g/{iid}")
        return "sin_resumen"
    if not summary.strip():
        summary = "(La IA no escribió nada.)"
    conn.execute("UPDATE saved SET summary = ?, status = 'listo', error = NULL, tried_at = ? WHERE id = ?",
                 (summary, _now(), iid))
    conn.commit()
    send(f"📌 *{title}*\n{summary}\n{link(iid)}", item["chat"], title=f"Vigía: {title}"[:120], path=f"/g/{iid}")
    return "listo"


def worker():
    """One item at a time; woken by save(), and every minute anyway (for the sin_resumen retries).
    An item that breaks in an unexpected way is marked error, so it isn't tried in a loop."""
    conn = connect()
    while True:
        item = None
        try:
            item = next_item(conn)
            if item is not None:
                process(conn, item)
                continue
        except Exception:  # noqa: BLE001 - the worker must outlive any error
            log.exception("guardado")
            if item is not None:
                conn.execute("UPDATE saved SET status = 'error', error = ? WHERE id = ?",
                             ("Algo falló al leerlo. Probá «Reintentar» más tarde.", item["id"]))
                conn.commit()
        wake.wait(60)
        wake.clear()


# --- reading it back ---------------------------------------------------------------------------------

def fts_query(q: str) -> str:
    """The words as FTS5 prefix terms, each one quoted (no operators from the person's text)."""
    words = re.findall(r"\w+", q or "")
    return " ".join(f'"{w}"*' for w in words[:12])


def search(conn, q: str, limit: int = 50) -> list:
    if not fts_query(q):
        return conn.execute("SELECT id, url, title, kind, status, created_at, substr(summary, 1, 300) AS snippet"
                            " FROM saved ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return conn.execute(
        "SELECT s.id, s.url, s.title, s.kind, s.status, s.created_at,"
        " snippet(saved_fts, -1, char(2), char(3), '…', 18) AS snippet"
        " FROM saved_fts JOIN saved s ON s.id = saved_fts.rowid WHERE saved_fts MATCH ? ORDER BY rank LIMIT ?",
        (fts_query(q), limit)).fetchall()
