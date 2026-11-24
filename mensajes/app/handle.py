"""One message from Telegram, start to end: who sent it (only authorized people get an answer),
what it carries (text, a voice note, a photo or a file), what it asks for (intents.py), which app
does it (house.py, or the AI in ia.py), and the answer back. Everything is kept in messages: the
history page, the AI's memory of the conversation and what "borrar" takes back.

Runs on the poller's thread (main.py), so messages are answered in the order they came."""
import json
import queue
import re
import threading
from datetime import datetime, timedelta

from . import db, house, ia, intents, notify, people, telegram, voz
from .intents import fold

MESA_KINDS = ("mesa", "mesa_por_que", "mesa_pausar", "mesa_reanudar")
UNDO_WINDOW = timedelta(hours=6)
MAX_AUDIO_S = 300  # longer voice notes take Whisper minutes, and the bot answers one message at a time
MAX_LONG_AUDIO_S = 45 * 60  # up to this, a long one is transcribed in the background and kept in Vigía
MEMORY = timedelta(minutes=45)  # what the AI remembers of the conversation
SHEETS = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-excel", "text/csv")
STATEMENT_WORDS = re.compile(r"\b(?:resumen|extracto|tarjeta|movimientos|visa|master|mastercard|bbva|mercado ?pago)\b")
SAVE_WORDS = re.compile(r"\b(?:guarda|guardar|guardalo|guardala|papeles)\b")

HELP = """Hola 👋 Soy el bot del server de casa. Escribime o mandame un audio:

🛒 *Lista*: «comprar manteca y yerba», «¿qué falta?», «compré la leche», «terminé las compras»
💸 *Plata*: «gasté 8500 en la verdulería», «12 mil de nafta ayer», «saldos»
🧾 *Pagos del mes*: «¿qué me falta pagar?», «pagué la luz» (sin monto: lo marca pagado; con monto es un gasto)
⏰ *Agenda*: «recordame mañana a las 18 el dentista», «¿qué tengo esta semana?»
📄 *Papeles*: mandame la foto o el PDF de una factura o garantía y la guardo. El resumen de la tarjeta (PDF o Excel, con «resumen» en el texto) va a Finanzas.
🔭 *Vigía*: mandame un link y lo guardo con un resumen; «seguí el precio de <link>», «avisame si cambia <link>», «avisame cuando aparezca "turnos" en <link>», «¿qué estoy vigilando?». Un audio largo (hasta 45 minutos) lo transcribo y te mando el resumen.
🛵 *Pedidos*: «armá el pedido» (con la lista), «pedí la verdura», «pedí la carne», «pedí al súper», «pedí 2 kg de papas a la verdulería», «¿cuánto está la banana?», «pedidos». Lo prepara y te pasa el link; mandarlo y pagar lo hacés vos.
☀️ «buen día» · 🖥️ «¿cómo está el server?»
📈 *Mesa* (solo el dueño; la cartera de largo plazo que decide Claude): «¿cómo va la mesa?», «¿por qué compró MSFT?», «pausá la mesa», «reanudá la mesa»
🤖 Lo demás se lo pregunto a la IA de casa: «¿cuánto gasté en delivery este mes?», «¿dónde está la garantía de la heladera?»

Si algo quedó mal, respondé *borrar* y lo deshago. Con / tenés el menú."""
WELCOME = "Listo, ya te conozco. " + HELP.split("\n", 1)[1].lstrip()
OTHER_MEDIA = "Por ahora entiendo texto, audios, fotos y archivos (PDF, Excel)."


def _name(user: dict) -> str:
    return " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x).strip()


