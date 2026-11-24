"""Saving, finding and changing documents."""
import hashlib
import json
import mimetypes
import os
import re
import urllib.request
from datetime import date

from . import extract
from .db import files_dir

RECORDATORIOS = os.environ.get("RECORDATORIOS_URL", "http://host.docker.internal:3014")
PUBLIC_URL = os.environ.get("PAPELES_URL", "https://mac-server.tailnet-ejemplo.ts.net:3017")
MAX_BYTES = 40 * 1024 * 1024
ALLOWED = ("application/pdf", "image/jpeg", "image/png", "image/heic", "image/heif", "image/webp", "image/tiff", "text/plain")


def mime_of(name: str, given: str | None) -> str:
    guess = mimetypes.guess_type(name)[0]
    if name.lower().endswith((".heic", ".heif")):
        guess = "image/heic"
    m = (given or "").split(";")[0].strip().lower()
    return m if m in ALLOWED else (guess or m or "application/octet-stream")


def save(conn, name: str, data: bytes, mime: str | None = None) -> tuple[dict, bool]:
    """(the document, whether it's new). The same bytes twice give back the first one."""
    if not data:
        raise ValueError("El archivo llegó vacío.")
    if len(data) > MAX_BYTES:
        raise ValueError("Es muy grande (más de 40 MB).")
    mime = mime_of(name or "documento", mime)
    if mime not in ALLOWED:
        raise ValueError("Solo PDF, fotos (JPG, PNG, HEIC) o texto.")
    sha = hashlib.sha256(data).hexdigest()
    old = conn.execute("SELECT * FROM docs WHERE sha256 = ?", (sha,)).fetchone()
    if old:
        return dict(old), False
    ext = mimetypes.guess_extension(mime) or os.path.splitext(name)[1] or ""
    if mime == "image/heic":
        ext = ".heic"
    file = f"{sha}{ext}"
    (files_dir() / file).write_bytes(data)
    text = extract.text_of(data, mime)
    kind = extract.kind_of(text, name)
    when = extract.doc_date(text)
    expires = extract.expiry_of(text, when)
    title = extract.title_of(name or "", text, kind, when)
    cur = conn.execute(
        "INSERT INTO docs (title, kind, original_name, file, mime, size, sha256, text, doc_date, expires_found)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (title, kind, name or file, file, mime, len(data), sha, text, (when or date.today()).isoformat(),
         expires.isoformat() if expires else None))
    conn.commit()
    return dict(get(conn, cur.lastrowid)), True


def get(conn, doc_id: int):
    return conn.execute("SELECT * FROM docs WHERE id = ?", (doc_id,)).fetchone()


def _fts_query(q: str) -> str:
    """Each word as a prefix ('garan' finds 'garantía'), all of them required."""
    words = re.findall(r"\w+", extract.fold(q))
    return " ".join(f'"{w}"*' for w in words)


def search(conn, q: str = "", kind: str = "", limit: int = 200) -> list:
    args: list = []
    where = []
    if kind:
        where.append("d.kind = ?")
        args.append(kind)
    if q.strip() and _fts_query(q):
        sql = ("SELECT d.*, snippet(docs_fts, 1, '[', ']', ' … ', 12) AS snip FROM docs_fts JOIN docs d ON d.id = docs_fts.rowid"
               " WHERE docs_fts MATCH ?" + "".join(f" AND {w}" for w in where) + " ORDER BY rank LIMIT ?")
        return conn.execute(sql, [_fts_query(q), *args, limit]).fetchall()
    sql = "SELECT d.*, NULL AS snip FROM docs d" + (" WHERE " + " AND ".join(where) if where else "")
    return conn.execute(sql + " ORDER BY COALESCE(d.doc_date, d.created_at) DESC, d.id DESC LIMIT ?", [*args, limit]).fetchall()


