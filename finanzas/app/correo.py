"""Gmail, read by Finanzas itself every 10 minutes: BBVA's "Ya podés descargar tu resumen" notice
(a Hotmail rule forwards it there) and any card statement PDF that reaches that inbox (forwarding
the PDF from the phone works too). The notice leaves the due date and the amounts (app/aviso.py);
the PDF, the detail.

This used to be n8n's IMAP trigger, and it died for good whenever the internet or the DNS went away
for more than a minute (22 and 26/10): it keeps one connection open, retries five times in fifteen
seconds and then stays 'failed' until someone publishes it again. Here every round is a new
connection that reads what came after the last UID it saw, so an outage only costs the rounds it
lasts and the next one catches up. After 6 hours without reading it says so on the phone, and again
when it's back. The inbox is opened read-only: nothing is marked as read, moved or deleted.

Needs the Gmail address and an app password (myaccount.google.com/apppasswords), one per line, in
secrets/gmail-imap. Read on every round: no restart needed after changing it."""
import email
import email.policy
import imaplib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

from . import aviso, db, ledger
from .db import ACCOUNTS
from .importers import ImportError_, parse_file

SECRET_FILE = os.environ.get("FINANZAS_GMAIL_FILE", "/run/secrets/gmail-imap")
EVERY = 600                          # seconds between rounds
LOOK_BACK = timedelta(days=14)       # the first round (or a recreated inbox) reads the last two weeks
ALARM_AFTER = timedelta(hours=6)
MAX_SIZE = 30 * 1024 * 1024          # a bigger mail isn't a card statement
STUCK_ROUNDS = 6                     # a mail that breaks this many rounds in a row (an hour) is skipped
TRANSIENT = (sqlite3.OperationalError, ImportError_)  # the database busy, no dollar rate yet: next round
CANDIDATE = re.compile(r"bbva|resumen|tarjeta|visa", re.I)
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()  # IMAP dates are in English
_lock = threading.Lock()             # the loop and POST /api/correo never run a round at once


def config() -> dict | None:
    try:
        lines = [line.strip() for line in open(SECRET_FILE).read().splitlines() if line.strip()]
    except OSError:
        return None
    if len(lines) < 2:
        return None
    # Google shows app passwords in groups of four letters; the spaces aren't part of it.
    return {"user": lines[0], "password": lines[1].replace(" ", ""),
            "host": lines[2] if len(lines) > 2 else "imap.gmail.com"}


def _state(conn) -> dict:
    row = conn.execute("SELECT value FROM meta WHERE key = 'gmail'").fetchone()
    return json.loads(row[0]) if row else {}