def _media(m: dict) -> tuple[str, dict] | None:
    """(kind, {'file_id', 'mime', 'name'}) of what the message carries, if anything."""
    if v := (m.get("voice") or m.get("audio") or m.get("video_note")):
        return "audio", {"file_id": v["file_id"], "mime": v.get("mime_type") or "audio/ogg", "name": v.get("file_name") or "",
                         "duration": v.get("duration") or 0}
    if m.get("photo"):
        p = max(m["photo"], key=lambda p: p.get("file_size") or p.get("width", 0) * p.get("height", 0))
        return "image", {"file_id": p["file_id"], "mime": "image/jpeg", "name": ""}
    if d := m.get("document"):
        mime = (d.get("mime_type") or "").lower()
        return ("image" if mime.startswith("image/") else "document"), {"file_id": d["file_id"], "mime": mime,
                                                                         "name": d.get("file_name") or ""}
    if any(m.get(k) for k in ("video", "sticker", "location", "contact", "animation", "poll")):
        return "other", {}
    return None


def update(conn, upd: dict, bot: dict | None = None):
    if m := upd.get("message"):
        message(conn, m, bot or {})


def message(conn, m: dict, bot: dict):
    chat_info, user = m.get("chat") or {}, m.get("from") or {}
    if not chat_info.get("id") or not user.get("id") or user.get("is_bot"):
        return
    chat, in_group = chat_info["id"], chat_info.get("type") in ("group", "supergroup")
    msg_id = m.get("message_id")
    text = (m.get("text") or m.get("caption") or "").strip()
    if in_group and bot.get("username"):  # "@casa_bot comprar pilas" → "comprar pilas"
        text = re.sub(rf"(?i)^@{re.escape(bot['username'])}\b[\s,:]*", "", text)
    media = _media(m)
    kind = media[0] if media else "text"

    cur = conn.execute("INSERT OR IGNORE INTO messages (tg_key, chat, incoming, kind, text) VALUES (?, ?, 1, ?, ?)",
                       (f"{chat}:{msg_id}", str(chat), kind, text))
    conn.commit()
    if cur.rowcount == 0:
        return  # seen already (Telegram sent it again after a restart)
    row_id = cur.lastrowid
    person = people.note(conn, user["id"], _name(user), user.get("username") or "")
    conn.execute("UPDATE messages SET person_id = ? WHERE id = ?", (person["id"], row_id))
    conn.commit()

    if not person["allowed"]:
        code = re.match(r"^/start(?:@\w+)?\s+(\S+)", text)
        if code and not in_group and people.redeem(conn, code[1], person):
            send(conn, chat, WELCOME, person["id"])
            notify.publish("Alguien entró al bot de la casa", f"{people.who(person)} usó la invitación.", ["wave"],
                           path="/personas")
        else:
            _stranger(conn, person, in_group)
        return

    replies, intent, answer = [], None, None
    reply_to = msg_id if in_group else None

    if media and kind == "audio" and MAX_AUDIO_S < media[1]["duration"] <= MAX_LONG_AUDIO_S and not in_group:
        # Whisper takes minutes on it: one background thread does it (the poller goes on answering)
        # and the transcript goes to Vigía, which sends the summary back here.
        enqueue_long_audio({"file_id": media[1]["file_id"], "chat": chat, "person_id": person["id"],
                            "who": person["name"], "row_id": row_id, "duration": media[1]["duration"]})
        replies.append("🎙️ Es largo: lo transcribo y te mando el resumen.")
        intent = "long_audio"
    elif media and kind == "audio" and media[1]["duration"] > MAX_AUDIO_S:
        if not in_group:
            replies.append(f"Ese audio es muy largo (más de {MAX_LONG_AUDIO_S // 60} minutos). ¿Me lo mandás en partes?")
    elif media and kind == "audio":
        try:
            with telegram.typing(chat):
                data, path = telegram.download(media[1]["file_id"])
                heard = voz.transcribe(data, "." + (path.rsplit(".", 1)[-1] if "." in path else "ogg"))
        except Exception as e:  # noqa: BLE001 - any failure here is answered, not raised
            heard = ""
            print(f"voz: {type(e).__name__}: {e}", flush=True)
        if heard:
            conn.execute("UPDATE messages SET text = ? WHERE id = ?", (heard, row_id))
            conn.commit()
            text = heard
        elif not in_group:
            replies.append("No entendí el audio. ¿Me lo escribís?")
    elif media and kind in ("image", "document"):
        if not in_group or STATEMENT_WORDS.search(fold(text)) or SAVE_WORDS.search(fold(text)):
            with telegram.typing(chat):
                answer = _document(media[1], text)
            replies.append(_with_icon(answer))
            intent = "document"
        else:
            text = ""  # a photo in a group, not for the bot
    elif media and not in_group:
        replies.append(OTHER_MEDIA)

    if text and intent not in ("document", "long_audio"):
        it = intents.read(text, in_group)
        intent = it.kind
        answer = run(conn, it, chat, person, row_id)
        if answer is not None:
            reply = _with_icon(answer)
            if kind == "audio":
                reply = f"🎙️ _{text}_\n\n{reply}"
            replies.append(reply)
    elif text and intent == "document":
        it = intents.read(text, in_group)
        if it.kind in ("expense", "list_add", "reminder"):  # a ticket's photo with "gasté 8500 en la ferretería"
            extra = run(conn, it, chat, person, row_id)
            if extra is not None:
                replies.append(_with_icon(extra))
                answer.undo += extra.undo

    conn.execute("UPDATE messages SET intent = ? WHERE id = ?", (intent, row_id))
    conn.commit()
    if replies:
        send(conn, chat, "\n\n".join(replies), person["id"], answer, reply_to)