def counts(conn) -> dict:
    return {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM docs GROUP BY kind")}


def update(conn, doc_id: int, title: str, kind: str, doc_date: str, expires: str, notes: str) -> str | None:
    """Saves the form; when the expiry changes, the Recordatorios reminder follows. Returns a
    sentence about the reminder, or None."""
    doc = get(conn, doc_id)
    if not doc:
        return None
    kind = kind if kind in extract.KINDS else doc["kind"]
    for d in (doc_date, expires):
        if d:
            date.fromisoformat(d)  # ValueError: the page says so
    conn.execute("UPDATE docs SET title = ?, kind = ?, doc_date = ?, expires = ?, notes = ? WHERE id = ?",
                 (title.strip() or doc["title"], kind, doc_date or None, expires or None, notes.strip(), doc_id))
    conn.commit()
    if expires and expires != doc["expires"]:
        return remind(conn, doc_id)
    if not expires and doc["reminder_id"]:  # the expiry was cleared: its reminder goes too
        _forget_reminder(conn, doc)
        return "Sin fecha de vencimiento: saqué el aviso de Recordatorios."
    return None


def _held_by_another(conn, reminder_id, doc_id: int) -> bool:
    return conn.execute("SELECT 1 FROM docs WHERE reminder_id = ? AND id != ?", (reminder_id, doc_id)).fetchone() is not None


def _forget_reminder(conn, doc) -> None:
    """Removes the reminder made for an earlier expiry (or for a deleted document), unless another
    document holds the same one."""
    if not doc["reminder_id"]:
        return
    if not _held_by_another(conn, doc["reminder_id"], doc["id"]):
        try:
            req = urllib.request.Request(f"{RECORDATORIOS}/r/{doc['reminder_id']}/borrar", data=b"", method="POST")
            urllib.request.urlopen(req, timeout=10).close()
        except Exception:
            pass  # already gone, or Recordatorios down: the stale one says "Vence: ..." with this link
    conn.execute("UPDATE docs SET reminder_id = NULL WHERE id = ?", (doc["id"],))
    conn.commit()


def _post_reminder(body: dict) -> dict:
    req = urllib.request.Request(f"{RECORDATORIOS}/api/recordatorio", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def remind(conn, doc_id: int) -> str:
    """A reminder in Recordatorios for when it expires: a week before, the day before and that day.
    The one for an earlier date goes. Each document gets its own: Recordatorios saves the same title
    on the same date once, so a second paper with the same name and expiry adds "(papel N)"."""
    _forget_reminder(conn, get(conn, doc_id))
    doc = get(conn, doc_id)
    label = extract.KINDS[doc["kind"]][0].lower()
    body = {"titulo": f"Vence: {doc['title']}", "fecha": doc["expires"], "avisos": [10080, 1440, 0],
            "notas": f"{label.capitalize()} guardado en Papeles: {PUBLIC_URL}/d/{doc_id}"}
    try:
        res = _post_reminder(body)
        if not res.get("nuevo", True) and _held_by_another(conn, res.get("id"), doc_id):
            res = _post_reminder({**body, "titulo": f"{body['titulo']} (papel {doc_id})"})
        conn.execute("UPDATE docs SET reminder_id = ? WHERE id = ?", (res.get("id"), doc_id))
        conn.commit()
        return f"Te aviso en Recordatorios: {res.get('texto', doc['expires'])}"
    except Exception:
        return "No pude crear el aviso en Recordatorios (¿está andando?). La fecha quedó guardada."


def delete(conn, doc_id: int):
    doc = get(conn, doc_id)
    if not doc:
        return
    _forget_reminder(conn, doc)
    conn.execute("DELETE FROM docs WHERE id = ?", (doc_id,))
    conn.commit()
    (files_dir() / doc["file"]).unlink(missing_ok=True)


def upcoming(conn, days: int = 60) -> list:
    """Documents that expire soon (or already did, up to a month ago)."""
    return conn.execute("SELECT * FROM docs WHERE expires IS NOT NULL AND expires >= date('now', 'localtime', '-30 days')"
                        " AND expires <= date('now', 'localtime', ?) ORDER BY expires", (f"+{days} days",)).fetchall()