def _save(conn, state: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('gmail', ?)", (json.dumps(state),))
    conn.commit()


def status(conn) -> dict:
    """For the screens and healthcheck.sh: last good round, the error of the last bad one."""
    return {**_state(conn), "on": config() is not None}


# --- one mail (pure but for the database, tested) ---------------------------------------

def read(raw: bytes) -> dict:
    """{'from', 'subject', 'text', 'pdfs': [(name, bytes)]}. A mail forwarded as an attachment is
    read too: walk() goes into it."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    texts, pdfs = [], []
    for part in msg.walk():
        if part.get_content_type() == "message/rfc822":  # who sent the forwarded one, and about what
            for inner in part.iter_parts():
                texts.append(f"{inner.get('From', '')} {inner.get('Subject', '')}")
        if part.is_multipart():
            continue
        ctype, name = part.get_content_type(), part.get_filename() or ""
        if ctype == "application/pdf" or name.lower().endswith(".pdf"):
            data = part.get_payload(decode=True)
            if data:  # the importers know a PDF by its name
                pdfs.append((name if name.lower().endswith(".pdf") else (name or "resumen") + ".pdf", data))
        elif ctype in ("text/plain", "text/html") and part.get_content_disposition() != "attachment":
            try:
                texts.append(part.get_content())
            except Exception:  # an unknown charset
                texts.append((part.get_payload(decode=True) or b"").decode("utf-8", "replace"))

    def header(key: str) -> str:
        try:
            return str(msg.get(key, ""))
        except Exception:  # a malformed header
            return ""
    return {"from": header("From"), "subject": header("Subject"), "text": "\n".join(texts), "pdfs": pdfs}


def handle(conn, mail: dict, today: date) -> list[tuple[str, str, str, str]]:
    """What one mail leaves in Finanzas, and the notifications that say so: (title, body, path, tags).
    The same mail twice leaves the same and says nothing the second time."""
    out = []
    loaded = False
    for name, data in mail["pdfs"]:
        try:
            account, rows = parse_file(name, data, "bbva_tarjeta")
        except ImportError_:
            continue  # another PDF (an invoice, a ticket)
        except Exception:
            continue  # a broken one
        res = ledger.store(conn, name, account, rows)
        loaded = True
        if res["new"] or res.get("replaced"):
            extra = f", reemplazó {res['replaced']} del Excel" if res.get("replaced") else ""
            out.append(("Resumen de la tarjeta", f"{ACCOUNTS[account]}: llegó el resumen por mail, "
                        f"{res['new']} movimientos nuevos{extra}.", "/mes", "credit_card"))
    words = f"{mail['from']} {mail['subject']} {aviso.plain(mail['text'])}".lower()
    if loaded or "bbva" not in words:
        return out
    notice = aviso.parse(f"{mail['subject']}\n{mail['text']}")
    if notice:
        seen, stored = aviso.record(conn, notice, today)  # its PDF already loaded: that one counts
        if seen == "new":
            out.append(("Resumen de la tarjeta", aviso.message(stored), "/saldos", "credit_card"))
    elif re.search(r"resumen", mail["subject"], re.I) and re.search(r"tarjeta|visa", mail["subject"], re.I):
        # Never again a notice that goes by in silence: if BBVA changes the mail, you hear about it.
        out.append(("Un mail de BBVA que no entendí", f"«{mail['subject']}»: no encontré el vencimiento ni el "
                    "saldo. Si es el resumen, bajá el PDF y compartilo con Finanzas.", "/cargar", "warning"))
    return out


# --- the inbox ---------------------------------------------------------------------------

def _imap(cfg: dict):
    conn = imaplib.IMAP4_SSL(cfg["host"], 993, timeout=60)
    conn.login(cfg["user"], cfg["password"])
    return conn


def _flat(data) -> bytes:
    out = []
    for x in data or []:
        if isinstance(x, tuple):
            out.extend(p for p in x if isinstance(p, bytes))
        elif isinstance(x, bytes):
            out.append(x)
    return b"\n".join(out)


def _literal(data) -> bytes:
    """The body of a FETCH answer: the second half of its first (envelope, literal) pair."""
    for x in data or []:
        if isinstance(x, tuple) and len(x) > 1:
            return x[1]
    return b""


def _ok(answer, what: str):
    typ, data = answer
    if typ != "OK":
        raise imaplib.IMAP4.error(f"{what}: {_flat(data).decode('utf-8', 'replace')[:120]}")
    return data


def _fetch(imap, uid: int) -> dict | None:
    """The whole mail, only if it may be about the card (its sender or subject say so, or it has a
    PDF): most of the inbox is never downloaded."""
    data = _ok(imap.uid("FETCH", str(uid), "(RFC822.SIZE BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])"),
               "FETCH")
    flat = _flat(data)
    if not flat:
        return None  # gone between SEARCH and FETCH
    size = re.search(rb"RFC822\.SIZE (\d+)", flat)
    head = email.message_from_bytes(_literal(data), policy=email.policy.default)
    try:
        who = f"{head.get('From', '')} {head.get('Subject', '')}"
    except Exception:
        who = ""
    # A PDF, or a mail forwarded as an attachment (Outlook's "Reenviar como datos adjuntos").
    inside = re.search(rb'"application" "pdf"|\.pdf"|"message" "rfc822"', flat, re.I)
    if not (CANDIDATE.search(who) or inside) or (size and int(size.group(1)) > MAX_SIZE):
        return None
    return read(_literal(_ok(imap.uid("FETCH", str(uid), "(BODY.PEEK[])"), "FETCH")))


def _response(imap, code: str) -> int | None:
    typ, data = imap.response(code)
    try:
        return int(data[0])
    except (TypeError, ValueError, IndexError):
        return None


def sync(conn, now: datetime | None = None, connect=_imap, send=None) -> str:
    """One round: reads what arrived since the last one. Returns what it did."""
    cfg = config()
    if not cfg:
        return "off"
    now = now or datetime.now()
    send = send or _send
    state = _state(conn)
    if state.get("unsent"):  # what ntfy didn't take last time
        state["unsent"] = _deliver(send, state["unsent"])
    current = validity = None  # (uid, subject) of the mail being read if the round breaks; the numbering
    try:
        imap = connect(cfg)
        try:
            _ok(imap.select("INBOX", readonly=True), "SELECT")
            validity, uidnext = _response(imap, "UIDVALIDITY"), _response(imap, "UIDNEXT")
            if "last_uid" in state and state.get("validity") == validity:
                last = state["last_uid"]
                criteria = ("UID", f"{last + 1}:*")
            else:  # first round, or Gmail renumbered the inbox
                last, since = 0, (now - LOOK_BACK).date()
                state.pop("last_uid", None)
                criteria = ("SINCE", f"{since.day:02d}-{MONTHS[since.month - 1]}-{since.year}")
            found = _ok(imap.uid("SEARCH", None, *criteria), "SEARCH")
            # 'N:*' always matches the newest mail, even below N.
            uids = sorted(u for u in (int(x) for x in (found[0] or b"").split()) if u > last)
            for uid in uids:
                current = (uid, None)
                mail = _fetch(imap, uid)  # a dropped connection fails the round: this uid comes again
                if mail:
                    current = (uid, mail["subject"])
                    try:
                        notes = handle(conn, mail, now.date())
                    except TRANSIENT:
                        conn.rollback()
                        raise  # the round fails here and this mail comes again (see _stuck)
                    except Exception as e:  # a mail that can't be read: skipped, and said once
                        conn.rollback()
                        state["skipped"] = f"UID {uid}: {type(e).__name__}: {e}"[:200]
                        notes = [_skipped(mail["subject"], e)]
                    state.update(validity=validity, last_uid=uid)
                    _save(conn, state)
                    _later(state, _deliver(send, notes))  # now: a round that breaks later doesn't lose them
            current = None
            # The first round starts from now (what it read of the last two weeks aside).
            state.update(validity=validity, last_uid=max([last, (uidnext or 1) - 1, *uids]))
        finally:
            try:
                imap.logout()
            except Exception:
                pass
    except Exception as e:  # no internet, no DNS, Gmail down, a revoked password: next round
        conn.rollback()
        state["error"] = f"{type(e).__name__}: {e}"[:200]
        state.setdefault("failing_since", now.isoformat(timespec="seconds"))
        if current:
            _stuck(state, current, validity, e, send)
        alarm = _alarm(state, now)
        if alarm and _deliver(send, [alarm]):
            state.pop("alarmed", None)  # ntfy is down too: next round
        _save(conn, state)
        return "error"
    state.update(ok=now.isoformat(timespec="seconds"), error=None, read=len(uids))
    state.pop("failing_since", None)
    state.pop("stuck", None)
    # It's back only once a whole round went well (a round that connects and then breaks isn't):
    # until then 'alarmed' stays, so the outage is told once and its end once.
    if state.pop("alarmed", None):
        _later(state, _deliver(send, [("Finanzas volvió a leer el Gmail", "Ya leí lo que llegó mientras tanto.",
                                       "/cargar", "white_check_mark")]))
    _save(conn, state)
    return f"read {len(uids)}"


def _stuck(state: dict, current: tuple[int, str | None], validity, error: Exception, send) -> None:
    """The round broke while reading one mail (the connection, the database, or the mail itself).
    It comes again next round; one that breaks STUCK_ROUNDS rounds in a row is skipped with a
    warning, so a single mail can never stop the inbox for good."""
    uid, subject = current
    stuck = state.get("stuck") or {}
    rounds = stuck.get("rounds", 0) + 1 if stuck.get("uid") == uid else 1
    if rounds < STUCK_ROUNDS:
        state["stuck"] = {"uid": uid, "rounds": rounds}
        return
    state.pop("stuck", None)
    state.update(validity=validity, last_uid=uid, skipped=f"UID {uid}: {type(error).__name__}: {error}"[:200])
    _later(state, _deliver(send, [_skipped(subject, error)]))


def _skipped(subject: str | None, error: Exception) -> tuple[str, str, str, str]:
    what = f"«{subject}»" if subject else "Un mail del Gmail"
    return ("Un mail que no pude leer", f"{what} me dio un error ({type(error).__name__}: {str(error)[:80]}) y lo "
            "salteo. Si era el resumen de la tarjeta, bajá el PDF y compartilo con Finanzas.", "/cargar", "warning")


def _alarm(state: dict, now: datetime) -> tuple[str, str, str, str] | None:
    """Once per outage, past ALARM_AFTER and not at night."""
    since = datetime.fromisoformat(state["failing_since"])
    if state.get("alarmed") or now - since < ALARM_AFTER or not 9 <= now.hour < 22:
        return None
    state["alarmed"] = now.isoformat(timespec="seconds")
    error = state.get("error") or ""
    if re.search(r"AUTHENTICATIONFAILED|Invalid credentials|LOGIN failed", error, re.I):
        hint = ("Google no acepta la contraseña: creá otra contraseña de aplicación "
                "(myaccount.google.com/apppasswords) y ponela en secrets/gmail-imap.")
    else:
        hint = "Vuelve a probar sola cada 10 minutos; mientras, el resumen se puede subir con Compartir → Finanzas."
    return ("Finanzas no puede leer el Gmail",
            f"Desde el {since:%d/%m a las %H:%M} no puedo leer el mail del resumen ({error[:80]}). {hint}",
            "/cargar", "warning")


def _send(title: str, body: str, path: str, tags: str) -> None:
    if os.environ.get("FINANZAS_NOTIFY") == "1":
        from . import notify
        notify.send(title, body, path, tags)


def _deliver(send, notes) -> list[list[str]]:
    """Sends each one; returns the ones that didn't go (ntfy down)."""
    failed = []
    for note in notes:
        try:
            send(*note)
        except Exception:
            failed.append(list(note))
    return failed


def _later(state: dict, failed: list) -> None:
    """Notes that didn't go: the next round sends them (a notice is told only once, so it can't be lost)."""
    if failed:
        state["unsent"] = ((state.get("unsent") or []) + failed)[-20:]


def run_once() -> str:
    with _lock:
        conn = db.connect()
        try:
            result = sync(conn)
        finally:
            conn.close()
    if result.startswith("read") and result != "read 0":
        from . import classifier
        classifier.suggest_pending()
    return result


def _loop():
    while True:
        try:
            run_once()
        except Exception:
            pass
        time.sleep(EVERY)


def start() -> None:
    # Always on: with no secret a round does nothing, and the secret can come later.
    threading.Thread(target=_loop, name="gmail", daemon=True).start()