def _with_icon(a: house.Answer) -> str:
    return f"{house.ICONS.get(a.app, '')} {a.text}".strip() if a.app else a.text


def _document(media: dict, caption: str) -> house.Answer:
    mime = media["mime"]
    if not (mime.startswith("image/") or mime == "application/pdf" or mime in SHEETS):
        return house.Answer(OTHER_MEDIA, ok=False)
    try:
        data, path = telegram.download(media["file_id"])
    except Exception:  # noqa: BLE001
        return house.Answer("No pude bajar el archivo (los bots reciben hasta 20 MB). Subilo desde la app.", ok=False)
    name = media["name"] or path.rsplit("/", 1)[-1] or f"telegram-{datetime.now():%Y%m%d-%H%M%S}"
    if mime in SHEETS or (mime == "application/pdf" and STATEMENT_WORDS.search(fold(caption))):
        return house.statement(name, data, mime)
    return house.paper(name, data, mime)


def _stranger(conn, person, in_group: bool):
    """Someone not authorized: no answer (the bot doesn't exist for them); the iPhone hears it once."""
    if person["notified"] or in_group:
        return
    conn.execute("UPDATE people SET notified = 1 WHERE id = ?", (person["id"],))
    conn.commit()
    notify.publish("Le escribieron al bot de la casa", f"{people.who(person)} no está autorizado: no le contesté. "
                   "Si lo conocés, autorizalo en Mensajes → Personas.", ["speech_balloon"], path="/personas")


def run(conn, it: intents.Intent, chat, person, row_id: int) -> house.Answer | None:
    """The answer for one intent; None when there's nothing to say (a group message not for the bot)."""
    k = it.kind
    if k == "none":
        return None
    if k == "help":
        return house.Answer(HELP)
    if k == "undo":
        return undo(conn, str(chat), person["id"])
    if k == "list_add":
        return house.list_add(it.text)
    if k == "list_show":
        return house.list_show()
    if k == "list_tick":
        return house.list_tick(it.text)
    if k == "list_finish":
        return house.list_finish()
    if k == "expense":
        return house.expense(it.text, person["name"])
    if k == "balances":
        return house.balances()
    if k == "bills":
        return house.bills()
    if k == "bill_paid":
        a = house.bill_paid(it.text, person["name"])
        fallback = it.args.get("si_no")  # set when the rules read it (a /pague command has none)
        if a.unknown and fallback:  # "pagué el almuerzo": not a bill, so what it was before bills existed
            return run(conn, intents.Intent(fallback, it.text), chat, person, row_id)
        return a
    if k == "reminder":
        a = house.reminder(it.text)
        rest = re.sub(r"^\W*\S+\s+(?:que\s+)?", "", it.text)
        if not a.ok and re.match(r"(?:hay que |tengo que )?compr", fold(rest)):
            return house.list_add(rest)  # "recordame comprar manteca": that's the list
        return a
    if k == "agenda":
        return house.agenda(it.args.get("dias", 7))
    if k == "morning":
        return house.morning(mesa=bool(person["avisos"]))  # the mesa is Nahuel's (see MESA_KINDS below)
    if k == "server":
        return house.server()
    if k in MESA_KINDS:
        if not person["avisos"]:  # the mesa is Nahuel's alone: whoever gets the server's alerts
            return house.Answer("La mesa es solo del dueño del server.", ok=False)
        if k == "mesa":
            return house.mesa()
        if k == "mesa_por_que":
            return house.mesa_por_que(it.args.get("ticker", ""))
        return house.mesa_control("pausar" if k == "mesa_pausar" else "reanudar", person["name"])
    if k == "save":
        return house.save(it.text, person["name"], chat)
    if k == "watch":
        return house.watch(it.text, person["name"], chat)
    if k == "watch_list":
        return house.watches()
    if k == "order_make":
        return house.order_make(it.args.get("proveedor", ""), it.args.get("texto", ""), person["name"])
    if k == "orders_list":
        return house.orders()
    if k == "price":
        return house.price(it.text)
    if k == "ai":
        return ask_ai(conn, it.text, chat, row_id, person["name"])
    return None


# --- long voice notes, in the background -------------------------------------------------------------

LONG_AUDIOS: queue.Queue = queue.Queue()
_long_thread: threading.Thread | None = None
_long_lock = threading.Lock()


def enqueue_long_audio(job: dict):
    """One thread, started on the first long audio, takes them one at a time."""
    global _long_thread
    LONG_AUDIOS.put(job)
    with _long_lock:
        if _long_thread is None or not _long_thread.is_alive():
            _long_thread = threading.Thread(target=_long_worker, name="long-audio", daemon=True)
            _long_thread.start()


def _long_worker():
    conn = db.connect()  # its own: the poller's connection stays the poller's
    while True:
        job = LONG_AUDIOS.get()
        try:
            long_audio(conn, job)
        except Exception as e:  # noqa: BLE001 - one bad audio must not stop the next
            print(f"long-audio: {type(e).__name__}: {e}", flush=True)
        finally:
            LONG_AUDIOS.task_done()


def long_audio(conn, job: dict) -> house.Answer | None:
    """Downloads, transcribes and hands the text to Vigía (as an audio). Says something only when it
    goes wrong: on success Vigía sends the summary to the chat when it's ready."""
    chat = job["chat"]
    try:
        data, path = telegram.download(job["file_id"])
    except Exception as e:  # noqa: BLE001
        print(f"long-audio download: {type(e).__name__}: {e}", flush=True)
        send(conn, chat, "No pude bajar el audio (los bots reciben hasta 20 MB). ¿Lo mandás en partes?", job["person_id"])
        return None
    try:
        heard = voz.transcribe(data, "." + (path.rsplit(".", 1)[-1] if "." in path else "ogg"))
    except Exception as e:  # noqa: BLE001
        print(f"long-audio voz: {type(e).__name__}: {e}", flush=True)
        heard = ""
    if not heard:
        send(conn, chat, "No pude entender el audio largo. ¿Me lo mandás de nuevo o en partes?", job["person_id"])
        return None
    conn.execute("UPDATE messages SET text = ? WHERE id = ?", (heard, job["row_id"]))
    conn.commit()
    minutes = round(job.get("duration", 0) / 60)
    title = f"Audio de {job['who'] or 'Telegram'} del {datetime.now():%d/%m %H:%M}" + (f" ({minutes} min)" if minutes else "")
    a = house.save_audio(heard, title, job["who"], chat)
    if not a.ok:
        send(conn, chat, f"Lo transcribí pero no lo pude guardar ({a.text}). Este es el texto:\n\n{heard[:3500]}",
             job["person_id"])
    return a


def ask_ai(conn, text: str, chat, row_id: int, who: str = "") -> house.Answer:
    if not ia.enabled():
        return house.Answer("No entendí. Escribí *ayuda* para ver lo que sé hacer.", ok=False)
    since = (datetime.now() - MEMORY).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute("SELECT incoming, text FROM messages WHERE chat = ? AND id < ? AND at >= ? AND text != ''"
                        " ORDER BY id DESC LIMIT 6", (str(chat), row_id, since)).fetchall()
    try:
        with telegram.typing(chat) if chat != "web" else _nothing():
            reply, used = ia.answer(text, [(bool(r["incoming"]), r["text"]) for r in reversed(rows)], who=who)
    except Exception as e:  # noqa: BLE001
        print(f"ia: {type(e).__name__}: {e}", flush=True)
        return house.Answer("La IA de casa no respondió. Escribí *ayuda* para ver lo que sé hacer sin ella.", ok=False)
    return house.Answer(f"🤖 {reply}" if reply else "No entendí. Escribí *ayuda*.", ok=bool(used), wrote=ia.wrote(used))


class _nothing:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def undo(conn, chat: str, person_id: int) -> house.Answer:
    since = (datetime.now() - UNDO_WINDOW).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute("SELECT * FROM messages WHERE chat = ? AND incoming = 0 AND person_id = ? AND undo IS NOT NULL"
                       " AND undone = 0 AND at >= ? ORDER BY id DESC LIMIT 1", (chat, person_id, since)).fetchone()
    if row is None:
        return house.Answer("No tengo nada reciente para deshacer.", ok=False)
    steps = json.loads(row["undo"])
    if not steps:  # the last thing changed something with no way back from here: not an older one instead
        first = row["text"].split("\n\n")[-1].strip()
        return house.Answer(f"Lo último que hice no lo puedo deshacer desde acá: revisalo en la app.\n_{first}_", ok=False)
    ok = house.take_back(steps)
    conn.execute("UPDATE messages SET undone = 1 WHERE id = ?", (row["id"],))
    conn.commit()
    first = row["text"].split("\n\n")[-1].strip()
    if not ok:
        return house.Answer(f"No pude deshacerlo del todo: revisalo en la app.\n_{first}_", ok=False)
    return house.Answer(f"↩️ Deshecho: _{first}_")


def undo_of(answer: house.Answer | None) -> str | None:
    """What a reply keeps for "borrar": its steps; [] when it changed something that can't be taken
    back (so "borrar" stops there); NULL when it only read (so "borrar" looks further back)."""
    if answer is None or not (answer.undo or answer.wrote):
        return None
    return json.dumps(answer.undo)


def send(conn, chat, text: str, person_id: int | None = None, answer: house.Answer | None = None,
         reply_to: int | None = None):
    try:
        sent = telegram.send(chat, text, reply_to)
    except Exception as e:  # noqa: BLE001
        print(f"send: {type(e).__name__}: {e}", flush=True)
        sent = None
    undo_json = undo_of(answer)
    conn.execute("INSERT OR IGNORE INTO messages (tg_key, chat, person_id, incoming, text, app, undo) VALUES (?, ?, ?, 0, ?, ?, ?)",
                 (f"{chat}:{sent}" if sent else None, str(chat), person_id, text, answer.app if answer else None, undo_json))
    conn.commit()
